import hashlib
from pathlib import Path

from kickindirici import updater

NEW = b"MZ yeni surum" * 1000


def make(tmp_path, tag="v9.0.0", sha=None, assets=True, current="1.2.0"):
    exe = tmp_path / "KickIndirici.exe"
    exe.write_bytes(b"MZ eski")
    files = {"https://x/exe": NEW,
             "https://x/sha": f"{sha or hashlib.sha256(NEW).hexdigest()}  KickIndirici.exe\n".encode()}
    rel = {"tag_name": tag, "assets": [
        {"name": "KickIndirici.exe", "browser_download_url": "https://x/exe"},
        {"name": "KickIndirici.exe.sha256", "browser_download_url": "https://x/sha"}] if assets else []}

    def download(url, dest, on_progress=None):
        Path(dest).write_bytes(files[url])
        if on_progress:
            on_progress(len(files[url]), len(files[url]))

    restarted = []
    u = updater.Updater(exe, enabled=lambda: True, busy=lambda: False,
                        restart=lambda: restarted.append(True), current=current,
                        fetch_json=lambda url: rel, download=download)
    return u, exe, restarted


def test_versions():
    assert updater.is_newer("v1.10.0", "1.9.9")
    assert updater.is_newer("v2.0.0", "1.99.99")
    assert not updater.is_newer("v1.2.0", "1.2.0")
    assert not updater.is_newer("v1.1.9", "1.2.0")
    assert not updater.is_newer("", "1.0.0")


def test_download_verify_and_apply(tmp_path):
    u, exe, restarted = make(tmp_path)
    assert u.check_and_download()
    assert u.snapshot()["status"] == "ready" and u.snapshot()["latest"] == "9.0.0"
    assert exe.read_bytes() == b"MZ eski", "iş varken exe değişmemeli; sadece hazırlanır"
    assert u.apply()
    assert exe.read_bytes() == NEW
    assert (tmp_path / "KickIndirici.exe.old").read_bytes() == b"MZ eski"
    assert restarted == [True]
    updater.cleanup_old(exe)
    assert not (tmp_path / "KickIndirici.exe.old").exists()


def test_up_to_date(tmp_path):
    u, exe, _ = make(tmp_path, tag="v1.2.0")
    assert not u.check_and_download()
    assert u.snapshot()["status"] == "uptodate"
    assert not (tmp_path / "KickIndirici.exe.new").exists()


def test_bad_checksum_rejected(tmp_path):
    u, exe, restarted = make(tmp_path, sha="0" * 64)
    assert not u.check_and_download()
    st = u.snapshot()
    assert st["status"] == "error" and "SHA256" in st["error"]
    assert not list(tmp_path.glob("*.new")) and not list(tmp_path.glob("*.download"))
    assert not u.apply() and exe.read_bytes() == b"MZ eski" and not restarted


def test_missing_assets(tmp_path):
    u, *_ = make(tmp_path, assets=False)
    assert not u.check_and_download()
    assert u.snapshot()["status"] == "error"


def test_loop_waits_for_running_jobs(tmp_path, monkeypatch):
    import threading
    import time
    u, exe, restarted = make(tmp_path)
    busy = {"v": True}
    u.busy = lambda: busy["v"]
    monkeypatch.setattr(updater, "FIRST_CHECK_DELAY", 0)
    real_sleep = time.sleep
    monkeypatch.setattr(updater.time, "sleep", lambda s: real_sleep(0.01))
    t = threading.Thread(target=u.loop, daemon=True)
    t.start()
    for _ in range(200):
        if u.snapshot()["status"] == "ready":
            break
        real_sleep(0.01)
    real_sleep(0.1)
    assert u.snapshot()["status"] == "ready" and not restarted, "iş bitmeden yeniden başlamamalı"
    busy["v"] = False
    t.join(timeout=5)
    assert restarted == [True] and exe.read_bytes() == NEW


def test_disabled_does_nothing(tmp_path, monkeypatch):
    import threading
    u, exe, restarted = make(tmp_path)
    u.enabled = lambda: False
    monkeypatch.setattr(updater, "FIRST_CHECK_DELAY", 0)
    monkeypatch.setattr(updater, "CHECK_EVERY", 0.05)
    t = threading.Thread(target=u.loop, daemon=True)
    t.start()
    t.join(timeout=0.3)
    assert u.snapshot()["status"] == "idle" and not restarted
