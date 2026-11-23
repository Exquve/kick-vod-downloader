"""Resmi yt-dlp.exe'nin indirilmesi, güncellenmesi ve çıktısının ayrıştırılması."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

from . import procs
from .paths import IS_WINDOWS, bin_dir, ffmpeg_path

log = logging.getLogger(__name__)

RELEASE_BASE = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
PROGRESS_PREFIX = "[KP]"
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_PREFIX +
    "%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s|"
    "%(progress.fragment_index)s|%(progress.fragment_count)s"
)
MERGE_MARKERS = ("[Merger]", "[FixupM3u8]", "[VideoRemuxer]", "[FixupM4a]",
                 "[FixupTimestamp]", "[FixupDuration]", "[VideoConvertor]")

_ready = threading.Event()
_ready_error: list[str] = []
_lock = threading.Lock()


def ytdlp_path() -> Path:
    override = os.environ.get("KICK_YTDLP")
    if override:
        return Path(override)
    return bin_dir() / ("yt-dlp.exe" if IS_WINDOWS else "yt-dlp")


def download_ytdlp(dest: Path) -> None:
    asset = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
    tmp = dest.with_suffix(".download")
    log.info("yt-dlp indiriliyor: %s", RELEASE_BASE + asset)
    req = urllib.request.Request(RELEASE_BASE + asset, headers={"User-Agent": "KickIndirici"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 256)
    if not IS_WINDOWS:
        tmp.chmod(0o755)
    os.replace(tmp, dest)


def prepare() -> None:
    """İlk açılışta indirir, sonra `yt-dlp -U` ile günceller. Arka planda çalıştırılır."""
    path = ytdlp_path()
    try:
        if not path.exists():
            download_ytdlp(path)
        else:
            try:
                r = procs.run([str(path), "-U"], timeout=180)
                log.info("yt-dlp -U: %s", (r.stdout or b"").decode("utf-8", "replace").strip()[-300:])
            except subprocess.TimeoutExpired:
                log.warning("yt-dlp -U zaman aşımı")
            except OSError as e:
                log.warning("yt-dlp -U çalışmadı (%s), yeniden indiriliyor", e)
                download_ytdlp(path)
    except Exception as e:  # ağ yoksa vb.
        log.exception("yt-dlp hazırlanamadı")
        _ready_error[:] = [str(e)]
    finally:
        _ready.set()


def start_background_prepare() -> threading.Thread:
    t = threading.Thread(target=prepare, name="ytdlp-update", daemon=True)
    t.start()
    return t


def mark_ready() -> None:
    _ready.set()


def wait_ready(timeout: float | None = None) -> bool:
    return _ready.wait(timeout)


def is_ready() -> bool:
    return _ready.is_set() and ytdlp_path().exists()


def ready_error() -> str:
    return _ready_error[0] if _ready_error else ""


def common_args(cookies_browser: str = "") -> list[str]:
    args = ["--no-playlist", "--encoding", "utf-8", "--no-colors"]
    if cookies_browser:
        args += ["--cookies-from-browser", cookies_browser]
    return args


def info_args(url: str, cookies_browser: str = "") -> list[str]:
    return [str(ytdlp_path()), "-J", "--skip-download", *common_args(cookies_browser), "--", url]


def download_args(url: str, out_template: str, cookies_browser: str = "",
                  ffmpeg: str | None = None) -> list[str]:
    ff = ffmpeg or ffmpeg_path()
    ff_dir = str(Path(ff).parent) if os.path.dirname(ff) else ff
    return [
        str(ytdlp_path()),
        *common_args(cookies_browser),
        "--newline",
        "--progress",
        "--progress-template", PROGRESS_TEMPLATE,
        "--ffmpeg-location", ff_dir,
        "-N", "8",
        "--retries", "10",
        "--fragment-retries", "50",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--no-mtime",
        "-o", out_template,
        "--", url,
    ]


def _num(v: str) -> float | None:
    v = v.strip()
    if not v or v in ("NA", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_progress(line: str) -> dict | None:
    """`[KP]status|downloaded|total|estimate|speed|eta|frag_i|frag_n` satırını çözer."""
    idx = line.find(PROGRESS_PREFIX)
    if idx < 0:
        return None
    parts = line[idx + len(PROGRESS_PREFIX):].strip().split("|")
    if len(parts) < 6:
        return None
    parts += ["NA"] * (8 - len(parts))
    status = parts[0].strip()
    downloaded = _num(parts[1])
    total = _num(parts[2]) or _num(parts[3])
    speed = _num(parts[4])
    eta = _num(parts[5])
    fi, fn = _num(parts[6]), _num(parts[7])
    percent = None
    if fi is not None and fn:
        percent = min(100.0, fi / fn * 100.0)
    if downloaded is not None and total:
        percent = min(100.0, downloaded / total * 100.0)
    if status == "finished":
        percent = 100.0
    return {
        "status": status,
        "downloaded": downloaded,
        "total": total,
        "speed": speed,
        "eta": eta,
        "percent": percent,
    }


def is_merge_line(line: str) -> bool:
    return any(line.lstrip().startswith(m) for m in MERGE_MARKERS)


def fetch_info(url: str, cookies_browser: str = "", timeout: float = 120) -> dict:
    r = procs.run(info_args(url, cookies_browser), timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(summarize_error((r.stderr or b"").decode("utf-8", "replace")))
    data = json.loads((r.stdout or b"{}").decode("utf-8", "replace"))
    if data.get("_type") == "playlist" and data.get("entries"):
        data = data["entries"][0]
    return data


def summarize_error(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    errs = [l for l in lines if l.startswith("ERROR")]
    msg = (errs[-1] if errs else (lines[-1] if lines else "Bilinmeyen hata"))
    msg = msg.replace("ERROR: ", "")
    low = msg.lower()
    if "subscriber" in low or "login" in low or "403" in low or "cookies" in low:
        msg += " — Abonelere özel bir VOD olabilir: Ayarlar'dan tarayıcı çerezini seçin."
    return msg[:500]
