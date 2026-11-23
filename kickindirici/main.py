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

from . import __version__, jobs, procs, tunnel as tunnelmod, winutil, ytdlp
from .config import Config
from .paths import IS_WINDOWS, appdata_dir, is_frozen, package_dir
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
    ytdlp.start_background_prepare()
    winutil.ensure_firewall(cfg)

    manager = jobs.JobManager(appdata_dir() / "jobs.json", cfg.download_dir,
                              jobs.default_processor(lambda: cfg["cookies_browser"]))
    manager.start()
    tun = tunnelmod.Tunnel()
    port = find_port(args.port or cfg["port"])

    stopping = threading.Event()
    tray_stop = threading.Event()

    def shutdown() -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("Kapanıyor: tüm alt süreçler sonlandırılıyor")
        try:
            manager.stop()
            tun.stop()
            procs.kill_all()
            winutil.instance_file().unlink(missing_ok=True)
        finally:
            logging.shutdown()
            os._exit(0)

    state = AppState(cfg, manager, tun, port, on_quit=shutdown)
    app = create_app(state)

    from waitress import create_server
    server = create_server(app, host="0.0.0.0", port=port, threads=16,
                           channel_timeout=300, ident="KickIndirici")
    threading.Thread(target=server.run, name="waitress", daemon=True).start()
    winutil.write_instance(port)
    log.info("Sunucu: http://0.0.0.0:%s", port)
    if cfg["remote_enabled"]:
        tun.start(port)
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
