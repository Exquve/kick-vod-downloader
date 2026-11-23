import shutil
import threading

import pytest

from kickindirici import hls, splitter
from kickindirici.paths import ffmpeg_path

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:10.0,
0.ts
#EXTINF:10.0,
1.ts
#EXTINF:10.0,
https://cdn.example.com/x/2.ts
#EXTINF:5.5,
3.ts
#EXT-X-ENDLIST
"""


def test_parse_media():
    pl = hls.parse_media(MEDIA, "https://a.com/vod/1080p/index.m3u8")
    assert [s.uri for s in pl.segments] == [
        "https://a.com/vod/1080p/0.ts", "https://a.com/vod/1080p/1.ts",
        "https://cdn.example.com/x/2.ts", "https://a.com/vod/1080p/3.ts"]
    assert [s.start for s in pl.segments] == [0, 10, 20, 30]
    assert pl.total == 35.5


def test_master_picks_highest_bandwidth():
    text = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360p/i.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS=\"avc1,mp4a\"\n1080p60/i.m3u8\n")
    assert hls.is_master(text)
    assert hls.best_variant(text, "https://a.com/m/master.m3u8") == "https://a.com/m/1080p60/i.m3u8"


def test_encrypted_unsupported():
    with pytest.raises(hls.Unsupported):
        hls.parse_media("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n#EXTINF:4,\na.ts\n", "https://a/")


def test_assign_partitions_without_gaps_or_overlap():
    pl = hls.Playlist([hls.Segment(f"{i}.ts", i * 4.0, 4.0) for i in range(50)])  # 200 sn
    groups = hls.assign(pl, [10, 70, 130, 190])
    assert groups[0][0] == 2  # 8-12 sn segmenti aralık başlangıcını (10) içerir
    flat = [i for g in groups for i in g]
    assert flat == list(range(flat[0], flat[-1] + 1)), "boşluk/çakışma olmamalı"
    assert flat[-1] == 47  # 188-192 sn, 190'ı içerir
    assert groups[1][0] == 18 and groups[2][0] == 33  # 72 ve 132 sn


def test_load_playlist_and_download_only_needed_segments(hls_dir, http_root, tmp_path):
    root, base, hits = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    pl = hls.load_playlist(f"{base}/master.m3u8")
    assert len(pl.segments) == 50 and pl.total == pytest.approx(200, abs=0.5)
    idxs = hls.assign(pl, [60, 120])[0]
    hits.clear()
    progress = []
    out = tmp_path / "parca.mp4"
    hls.download_part(pl, idxs, str(out), ffmpeg_path(), on_progress=lambda k, n, b: progress.append((k, n)))
    assert out.exists() and not (tmp_path / "parca.mp4.part.mp4").exists()
    assert splitter.probe_duration(out) == pytest.approx(60, abs=4.5)
    assert len(hits) == len(idxs), "yalnızca aralıktaki segmentler indirilmeli"
    assert progress[-1] == (len(idxs), len(idxs))


def test_download_cancel_cleans_up(hls_dir, http_root, tmp_path):
    root, base, _ = http_root
    shutil.copytree(hls_dir, root, dirs_exist_ok=True)
    pl = hls.load_playlist(f"{base}/v/index.m3u8")
    cancel = threading.Event()
    out = tmp_path / "x.mp4"

    def prog(k, n, b):
        if k == 3:
            cancel.set()
    with pytest.raises(InterruptedError):
        hls.download_part(pl, list(range(len(pl.segments))), str(out), ffmpeg_path(),
                          cancel=cancel, on_progress=prog, workers=1)
    assert not out.exists() and not (tmp_path / "x.mp4.part.mp4").exists()


def test_http_error_propagates(http_root, tmp_path):
    root, base, _ = http_root
    pl = hls.Playlist([hls.Segment(f"{base}/yok.ts", 0, 4)])
    with pytest.raises(Exception) as e:
        hls.download_part(pl, [0], str(tmp_path / "y.mp4"), ffmpeg_path())
    assert "404" in str(e.value)
