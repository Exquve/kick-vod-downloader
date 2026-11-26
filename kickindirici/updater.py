"""Otomatik güncelleme: GitHub'daki en son Release'i kontrol eder, exe'yi indirir,
SHA256 ile doğrular, devam eden iş yokken exe'yi değiştirip yeniden başlatır.

Çalışan bir exe Windows'ta silinemez ama yeniden adlandırılabilir:
  KickIndirici.exe -> KickIndirici.exe.old ; KickIndirici.exe.new -> KickIndirici.exe
Yeni sürüm açılınca .old silinir. Bir şey ters giderse eski exe geri konur.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__

log = logging.getLogger(__name__)

REPO = "Exquve/kick-vod-downloader"
# KICK_UPDATE_API yalnızca otomatik testler içindir (sahte release sunucusu).
LATEST_API = os.environ.get("KICK_UPDATE_API") or f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "KickIndirici.exe"
CHECK_EVERY = 6 * 3600
FIRST_CHECK_DELAY = float(os.environ.get("KICK_UPDATE_FIRST_DELAY") or 20)


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def is_newer(tag: str, current: str = __version__) -> bool:
    return parse_version(tag) > parse_version(current)


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "KickIndirici-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_download(url: str, dest: Path, on_progress: Callable[[int, int], None] | None = None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "KickIndirici-updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cleanup_old(exe: Path) -> None:
    """Önceki güncellemeden kalan .old / yarım .new dosyalarını siler."""
    for suffix in (".old", ".new"):
        p = exe.with_name(exe.name + suffix)
        for _ in range(10):
            try:
                p.unlink(missing_ok=True)
                break
            except OSError:  # eski süreç hâlâ kapanıyor olabilir
                time.sleep(1)


class Updater:
    def __init__(self, exe: Path, enabled: Callable[[], bool], busy: Callable[[], bool],
                 restart: Callable[[], None], current: str = __version__,
                 fetch_json: Callable[[str], dict] = _http_json,
                 download: Callable[..., None] = _http_download):
        self.exe = Path(exe)
        self.enabled = enabled
        self.busy = busy
        self.restart = restart
        self.current = current
        self.fetch_json = fetch_json
        self.download = download
        self.state: dict = {"status": "idle", "current": current, "latest": None,
                            "error": "", "progress": None}
        self._lock = threading.Lock()
        self._wake = threading.Event()

    @property
    def new_path(self) -> Path:
        return self.exe.with_name(self.exe.name + ".new")

    @property
    def old_path(self) -> Path:
        return self.exe.with_name(self.exe.name + ".old")

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    # ---------- adımlar ----------
    def check_and_download(self) -> bool:
        """Yeni sürüm varsa indirip doğrular. Hazırsa True."""
        if self.state["status"] == "ready" and self.new_path.exists():
            return True
        self._set(status="checking", error="")
        try:
            rel = self.fetch_json(LATEST_API)
            tag = str(rel.get("tag_name") or "")
            self._set(latest=tag.lstrip("v") or None)
            if not is_newer(tag, self.current):
                self._set(status="uptodate")
                return False
            assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets", [])}
            exe_url, sha_url = assets.get(ASSET), assets.get(ASSET + ".sha256")
            if not exe_url or not sha_url:
                raise RuntimeError("Release'te exe ya da SHA256 dosyası yok")
            if not os.access(self.exe.parent, os.W_OK):
                raise RuntimeError(f"{self.exe.parent} klasörüne yazılamıyor; exe'yi yazılabilir bir "
                                   "klasöre (ör. Belgeler) taşıyın")
            tmp_sha = self.exe.with_name(self.exe.name + ".sha256.tmp")
            self.download(sha_url, tmp_sha)
            expected = tmp_sha.read_text("ascii", "replace").split()[0].strip().lower()
            tmp_sha.unlink(missing_ok=True)
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise RuntimeError("SHA256 dosyası geçersiz")
            self._set(status="downloading", progress=0.0)
            part = self.exe.with_name(self.exe.name + ".download")
            self.download(exe_url, part,
                          lambda d, t: self._set(progress=round(d / t * 100, 1) if t else None))
            got = sha256_of(part)
            if got != expected:
                part.unlink(missing_ok=True)
                raise RuntimeError("İndirilen güncellemenin SHA256 değeri tutmuyor")
            os.replace(part, self.new_path)
            self._set(status="ready", progress=100.0)
            log.info("Güncelleme hazır: %s", tag)
            return True
        except Exception as e:
            log.warning("Güncelleme kontrolü başarısız: %s", e)
            self._set(status="error", error=str(e), progress=None)
            return False

    def apply(self) -> bool:
        """exe'yi değiştirir ve yeniden başlatır. Başarısızsa eskiyi geri koyar."""
        if not self.new_path.exists():
            return False
        self._set(status="applying")
        try:
            self.old_path.unlink(missing_ok=True)
            os.replace(self.exe, self.old_path)
            try:
                os.replace(self.new_path, self.exe)
            except OSError:
                os.replace(self.old_path, self.exe)
                raise
        except OSError as e:
            log.exception("Güncelleme uygulanamadı")
            self._set(status="error", error=f"exe değiştirilemedi: {e}")
            return False
        log.info("Yeni exe yerinde, yeniden başlatılıyor")
        self.restart()
        return True

    def check_now(self) -> None:
        """Ayarlar'daki 'Güncellemeleri kontrol et' (yalnızca indirir, uygulamaz)."""
        threading.Thread(target=self.check_and_download, daemon=True, name="update-check").start()

    # ---------- arka plan döngüsü ----------
    def loop(self) -> None:
        self._wake.wait(FIRST_CHECK_DELAY)
        while True:
            self._wake.clear()
            if self.enabled() and self.check_and_download():
                while self.enabled() and self.busy():  # devam eden iş varsa bitmesini bekle
                    time.sleep(15)
                if self.enabled() and self.apply():
                    return
            self._wake.wait(CHECK_EVERY)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.loop, name="updater", daemon=True)
        t.start()
        return t


