"""Gerçek yt-dlp + ffmpeg ile uçtan uca: yerel HTTP sunucudaki videoyu indir ve böl."""
import functools
import http.server
import os
import threading
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
def http_server(test_video, tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    (root / "yayin.mp4").write_bytes(test_video.read_bytes())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_download_and_split_end_to_end(ytdlp_bin, http_server, tmp_path, monkeypatch):
    monkeypatch.setenv("KICK_YTDLP", str(ytdlp_bin))
    ytdlp.mark_ready()
    dl = tmp_path / "dl"
    dl.mkdir()
    m = jobs.JobManager(tmp_path / "jobs.json", lambda: dl, jobs.default_processor(lambda: ""))
    seen = set()
    m.start()
    try:
        j = m.add(f"{http_server}/yayin.mp4", 1)  # 1 dk parçalar
        end = time.time() + 120
        while time.time() < end and m.get(j.id).status not in jobs.FINISHED:
            seen.add(m.get(j.id).status)
            time.sleep(0.05)
        job = m.get(j.id)
        assert job.status == jobs.DONE, job.error
        assert job.title == "yayin"
        names = [f["name"] for f in job.files]
        assert names == ["yayin - Parça 01.mp4", "yayin - Parça 02.mp4", "yayin - Parça 03.mp4"]
        d = Path(job.dir)
        assert sorted(p.name for p in d.iterdir()) == names, "orijinal silinmeli, artık dosya kalmamalı"
        total = sum(splitter.probe_duration(d / n) for n in names)
        assert total == pytest.approx(200, abs=1.5)
        assert jobs.DOWNLOADING in seen
    finally:
        m.stop()
