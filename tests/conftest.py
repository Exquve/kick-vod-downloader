import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("KICK_APPDATA", str(tmp_path / "appdata"))
    yield


def _ffmpeg():
    from kickindirici.paths import ffmpeg_path
    p = ffmpeg_path()
    return p if (os.path.exists(p) or shutil.which(p)) else None


@pytest.fixture(scope="session")
def test_video(tmp_path_factory):
    """ffmpeg ile üretilmiş 200 sn'lik küçük test videosu (H.264 + AAC, 2 sn'de bir anahtar kare)."""
    ff = _ffmpeg()
    if not ff:
        pytest.skip("ffmpeg yok")
    out = tmp_path_factory.mktemp("vid") / "kaynak.mp4"
    subprocess.run([ff, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
                    "-t", "200", "-c:v", "libx264", "-preset", "ultrafast", "-g", "20",
                    "-c:a", "aac", "-b:a", "32k", "-shortest", str(out)], check=True)
    return out


@pytest.fixture(scope="session")
def hls_dir(test_video, tmp_path_factory):
    """Test videosundan 4 sn'lik segmentli VOD HLS (master + media playlist)."""
    ff = _ffmpeg()
    d = tmp_path_factory.mktemp("hls")
    (d / "v").mkdir()
    subprocess.run([ff, "-v", "error", "-y", "-i", str(test_video), "-c", "copy", "-f", "hls",
                    "-hls_time", "4", "-hls_playlist_type", "vod",
                    "-hls_segment_filename", str(d / "v" / "seg%03d.ts"), str(d / "v" / "index.m3u8")],
                   check=True)
    (d / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nv/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=50000\nlow/index.m3u8\n")
    return d


@pytest.fixture
def http_root(tmp_path):
    """Yerel HTTP sunucu; istenen yolları kaydeder."""
    import functools
    import http.server
    import threading

    root = tmp_path / "www"
    root.mkdir()

    class Hits(list):
        delay = 0.0  # testler .ts isteklerini yavaşlatmak için ayarlayabilir

    hits = Hits()

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            if hits.delay and self.path.endswith(".ts"):
                import time
                time.sleep(hits.delay)
            rng = self.headers.get("Range")
            path = Path(self.translate_path(self.path))
            if not rng or not path.is_file():
                return super().do_GET()
            size = path.stat().st_size
            a, _, b = rng.replace("bytes=", "").partition("-")
            start = int(a) if a else size - int(b)
            end = int(b) if (a and b) else size - 1
            if start >= size:
                self.send_response(416)
                self.end_headers()
                return
            data = path.read_bytes()[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def end_headers(self):
            if not self._headers_buffer or b"Accept-Ranges" not in b"".join(self._headers_buffer):
                self.send_header("Accept-Ranges", "bytes")
            super().end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()
