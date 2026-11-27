"""Gerçek yt-dlp + ffmpeg ile uçtan uca: yerel HTTP sunucudan aralıklı, parça parça indirme."""
import os
import shutil
import time
from pathlib import Path

import pytest

from kickindirici import jobs, splitter, ytdlp


@pytest.fixture(scope="session")
def ytdlp_bin(tmp_path_factory):
    if os.environ.get("KICK_YTDLP") and Path(os.environ["KICK_YTDLP"]).exists():
        return Path(os.environ["KICK_YTDLP"])
    dest = tmp_path_factory.mktemp("ytdlp") / ("yt-dlp.exe" if os.name == "nt" else "yt-dlp")
    try:
        ytdlp.download_ytdlp(dest)
    except Exception as e:
        pytest.skip(f"yt-dlp indirilemedi: {e}")
    return dest


@pytest.fixture
def manager(ytdlp_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("KICK_YTDLP", str(ytdlp_bin))
    ytdlp.mark_ready()
    dl = tmp_path / "dl"
    dl.mkdir()
    m = jobs.JobManager(tmp_path / "jobs.json", lambda: dl, jobs.default_processor(lambda: ""))
    m.start()
    yield m
    m.stop()


def run_job(m, job_id, timeout=120, on_tick=None):
    end = time.time() + timeout
    while time.time() < end and m.get(job_id).status not in jobs.FINISHED:
        if on_tick:
            on_tick(m.get(job_id))
        time.sleep(0.05)
    return m.get(job_id)


def test_hls_range_parts_end_to_end(manager, hls_dir, http_root):
    root, base, hits = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    ready_while_running = []

    def tick(j):
        if j.status == jobs.DOWNLOADING and any(p["status"] == "done" for p in j.parts):
            ready_while_running.append(True)

    # 0:30 - 3:10 arası, 1 dk'lık parçalar -> 30-90, 90-150, 150-190 ; son parça 40 sn < 1 dk
    # olduğu için öncekine eklenir -> 30-90, 90-190
    j = manager.add(f"{base}/master.m3u8", 1, start=30, end=190)
    job = run_job(manager, j.id, on_tick=tick)
    assert job.status == jobs.DONE, job.error
    names = [p["name"] for p in job.parts]
    assert names == ["master - Parça 01.mp4", "master - Parça 02.mp4"]
    assert ready_while_running, "ilk parça, iş bitmeden hazır olmalı"
    d = Path(job.dir)
    assert sorted(f.name for f in d.iterdir()) == names
    durs = [splitter.probe_duration(d / n) for n in names]
    assert sum(durs) == pytest.approx(160, abs=8)
    assert job.parts[0]["start"] <= 30 and job.parts[-1]["end"] >= 190
    seg_hits = [h for h in hits if h.endswith(".ts")]
    assert len(seg_hits) <= 42, f"tüm VOD değil yalnızca aralık inmeli ({len(seg_hits)} segment)"


def test_mp4_fallback_range(manager, test_video, http_root):
    root, base, _ = http_root
    (root / "yayin.mp4").write_bytes(test_video.read_bytes())
    j = manager.add(f"{base}/yayin.mp4", 1, start=0, end=130)
    job = run_job(manager, j.id)
    assert job.status == jobs.DONE, job.error
    assert [p["name"] for p in job.parts] == ["yayin - Parça 01.mp4", "yayin - Parça 02.mp4"]
    d = Path(job.dir)
    durs = [splitter.probe_duration(d / p["name"]) for p in job.parts]
    assert durs[0] == pytest.approx(60, abs=3) and durs[1] == pytest.approx(70, abs=3)
    assert not [f for f in d.iterdir() if f.name.startswith(".tmp")]


def test_whole_video_no_split(manager, hls_dir, http_root):
    root, base, _ = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    j = manager.add(f"{base}/v/index.m3u8", 0)
    job = run_job(manager, j.id)
    assert job.status == jobs.DONE, job.error
    assert [p["name"] for p in job.parts] == ["index.mp4"]
    assert splitter.probe_duration(Path(job.dir) / "index.mp4") == pytest.approx(200, abs=1)


def test_resume_skips_finished_parts(manager, hls_dir, http_root):
    root, base, hits = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    # 2. parçanın segmentlerinden birini bozuk yap -> ilk parça biter, sonra hata
    seg = root / "v" / "seg020.ts"
    good = seg.read_bytes()
    seg.unlink()
    j = manager.add(f"{base}/v/index.m3u8", 1, start=0, end=130)
    job = run_job(manager, j.id)
    assert job.status == jobs.ERROR
    assert [p["status"] for p in job.parts] == ["done", "downloading", "queued"] or \
        job.parts[0]["status"] == "done"
    seg.write_bytes(good)
    hits.clear()
    assert manager.retry(j.id)
    job = run_job(manager, j.id)
    assert job.status == jobs.DONE, job.error
    assert all(p["status"] == "done" for p in job.parts)
    first = {f"/v/seg{i:03d}.ts" for i in range(0, 15)}
    assert not first & set(hits), "biten 1. parça tekrar indirilmemeli"


def test_pause_mid_part_and_resume(manager, hls_dir, http_root):
    root, base, hits = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    hits.delay = 0.03
    j = manager.add(f"{base}/v/index.m3u8", 0)  # tek parça, 50 segment
    end = time.time() + 60
    while time.time() < end:
        job = manager.get(j.id)
        if job.parts and job.parts[0].get("percent", 0) >= 30:
            break
        time.sleep(0.02)
    assert manager.pause(j.id)
    job = run_job(manager, j.id)
    assert job.status == jobs.PAUSED
    pct = job.parts[0]["percent"]
    assert 30 <= pct < 100 and job.parts[0]["status"] == "queued"
    before = {h for h in hits if h.endswith(".ts")}
    hits.clear()
    hits.delay = 0
    assert manager.retry(j.id)  # "Devam et"
    job = run_job(manager, j.id)
    assert job.status == jobs.DONE, job.error
    again = {h for h in hits if h.endswith(".ts")}
    k = round(pct * 50 / 100)  # durdurulduğunda yazılmış segment sayısı
    assert len(again) == 50 - k, "yalnızca kalan segmentler inmeli; öncekiler tekrar inmemeli"
    assert {f"/v/seg{i:03d}.ts" for i in range(k)}.isdisjoint(again)
    assert splitter.probe_duration(Path(job.dir) / job.parts[0]["name"]) == pytest.approx(200, abs=1)
    assert sorted(p.name for p in Path(job.dir).iterdir()) == [job.parts[0]["name"]]
    assert before


def test_deleted_part_is_not_downloaded_again(manager, hls_dir, http_root):
    root, base, hits = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    seg = root / "v" / "seg020.ts"  # 2. parçayı bozarak işi yarıda bırak
    good = seg.read_bytes()
    seg.unlink()
    j = manager.add(f"{base}/v/index.m3u8", 1, start=0, end=130)
    job = run_job(manager, j.id)
    assert job.status == jobs.ERROR and job.parts[0]["status"] == "done"
    assert manager.delete_part(j.id, 1) == ""
    assert not (Path(job.dir) / job.parts[0]["name"]).exists()
    seg.write_bytes(good)
    hits.clear()
    assert manager.retry(j.id)
    job = run_job(manager, j.id)
    assert job.status == jobs.DONE, job.error
    assert [p["status"] for p in job.parts] == ["deleted", "done"]
    assert not {f"/v/seg{i:03d}.ts" for i in range(15)} & set(hits), "silinen parça tekrar inmemeli"
    assert [f["name"] for f in job.files] == [job.parts[1]["name"]]
    # program yeniden açılınca da silindi olarak kalmalı
    m2 = jobs.JobManager(manager.store, manager.download_dir, lambda ctx: None)
    assert m2.get(j.id).parts[0]["status"] == "deleted"
