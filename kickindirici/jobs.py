"""İş kuyruğu: işler sırayla, tek tek işlenir ve diske kaydedilir."""
from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import hls, procs, splitter, ytdlp
from .paths import ffmpeg_path

log = logging.getLogger(__name__)

QUEUED, PREPARING, DOWNLOADING, MERGING, SPLITTING = "queued", "preparing", "downloading", "merging", "splitting"
DONE, ERROR, CANCELED = "done", "error", "canceled"
ACTIVE = {PREPARING, DOWNLOADING, MERGING, SPLITTING}
FINISHED = {DONE, ERROR, CANCELED}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".ts", ".m4v", ".flv"}


@dataclass
class Job:
    id: str
    url: str
    chunk: int  # dakika, 0 = bölme yok
    start: float = 0.0  # seçilen aralık (sn)
    end: float | None = None  # None = sonuna kadar
    created: float = field(default_factory=time.time)
    title: str = ""
    status: str = QUEUED
    percent: float = 0.0
    speed: float | None = None
    eta: float | None = None
    downloaded: float | None = None
    total: float | None = None
    duration: float | None = None
    error: str = ""
    dir: str = ""
    files: list[dict] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)
    part_index: int = 0  # şu an işlenen parça (1'den başlar)
    finished: float | None = None

    def public(self) -> dict:
        d = asdict(self)
        d.pop("dir", None)
        return d


class Canceled(Exception):
    pass


class JobContext:
    """İşleyiciye (downloader) verilen yardımcılar."""

    def __init__(self, manager: "JobManager", job: Job):
        self.manager = manager
        self.job = job
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None

    def set_proc(self, p: subprocess.Popen | None) -> None:
        self.proc = p
        if p is not None and self.cancel.is_set():
            procs.kill_tree(p)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Canceled()

    def update(self, save: bool = False, **kw) -> None:
        self.manager._update(self.job, save=save, **kw)


Processor = Callable[[JobContext], None]


class JobManager:
    def __init__(self, store: Path, download_dir: Callable[[], Path],
                 processor: Processor | None = None):
        self.store = Path(store)
        self.download_dir = download_dir
        self.processor = processor or default_processor(lambda: "")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._current: JobContext | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._load()

    # ---------- kalıcılık ----------
    def _load(self) -> None:
        if not self.store.exists():
            return
        try:
            data = json.loads(self.store.read_text("utf-8"))
        except (OSError, ValueError):
            log.exception("jobs.json okunamadı")
            return
        for raw in data.get("jobs", []):
            try:
                job = Job(**{k: v for k, v in raw.items() if k in Job.__dataclass_fields__})
            except TypeError:
                continue
            if job.status in ACTIVE:
                job.status = ERROR
                job.error = "Uygulama kapandığı için yarıda kaldı. Tekrar deneyebilirsiniz."
            for part in job.parts:
                if part.get("status") != DONE:
                    part["status"], part["percent"] = QUEUED, 0.0
            job.files = [f for f in job.files if Path(job.dir, f["name"]).exists()]
            self._jobs[job.id] = job

    def _save(self) -> None:
        with self._lock:
            data = {"jobs": [asdict(j) for j in self._jobs.values()]}
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.store)

    # ---------- dış API ----------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            if self._current:
                self._cancel_ctx(self._current)
            self._wake.notify_all()

    def list(self) -> list[Job]:
        with self._lock:
            snap = [Job(**asdict(j)) for j in self._jobs.values()]
        return sorted(snap, key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def add(self, url: str, chunk: int, start: float = 0.0, end: float | None = None,
            title: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:10], url=url.strip(), chunk=int(chunk),
                  start=max(0.0, float(start or 0)), end=float(end) if end else None, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._save()
            self._wake.notify_all()
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in FINISHED:
                return False
            if self._current and self._current.job.id == job_id:
                self._cancel_ctx(self._current)
            else:
                job.status = CANCELED
                job.finished = time.time()
                self._save()
            return True

    def retry(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in (ERROR, CANCELED):
                return False
            job.status, job.error = QUEUED, ""
            job.speed = job.eta = None
            for part in job.parts:
                if part.get("status") != DONE:
                    part["status"], part["percent"] = QUEUED, 0.0
            job.created = time.time()
            self._save()
            self._wake.notify_all()
            return True

    def delete(self, job_id: str, timeout: float = 15) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if self._current and self._current.job.id == job_id:
                self._cancel_ctx(self._current)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not (self._current and self._current.job.id == job_id):
                    break
            time.sleep(0.1)
        with self._lock:
            self._remove_dir(job)
            self._jobs.pop(job_id, None)
            self._save()
        return True

    def file_path(self, job_id: str, name: str) -> Path | None:
        job = self.get(job_id)
        if not job or not job.dir:
            return None
        if name not in {f["name"] for f in job.files}:
            return None
        p = Path(job.dir) / name
        return p if p.is_file() else None

    # ---------- iç ----------
    def _remove_dir(self, job: Job) -> None:
        if not job.dir:
            return
        d = Path(job.dir)
        # Sadece bu işe ait klasörü sil (adı [id] ile biter)
        if d.is_dir() and d.name.endswith(f"[{job.id}]"):
            shutil.rmtree(d, ignore_errors=True)

    def _cancel_ctx(self, ctx: JobContext) -> None:
        ctx.cancel.set()
        if ctx.proc is not None:
            threading.Thread(target=procs.kill_tree, args=(ctx.proc,), daemon=True).start()

    def _update(self, job: Job, save: bool = False, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(job, k, v)
            if save:
                self._save()

    def _next(self) -> Job | None:
        queued = [j for j in self._jobs.values() if j.status == QUEUED]
        return min(queued, key=lambda j: j.created) if queued else None

    def _worker(self) -> None:
        while True:
            with self._lock:
                while not self._stop and self._next() is None:
                    self._wake.wait(1.0)
                if self._stop:
                    return
                job = self._next()
                ctx = JobContext(self, job)
                self._current = ctx
                job.status = PREPARING
                job.error = ""
                self._save()
            try:
                self.processor(ctx)
                ctx.check()
                self._update(job, save=True, status=DONE, percent=100.0, speed=None, eta=None,
                             finished=time.time())
            except (Canceled, InterruptedError):
                self._update(job, save=True, status=CANCELED, speed=None, eta=None,
                             finished=time.time())
            except Exception as e:
                if ctx.cancel.is_set():
                    self._update(job, save=True, status=CANCELED, speed=None, eta=None,
                                 finished=time.time())
                else:
                    log.exception("İş başarısız: %s", job.url)
                    self._update(job, save=True, status=ERROR, error=str(e) or e.__class__.__name__,
                                 speed=None, eta=None, finished=time.time())
            finally:
                with self._lock:
                    self._current = None


def list_files(directory: Path) -> list[dict]:
    out = []
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.endswith(".part.mp4"):
            out.append({"name": p.name, "size": p.stat().st_size})
    return out


class Prober:
    """yt-dlp -J sonuçlarını kısa süre önbellekler (arayüzdeki önizleme + iş başlangıcı)."""

    TTL = 15 * 60

    def __init__(self, cookies_browser: Callable[[], str]):
        self.cookies_browser = cookies_browser
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, url: str, max_age: float | None = None) -> dict:
        max_age = self.TTL if max_age is None else max_age
        with self._lock:
            hit = self._cache.get(url)
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
        info = ytdlp.fetch_info(url, self.cookies_browser())
        if not info.get("duration") and info.get("url") and not info.get("requested_formats"):
            # Doğrudan dosya linkleri: süreyi ffprobe ile öğren (aralık seçimi için gerekli)
            try:
                info["duration"] = splitter.probe_duration(info["url"])
            except Exception:
                pass
        with self._lock:
            self._cache[url] = (time.time(), info)
        return info

    @staticmethod
    def summary(info: dict) -> dict:
        return {
            "title": info.get("title") or info.get("id") or "video",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel") or "",
        }


def hls_source(info: dict) -> tuple[str, dict] | None:
    """Seçilen format tek parça HLS ise (Kick) playlist URL'si ve başlıkları."""
    if info.get("requested_formats"):
        return None
    url, proto = info.get("url"), str(info.get("protocol") or "")
    if url and proto.startswith("m3u8"):
        return url, dict(info.get("http_headers") or {})
    return None


def _part_file_name(title: str, n: int, i: int, a: float, b: float | None, full: bool) -> str:
    if n > 1:
        return splitter.part_name(title, i, n)
    if full or b is None:
        return splitter.safe_filename(title) + ".mp4"
    rng = f"{splitter.fmt_time(a)}-{splitter.fmt_time(b)}".replace(":", ".")
    return f"{splitter.safe_filename(title)} ({rng}).mp4"


def plan_parts(title: str, start: float, end: float | None, total: float | None, chunk_min: int,
               pl: hls.Playlist | None) -> list[dict]:
    if total is None:  # süre bilinmiyor: tek parça, tamamı
        return [{"index": 1, "start": 0.0, "end": None, "status": QUEUED, "percent": 0.0,
                 "name": splitter.safe_filename(title) + ".mp4", "size": None}]
    a = min(max(0.0, start), total)
    b = total if end is None else min(max(end, a), total)
    if b - a < 1:
        raise RuntimeError("Seçilen aralık çok kısa.")
    bounds = splitter.plan_bounds(a, b, chunk_min * 60) if chunk_min else [a, b]
    ranges: list[tuple[float, float]] = []
    if pl is not None:
        for group in hls.assign(pl, bounds):
            if group:
                ranges.append((pl.segments[group[0]].start, pl.segments[group[-1]].end))
    else:
        ranges = list(zip(bounds, bounds[1:]))
    full = a <= 0.5 and b >= total - 0.5
    n = len(ranges)
    return [{"index": i, "start": s, "end": e, "status": QUEUED, "percent": 0.0,
             "name": _part_file_name(title, n, i, s, e, full), "size": None}
            for i, (s, e) in enumerate(ranges, 1)]


class _Rate:
    """Hız ve kalan süre için kayan pencere."""

    def __init__(self, window: float = 6.0):
        self.window = window
        self.samples: collections.deque = collections.deque()

    def add(self, nbytes: float, video_sec: float) -> None:
        now = time.time()
        self.samples.append((now, nbytes, video_sec))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def rates(self) -> tuple[float | None, float | None]:
        if len(self.samples) < 2:
            return None, None
        (t0, b0, v0), (t1, b1, v1) = self.samples[0], self.samples[-1]
        dt = t1 - t0
        if dt <= 0.2:
            return None, None
        return (b1 - b0) / dt, (v1 - v0) / dt


def default_processor(cookies_browser: Callable[[], str], prober: Prober | None = None) -> Processor:
    """Gerçek işleyici: seçilen aralığı parça parça indirir; her parça bitince hazır olur."""
    prober = prober or Prober(cookies_browser)

    def process(ctx: JobContext) -> None:
        job = ctx.job
        mgr = ctx.manager
        ctx.update(status=PREPARING)
        while not ytdlp.wait_ready(0.5):
            ctx.check()
        if not ytdlp.ytdlp_path().exists():
            raise RuntimeError("yt-dlp indirilemedi (internet bağlantısını kontrol edin). "
                               + ytdlp.ready_error())
        info = prober.get(job.url)
        ctx.check()
        title = job.title or info.get("title") or info.get("id") or "video"
        if job.dir and Path(job.dir).is_dir():
            d = Path(job.dir)
        else:
            d = mgr.download_dir() / f"{splitter.safe_filename(title, 80)} [{job.id}]"
            d.mkdir(parents=True, exist_ok=True)

        pl: hls.Playlist | None = None
        src = hls_source(info)
        if src:
            try:
                pl = hls.load_playlist(*src)
            except Exception as e:  # şifreli, erişim yok vb. -> yt-dlp yedeği
                log.warning("HLS doğrudan okunamadı (%s); yt-dlp ile indirilecek", e)
                pl = None
        total = pl.total if pl else info.get("duration")
        ctx.update(save=True, title=title, duration=total, dir=str(d), status=DOWNLOADING)

        if not job.parts or any(not Path(d, p["name"]).exists() for p in job.parts if p["status"] == DONE):
            ctx.update(save=True, parts=plan_parts(title, job.start, job.end, total, job.chunk, pl))
        parts = job.parts
        span = sum(((p["end"] if p["end"] is not None else (total or 0)) - p["start"]) for p in parts) or 1
        rate = _Rate()

        def done_seconds() -> float:
            return sum(((p["end"] or 0) - p["start"]) for p in parts if p["status"] == DONE)

        for part in parts:
            if part["status"] == DONE and Path(d, part["name"]).exists():
                continue
            ctx.check()
            p_len = (part["end"] if part["end"] is not None else (total or 0)) - part["start"]
            base_done = done_seconds()
            part.update(status=DOWNLOADING, percent=0.0)
            ctx.update(save=True, part_index=part["index"])
            out = str(d / part["name"])

            def progress(frac: float | None, nbytes: float) -> None:
                if frac is not None:
                    part["percent"] = round(frac * 100, 1)
                vid = base_done + (frac or 0) * p_len
                rate.add(nbytes + sum(p.get("size") or 0 for p in parts if p["status"] == DONE), vid)
                bps, vps = rate.rates()
                eta = (span - vid) / vps if vps else None
                ctx.update(percent=min(100.0, vid / span * 100), speed=bps, eta=eta)

            if pl is not None:
                idxs = hls.assign(pl, [part["start"], part["end"]])[0]
                hls.download_part(pl, idxs, out, ffmpeg_path(), headers=src[1], cancel=ctx.cancel,
                                  on_process=ctx.set_proc,
                                  on_progress=lambda k, n, b: progress(k / n, b))
            else:
                _ytdlp_section(ctx, job.url, cookies_browser(), part, total, out, progress)
            ctx.check()
            part.update(status=DONE, percent=100.0, size=Path(out).stat().st_size)
            ctx.update(save=True, files=list_files(d))
        ctx.update(files=list_files(d), part_index=0)

    return process


def _ytdlp_section(ctx: JobContext, url: str, cookies: str, part: dict, total: float | None,
                   out: str, progress: Callable[[float | None, float], None]) -> None:
    """Yedek yol: yt-dlp --download-sections ile yalnızca bu parçanın aralığı."""
    d = Path(out).parent
    stem = f".tmp-part-{part['index']:03d}"
    for old in d.glob(stem + ".*"):
        old.unlink(missing_ok=True)
    args = ytdlp.download_args(url, str(d / (stem + ".%(ext)s")), cookies)
    whole = part["end"] is None or (part["start"] <= 0.5 and total and part["end"] >= total - 0.5)
    if not whole:
        sep = args.index("--")
        args[sep:sep] = ["--download-sections", f"*{part['start']:.3f}-{part['end']:.3f}",
                         "--force-overwrites"]
    p = procs.popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ctx.set_proc(p)
    tail: list[str] = []
    try:
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            prog = ytdlp.parse_progress(line)
            if prog:
                frac = prog["percent"] / 100 if prog["percent"] is not None else None
                progress(frac, prog["downloaded"] or 0)
                continue
            tail.append(line)
            del tail[:-30]
        p.wait()
    finally:
        procs.forget(p)
        ctx.set_proc(None)
    ctx.check()
    if p.returncode != 0:
        raise RuntimeError(ytdlp.summarize_error("\n".join(tail)))
    got = [f for f in d.glob(stem + ".*") if f.suffix.lower() in VIDEO_EXTS]
    if not got:
        raise RuntimeError("İndirilen video dosyası bulunamadı.")
    best = max(got, key=lambda f: f.stat().st_size)
    try:
        ok = splitter.probe_duration(best) > 0
    except Exception:
        ok = False
    if not ok:
        for f in d.glob(stem + ".*"):
            f.unlink(missing_ok=True)
        raise RuntimeError("Bu aralık indirilemedi (sunucu ileri sarmayı desteklemiyor olabilir). "
                           "Tamamını indirmeyi deneyin.")
    os.replace(best, out)
    for f in d.glob(stem + ".*"):
        f.unlink(missing_ok=True)
