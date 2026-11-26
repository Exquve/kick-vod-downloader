"""Kendi HLS indiricimiz: yalnızca istenen aralığın segmentlerini paralel indirir.

Neden?  Kick VOD'ları HLS'tir (çok sayıda kısa .ts segmenti). Tüm VOD'u indirip sonra
bölmek yerine sadece seçilen aralığın segmentleri 8 paralel bağlantıyla çekilir ve
sırayla ffmpeg'in girişine (stdin) yazılır; ffmpeg yeniden kodlamadan (-c copy) mp4 üretir.
Böylece her parça hazır olur olmaz kullanılabilir.
Parçalar segment sınırından kesilir: aradaki boşluk/çakışma olmaz.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin

from . import procs
from .paths import long_path

log = logging.getLogger(__name__)


class Meter:
    """İnternetten gerçekten alınan bayt sayısı (iş parçacığı güvenli)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0

    def add(self, n: int) -> None:
        with self._lock:
            self.total += n


class _PipeClosed(Exception):
    pass


class Unsupported(Exception):
    """Bu playlist bizim indiricimizle indirilemez (şifreli vb.) -> yt-dlp yedeği."""


@dataclass
class Segment:
    uri: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Playlist:
    segments: list[Segment] = field(default_factory=list)
    init_uri: str | None = None

    @property
    def total(self) -> float:
        return self.segments[-1].end if self.segments else 0.0


_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(s: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR.findall(s)}


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def best_variant(text: str, base: str) -> str:
    best, best_bw = None, -1
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            bw = int(_attrs(line.split(":", 1)[1]).get("BANDWIDTH", "0") or 0)
            for nxt in lines[i + 1:]:
                nxt = nxt.strip()
                if nxt and not nxt.startswith("#"):
                    if bw > best_bw:
                        best, best_bw = urljoin(base, nxt), bw
                    break
    if not best:
        raise Unsupported("master playlist'te varyant yok")
    return best


def parse_media(text: str, base: str) -> Playlist:
    if not text.lstrip().startswith("#EXTM3U"):
        raise Unsupported("m3u8 değil")
    pl = Playlist()
    t = 0.0
    dur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",", 1)[0])
        elif line.startswith("#EXT-X-KEY"):
            if _attrs(line.split(":", 1)[1]).get("METHOD", "NONE").upper() != "NONE":
                raise Unsupported("şifreli HLS")
        elif line.startswith("#EXT-X-BYTERANGE"):
            raise Unsupported("byterange HLS")
        elif line.startswith("#EXT-X-MAP"):
            uri = _attrs(line.split(":", 1)[1]).get("URI")
            if uri:
                pl.init_uri = urljoin(base, uri)
        elif not line.startswith("#"):
            if dur is None:
                raise Unsupported("EXTINF eksik")
            pl.segments.append(Segment(urljoin(base, line), t, dur))
            t += dur
            dur = None
    if not pl.segments:
        raise Unsupported("segment yok")
    return pl


def fetch(url: str, headers: dict | None = None, timeout: float = 30, retries: int = 6,
          cancel: threading.Event | None = None, meter: Meter | None = None) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("iptal")
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                chunks = []
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("iptal")
                    c = r.read(65536)
                    if not c:
                        break
                    chunks.append(c)
                    if meter is not None:
                        meter.add(len(c))
                return b"".join(chunks)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404, 410):
                raise
        except (OSError, urllib.error.URLError) as e:
            last = e
        time.sleep(min(8, 0.5 * 2 ** attempt))
    raise last or RuntimeError("indirilemedi")


def load_playlist(url: str, headers: dict | None = None) -> Playlist:
    text = fetch(url, headers).decode("utf-8", "replace")
    if is_master(text):
        url = best_variant(text, url)
        text = fetch(url, headers).decode("utf-8", "replace")
    return parse_media(text, url)


def assign(pl: Playlist, bounds: list[float]) -> list[list[int]]:
    """Segmentleri [b0,b1),[b1,b2)... aralıklarına dağıtır (segment başlangıcına göre).

    Aralık başlangıcını içeren segment ilk parçaya dahil edilir; böylece istenen an kaçmaz.
    """
    a, b = bounds[0], bounds[-1]
    groups: list[list[int]] = [[] for _ in range(len(bounds) - 1)]
    for i, s in enumerate(pl.segments):
        if s.end <= a or s.start >= b:
            continue
        if s.start < a:
            groups[0].append(i)
            continue
        k = 0
        while k + 1 < len(bounds) - 1 and s.start >= bounds[k + 1]:
            k += 1
        groups[k].append(i)
    return groups


def download_part(pl: Playlist, idxs: list[int], out_path: str, ffmpeg: str,
                  headers: dict | None = None, workers: int = 8,
                  cancel: threading.Event | None = None,
                  on_progress: Callable[[int, int, int], None] | None = None,
                  on_process: Callable[[subprocess.Popen | None], None] | None = None,
                  meter: Meter | None = None) -> None:
    """idxs segmentlerini indirip out_path'e mp4 olarak yazar.

    on_progress(yazılan_segment, toplam_segment, yazılan_bayt) yazma sırasında ve
    beklerken yarım saniyede bir çağrılır (hız göstergesi bekleme anlarında da güncel kalsın).
    """
    cancel = cancel or threading.Event()
    tmp = out_path + ".part.mp4"
    in_fmt = [] if pl.init_uri else ["-f", "mpegts"]
    args = [ffmpeg, "-hide_banner", "-y", "-v", "error", *in_fmt, "-i", "pipe:0",
            "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart",
            "-f", "mp4", tmp]
    p = procs.popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if on_process:
        on_process(p)
    err: list[bytes] = []
    t_err = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t_err.start()
    ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="seg")
    ok = False

    def write(data: bytes) -> None:
        try:
            p.stdin.write(data)
        except OSError as e:  # ffmpeg erken kapandı
            raise _PipeClosed(str(e)) from e

    try:
        if pl.init_uri:
            write(fetch(pl.init_uri, headers, cancel=cancel, meter=meter))
        queue: collections.deque = collections.deque()
        it = iter(idxs)

        def submit_next() -> None:
            i = next(it, None)
            if i is not None:
                queue.append(ex.submit(fetch, pl.segments[i].uri, headers, 30, 6, cancel, meter))

        for _ in range(workers * 2):
            submit_next()
        done = nbytes = 0
        while queue:
            fut = queue.popleft()
            while True:
                if cancel.is_set():
                    raise InterruptedError("iptal")
                try:
                    data = fut.result(timeout=0.5)
                    break
                except TimeoutError:
                    if on_progress:
                        on_progress(done, len(idxs), nbytes)
                    continue
            write(data)
            done += 1
            nbytes += len(data)
            if on_progress:
                on_progress(done, len(idxs), nbytes)
            submit_next()
        try:
            p.stdin.close()
        except OSError as e:
            raise _PipeClosed(str(e)) from e
        p.wait()
        t_err.join(timeout=5)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg hatası: " + b"".join(err).decode("utf-8", "replace")[-400:])
        os.replace(long_path(tmp), long_path(out_path))
        ok = True
    except _PipeClosed as e:
        if cancel.is_set():
            raise InterruptedError("iptal")
        p.wait(timeout=10)
        t_err.join(timeout=5)
        msg = b"".join(err).decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"Parça yazılamadı: {msg or e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        if not ok:
            procs.kill_tree(p)
            try:
                os.remove(long_path(tmp))
            except OSError:
                pass
        procs.forget(p)
        if on_process:
            on_process(None)
