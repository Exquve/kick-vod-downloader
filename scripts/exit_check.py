"""Çıkış sonrası hiçbir KickIndirici / yt-dlp / ffmpeg süreci kalmadığını doğrular.

Kullanım:
    python scripts/exit_check.py dist/KickIndirici.exe
    python scripts/exit_check.py python run.py          (geliştirme)

Senaryolar:
  1. Çıkış (tepsi menüsündeki "Çıkış" ile aynı kod yolu: /api/quit)
  2. İçteki süreç Görev Yöneticisi'nden zorla öldürülmüş gibi (Job Object testi)
  3. Dıştaki süreç (PyInstaller bootloader) zorla öldürülmüş gibi
Her senaryoda önce yt-dlp ve ffmpeg alt süreçlerinin gerçekten çalıştığı görülür.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import psutil

NAMES = ("kickindirici", "yt-dlp", "ffmpeg", "cloudflared")


def http(port: int, path: str, method: str = "GET") -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def wait(pred, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"ZAMAN AŞIMI: {what}")


def tree(root: psutil.Process) -> list[psutil.Process]:
    try:
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def short(p: psutil.Process) -> str:
    try:
        return f"{p.pid}:{p.name()}"
    except psutil.Error:
        return f"{p.pid}:?"


def start(cmd: list[str], appdata: Path) -> tuple[psutil.Process, int]:
    env = dict(os.environ, KICK_TEST="1", KICK_APPDATA=str(appdata))
    env.pop("KICK_YTDLP", None)
    (appdata / "instance.json").unlink(missing_ok=True)
    p = subprocess.Popen([*cmd, "--no-browser"], env=env)
    port = wait(lambda: json.loads((appdata / "instance.json").read_text())["port"], 60, "sunucu açılışı")
    wait(lambda: http(port, "/api/ping")["ok"], 30, "ping")
    return psutil.Process(p.pid), port


def spawn_children(root: psutil.Process, port: int) -> set[int]:
    wait(lambda: http(port, "/api/state")["ytdlp_ready"], 240, "yt-dlp hazır")
    r = http(port, "/api/test/spawn", "POST")
    assert r.get("job"), "test işi eklenemedi"

    def both():
        names = " ".join(short(p).lower() for p in tree(root))
        return "ffmpeg" in names and "yt-dlp" in names
    wait(both, 90, "yt-dlp ve ffmpeg alt süreçleri")
    procs = tree(root)
    print("   çalışan:", ", ".join(short(p) for p in procs))
    return {p.pid for p in procs}


def _alive(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def leftovers(pids: set[int]) -> list[str]:
    out = []
    for p in psutil.process_iter():
        try:
            if p.pid == os.getpid() or not _alive(p):
                continue
            if p.pid in pids or (p.name() or "").lower().startswith(NAMES):
                out.append(short(p))
        except psutil.Error:
            pass
    return out


def verify_gone(pids: set[int], label: str) -> bool:
    end = time.time() + 15
    left = leftovers(pids)
    while left and time.time() < end:
        time.sleep(0.5)
        left = leftovers(pids)
    print(f"[{'TAMAM' if not left else 'HATA'}] {label}" + (f" — kalanlar: {left}" if left else ""))
    return not left


def innermost(root: psutil.Process) -> psutil.Process:
    """PyInstaller onefile'da Python kodunu çalıştıran (içteki) KickIndirici süreci."""
    me = root
    for c in root.children(recursive=True):
        try:
            if c.name().lower() == root.name().lower():
                me = c
        except psutil.Error:
            pass
    return me


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    cmd = sys.argv[1:]
    if not cmd:
        print(__doc__)
        return 2
    appdata = Path(tempfile.mkdtemp(prefix="kick-exit-"))
    results = []

    print("1) Çıkış menüsü")
    root, port = start(cmd, appdata)
    pids = spawn_children(root, port)
    http(port, "/api/quit", "POST")
    results.append(verify_gone(pids, "Çıkış sonrası süreç kalmadı"))

    print("2) İçteki süreç zorla öldürüldü (Job Object)")
    root, port = start(cmd, appdata)
    pids = spawn_children(root, port)
    innermost(root).kill()
    results.append(verify_gone(pids, "Zorla kapatma sonrası alt süreçler öldü"))

    if innermost(root).pid != root.pid or os.name == "nt":
        print("3) Dıştaki süreç zorla öldürüldü")
        root, port = start(cmd, appdata)
        pids = spawn_children(root, port)
        root.kill()
        results.append(verify_gone(pids, "Dış süreç öldürülünce her şey kapandı"))

    print("SONUÇ:", "BAŞARILI" if all(results) else "BAŞARISIZ")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
