"""Parça planlama ve dosya adı yardımcıları.

Seçilen aralık, seçilen sürelik parçalara ayrılır. Aralık seçilen süreden en az
1 dakika uzun değilse bölünmez; son parça 1 dakikadan kısa kalacaksa öncekine eklenir.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from . import procs
from .paths import ffprobe_path

log = logging.getLogger(__name__)

MIN_TAIL = 60.0  # saniye

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def _strip_symbols(name: str) -> str:
    """Emojileri ve görünmez karakterleri atar (dosya adında sorun çıkarıyorlar;
    Windows'ta emoji 2 karakter sayılır ve yol sınırını hızla doldurur)."""
    out = []
    for ch in name:
        if ord(ch) > 0xFFFF or ch in "\u200d\ufe0e\ufe0f" or unicodedata.category(ch) in ("So", "Cs", "Co", "Cf"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def utf16_len(s: str) -> int:
    """Windows yol uzunluğu UTF-16 birimiyle ölçülür."""
    return len(s.encode("utf-16-le")) // 2


def safe_filename(name: str, max_len: int = 120) -> str:
    name = _INVALID.sub(" ", _strip_symbols(name or ""))
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
                   "-of", "default=nw=1:nk=1", str(path)], timeout=60)
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


def part_name(title: str, index: int, total: int, ext: str = ".mp4", max_len: int = 120) -> str:
    width = max(2, len(str(total)))
    return f"{safe_filename(title, max_len)} - Parça {index:0{width}d}{ext}"


def plan_bounds(start: float, end: float, chunk_seconds: float) -> list[float]:
    """[start, kesimler..., end] — seçilen aralık için parça sınırları."""
    cuts = [start + c for c in plan_cuts(end - start, chunk_seconds)]
    return [start, *cuts, end]


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
