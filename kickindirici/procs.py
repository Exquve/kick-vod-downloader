"""Alt süreç yönetimi.

* Windows'ta süreç bir Job Object'e (KILL_ON_JOB_CLOSE) konur: uygulama çökse ya da
  Görev Yöneticisi'nden öldürülse bile yt-dlp/ffmpeg/cloudflared alt süreçleri ölür.
* Tüm alt süreçler kaydedilir; Çıkış'ta kill_all() hepsini ağacıyla birlikte öldürür.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from typing import Any

import psutil

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000

_lock = threading.Lock()
_procs: set[subprocess.Popen] = set()
_job_handle = None  # GC olmaması için global tutulur


def setup_job_object() -> bool:
    """Bu süreci ve gelecekteki tüm torunlarını KILL_ON_JOB_CLOSE job'ına koyar."""
    global _job_handle
    if not IS_WINDOWS or _job_handle is not None:
        return _job_handle is not None
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE

    job = k32.CreateJobObjectW(None, None)
    if not job:
        log.warning("CreateJobObject başarısız: %s", ctypes.get_last_error())
        return False
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        log.warning("SetInformationJobObject başarısız: %s", ctypes.get_last_error())
        return False
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        log.warning("AssignProcessToJobObject başarısız: %s", ctypes.get_last_error())
        return False
    _job_handle = job
    log.info("Job Object hazır (KILL_ON_JOB_CLOSE)")
    return True


def _linux_pdeathsig() -> None:  # pragma: no cover - sadece Linux geliştirme ortamı
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def popen(args: list[str], **kw: Any) -> subprocess.Popen:
    """Konsol penceresi açmadan, kayıtlı bir alt süreç başlatır."""
    if IS_WINDOWS:
        kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
    elif os.uname().sysname == "Linux":
        kw.setdefault("preexec_fn", _linux_pdeathsig)
    env = dict(kw.pop("env", None) or os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    kw["env"] = env
    kw.setdefault("stdin", subprocess.DEVNULL)
    p = subprocess.Popen(args, **kw)
    with _lock:
        _procs.add(p)
    return p


def run(args: list[str], timeout: float | None = None, **kw: Any) -> subprocess.CompletedProcess:
    """popen + bekle; kayıt otomatik silinir."""
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    p = popen(args, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        out, err = p.communicate()
        raise
    finally:
        forget(p)
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def forget(p: subprocess.Popen) -> None:
    with _lock:
        _procs.discard(p)


def kill_tree(p: subprocess.Popen | int) -> None:
    pid = p if isinstance(p, int) else p.pid
    try:
        parent = psutil.Process(pid)
        victims = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for v in victims:
        try:
            v.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=5)


def kill_all() -> None:
    with _lock:
        procs = list(_procs)
        _procs.clear()
    for p in procs:
        kill_tree(p)
    # Kayıt dışı kalmış torunları da temizle
    try:
        for child in psutil.Process().children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


def active_count() -> int:
    with _lock:
        return sum(1 for p in _procs if p.poll() is None)
