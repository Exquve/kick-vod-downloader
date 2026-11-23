"""Uygulama klasörleri ve gömülü araçların yolları."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import APP_NAME

IS_WINDOWS = os.name == "nt"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_name(name: str) -> str:
    return name + ".exe" if IS_WINDOWS else name


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """PyInstaller'ın açtığı geçici klasör (sys._MEIPASS) ya da proje kökü."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return package_dir().parent


def appdata_dir() -> Path:
    override = os.environ.get("KICK_APPDATA")
    if override:
        base = Path(override)
    elif IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def bin_dir() -> Path:
    d = appdata_dir() / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_download_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Videos" / "KickVODs"


def _tool(name: str, env: str) -> str:
    override = os.environ.get(env)
    if override:
        return override
    bundled = bundle_dir() / "bin" / exe_name(name)
    if bundled.exists():
        return str(bundled)
    vendored = package_dir().parent / "vendor" / exe_name(name)
    if vendored.exists():
        return str(vendored)
    found = shutil.which(name)
    return found or exe_name(name)


def ffmpeg_path() -> str:
    return _tool("ffmpeg", "KICK_FFMPEG")


def ffprobe_path() -> str:
    return _tool("ffprobe", "KICK_FFPROBE")
