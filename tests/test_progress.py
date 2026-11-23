from kickindirici import ytdlp


def test_parse_bytes_progress():
    p = ytdlp.parse_progress("[KP]downloading|500|1000|NA|2048.5|12|NA|NA")
    assert p["percent"] == 50.0
    assert p["speed"] == 2048.5
    assert p["eta"] == 12
    assert p["total"] == 1000


def test_parse_estimate_used_when_total_missing():
    p = ytdlp.parse_progress("[KP]downloading|250|NA|1000|NA|NA|NA|NA")
    assert p["percent"] == 25.0
    assert p["speed"] is None and p["eta"] is None


def test_parse_fragment_progress_without_bytes_total():
    p = ytdlp.parse_progress("[KP]downloading|123456|NA|NA|1000|30|40|200")
    assert p["percent"] == 20.0


def test_parse_finished_is_100():
    assert ytdlp.parse_progress("[KP]finished|10|10|NA|NA|NA|NA|NA")["percent"] == 100.0


def test_ignores_other_lines():
    assert ytdlp.parse_progress("[download] Destination: x.mp4") is None
    assert ytdlp.parse_progress("[KP]garbage") is None


def test_parse_with_prefix_noise():
    p = ytdlp.parse_progress("  \r[KP]downloading|1|4|NA|NA|NA|NA|NA\n")
    assert p["percent"] == 25.0


def test_merge_lines():
    assert ytdlp.is_merge_line('[FixupM3u8] Fixing MPEG-TS in MP4 container of "a.mp4"')
    assert ytdlp.is_merge_line('[Merger] Merging formats into "a.mp4"')
    assert not ytdlp.is_merge_line("[download] 10%")


def test_download_args_contains_required_flags():
    a = ytdlp.download_args("https://kick.com/video/x", "out.%(ext)s", "firefox", ffmpeg="/x/bin/ffmpeg")
    j = " ".join(a)
    assert "--newline" in a and "--progress-template" in a
    assert a[a.index("-N") + 1] == "8"
    assert a[a.index("--ffmpeg-location") + 1] == "/x/bin"
    assert a[a.index("--cookies-from-browser") + 1] == "firefox"
    assert a[-1] == "https://kick.com/video/x" and a[-2] == "--"
    assert "[KP]" in j


def test_no_cookies_by_default():
    assert "--cookies-from-browser" not in ytdlp.download_args("u", "o", "")


def test_summarize_error():
    msg = ytdlp.summarize_error("WARNING: x\nERROR: [kick] abc: HTTP Error 403: Forbidden\n")
    assert msg.startswith("[kick] abc: HTTP Error 403")
    assert "çerez" in msg
