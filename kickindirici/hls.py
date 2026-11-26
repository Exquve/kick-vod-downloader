"""Kendi HLS indiricimiz: yalnızca istenen aralığın segmentlerini paralel indirir.

Neden?  Kick VOD'ları HLS'tir (çok sayıda kısa .ts segmenti). Tüm VOD'u indirip sonra
bölmek yerine sadece seçilen aralığın segmentleri 8 paralel bağlantıyla çekilir ve
sırayla ffmpeg'in girişine (stdin) yazılır; ffmpeg yeniden kodlamadan (-c copy) mp4 üretir.
Böylece her parça hazır olur olmaz kullanılabilir.
Parçalar segment sınırından kesilir: aradaki boşluk/çakışma olmaz.
"""
from __future__ import annotations

import collections
import json
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


def _read_state(path: str) -> dict | None:
    try:
        with open(long_path(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_state(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(long_path(tmp), "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(long_path(tmp), long_path(path))
    except OSError:  # antivirüs kilidi vb.: bir sonraki segmentte tekrar yazılır
        pass


def part_files(out_path: str) -> tuple[str, str, str]:
    """(ham veri, durum dosyası, mp4'e çevirme ara dosyası)"""
    return out_path + ".part.ts", out_path + ".part.json", out_path + ".part.mp4"


def download_part(pl: Playlist, idxs: list[int], out_path: str, ffmpeg: str,
                  headers: dict | None = None, workers: int = 8,
                  cancel: threading.Event | None = None,
                  on_progress: Callable[[int, int, int], None] | None = None,
                  on_process: Callable[[subprocess.Popen | None], None] | None = None,
                  meter: Meter | None = None,
                  on_finalize: Callable[[], None] | None = None) -> None:
    """idxs segmentlerini indirip out_path'e mp4 olarak yazar; kaldığı yerden devam edebilir.

    Segmentler sırayla ham dosyaya (.part.ts) eklenir ve her segmentten sonra kaçıncı
    segmentte kalındığı .part.json'a yazılır. Durdurma, internet kopması ya da programın
    kapanması sonrası aynı çağrı yalnızca kalan segmentleri indirir. Hepsi inince ham dosya
    yeniden kodlamadan (-c copy, +faststart) mp4'e çevrilir.

    on_progress(yazılan_segment, toplam_segment, ham_dosya_baytı) yazma sırasında ve
    beklerken yarım saniyede bir çağrılır (hız göstergesi bekleme anlarında da güncel kalsın).
    """
    cancel = cancel or threading.Event()
    raw, state_path, tmp = part_files(out_path)
    n = len(idxs)
    sig = {"n": n, "first": pl.segments[idxs[0]].uri if idxs else "",
           "last": pl.segments[idxs[-1]].uri if idxs else "", "init": pl.init_uri}
    k, offset = 0, 0
    st = _read_state(state_path)
    if st and st.get("sig") == sig and os.path.exists(long_path(raw)):
        k, offset = int(st.get("k", 0)), int(st.get("offset", 0))
        if os.path.getsize(long_path(raw)) < offset:  # tutarsız: baştan başla
            k, offset = 0, 0
    if k:
        log.info("Parça kaldığı yerden devam ediyor: %d/%d segment", k, n)

    if k < n:
        f = open(long_path(raw), "r+b" if k else "wb")
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="seg")
        try:
            f.truncate(offset)
            f.seek(offset)
            if not k and pl.init_uri:
                data = fetch(pl.init_uri, headers, cancel=cancel, meter=meter)
                f.write(data)
                offset += len(data)
            queue: collections.deque = collections.deque()
            it = iter(idxs[k:])

            def submit_next() -> None:
                i = next(it, None)
                if i is not None:
                    queue.append(ex.submit(fetch, pl.segments[i].uri, headers, 30, 6, cancel, meter))

            for _ in range(workers * 2):
                submit_next()
            if on_progress:
                on_progress(k, n, offset)
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
                            on_progress(k, n, offset)
                f.write(data)
                f.flush()
                k += 1
                offset += len(data)
                _write_state(state_path, {"sig": sig, "k": k, "offset": offset})
                if on_progress:
                    on_progress(k, n, offset)
                submit_next()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
            f.close()

    # Ham veriyi mp4'e çevir (yeniden kodlama yok)
    if on_finalize:
        on_finalize()
    if cancel.is_set():
        raise InterruptedError("iptal")
    in_fmt = ["-f", "mpegts"] if not pl.init_uri else []
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", *in_fmt, "-i", raw,
            "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp]
    p = procs.popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if on_process:
        on_process(p)
    try:
        _, err = p.communicate()
    finally:
        procs.forget(p)
        if on_process:
            on_process(None)
    if cancel.is_set() or p.returncode != 0:
        try:
            os.remove(long_path(tmp))
        except OSError:
            pass
        if cancel.is_set():
            raise InterruptedError("iptal")
        raise RuntimeError("ffmpeg hatası: " + (err or b"").decode("utf-8", "replace")[-400:])
    os.replace(long_path(tmp), long_path(out_path))
    for leftover in (raw, state_path):
        try:
            os.remove(long_path(leftover))
        except OSError:
            pass
