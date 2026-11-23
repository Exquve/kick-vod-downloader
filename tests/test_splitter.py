import pytest

from kickindirici import splitter


def test_should_split_needs_one_minute_extra():
    assert not splitter.should_split(7200 + 59, 7200)
    assert splitter.should_split(7200 + 60, 7200)
    assert not splitter.should_split(10_000, 0)


def test_plan_cuts_regular():
    assert splitter.plan_cuts(3 * 3600 + 600, 3600) == [3600, 7200, 10800]


def test_plan_cuts_merges_short_tail():
    # son parça 30 sn kalacaktı -> bir önceki parçaya eklenir
    assert splitter.plan_cuts(3 * 3600 + 30, 3600) == [3600, 7200]


def test_plan_cuts_none_when_short():
    assert splitter.plan_cuts(3600 + 30, 3600) == []


def test_safe_filename():
    assert splitter.safe_filename('a/b:c*?"<>|') == "a b c"
    assert splitter.safe_filename("CON") == "_CON"
    assert splitter.safe_filename("  ...  ") == "video"
    assert len(splitter.safe_filename("x" * 500)) == 120


def test_part_name():
    assert splitter.part_name("Yayın", 1, 5) == "Yayın - Parça 01.mp4"
    assert splitter.part_name("Yayın", 7, 120) == "Yayın - Parça 007.mp4"


def test_real_split(test_video, tmp_path):
    src = tmp_path / "Başlık %d test.mp4"
    src.write_bytes(test_video.read_bytes())
    progress = []
    files = splitter.split(src, tmp_path, "Başlık %d test", 60, on_progress=progress.append)
    names = [f.name for f in files]
    # 200 sn / 60 sn -> 60,120,180 ; son parça 20 sn < 60 -> 3 parça
    assert names == [f"Başlık %d test - Parça 0{i}.mp4" for i in (1, 2, 3)]
    assert not src.exists(), "orijinal dosya silinmeli"
    durs = [splitter.probe_duration(f) for f in files]
    assert sum(durs) == pytest.approx(200, abs=1.5)
    assert durs[0] == pytest.approx(60, abs=2.5)
    assert durs[-1] == pytest.approx(80, abs=2.5)
    assert progress and progress[-1] == 100.0


def test_no_split_when_not_long_enough(test_video, tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(test_video.read_bytes())
    files = splitter.split(src, tmp_path, "v", 180)
    assert files == [src] and src.exists()
