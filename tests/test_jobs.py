import pytest
import threading
import time
from pathlib import Path

from kickindirici import jobs


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def make(tmp_path, processor):
    dl = tmp_path / "dl"
    dl.mkdir(exist_ok=True)
    m = jobs.JobManager(tmp_path / "jobs.json", lambda: dl, processor)
    return m, dl


def fake_processor(log, gate=None, fail_on=None):
    active = {"n": 0, "max": 0}

    def proc(ctx):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        try:
            log.append(ctx.job.url)
            d = ctx.manager.download_dir() / f"t [{ctx.job.id}]"
            d.mkdir(exist_ok=True)
            ctx.update(dir=str(d), title="Başlık", status=jobs.DOWNLOADING)
            for i in range(5):
                ctx.check()
                ctx.update(percent=i * 20)
                if gate is not None:
                    gate.wait(5)
                time.sleep(0.01)
            if fail_on and ctx.job.url == fail_on:
                raise RuntimeError("bozuk link")
            (d / "Başlık - Parça 01.mp4").write_bytes(b"x" * 10)
            ctx.update(files=jobs.list_files(d))
        finally:
            active["n"] -= 1

    proc.active = active
    return proc


def test_jobs_processed_sequentially_in_order(tmp_path):
    log = []
    p = fake_processor(log)
    m, _ = make(tmp_path, p)
    ids = [m.add(f"https://kick.com/v{i}", 120).id for i in range(3)]
    m.start()
    assert wait_for(lambda: all(m.get(i).status == jobs.DONE for i in ids))
    assert log == [f"https://kick.com/v{i}" for i in range(3)]
    assert p.active["max"] == 1, "işler aynı anda değil, tek tek işlenmeli"
    assert m.get(ids[0]).files[0]["name"] == "Başlık - Parça 01.mp4"
    m.stop()


def test_error_is_recorded(tmp_path):
    m, _ = make(tmp_path, fake_processor([], fail_on="bad"))
    j = m.add("bad", 0)
    m.start()
    assert wait_for(lambda: m.get(j.id).status == jobs.ERROR)
    assert m.get(j.id).error == "bozuk link"
    m.stop()


def test_cancel_running_and_queued(tmp_path):
    gate = threading.Event()
    m, _ = make(tmp_path, fake_processor([], gate=gate))
    a = m.add("a", 0)
    b = m.add("b", 0)
    m.start()
    assert wait_for(lambda: m.get(a.id).status == jobs.DOWNLOADING)
    assert m.cancel(b.id)
    assert m.get(b.id).status == jobs.CANCELED
    assert m.cancel(a.id)
    gate.set()
    assert wait_for(lambda: m.get(a.id).status == jobs.CANCELED)
    assert not m.cancel(a.id)
    m.stop()


def test_retry(tmp_path):
    m, _ = make(tmp_path, fake_processor([], fail_on="x"))
    j = m.add("x", 0)
    m.start()
    assert wait_for(lambda: m.get(j.id).status == jobs.ERROR)
    m.processor = fake_processor([])
    assert m.retry(j.id)
    assert wait_for(lambda: m.get(j.id).status == jobs.DONE)
    m.stop()


def test_delete_removes_files(tmp_path):
    m, dl = make(tmp_path, fake_processor([]))
    j = m.add("a", 0)
    m.start()
    assert wait_for(lambda: m.get(j.id).status == jobs.DONE)
    d = Path(m.get(j.id).dir)
    assert d.exists()
    assert m.delete(j.id)
    assert not d.exists() and dl.exists()
    assert m.get(j.id) is None
    m.stop()


def test_delete_running_job(tmp_path):
    gate = threading.Event()
    m, _ = make(tmp_path, fake_processor([], gate=gate))
    j = m.add("a", 0)
    m.start()
    assert wait_for(lambda: m.get(j.id).status == jobs.DOWNLOADING)
    d = Path(m.get(j.id).dir)
    threading.Timer(0.2, gate.set).start()
    assert m.delete(j.id)
    assert not d.exists()
    m.stop()


def test_persistence_and_interrupted_jobs(tmp_path):
    gate = threading.Event()
    m, dl = make(tmp_path, fake_processor([], gate=gate))
    done = m.add("done", 60)
    m.start()
    gate.set()
    assert wait_for(lambda: m.get(done.id).status == jobs.DONE)
    gate.clear()
    running = m.add("running", 60)
    queued = m.add("queued", 60)
    assert wait_for(lambda: m.get(running.id).status == jobs.DOWNLOADING)
    # "uygulama kapandı": yeni yönetici aynı dosyadan yükler
    m2 = jobs.JobManager(tmp_path / "jobs.json", lambda: dl, fake_processor([]))
    assert m2.get(done.id).status == jobs.DONE
    assert m2.get(done.id).files and m2.get(done.id).chunk == 60
    assert m2.get(running.id).status == jobs.ERROR
    assert "yarıda" in m2.get(running.id).error
    assert m2.get(queued.id).status == jobs.QUEUED
    gate.set()
    m.stop()


def test_file_path_only_known_files(tmp_path):
    m, _ = make(tmp_path, fake_processor([]))
    j = m.add("a", 0)
    m.start()
    assert wait_for(lambda: m.get(j.id).status == jobs.DONE)
    assert m.file_path(j.id, "Başlık - Parça 01.mp4") is not None
    assert m.file_path(j.id, "../jobs.json") is None
    assert m.file_path("yok", "x") is None
    m.stop()


def test_plan_parts_hls_aligned_and_named():
    from kickindirici import hls
    pl = hls.Playlist([hls.Segment(f"{i}.ts", i * 4.0, 4.0) for i in range(50)])  # 200 sn
    parts = jobs.plan_parts("Yayın", 30, 190, pl.total, 1, pl)
    assert [p["name"] for p in parts] == ["Yayın - Parça 01.mp4", "Yayın - Parça 02.mp4"]
    assert parts[0]["start"] == 28 and parts[0]["end"] == parts[1]["start"] == 92
    assert parts[1]["end"] == 192
    assert all(p["status"] == jobs.QUEUED for p in parts)


def test_plan_parts_single_names():
    assert jobs.plan_parts("Yayın", 0, None, 7200, 0, None)[0]["name"] == "Yayın.mp4"
    one = jobs.plan_parts("Yayın", 600, 1800, 7200, 120, None)
    assert len(one) == 1 and one[0]["name"] == "Yayın (0.10.00-0.30.00).mp4"
    unknown = jobs.plan_parts("Yayın", 0, None, None, 60, None)
    assert unknown[0]["end"] is None


def test_plan_parts_end_clamped_to_total():
    parts = jobs.plan_parts("x", 0, 99999, 7300, 60, None)
    assert parts[-1]["end"] == 7300 and len(parts) == 3


def test_add_stores_range(tmp_path):
    m, _ = make(tmp_path, fake_processor([]))
    j = m.add("u", 60, start=120, end=3600, title="T")
    assert (j.start, j.end, j.title) == (120, 3600, "T")
    m2 = jobs.JobManager(tmp_path / "jobs.json", lambda: tmp_path, fake_processor([]))
    assert (m2.get(j.id).start, m2.get(j.id).end) == (120, 3600)


def test_hls_source():
    assert jobs.hls_source({"url": "https://a/i.m3u8", "protocol": "m3u8_native",
                            "http_headers": {"User-Agent": "x"}}) == ("https://a/i.m3u8", {"User-Agent": "x"})
    assert jobs.hls_source({"url": "https://a/v.mp4", "protocol": "https"}) is None
    assert jobs.hls_source({"requested_formats": [{}, {}], "url": "x", "protocol": "m3u8"}) is None


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rate_steady():
    c = FakeClock()
    r = jobs._Rate(clock=c)
    total = 0
    for _ in range(40):  # 10 MB/sn, 4 sn video/sn
        c.t += 0.5
        total += 5_000_000
        r.add(total, total / 2_500_000)
    speed, vrate = r.rates()
    assert speed == pytest.approx(10_000_000, rel=0.05)
    assert vrate == pytest.approx(4, rel=0.05)


def test_rate_burst_after_stall_is_not_inflated():
    """Sıralı yazmada biriken segmentler bir anda yazılınca hız şişmemeli."""
    c = FakeClock()
    r = jobs._Rate(clock=c)
    total = 0
    for _ in range(20):  # 15 MB/sn (120 Mbps) sabit
        c.t += 0.5
        total += 7_500_000
        r.add(total, 0)
    for _ in range(6):  # 3 sn takılma: ağdan veri gelmiyor ama tick devam ediyor
        c.t += 0.5
        r.add(total, 0)
    c.t += 0.2  # sonra 45 MB'lık birikmiş veri bir anda
    total += 45_000_000
    r.add(total, 0)
    speed, _ = r.rates()
    assert speed <= 15_000_000 * 1.05, speed


def test_rate_needs_a_second_of_data():
    c = FakeClock()
    r = jobs._Rate(clock=c)
    r.add(0, 0)
    c.t += 0.3
    r.add(50_000_000, 1)
    assert r.rates()[0] is None
