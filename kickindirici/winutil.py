"""Windows'a özgü yardımcılar: güvenlik duvarı, mesaj kutusu, ağ profili, tek örnek."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

from . import procs
from .paths import IS_WINDOWS, appdata_dir

log = logging.getLogger(__name__)

RULE_NAME = "Kick VOD Indirici"


# ---------------- tek örnek ----------------
class SingleInstance:
    def __init__(self, name: str = "KickIndirici_SingleInstance"):
        self.name = name
        self._handle = None
        self._fh = None

    def acquire(self) -> bool:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            self._handle = k32.CreateMutexW(None, False, "Local\\" + self.name)
            return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        import fcntl
        self._fh = open(appdata_dir() / "instance.lock", "w")  # noqa: SIM115
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self) -> None:
        if IS_WINDOWS and self._handle:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def instance_file():
    return appdata_dir() / "instance.json"


def write_instance(port: int) -> None:
    instance_file().write_text(json.dumps({"port": port, "pid": os.getpid()}), "utf-8")


def read_instance_port(wait: float = 10) -> int | None:
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            return int(json.loads(instance_file().read_text("utf-8"))["port"])
        except (OSError, ValueError, KeyError):
            time.sleep(0.3)
    return None


# ---------------- mesaj kutusu ----------------
MB_YESNO, MB_ICONQUESTION, MB_ICONINFO, IDYES = 0x4, 0x20, 0x40, 6


def ask_yes_no(title: str, text: str) -> bool:
    if not IS_WINDOWS:
        return False
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, text, title, MB_YESNO | MB_ICONQUESTION | 0x10000) == IDYES


# ---------------- güvenlik duvarı ----------------
def firewall_rule_exists() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        r = procs.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE_NAME}"], timeout=20)
        return r.returncode == 0
    except Exception:
        log.exception("netsh çalışmadı")
        return False


def add_firewall_rule_elevated(program: str) -> bool:
    """UAC ile netsh çalıştırır; kullanıcı reddederse False."""
    if not IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    params = (f'advfirewall firewall add rule name="{RULE_NAME}" dir=in action=allow '
              f'program="{program}" enable=yes profile=private protocol=TCP')
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb, sei.lpFile, sei.lpParameters, sei.nShow = "runas", "netsh", params, 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        log.info("UAC reddedildi (%s)", ctypes.get_last_error())
        return False
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject(sei.hProcess, 30000)
    k32.CloseHandle(sei.hProcess)
    return firewall_rule_exists()


def ensure_firewall(cfg) -> None:
    """Kural yoksa bir kez sorar. Sonuç config'e yazılır."""
    if not IS_WINDOWS or os.environ.get("KICK_TEST"):
        return
    if firewall_rule_exists():
        cfg.update(firewall_declined=False)
        return
    if cfg["firewall_prompted"]:
        return
    cfg.update(firewall_prompted=True)
    ok = ask_yes_no(
        "Kick VOD İndirici",
        "Telefonun bu bilgisayara bağlanabilmesi için Windows Güvenlik Duvarı'nda\n"
        "yalnızca ÖZEL ağlar için gelen bağlantı izni eklensin mi?\n\n"
        "Bir sonraki adımda Windows yönetici izni (UAC) isteyecek.")
    added = ok and add_firewall_rule_elevated(sys.executable)
    cfg.update(firewall_declined=not added)


_profile_cache: tuple[float, list] = (0.0, [])


def network_profiles() -> list[dict]:
    """[{InterfaceAlias, NetworkCategory}] — 0=Public, 1=Private, 2=Domain"""
    global _profile_cache
    if not IS_WINDOWS:
        return []
    if time.time() - _profile_cache[0] < 30:
        return _profile_cache[1]
    try:
        r = procs.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                       "Get-NetConnectionProfile | Select-Object InterfaceAlias,"
                       "@{n='Category';e={[int]$_.NetworkCategory}} | ConvertTo-Json -Compress"],
                      timeout=15)
        data = json.loads((r.stdout or b"[]").decode("utf-8", "replace") or "[]")
        if isinstance(data, dict):
            data = [data]
    except (subprocess.SubprocessError, ValueError, OSError):
        data = []
    _profile_cache = (time.time(), data)
    return data


def is_public_network(alias: str) -> bool:
    for p in network_profiles():
        if p.get("InterfaceAlias") == alias:
            return p.get("Category") == 0
    return False


def open_folder(path: str) -> None:
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
