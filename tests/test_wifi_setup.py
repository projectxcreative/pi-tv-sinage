from signage.wifi_setup import friendly_error, parse_online, parse_wifi_list, split_terse


def test_split_terse_handles_escaped_colons():
    assert split_terse(r"*:Cafe\:Guest:70:WPA2") == ["*", "Cafe:Guest", "70", "WPA2"]
    assert split_terse(r" :back\\slash:5:--") == [" ", "back\\slash", "5", "--"]


def test_parse_wifi_list_dedupes_sorts_and_drops_hidden():
    out = "\n".join([
        " :Shop:40:WPA2",
        "*:Shop:80:WPA2",
        " ::90:WPA2",          # hidden network, no name
        " :Cafe Free:60:--",
        " :Mesh:55:WPA1 WPA2",
        " :Mesh:20:WPA1 WPA2",
    ])
    nets = parse_wifi_list(out)
    assert [n.ssid for n in nets] == ["Shop", "Cafe Free", "Mesh"]
    shop, cafe, mesh = nets
    assert shop.signal == 80 and shop.secure and shop.in_use
    assert not cafe.secure
    assert mesh.signal == 55


def test_parse_online():
    assert parse_online("wifi:connected\nloopback:connected (externally)\n")
    assert parse_online("ethernet:connected\nwifi:disconnected\n")
    assert not parse_online("wifi:disconnected\nethernet:unavailable\nloopback:connected (externally)\n")
    assert not parse_online("wifi:connecting (getting IP configuration)\n")


def test_friendly_error():
    assert "password" in friendly_error("Error: Connection activation failed: Secrets were required, but not provided.")
    assert "could not be found" in friendly_error("Error: No network with SSID 'x' found.")


def test_idle_countdown_raises_after_timeout(monkeypatch):
    import pytest
    from signage import wifi_setup as ws

    ui = ws.UI.__new__(ws.UI)
    ui.idle, ui.idle_until = 30, None
    assert ui.idle_left() is None          # no countdown unless started
    ui.touched()
    assert ui.idle_until is None           # keys don't start one either

    now = [1000.0]
    monkeypatch.setattr(ws.time, "monotonic", lambda: now[0])
    ui.idle_until = now[0] + 30
    assert ui.idle_left() == 30
    now[0] += 29.5
    assert ui.idle_left() == 1
    ui.touched()                           # a key press restarts it
    assert ui.idle_left() == 30
    now[0] += 30
    with pytest.raises(ws.IdleTimeout):
        ui.idle_left()
