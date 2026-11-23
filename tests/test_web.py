import time
from pathlib import Path

import pytest

from kickindirici import jobs
from kickindirici.config import Config
from kickindirici.web import AppState, create_app

DATA = bytes(range(256)) * 400  # 102400 bayt


@pytest.fixture
def env(tmp_path):
    cfg = Config(tmp_path / "config.json")
    cfg.update(download_dir=str(tmp_path / "dl"))

    def proc(ctx):
        d = ctx.manager.download_dir() / f"x [{ctx.job.id}]"
        d.mkdir(parents=True)
        (d / "Yayın - Parça 01.mp4").write_bytes(DATA)
        ctx.update(dir=str(d), title="Yayın", files=jobs.list_files(d))

    class FakeProber:
        def get(self, url):
            if "bozuk" in url:
                raise RuntimeError("Video bulunamadı")
            return {"title": "Yayın", "duration": 7300.0, "thumbnail": "https://img/x.jpg",
                    "uploader": "kanal", "url": "https://cdn/x.m3u8"}

    m = jobs.JobManager(tmp_path / "jobs.json", cfg.download_dir, proc)
    m.start()
    app = create_app(AppState(cfg, m, None, 8765, prober=FakeProber()))
    app.testing = True
    yield app, cfg, m
    m.stop()


def remote(client):
    client.environ_base["REMOTE_ADDR"] = "192.168.1.50"
    return client


def add_and_wait(client, url="https://kick.com/x/videos/abc", chunk=120):
    r = client.post("/api/jobs", json={"url": url, "chunk": chunk})
    assert r.status_code == 201, r.json
    jid = r.json["job"]["id"]
    for _ in range(200):
        s = client.get("/api/state").json
        j = next(j for j in s["jobs"] if j["id"] == jid)
        if j["status"] == "done":
            return j
        time.sleep(0.02)
    raise AssertionError("iş bitmedi")


def test_index_local(env):
    app, *_ = env
    r = app.test_client().get("/")
    assert r.status_code == 200
    assert "Kick" in r.get_data(as_text=True)


def test_remote_requires_token(env):
    app, cfg, _ = env
    c = remote(app.test_client())
    assert c.get("/").status_code == 401
    assert c.get("/api/state").status_code == 401
    r = c.get("/?k=" + cfg["token"])
    assert r.status_code == 200
    assert "kt=" in r.headers.get("Set-Cookie", "")
    # cookie ile devam
    assert c.get("/api/state").status_code == 200


def test_wrong_token_rejected(env):
    app, *_ = env
    c = remote(app.test_client())
    assert c.get("/api/state?k=yanlis").status_code == 401


def test_tunnel_requests_from_loopback_need_token(env):
    app, cfg, _ = env
    c = app.test_client()
    assert c.get("/api/state", headers={"Cf-Connecting-Ip": "1.2.3.4"}).status_code == 401
    assert c.get("/api/state?k=" + cfg["token"], headers={"Cf-Connecting-Ip": "1.2.3.4"}).status_code == 200


def test_quit_only_local(env):
    app, cfg, _ = env
    c = remote(app.test_client())
    assert c.post("/api/quit?k=" + cfg["token"]).status_code == 403


def test_add_job_validation(env):
    app, *_ = env
    c = app.test_client()
    assert c.post("/api/jobs", json={"url": "kick.com/x", "chunk": 120}).status_code == 400
    assert c.post("/api/jobs", json={"url": "https://kick.com/x", "chunk": 45}).status_code == 400


def test_file_links_and_infuse(env):
    app, cfg, _ = env
    c = app.test_client()
    j = add_and_wait(c)
    f = j["files"][0]
    assert f["name"] == "Yayın - Parça 01.mp4"
    assert f["size"] == len(DATA)
    assert f["download"].endswith("?dl=1")
    assert f["url"].startswith("http://localhost/f/") and ("k=" + cfg["token"]) in f["url"]
    assert f["infuse"].startswith("infuse://x-callback-url/play?url=http%3A%2F%2Flocalhost%2Ff%2F")


def test_attachment_download(env):
    app, *_ = env
    c = app.test_client()
    f = add_and_wait(c)["files"][0]
    r = c.get(f["download"])
    assert r.status_code == 200
    assert r.data == DATA
    cd = r.headers["Content-Disposition"]
    assert cd.startswith("attachment")
    assert "filename*=UTF-8''Yay%C4%B1n%20-%20Par%C3%A7a%2001.mp4" in cd
    assert r.headers["Accept-Ranges"] == "bytes"


def test_range_requests(env):
    app, cfg, _ = env
    c = app.test_client()
    f = add_and_wait(c)["files"][0]
    path = f["download"].split("?")[0]
    r = c.get(path, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 100-199/{len(DATA)}"
    assert r.data == DATA[100:200]
    r = c.get(path, headers={"Range": "bytes=-50"})
    assert r.status_code == 206 and r.data == DATA[-50:]
    r = c.get(path, headers={"Range": f"bytes={len(DATA)}-"})
    assert r.status_code == 416
    # Infuse: tam URL token ile, uzaktan
    rc = remote(app.test_client())
    r = rc.get(f["url"].replace("http://localhost", ""), headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.data == DATA[:10]


def test_unknown_file_404(env):
    app, *_ = env
    c = app.test_client()
    j = add_and_wait(c)
    assert c.get(f"/f/{j['id']}/..%2Fjobs.json").status_code == 404
    assert c.get(f"/f/{j['id']}/baska.mp4").status_code == 404


def test_delete_via_api(env):
    app, _, m = env
    c = app.test_client()
    j = add_and_wait(c)
    assert c.delete(f"/api/jobs/{j['id']}").json["ok"]
    assert m.get(j["id"]) is None


def test_settings_roundtrip(env, tmp_path):
    app, cfg, _ = env
    c = app.test_client()
    new_dir = tmp_path / "yeni klasör"
    r = c.post("/api/settings", json={"download_dir": str(new_dir), "cookies_browser": "firefox",
                                      "default_chunk": 60})
    assert r.status_code == 200, r.json
    assert new_dir.is_dir()
    assert cfg["cookies_browser"] == "firefox" and cfg["default_chunk"] == 60
    assert c.post("/api/settings", json={"cookies_browser": "opera"}).status_code == 400
    assert c.post("/api/settings", json={"download_dir": "goreli/yol"}).status_code == 400
    # kalıcı mı?
    assert Config(cfg.path)["cookies_browser"] == "firefox"


def test_defaults(tmp_path):
    cfg = Config(tmp_path / "c.json")
    assert cfg["cookies_browser"] == ""
    assert cfg["default_chunk"] == 120
    assert cfg["download_dir"].replace("\\", "/").endswith("Videos/KickVODs")


def test_qr_svg(env):
    app, *_ = env
    r = app.test_client().get("/qr.svg?d=http%3A%2F%2F192.168.1.2%3A8765%2F%3Fk%3Dabc")
    assert r.status_code == 200 and b"<svg" in r.data


def test_connect_page(env):
    app, *_ = env
    r = app.test_client().get("/baglan")
    assert r.status_code == 200
    assert ":8765" in r.get_data(as_text=True)


def test_probe(env, monkeypatch):
    from kickindirici import ytdlp
    app, *_ = env
    monkeypatch.setattr(ytdlp, "wait_ready", lambda t=None: True)
    monkeypatch.setattr(ytdlp, "ytdlp_path", lambda: Path(__file__))
    c = app.test_client()
    r = c.post("/api/probe", json={"url": "https://kick.com/k/videos/1"})
    assert r.status_code == 200
    assert r.json == {"title": "Yayın", "duration": 7300.0, "thumbnail": "https://img/x.jpg", "uploader": "kanal"}
    r = c.post("/api/probe", json={"url": "https://kick.com/bozuk"})
    assert r.status_code == 422 and "bulunamadı" in r.json["error"]
    assert c.post("/api/probe", json={"url": "selam"}).status_code == 400


def test_add_job_with_range(env):
    app, _, m = env
    c = app.test_client()
    r = c.post("/api/jobs", json={"url": "https://kick.com/x", "chunk": 60, "start": 600, "end": 4200, "title": "Yayın"})
    assert r.status_code == 201
    j = m.get(r.json["job"]["id"])
    assert (j.start, j.end, j.chunk) == (600, 4200, 60)
    assert c.post("/api/jobs", json={"url": "https://kick.com/x", "chunk": 60, "start": 600, "end": 600}).status_code == 400
    assert c.post("/api/jobs", json={"url": "https://kick.com/x", "chunk": 60, "start": "abc"}).status_code == 400


def test_done_parts_get_links(env):
    app, _, m = env
    c = app.test_client()
    j = add_and_wait(c)
    with m._lock:
        m._jobs[j["id"]].parts = [{"index": 1, "start": 0, "end": 10, "status": "done",
                                   "name": "Yayın - Parça 01.mp4", "size": 1},
                                  {"index": 2, "start": 10, "end": 20, "status": "queued",
                                   "name": "Yayın - Parça 02.mp4", "size": None}]
    s = c.get("/api/state").json
    parts = next(x for x in s["jobs"] if x["id"] == j["id"])["parts"]
    assert parts[0]["download"].endswith("?dl=1") and parts[0]["infuse"].startswith("infuse://")
    assert "download" not in parts[1]
