"""Uygulama giriş noktası."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, jobs, procs, tunnel as tunnelmod, updater as updmod, winutil, ytdlp
from .config import Config
from .paths import appdata_dir, is_frozen, package_dir
from .web import AppState, create_app

log = logging.getLogger("kickindirici")


def setup_logging() -> None:
    # --noconsole ile stdout/stderr None olur; yazmaya çalışan kütüphaneler çökmesin.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    h = logging.handlers.RotatingFileHandler(appdata_dir() / "log.txt", maxBytes=1_000_000,
                                             backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    if not is_frozen():
        root.addHandler(logging.StreamHandler())


def wait_port_free(port: int, timeout: float) -> None:
    """Güncelleme sonrası: eski sürüm portu bırakana kadar bekle (QR adresi değişmesin)."""
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return
            except OSError:
                time.sleep(0.5)


def find_port(preferred: int) -> int:
    for port in [preferred, *range(8765, 8786)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Boş port bulunamadı")


def parent_watchdog(shutdown) -> None:
    """PyInstaller onefile'da üst (bootloader) süreç öldürülürse biz de kapanırız."""
    import psutil
    try:
        parent = psutil.Process(os.getppid())
        name = parent.name().lower()
    except psutil.Error:
        return
    if not name.startswith("kickindirici"):
        return
    while True:
        time.sleep(2)
        if not parent.is_running():
            log.info("Üst süreç kapandı, çıkılıyor")
            shutdown()
            return


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="KickIndirici")
    ap.add_argument("--no-browser", action="store_true", help="Açılışta tarayıcı açma")
    ap.add_argument("--no-tray", action="store_true", help="Tepsi ikonu olmadan çalış")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    setup_logging()
    log.info("Kick VOD İndirici %s başlıyor (pid %s)", __version__, os.getpid())
    procs.setup_job_object()

    inst = winutil.SingleInstance()
    if not inst.acquire():
        port = winutil.read_instance_port()
        log.info("Zaten çalışıyor (port %s); sayfası açılıyor", port)
        if port:
            webbrowser.open(f"http://127.0.0.1:{port}/baglan")
        return 0

    cfg = Config()
    restart_flag = appdata_dir() / "restart.flag"
    if restart_flag.exists():  # otomatik güncellemeden sonra açıldık
        log.info("Güncelleme sonrası açılış (sürüm %s)", __version__)
        restart_flag.unlink(missing_ok=True)
        args.no_browser = True
        wait_port_free(args.port or cfg["port"], 20)
    exe = Path(sys.executable)
    if is_frozen():
        threading.Thread(target=updmod.cleanup_old, args=(exe,), daemon=True).start()
    ytdlp.start_background_prepare()
    winutil.ensure_firewall(cfg)

    prober = jobs.Prober(lambda: cfg["cookies_browser"])
    manager = jobs.JobManager(appdata_dir() / "jobs.json", cfg.download_dir,
                              jobs.default_processor(lambda: cfg["cookies_browser"], prober))
    manager.start()
    tun = tunnelmod.Tunnel()
    port = find_port(args.port or cfg["port"])

    stopping = threading.Event()

    def shutdown(after_cleanup=None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("Kapanıyor: tüm alt süreçler sonlandırılıyor")
        try:
            manager.stop()
            tun.stop()
            procs.kill_all()
            winutil.instance_file().unlink(missing_ok=True)
            if after_cleanup is not None:
                # Alt süreçler öldürüldükten SONRA çalışır (ör. güncellenen sürümü başlatmak);
                # böylece yeni sürüm, kendi alt süreçlerimizle birlikte öldürülmez.
                after_cleanup()
        except Exception:
            log.exception("Kapanışta hata")
        finally:
            logging.shutdown()
            os._exit(0)

    server_ref: list = []

    def restart_into_new_version() -> None:
        """Güncellenen exe'yi başlatıp kapanır. Sürmekte olan iş yokken çağrılır."""
        log.info("Yeni sürümle yeniden başlatılıyor")
        restart_flag.write_text(str(port), "utf-8")
        try:
            server_ref[0].close()
        except Exception:
            pass
        inst.release()

        def launch() -> None:
            try:
                procs.launch_detached(str(exe))
                log.info("Yeni sürüm başlatıldı")
            except Exception:
                log.exception("Yeni sürüm başlatılamadı")
                restart_flag.unlink(missing_ok=True)
        shutdown(after_cleanup=launch)

    upd = None
    if is_frozen() and (not os.environ.get("KICK_TEST") or os.environ.get("KICK_UPDATE_API")):
        upd = updmod.Updater(exe, enabled=lambda: bool(cfg["auto_update"]), busy=manager.busy,
                             restart=restart_into_new_version)

    state = AppState(cfg, manager, tun, port, on_quit=shutdown, prober=prober, updater=upd)
    app = create_app(state)

    from waitress import create_server
    server = create_server(app, host="0.0.0.0", port=port, threads=16,
                           channel_timeout=300, ident="KickIndirici")
    server_ref.append(server)
    threading.Thread(target=server.run, name="waitress", daemon=True).start()
    winutil.write_instance(port)
    log.info("Sunucu: http://0.0.0.0:%s", port)
    if cfg["remote_enabled"]:
        tun.start(port)
    if upd is not None:
        upd.start()
    if is_frozen():
        threading.Thread(target=parent_watchdog, args=(shutdown,), daemon=True).start()

    local_url = f"http://127.0.0.1:{port}/baglan"
    if not args.no_browser:
        webbrowser.open(local_url)

    if args.no_tray:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        shutdown()
        return 0

    from .tray import run_tray
    try:
        run_tray(str(package_dir() / "assets" / "icon.png"),
                 on_open=lambda: webbrowser.open(local_url),
                 on_folder=lambda: winutil.open_folder(str(cfg.download_dir())),
                 on_quit=shutdown)
    except Exception:
        log.exception("Tepsi ikonu başlatılamadı; tepsisiz devam ediliyor")
        while not stopping.is_set():
            time.sleep(1)
    shutdown()
    return 0
