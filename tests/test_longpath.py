"""Uzun başlık / emoji / Windows 260 karakter yol sınırı."""
import os
import shutil
from pathlib import Path

import pytest

from kickindirici import hls, jobs, splitter
from kickindirici.paths import ffmpeg_path, long_path

TITLE = ("🥊 60 FPS 🥊 GTA 6 PLAYTHROUGH 🥊 LIVE 🥊 FINISHING THE GAME PERHAPS 🥊 LOCK IT IN 🥊 "
         "BEST GTA 6 AIMER 🥊 BEST GTA 6 DRIVER 🥊 BEST GTA 6 PLAYER 🥊")


def test_emoji_removed_from_names():
    n = splitter.safe_filename(TITLE, 200)
    assert "🥊" not in n and "  " not in n
    assert n.startswith("60 FPS GTA 6 PLAYTHROUGH LIVE")
    assert splitter.safe_filename("Yayın️‍ çğüşöı İ") == "Yayın çğüşöı İ"


def test_names_fit_windows_path_limit(tmp_path):
    # Kullanıcının durumuna benzer: uzun başlıklı iş klasörü
    d = tmp_path / ("x" * 40) / f"{splitter.safe_filename(TITLE, jobs.DIR_TITLE_MAX)} [a0a37084f7]"
    parts = jobs.plan_parts(TITLE, 0, None, 7 * 3600, 120, None, jobs.name_budget(d))
    assert len(parts) == 4
    for p in parts:
        full = os.path.abspath(str(d / (p["name"] + ".part.mp4")))
        assert splitter.utf16_len(full) < 260 or jobs.name_budget(d) == 20
        assert "🥊" not in p["name"] and p["name"].endswith(f"Parça 0{p['index']}.mp4")


def test_download_into_very_long_path(hls_dir, http_root, tmp_path):
    """Yol 260 karakteri aşsa bile parça yazılabilmeli (Windows'ta \\\\?\\ öneki)."""
    root, base, _ = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    pl = hls.load_playlist(f"{base}/v/index.m3u8")
    d = tmp_path / ("uzun klasör " * 12).strip() / ("🥊 " + "a" * 60 + " [id]")
    os.makedirs(long_path(d))
    out = d / ("b" * 100 + " - Parça 01.mp4")
    assert len(os.path.abspath(str(out))) > 260
    hls.download_part(pl, list(range(5)), str(out), ffmpeg_path())
    assert os.path.exists(long_path(out))
    assert jobs.list_files(Path(long_path(d)))[0]["size"] > 0


def _part(i, s, e, name, status="queued"):
    return {"index": i, "start": s, "end": e, "status": status, "percent": 0.0, "name": name, "size": None}


def test_salvage_completed_part_with_old_long_name(test_video, tmp_path):
    old_name = "Çok uzun eski ad 🥊 - Parça 01.mp4"
    shutil.copy(test_video, tmp_path / (old_name + ".part.mp4"))  # 200 sn, tamamı inmiş
    (tmp_path / "yarım - Parça 02.mp4.part.mp4").write_bytes(b"\0" * 100)  # bozuk artık
    old = [_part(1, 0.0, 200.0, old_name, "downloading"), _part(2, 200.0, 400.0, "yarım - Parça 02.mp4")]
    new = [_part(1, 0.0, 200.0, "Kısa - Parça 01.mp4"), _part(2, 200.0, 400.0, "Kısa - Parça 02.mp4")]
    jobs._salvage(tmp_path, old, new)
    assert new[0]["status"] == "done" and new[0]["size"] > 0
    assert (tmp_path / "Kısa - Parça 01.mp4").exists()
    assert new[1]["status"] == "queued"
    assert not list(tmp_path.glob("*.part.mp4")), "artık yarım dosyalar silinmeli"


def test_salvage_rejects_incomplete_file(test_video, tmp_path):
    shutil.copy(test_video, tmp_path / "a - Parça 01.mp4.part.mp4")  # 200 sn ama 600 sn bekleniyor
    new = [_part(1, 0.0, 600.0, "b - Parça 01.mp4")]
    jobs._salvage(tmp_path, [_part(1, 0.0, 600.0, "a - Parça 01.mp4")], new)
    assert new[0]["status"] == "queued" and not list(tmp_path.iterdir())


@pytest.mark.parametrize("dummy", [0])
def test_failed_job_does_not_leave_part_downloading(tmp_path, dummy):
    import time

    def proc(ctx):
        ctx.update(parts=[_part(1, 0, 10, "x.mp4", "downloading")])
        raise RuntimeError("bozuldu")
    m = jobs.JobManager(tmp_path / "j.json", lambda: tmp_path, proc)
    j = m.add("u", 0)
    m.start()
    for _ in range(200):
        if m.get(j.id).status == jobs.ERROR:
            break
        time.sleep(0.02)
    time.sleep(0.1)
    assert m.get(j.id).parts[0]["status"] == "queued"
    m.stop()
