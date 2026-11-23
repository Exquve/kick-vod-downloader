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
