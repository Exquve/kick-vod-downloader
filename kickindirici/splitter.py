"""ffmpeg ile yeniden kodlamadan (-c copy) parçalara bölme.

Neden segment muxer?  Tek geçişte bütün parçaları üretir (N kez dosyayı okumaz),
kesimler anahtar kareye denk gelir; böylece her parça kendi başına sorunsuz oynar.
Ayrıca son parça 1 dakikadan kısa kalacaksa bir önceki parçaya eklenir.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import procs
from .paths import ffmpeg_path, ffprobe_path

log = logging.getLogger(__name__)

MIN_TAIL = 60.0  # saniye

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, max_len: int = 120) -> str:
    name = _INVALID.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = "video"
    if name.upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


def probe_duration(path: str | Path) -> float:
    r = procs.run([ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(path)], timeout=120)
    try:
        return float((r.stdout or b"").decode().strip())
    except ValueError:
        raise RuntimeError("Videonun süresi okunamadı: " +
                           (r.stderr or b"").decode("utf-8", "replace")[-300:])


def should_split(duration: float, chunk_seconds: float) -> bool:
    """Video seçilen süreden en az 1 dakika uzun değilse bölme yapılmaz."""
    return chunk_seconds > 0 and duration >= chunk_seconds + MIN_TAIL


def plan_cuts(duration: float, chunk_seconds: float) -> list[float]:
    """Kesim noktaları. Son parça MIN_TAIL'den kısaysa son kesim atılır."""
    if not should_split(duration, chunk_seconds):
        return []
    cuts: list[float] = []
    t = chunk_seconds
    while t < duration:
        cuts.append(t)
        t += chunk_seconds
    while cuts and duration - cuts[-1] < MIN_TAIL:
        cuts.pop()
    return cuts


def part_name(title: str, index: int, total: int, ext: str = ".mp4") -> str:
    width = max(2, len(str(total)))
    return f"{safe_filename(title)} - Parça {index:0{width}d}{ext}"


def split(src: str | Path, out_dir: str | Path, title: str, chunk_seconds: float,
          duration: float | None = None,
          on_progress: Callable[[float], None] | None = None,
          cancel: threading.Event | None = None,
          on_process: Callable[[subprocess.Popen | None], None] | None = None) -> list[Path]:
    src = Path(src)
    out_dir = Path(out_dir)
    if duration is None:
        duration = probe_duration(src)
    cuts = plan_cuts(duration, chunk_seconds)
    if not cuts:
        return [src]
    total = len(cuts) + 1
    width = max(2, len(str(total)))
    base = safe_filename(title).replace("%", "%%")
    pattern = str(out_dir / f"{base} - Parça %0{width}d.mp4")
    args = [
        ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-i", str(src),
        "-map", "0:v?", "-map", "0:a?", "-c", "copy",
        "-f", "segment",
        "-segment_times", ",".join(f"{c:.3f}" for c in cuts),
        "-segment_start_number", "1",
        "-reset_timestamps", "1",
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        "-progress", "pipe:1", "-nostats",
        pattern,
    ]
    log.info("Bölme: %s parça", total)
    p = procs.popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if on_process:
        on_process(p)
    err_chunks: list[bytes] = []
    t_err = threading.Thread(target=lambda: err_chunks.append(p.stderr.read()), daemon=True)
    t_err.start()
    try:
        for raw in p.stdout:
            if cancel is not None and cancel.is_set():
                procs.kill_tree(p)
                break
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and on_progress and duration:
                try:
                    us = int(line.split("=", 1)[1])
                    on_progress(max(0.0, min(100.0, us / 1e6 / duration * 100)))
                except ValueError:
                    pass
        p.wait()
        t_err.join(timeout=5)
    finally:
        procs.forget(p)
        if on_process:
            on_process(None)
    expected = [out_dir / part_name(title, i, total) for i in range(1, total + 1)]
    if cancel is not None and cancel.is_set():
        for f in expected:
            f.unlink(missing_ok=True)
        raise InterruptedError("iptal")
    if p.returncode != 0 or not all(f.exists() and f.stat().st_size > 0 for f in expected):
        for f in expected:
            f.unlink(missing_ok=True)
        msg = b"".join(err_chunks).decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"ffmpeg bölme hatası (kod {p.returncode}): {msg}")
    if on_progress:
        on_progress(100.0)
    os.remove(src)
    return expected
