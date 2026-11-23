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


def test_plan_bounds_with_offset():
    assert splitter.plan_bounds(600, 600 + 3 * 3600 + 30, 3600) == [600, 4200, 7800, 600 + 3 * 3600 + 30]
    assert splitter.plan_bounds(0, 3000, 0) == [0, 3000]
    assert splitter.plan_bounds(0, 7200 + 59, 7200) == [0, 7200 + 59]


def test_fmt_time():
    assert splitter.fmt_time(3723) == "1:02:03"


def test_probe_duration(test_video):
    assert splitter.probe_duration(test_video) == pytest.approx(200, abs=0.5)
