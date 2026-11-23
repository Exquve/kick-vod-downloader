"""İş kuyruğu: işler sırayla, tek tek işlenir ve diske kaydedilir."""
from __future__ import annotations

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

from . import procs, splitter, ytdlp

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
            if job.status == DONE:
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
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def add(self, url: str, chunk: int) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], url=url.strip(), chunk=int(chunk))
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
            job.status, job.error, job.percent = QUEUED, "", 0.0
            job.speed = job.eta = None
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
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            out.append({"name": p.name, "size": p.stat().st_size})
    return out


def default_processor(cookies_browser: Callable[[], str]) -> Processor:
    """Gerçek işleyici: yt-dlp ile indir, gerekirse ffmpeg ile böl."""

    def process(ctx: JobContext) -> None:
        job = ctx.job
        mgr = ctx.manager
        ctx.update(status=PREPARING, percent=0.0)
        while not ytdlp.wait_ready(0.5):
            ctx.check()
        if not ytdlp.ytdlp_path().exists():
            raise RuntimeError("yt-dlp indirilemedi (internet bağlantısını kontrol edin). "
                               + ytdlp.ready_error())
        cookies = cookies_browser()
        info = ytdlp.fetch_info(job.url, cookies)
        ctx.check()
        title = info.get("title") or info.get("id") or "video"
        duration = info.get("duration")
        safe = splitter.safe_filename(title, 80)
        d = mgr.download_dir() / f"{safe} [{job.id}]"
        d.mkdir(parents=True, exist_ok=True)
        ctx.update(save=True, title=title, duration=duration, dir=str(d), status=DOWNLOADING)

        out_tmpl = str(d / (splitter.safe_filename(title).replace("%", "%%") + ".%(ext)s"))
        args = ytdlp.download_args(job.url, out_tmpl, cookies)
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
                    upd = {k: prog[k] for k in ("speed", "eta", "downloaded", "total")}
                    if prog["percent"] is not None:
                        upd["percent"] = prog["percent"]
                    if job.status != MERGING:
                        ctx.update(**upd)
                    continue
                if ytdlp.is_merge_line(line):
                    ctx.update(save=True, status=MERGING, percent=100.0, speed=None, eta=None)
                tail.append(line)
                del tail[:-30]
            p.wait()
        finally:
            procs.forget(p)
            ctx.set_proc(None)
        ctx.check()
        if p.returncode != 0:
            raise RuntimeError(ytdlp.summarize_error("\n".join(tail)))

        vids = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        if not vids:
            raise RuntimeError("İndirilen video dosyası bulunamadı.")
        src = max(vids, key=lambda f: f.stat().st_size)
        if job.chunk:
            dur = splitter.probe_duration(src)
            ctx.update(duration=dur)
            if splitter.should_split(dur, job.chunk * 60):
                ctx.update(save=True, status=SPLITTING, percent=0.0, speed=None, eta=None)
                splitter.split(src, d, title, job.chunk * 60, duration=dur,
                               on_progress=lambda pc: ctx.update(percent=pc),
                               cancel=ctx.cancel, on_process=ctx.set_proc)
        ctx.update(files=list_files(d))

    return process
