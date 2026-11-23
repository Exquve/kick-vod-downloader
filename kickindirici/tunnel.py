"""İsteğe bağlı uzaktan erişim: Cloudflare Quick Tunnel (cloudflared).

Port yönlendirme / modem ayarı gerekmez. cloudflared ilk kullanımda resmi GitHub
sürümünden indirilir ve Job Object içinde çalışır (Çıkış'ta kapanır).
Tünel üzerinden gelen her istek erişim anahtarı (token) ister.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import urllib.request

from . import procs
from .paths import IS_WINDOWS, bin_dir

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ASSET = ("cloudflared-windows-amd64.exe" if IS_WINDOWS else "cloudflared-linux-amd64")
DOWNLOAD = "https://github.com/cloudflare/cloudflared/releases/latest/download/" + ASSET


class Tunnel:
    def __init__(self):
        self.url: str | None = None
        self.status = "kapalı"
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def path(self):
        return bin_dir() / ("cloudflared.exe" if IS_WINDOWS else "cloudflared")

    def start(self, port: int) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self.status, self.url = "başlatılıyor", None
        threading.Thread(target=self._run, args=(port,), daemon=True, name="tunnel").start()

    def _run(self, port: int) -> None:
        try:
            exe = self.path()
            if not exe.exists():
                self.status = "cloudflared indiriliyor"
                tmp = exe.with_suffix(".download")
                req = urllib.request.Request(DOWNLOAD, headers={"User-Agent": "KickIndirici"})
                with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 18)
                if not IS_WINDOWS:
                    tmp.chmod(0o755)
                tmp.replace(exe)
            p = procs.popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            with self._lock:
                self.proc = p
            self.status = "bağlanıyor"
            for raw in p.stderr:
                line = raw.decode("utf-8", "replace")
                m = URL_RE.search(line)
                if m and not self.url:
                    self.url = m.group(0)
                    self.status = "açık"
                    log.info("Tünel: %s", self.url)
            p.wait()
            procs.forget(p)
            if self.status != "kapalı":
                self.status = "kapandı"
            self.url = None
        except Exception as e:
            log.exception("Tünel başlatılamadı")
            self.status = f"hata: {e}"

    def stop(self) -> None:
        with self._lock:
            p, self.proc = self.proc, None
            self.status, self.url = "kapalı", None
        if p:
            procs.kill_tree(p)
