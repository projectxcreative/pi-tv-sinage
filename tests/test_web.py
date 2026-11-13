import io
import re

import pytest
from PIL import Image

from signage import db
from signage.config import Config
from signage.web import create_app, set_credentials


@pytest.fixture
def app(tmp_path):
    config = Config(tmp_path)
    app = create_app(config)
    app.config["TESTING"] = True
    conn = db.connect(config.db_path)
    set_credentials(conn, "admin", "correct horse")
    return app


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def login(client, pw="correct horse"):
    token = csrf(client)
    return client.post("/login", data={"username": "admin", "password": pw, "csrf": token})


@pytest.fixture
def client(app):
    c = app.test_client()
    assert login(c).status_code == 302
    c.token = csrf(c, "/")
    return c


def jpeg_bytes(size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "JPEG")
    buf.seek(0)
    return buf


def upload(client, name="a.jpg", data=None):
    return client.post("/api/upload", headers={"X-CSRF-Token": client.token},
                       data={"file": (data or jpeg_bytes(), name)},
                       content_type="multipart/form-data")


def test_requires_login(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/api/media").status_code == 401


def test_bad_password_and_lockout(app):
    c = app.test_client()
    for _ in range(5):
        assert "Wrong username" in login(c, "nope").get_data(as_text=True)
    assert "Too many failed" in login(c).get_data(as_text=True)


def test_post_without_csrf_rejected(client):
    r = client.post("/api/reorder", json={"ids": []})
    assert r.status_code == 400


def test_upload_list_reorder_delete(client, app):
    for n in ("one.jpg", "two.jpg", "three.png"):
        assert upload(client, n).status_code == 200
    items = client.get("/api/media").get_json()["items"]
    assert [i["name"] for i in items] == ["one.jpg", "two.jpg", "three.png"]
    ids = [i["id"] for i in items]

    r = client.post("/api/reorder", json={"ids": [ids[2], ids[0], ids[1]]},
                    headers={"X-CSRF-Token": client.token})
    assert r.status_code == 200
    names = [i["name"] for i in client.get("/api/media").get_json()["items"]]
    assert names == ["three.png", "one.jpg", "two.jpg"]

    assert client.get(f"/thumb/{ids[0]}").status_code == 200
    r = client.delete(f"/api/media/{ids[0]}", headers={"X-CSRF-Token": client.token})
    assert r.status_code == 200
    media_dir = app.signage_config.media_dir
    assert len(list(media_dir.iterdir())) == 2


def test_rejects_unknown_type(client):
    r = upload(client, "evil.sh", io.BytesIO(b"#!/bin/sh"))
    assert r.status_code == 400
    assert "unsupported" in r.get_json()["errors"][0]


def test_big_photo_is_shrunk_to_4k(client):
    upload(client, "huge.jpg", jpeg_bytes((6000, 4000)))
    item = client.get("/api/media").get_json()["items"][0]
    assert (item["width"], item["height"]) == (3240, 2160)


def test_patch_duration_and_enabled(client):
    upload(client)
    mid = client.get("/api/media").get_json()["items"][0]["id"]
    h = {"X-CSRF-Token": client.token}
    assert client.patch(f"/api/media/{mid}", json={"duration": 0.2}, headers=h).status_code == 400
    assert client.patch(f"/api/media/{mid}", json={"duration": 25, "enabled": False}, headers=h).status_code == 200
    item = client.get("/api/media").get_json()["items"][0]
    assert item["duration"] == 25 and item["enabled"] is False


def test_command_and_status(client, app):
    upload(client)
    mid = client.get("/api/media").get_json()["items"][0]["id"]
    r = client.post("/api/command", json={"cmd": "play", "id": mid}, headers={"X-CSRF-Token": client.token})
    assert r.status_code == 200
    conn = db.connect(app.signage_config.db_path)
    assert db.get_json(conn, "player_command")["id"] == mid
    s = client.get("/api/status").get_json()
    assert s["player_online"] is False and s["now_playing"] is None


def test_settings_change_password(client):
    token = client.token
    r = client.post("/settings", data={"csrf": token, "action": "password", "username": "boss",
                                       "current_password": "correct horse",
                                       "new_password": "new password 1", "confirm_password": "new password 1"})
    assert "Login details updated" in r.get_data(as_text=True)
    r = client.post("/settings", data={"csrf": token, "action": "playback", "image_duration": "7",
                                       "fit_mode": "fill"})
    assert "Playback settings saved" in r.get_data(as_text=True)
