"""Player logic against a fake mpv that records commands."""
import queue

from signage import db
from signage.config import Config
from signage.player import Player, pick_next


class FakeMpv:
    def __init__(self, *_):
        self.events = queue.Queue()
        self.calls = []
        self.props = {"idle-active": True, "path": ""}

    def command(self, *args, timeout=10, **named):
        self.calls.append(named or args)
        return None

    def get(self, prop, default=None):
        return self.props.get(prop, default)

    def set(self, prop, value):
        self.props[prop] = value

    def close(self):
        pass

    def loads(self):
        return [c for c in self.calls if isinstance(c, dict) and c.get("name") == "loadfile"]


def make(tmp_path, names):
    config = Config(tmp_path)
    config.ensure_dirs()
    conn = db.connect(config.db_path)
    for n in names:
        kind = "image" if n.endswith(".jpg") else "video"
        db.add_media(conn, filename=n, original_name=n, kind=kind)
    p = Player(config, mpv_client_factory=FakeMpv, spawn=False)
    p.start()
    return p, conn


def loaded(p, filename):
    p.mpv.props["path"] = str(p.config.media_dir / filename)
    p.mpv.props["idle-active"] = False
    p.handle_event({"event": "file-loaded"})


def test_pick_next_wraps():
    items = [{"position": 0}, {"position": 2}, {"position": 5}]
    assert pick_next(items, 2)["position"] == 5
    assert pick_next(items, 5)["position"] == 0
    assert pick_next(items, None)["position"] == 0
    assert pick_next([], 1) is None


def test_starts_and_queues_next(tmp_path):
    p, conn = make(tmp_path, ["a.mp4", "b.jpg", "c.mp4"])
    p.handle_event({"event": "property-change", "name": "idle-active", "data": True})
    first = p.mpv.loads()[-1]
    assert first["flags"] == "replace" and first["url"].endswith("a.mp4")

    loaded(p, "a.mp4")
    nxt = p.mpv.loads()[-1]
    assert nxt["flags"] == "append" and nxt["url"].endswith("b.jpg")
    assert nxt["options"] == "image-display-duration=10"
    assert db.get_json(conn, "now_playing")["id"] == 1

    loaded(p, "c.mp4")
    assert p.mpv.loads()[-1]["url"].endswith("a.mp4")  # loops round


def test_reorder_requeues(tmp_path):
    p, conn = make(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
    loaded(p, "a.mp4")
    db.reorder(conn, [1, 3, 2])
    p.poll()
    assert p.mpv.loads()[-1]["url"].endswith("c.mp4")


def test_deleting_current_skips(tmp_path):
    p, conn = make(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
    loaded(p, "b.mp4")
    db.delete_media(conn, 2)
    p.poll()
    last = p.mpv.loads()[-1]
    assert last["flags"] == "replace" and last["url"].endswith("c.mp4")


def test_disabled_items_skipped(tmp_path):
    p, conn = make(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
    db.update_media(conn, 2, enabled=0)
    loaded(p, "a.mp4")
    assert p.mpv.loads()[-1]["url"].endswith("c.mp4")


def test_commands(tmp_path):
    p, conn = make(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
    loaded(p, "a.mp4")
    db.set_json(conn, "player_command", {"cmd": "play", "id": 3, "nonce": "x1"})
    p.poll()
    assert p.mpv.loads()[-1] == {"name": "loadfile", "url": str(p.config.media_dir / "c.mp4"),
                                 "flags": "replace", "options": ""}
    p.poll()  # same nonce: not repeated
    assert len([c for c in p.mpv.loads() if c["flags"] == "replace"]) == 1


def test_settings_applied(tmp_path):
    p, conn = make(tmp_path, ["a.mp4"])
    db.set_setting(conn, "mute", "1")
    db.set_setting(conn, "fit_mode", "fill")
    db.bump_version(conn)
    p.poll()
    assert p.mpv.props["mute"] is True and p.mpv.props["panscan"] == 1.0
