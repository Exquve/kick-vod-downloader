"""%LOCALAPPDATA%\\KickIndirici\\config.json içindeki ayarlar."""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from .paths import appdata_dir, default_download_dir

CHUNK_CHOICES = (0, 20, 30, 60, 120, 180)
BROWSERS = ("", "chrome", "firefox", "edge")


def _defaults() -> dict[str, Any]:
    return {
        "download_dir": str(default_download_dir()),
        "default_chunk": 120,
        "cookies_browser": "",
        "port": 8765,
        "remote_enabled": False,
        "auto_update": True,
        "token": secrets.token_urlsafe(18),
        "firewall_prompted": False,
        "firewall_declined": False,
    }


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else appdata_dir() / "config.json"
        self._lock = threading.RLock()
        self._data = _defaults()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    self._data.update({k: v for k, v in loaded.items() if k in self._data})
            except (OSError, ValueError):
                pass
        self.save()

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)

    def download_dir(self) -> Path:
        d = Path(self["download_dir"])
        d.mkdir(parents=True, exist_ok=True)
        return d

    def apply_user_settings(self, data: dict[str, Any]) -> dict[str, str]:
        """Web arayüzünden gelen ayarları doğrular. Hata sözlüğü döner."""
        errors: dict[str, str] = {}
        new: dict[str, Any] = {}
        if "download_dir" in data:
            raw = str(data["download_dir"]).strip().strip('"')
            if not raw:
                errors["download_dir"] = "Klasör boş olamaz."
            else:
                p = Path(os.path.expandvars(os.path.expanduser(raw)))
                if not p.is_absolute():
                    errors["download_dir"] = "Tam yol girin (ör. D:\\Videolar\\Kick)."
                else:
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                        new["download_dir"] = str(p)
                    except OSError as e:
                        errors["download_dir"] = f"Klasör oluşturulamadı: {e}"
        if "cookies_browser" in data:
            b = str(data["cookies_browser"] or "")
            if b not in BROWSERS:
                errors["cookies_browser"] = "Geçersiz tarayıcı."
            else:
                new["cookies_browser"] = b
        if "default_chunk" in data:
            try:
                c = int(data["default_chunk"])
            except (TypeError, ValueError):
                c = -1
            if c not in CHUNK_CHOICES:
                errors["default_chunk"] = "Geçersiz parça süresi."
            else:
                new["default_chunk"] = c
        if "remote_enabled" in data:
            new["remote_enabled"] = bool(data["remote_enabled"])
        if "auto_update" in data:
            new["auto_update"] = bool(data["auto_update"])
        if not errors and new:
            self.update(**new)
        return errors

    def reset_token(self) -> str:
        tok = secrets.token_urlsafe(18)
        self.update(token=tok)
        return tok
