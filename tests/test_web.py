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


# ---- Wi-Fi -------------------------------------------------------------------

import time as _time

from signage.wifi_setup import DemoNM


@pytest.fixture
def wifi_client(tmp_path):
    config = Config(tmp_path)
    nm = DemoNM(delay=0)
    app = create_app(config, nm=nm)
    app.config["TESTING"] = True
    set_credentials(db.connect(config.db_path), "admin", "correct horse")
    c = app.test_client()
    assert login(c).status_code == 302
    c.token = csrf(c, "/")
    c.nm, c.app_ = nm, app
    return c


def post_json(c, path, body, token=True):
    headers = {"X-CSRF-Token": c.token} if token else {}
    return c.post(path, json=body, headers=headers)


def wait_job(c):
    for _ in range(100):
        job = c.get("/api/wifi").get_json()["job"]
        if job["state"] != "connecting":
            return job
        _time.sleep(0.02)
    raise AssertionError("job never finished")


def test_wifi_requires_login(app):
    assert app.test_client().get("/api/wifi").status_code == 401


def test_wifi_status_and_scan(wifi_client):
    data = wifi_client.get("/api/wifi").get_json()
    assert data["available"] is True
    assert [p["ssid"] for p in data["saved"]] == ["Home Wi-Fi"]
    nets = wifi_client.get("/api/wifi/scan").get_json()["networks"]
    assert nets[0]["ssid"] == "The Store Guest" and nets[0]["secure"] is True


def test_wifi_connect_needs_csrf(wifi_client):
    r = post_json(wifi_client, "/api/wifi/connect", {"ssid": "The Store Guest", "password": "12345678"}, token=False)
    assert r.status_code == 400


def test_wifi_connect_success_and_failure(wifi_client):
    r = post_json(wifi_client, "/api/wifi/connect", {"ssid": "The Store Guest", "password": "wrong"})
    assert r.status_code == 400  # too short for WPA, rejected before trying
    r = post_json(wifi_client, "/api/wifi/connect", {"ssid": "The Store Guest", "password": "wrongwrong"})
    assert r.status_code == 202
    assert wait_job(wifi_client)["state"] == "ok"
    data = wifi_client.get("/api/wifi").get_json()
    assert data["status"]["ssid"] == "The Store Guest"
    assert any(p["ssid"] == "The Store Guest" and p["active"] for p in data["saved"])


def test_wifi_connect_reports_wrong_password(wifi_client, monkeypatch):
    monkeypatch.setattr(wifi_client.nm, "connect",
                        lambda ssid, pw, hidden=False: (False, "The password was not accepted."))
    assert post_json(wifi_client, "/api/wifi/connect", {"ssid": "X", "password": "12345678"}).status_code == 202
    job = wait_job(wifi_client)
    assert job["state"] == "failed" and "password" in job["message"]


def test_wifi_connect_validates(wifi_client):
    assert post_json(wifi_client, "/api/wifi/connect", {"ssid": ""}).status_code == 400
    assert post_json(wifi_client, "/api/wifi/connect", {"ssid": "x" * 33}).status_code == 400


def test_wifi_forget(wifi_client):
    post_json(wifi_client, "/api/wifi/connect", {"ssid": "The Store Staff", "password": "12345678"})
    wait_job(wifi_client)
    saved = {p["ssid"]: p for p in wifi_client.get("/api/wifi").get_json()["saved"]}
    # can't forget the network in use
    assert post_json(wifi_client, "/api/wifi/forget", {"uuid": saved["The Store Staff"]["uuid"]}).status_code == 409
    assert post_json(wifi_client, "/api/wifi/forget", {"uuid": saved["Home Wi-Fi"]["uuid"]}).status_code == 200
    assert post_json(wifi_client, "/api/wifi/forget", {"uuid": "nope"}).status_code == 404
    left = [p["ssid"] for p in wifi_client.get("/api/wifi").get_json()["saved"]]
    assert left == ["The Store Staff"]


def test_settings_page_has_wifi_section(client):
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="wifi"' in html and "wifi.js" in html
