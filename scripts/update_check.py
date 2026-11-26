"""Derlenmiş exe ile otomatik güncellemeyi gerçekten dener (Windows CI).

Sahte bir release sunucusu "v99.0.0" sürümünü yayınlar (aynı exe + doğru SHA256).
Beklenen:
  * exe yeni dosyayla değiştirilir ve uygulama kendini yeniden başlatır,
  * yeni süreç AYNI portta açılır (telefondaki QR/adres değişmez),
  * eski süreçler kapanır, KickIndirici.exe.old temizlenir,
  * ardından Çıkış'ta hiçbir süreç kalmaz.

Kullanım: python scripts/update_check.py dist\\KickIndirici.exe
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import psutil


def ping(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def app_procs(exe: Path) -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if (p.info["name"] or "").lower().startswith("kickindirici"):
                out.append(p)
        except psutil.Error:
            pass
    return out


def wait(pred, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.5)
    raise SystemExit(f"ZAMAN AŞIMI: {what}")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    src = Path(sys.argv[1]).resolve()
    work = Path(tempfile.mkdtemp(prefix="kick-upd-"))
    exe = work / "KickIndirici.exe"
    shutil.copy2(src, exe)
    payload = src.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    calls = {"latest": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/latest":
                calls["latest"] += 1
                tag = "v99.0.0" if calls["latest"] == 1 else "v0.0.1"  # yalnızca bir kez güncelle
                self.send(json.dumps({"tag_name": tag, "assets": [
                    {"name": "KickIndirici.exe", "browser_download_url": base + "/exe"},
                    {"name": "KickIndirici.exe.sha256", "browser_download_url": base + "/sha"}]}).encode(),
                    "application/json")
            elif self.path == "/exe":
                self.send(payload, "application/octet-stream")
            elif self.path == "/sha":
                self.send(f"{sha}  KickIndirici.exe\n".encode(), "text/plain")
            else:
                self.send_error(404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    appdata = work / "appdata"
    env = dict(os.environ, KICK_TEST="1", KICK_APPDATA=str(appdata),
               KICK_UPDATE_API=f"http://127.0.0.1:{srv.server_port}/latest",
               KICK_UPDATE_FIRST_DELAY="3")
    subprocess.Popen([str(exe), "--no-browser"], env=env)
    port = wait(lambda: json.loads((appdata / "instance.json").read_text())["port"]
                if (appdata / "instance.json").exists() else None, 60, "ilk açılış")
    wait(lambda: ping(port), 30, "ilk ping")
    old_pids = {p.pid for p in app_procs(exe)}
    print("ilk süreçler:", sorted(old_pids), "port", port)

    def restarted():
        now = {p.pid for p in app_procs(exe)}
        return now and not (now & old_pids) and ping(port)
    try:
        wait(restarted, 120, "güncelleme sonrası yeniden başlama")
    except SystemExit:
        print("[HATA] yeniden başlamadı. Süreçler:", [(p.pid, p.name()) for p in app_procs(exe)])
        print("dosyalar:", sorted(f.name for f in work.iterdir()), "| api çağrısı:", calls["latest"])
        print("----- log.txt -----")
        print((appdata / "log.txt").read_text("utf-8", "replace")[-6000:])
        for p in app_procs(exe):
            p.kill()
        return 1
    new_pids = {p.pid for p in app_procs(exe)}
    print("yeni süreçler:", sorted(new_pids), "aynı port", port)
    ok = True
    if calls["latest"] < 1:
        print("[HATA] güncelleme sunucusu hiç sorgulanmadı"); ok = False
    wait(lambda: not (work / "KickIndirici.exe.old").exists(), 30, ".old temizliği")
    for leftover in ("KickIndirici.exe.new", "KickIndirici.exe.download"):
        if (work / leftover).exists():
            print("[HATA] artık dosya:", leftover); ok = False
    if (appdata / "restart.flag").exists():
        print("[HATA] restart.flag kalmış"); ok = False
    if hashlib.sha256(exe.read_bytes()).hexdigest() != sha:
        print("[HATA] exe beklenen dosya değil"); ok = False
    time.sleep(3)
    log = (appdata / "log.txt").read_text("utf-8", "replace")
    after = log.split("Güncelleme sonrası açılış", 1)[-1]
    if "ERROR" in after or "Traceback" in after:
        print("[HATA] yeni sürüm hata kaydetti:\n" + after[-3000:]); ok = False
    print("log:", *[l for l in log.splitlines() if "Güncelleme" in l or "yeniden" in l.lower()], sep="\n  ")
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"", method="POST"), timeout=5)
    time.sleep(1)
    try:
        wait(lambda: not app_procs(exe), 20, "çıkış")
    except SystemExit:
        print("[HATA] çıkıştan sonra süreç kaldı:", [p.pid for p in app_procs(exe)]); ok = False
    print("[TAMAM] Otomatik güncelleme" if ok else "[HATA] Otomatik güncelleme")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
