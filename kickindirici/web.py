"""Flask web arayüzü ve API."""
from __future__ import annotations

import hmac
import io
import logging
import os
import threading
import time
from urllib.parse import quote

import qrcode
import qrcode.image.svg
from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from . import __version__, jobs as jobsmod, netinfo, procs, winutil, ytdlp
from .config import CHUNK_CHOICES

log = logging.getLogger(__name__)

PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "Forwarded")


class AppState:
    """Web katmanının ihtiyaç duyduğu her şey (testlerde kolayca taklit edilir)."""

    def __init__(self, cfg, manager, tunnel=None, port: int = 8765, on_quit=None, prober=None,
                 updater=None):
        self.cfg = cfg
        self.updater = updater
        self.prober = prober
        self.manager = manager
        self.tunnel = tunnel
        self.port = port
        self.on_quit = on_quit or (lambda: None)


def is_local_request() -> bool:
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return False
    return not any(h in request.headers for h in PROXY_HEADERS)


def qr_svg(data: str) -> bytes:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def create_app(state: AppState) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False  # type: ignore[attr-defined]

    def token() -> str:
        return state.cfg["token"]

    def authorized() -> bool:
        if is_local_request():
            return True
        given = request.args.get("k") or request.cookies.get("kt") or request.headers.get("X-Kick-Token") or ""
        return hmac.compare_digest(given.encode(), token().encode())

    @app.before_request
    def _auth():
        if request.path.startswith("/static/") or request.path == "/api/ping":
            return None
        if not authorized():
            if request.path.startswith(("/api/", "/f/")):
                return jsonify(error="Yetkisiz. QR kodu tekrar okutun."), 401
            return render_template("denied.html"), 401
        return None

    @app.after_request
    def _cookie(resp):
        k = request.args.get("k")
        if k and hmac.compare_digest(k.encode(), token().encode()) and not request.path.startswith("/f/"):
            resp.set_cookie("kt", k, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Lax")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    def base_url() -> str:
        return request.host_url.rstrip("/")

    def connect_info() -> dict:
        ip, iface = netinfo.pick_lan_ip()
        k = quote(token())
        lan = f"http://{ip}:{state.port}"
        info = {
            "lan_url": lan,
            "lan_link": f"{lan}/?k={k}",
            "interface": iface,
            "public_network": winutil.is_public_network(iface) if iface else False,
            "firewall_ok": winutil.firewall_rule_exists(),
            "firewall_declined": bool(state.cfg["firewall_declined"]),
            "remote_enabled": bool(state.cfg["remote_enabled"]),
            "tunnel_status": state.tunnel.status if state.tunnel else "kapalı",
            "tunnel_link": None,
            "tailscale_link": None,
        }
        if state.tunnel and state.tunnel.url:
            info["tunnel_link"] = f"{state.tunnel.url}/?k={k}"
        ts = netinfo.tailscale_ip()
        if ts:
            info["tailscale_link"] = f"http://{ts}:{state.port}/?k={k}"
        return info

    # ---------------- sayfalar ----------------
    @app.get("/")
    def index():
        return render_template("index.html", version=__version__, chunks=CHUNK_CHOICES,
                               default_chunk=state.cfg["default_chunk"], local=is_local_request())

    @app.get("/baglan")
    def connect_page():
        return render_template("connect.html", version=__version__, info=connect_info(),
                               local=is_local_request())

    @app.get("/qr.svg")
    def qr():
        data = request.args.get("d", "")
        if not data or len(data) > 1000:
            abort(400)
        return Response(qr_svg(data), mimetype="image/svg+xml",
                        headers={"Cache-Control": "no-store"})

    # ---------------- API ----------------
    @app.get("/api/ping")
    def ping():
        return jsonify(ok=True, version=__version__)

    @app.get("/api/state")
    def api_state():
        base = base_url()
        k = quote(token())
        out = []
        for j in state.manager.list():
            d = j.public()
            have = {f["name"] for f in d["files"]}

            def links(f: dict) -> None:
                path = f"/f/{j.id}/{quote(f['name'])}"
                full = f"{base}{path}?k={k}"
                f["download"] = f"{path}?dl=1"
                f["url"] = full
                f["infuse"] = "infuse://x-callback-url/play?url=" + quote(full, safe="")

            for f in d["files"]:
                links(f)
            for part in d["parts"]:
                if part.get("status") == "done" and part.get("name") in have:
                    links(part)
            out.append(d)
        return jsonify(jobs=out, ytdlp_ready=ytdlp.is_ready(), ytdlp_error=ytdlp.ready_error(),
                       version=__version__, update=update_info())

    def update_info() -> dict:
        if state.updater is None:
            return {"status": "disabled", "current": __version__}
        return state.updater.snapshot()

    @app.post("/api/update/check")
    def update_check():
        if state.updater is None:
            return jsonify(error="Otomatik güncelleme yalnızca exe sürümünde çalışır."), 400
        state.updater.check_now()
        return jsonify(ok=True)

    @app.post("/api/update/apply")
    def update_apply():
        if state.updater is None:
            return jsonify(error="Otomatik güncelleme yalnızca exe sürümünde çalışır."), 400
        if state.updater.snapshot()["status"] != "ready":
            return jsonify(error="Hazır bir güncelleme yok."), 400
        if state.manager.busy():
            return jsonify(error="Devam eden indirme var; bitince tekrar deneyin."), 409
        threading.Thread(target=state.updater.apply, daemon=True).start()
        return jsonify(ok=True)

    def valid_url(url: str) -> bool:
        return url.lower().startswith(("http://", "https://")) and len(url) < 2000

    @app.post("/api/probe")
    def probe():
        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "")).strip()
        if not valid_url(url):
            return jsonify(error="Geçerli bir link yapıştırın (https://kick.com/...)."), 400
        if state.prober is None:
            return jsonify(error="Önizleme kullanılamıyor."), 503
        if not ytdlp.wait_ready(60) or not ytdlp.ytdlp_path().exists():
            return jsonify(error="yt-dlp henüz hazır değil, biraz sonra tekrar deneyin."), 503
        try:
            info = state.prober.get(url)
        except Exception as e:
            return jsonify(error=str(e) or "Video bilgisi alınamadı."), 422
        return jsonify(jobsmod.Prober.summary(info))

    @app.post("/api/jobs")
    def add_job():
        data = request.get_json(silent=True) or request.form
        url = str(data.get("url", "")).strip()
        try:
            chunk = int(data.get("chunk", state.cfg["default_chunk"]))
            start = float(data.get("start") or 0)
            end = data.get("end")
            end = float(end) if end not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify(error="Geçersiz değer."), 400
        if not valid_url(url):
            return jsonify(error="Geçerli bir link yapıştırın (https://kick.com/...)."), 400
        if chunk not in CHUNK_CHOICES:
            return jsonify(error="Geçersiz parça süresi."), 400
        if start < 0 or (end is not None and end - start < 1):
            return jsonify(error="Bitiş, başlangıçtan sonra olmalı."), 400
        job = state.manager.add(url, chunk, start, end, str(data.get("title") or "")[:300])
        return jsonify(job=job.public()), 201

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        return jsonify(ok=state.manager.cancel(job_id))

    @app.post("/api/jobs/<job_id>/pause")
    def pause_job(job_id):
        return jsonify(ok=state.manager.pause(job_id))

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        return jsonify(ok=state.manager.retry(job_id))

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id):
        if not state.manager.get(job_id):
            abort(404)
        return jsonify(ok=state.manager.delete(job_id))

    @app.get("/api/settings")
    def get_settings():
        c = state.cfg.snapshot()
        return jsonify(download_dir=c["download_dir"], cookies_browser=c["cookies_browser"],
                       default_chunk=c["default_chunk"], remote_enabled=c["remote_enabled"],
                       auto_update=c["auto_update"], local=is_local_request())

    @app.post("/api/settings")
    def set_settings():
        data = request.get_json(silent=True) or {}
        was_remote = bool(state.cfg["remote_enabled"])
        errors = state.cfg.apply_user_settings(data)
        if errors:
            return jsonify(errors=errors), 400
        if state.tunnel is not None and bool(state.cfg["remote_enabled"]) != was_remote:
            if state.cfg["remote_enabled"]:
                state.tunnel.start(state.port)
            else:
                state.tunnel.stop()
        return get_settings()

    @app.post("/api/reset-token")
    def reset_token():
        if not is_local_request():
            abort(403)
        state.cfg.reset_token()
        return jsonify(ok=True)

    @app.get("/api/connect")
    def api_connect():
        return jsonify(connect_info())

    @app.post("/api/open-folder")
    def open_folder():
        if not is_local_request():
            abort(403)
        winutil.open_folder(str(state.cfg.download_dir()))
        return jsonify(ok=True)

    @app.post("/api/quit")
    def quit_app():
        if not is_local_request():
            abort(403)
        threading.Thread(target=lambda: (time.sleep(0.3), state.on_quit()), daemon=True).start()
        return jsonify(ok=True)

    # ---------------- dosya servisi (HTTP Range destekli) ----------------
    @app.get("/f/<job_id>/<path:name>")
    def serve_file(job_id, name):
        p = state.manager.file_path(job_id, name)
        if p is None:
            abort(404)
        dl = request.args.get("dl") == "1"
        resp = send_file(p, mimetype="video/mp4", as_attachment=dl, download_name=p.name,
                         conditional=True, max_age=0)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    # ---------------- test yardımcıları (yalnızca KICK_TEST=1) ----------------
    if os.environ.get("KICK_TEST"):
        @app.get("/test/slow.mp4")
        def slow():
            def gen():
                for _ in range(100000):
                    yield b"\0" * 1024
                    time.sleep(0.2)
            return Response(gen(), mimetype="video/mp4",
                            headers={"Content-Length": str(1024 * 100000)})

        @app.post("/api/test/spawn")
        def spawn():
            from .paths import ffmpeg_path
            import subprocess
            procs.popen([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
                         "-i", "testsrc=size=64x64:rate=5", "-f", "null", "-"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            job = None
            if ytdlp.is_ready():
                job = state.manager.add(f"http://127.0.0.1:{state.port}/test/slow.mp4?k={quote(token())}", 0)
            return jsonify(ok=True, job=job.id if job else None)

    return app
