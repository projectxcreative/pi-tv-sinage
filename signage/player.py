"""The TV player: drives mpv full screen through the playlist, forever.

mpv does the decoding (with the Pi's hardware decoder) and drawing straight to
the HDMI output via DRM/KMS, so no desktop is needed. This process keeps mpv's
own playlist one item ahead: whenever a file starts, it queues the next one,
so mpv moves from item to item without going idle. Changes made in the web
admin are picked up within a second or two.
"""
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import db
from .config import Config
from .mpv_ipc import MpvClient, MpvError

log = logging.getLogger("signage.player")

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 5.0


def pick_next(items, after_position):
    """First enabled item positioned after ``after_position``, wrapping round."""
    if not items:
        return None
    if after_position is not None:
        for item in items:
            if item["position"] > after_position:
                return item
    return items[0]


def mpv_command_line(config, image_duration):
    return [
        config.mpv_binary,
        "--idle=yes",
        "--force-window=yes",
        "--fullscreen",
        "--no-terminal",
        "--no-input-default-bindings",
        "--input-vo-keyboard=no",
        "--osc=no",
        "--osd-level=0",
        "--cursor-autohide=always",
        "--keep-open=no",
        "--prefetch-playlist=yes",
        "--loop-file=no",
        "--image-display-duration=%s" % image_duration,
        "--hwdec=%s" % config.hwdec,
        "--audio-device=%s" % config.audio_device,
        "--input-ipc-server=%s" % config.mpv_socket,
        *config.vo_args,
        *config.extra_mpv_args,
    ]


class Player:
    def __init__(self, config, mpv_client_factory=MpvClient, spawn=True):
        self.config = config
        self.conn = db.connect(config.db_path)
        self.proc = None
        self.spawn = spawn
        self.client_factory = mpv_client_factory
        self.mpv = None

        self.current = None            # media row now on screen
        self.last_position = None      # position of the last item we tried
        self.queued_filename = None
        self.version = None
        self.command_nonce = None
        self.last_heartbeat = 0.0
        self.last_idle_start = 0.0

        cmd = db.get_json(self.conn, "player_command")
        self.command_nonce = cmd.get("nonce") if cmd else None

    # ---- lifecycle -------------------------------------------------------------

    def start(self):
        self.config.ensure_dirs()
        if self.spawn:
            sock = Path(self.config.mpv_socket)
            sock.unlink(missing_ok=True)
            args = mpv_command_line(self.config, self.image_duration())
            log.info("Starting mpv: %s", " ".join(args))
            self.proc = subprocess.Popen(args)
        self.mpv = self.client_factory(self.config.mpv_socket)
        self.mpv.command("observe_property", 1, "idle-active")
        self.apply_settings()
        self.version = db.get_setting(self.conn, "playlist_version")

    def stop(self):
        if self.mpv:
            try:
                self.mpv.command("quit", timeout=2)
            except MpvError:
                pass
            self.mpv.close()
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.set_now_playing(None)

    def run(self):
        self.start()
        next_poll = 0.0
        try:
            while True:
                try:
                    event = self.mpv.events.get(timeout=POLL_SECONDS)
                except Exception:
                    event = None
                if event:
                    if event.get("event") == "_disconnected":
                        log.error("Lost connection to mpv; exiting so systemd restarts us")
                        return 1
                    self.handle_event(event)
                if time.monotonic() >= next_poll:
                    self.poll()
                    next_poll = time.monotonic() + POLL_SECONDS
        finally:
            self.stop()

    # ---- helpers -----------------------------------------------------------------

    def image_duration(self):
        try:
            return float(db.get_setting(self.conn, "image_duration", "10"))
        except ValueError:
            return 10.0

    def playlist(self):
        return db.list_media(self.conn, enabled_only=True)

    def path_for(self, item):
        return str(Path(self.config.media_dir) / item["filename"])

    def file_options(self, item):
        if item["kind"] == "image":
            secs = item["duration"] or self.image_duration()
            return f"image-display-duration={secs:g}"
        return ""

    def load(self, item, mode):
        """mode: 'replace' to show now, 'append' to queue after the current file."""
        log.info("%s %s (%s)", "Playing" if mode == "replace" else "Queued",
                 item["original_name"], item["filename"])
        self.mpv.command(name="loadfile", url=self.path_for(item), flags=mode,
                         options=self.file_options(item))
        if mode == "replace":
            self.last_position = item["position"]
            self.queued_filename = None
        else:
            self.queued_filename = item["filename"]

    def set_now_playing(self, item):
        db.set_json(self.conn, "now_playing",
                    {"id": item["id"], "since": time.time()} if item else None)

    def apply_settings(self):
        mute = db.get_setting(self.conn, "mute", "0") == "1"
        fill = db.get_setting(self.conn, "fit_mode", "fit") == "fill"
        try:
            self.mpv.set("mute", mute)
            self.mpv.set("panscan", 1.0 if fill else 0.0)
            self.mpv.set("image-display-duration", self.image_duration())
        except MpvError as exc:
            log.warning("Could not apply settings: %s", exc)

    # ---- playback logic ----------------------------------------------------------

    def queue_next(self):
        """Make sure exactly one correct item is queued after the current one."""
        try:
            self.mpv.command("playlist-clear")  # removes everything but the current file
        except MpvError as exc:
            log.warning("playlist-clear failed: %s", exc)
        self.queued_filename = None
        after = self.current["position"] if self.current else self.last_position
        nxt = pick_next(self.playlist(), after)
        if nxt:
            self.load(nxt, "append")

    def play_from(self, after_position):
        nxt = pick_next(self.playlist(), after_position)
        if nxt:
            self.load(nxt, "replace")
        return nxt

    def handle_event(self, event):
        name = event.get("event")
        if name == "file-loaded":
            path = self.mpv.get("path") or ""
            item = self._media_by_filename(Path(path).name)
            self.current = item
            if item:
                self.last_position = item["position"]
            self.set_now_playing(item)
            self.queue_next()
        elif name == "end-file" and event.get("reason") == "error":
            log.warning("mpv could not play a file: %s", event.get("file_error"))
        elif name == "property-change" and event.get("name") == "idle-active":
            if event.get("data"):
                self.on_idle()

    def on_idle(self):
        """mpv ran out of things to play (empty list, or a file failed)."""
        self.current = None
        self.set_now_playing(None)
        now = time.monotonic()
        if now - self.last_idle_start < 2:
            time.sleep(2)  # everything failing? don't spin
        self.last_idle_start = time.monotonic()
        if not self.play_from(self.last_position):
            log.info("Playlist is empty; showing a black screen")

    def _media_by_filename(self, filename):
        row = self.conn.execute("SELECT * FROM media WHERE filename = ?", (filename,)).fetchone()
        return dict(row) if row else None

    def poll(self):
        now = time.time()
        if now - self.last_heartbeat > HEARTBEAT_SECONDS:
            db.set_setting(self.conn, "player_heartbeat", f"{now:.0f}")
            self.last_heartbeat = now

        version = db.get_setting(self.conn, "playlist_version")
        if version != self.version:
            self.version = version
            self.on_playlist_changed()

        cmd = db.get_json(self.conn, "player_command")
        if cmd and cmd.get("nonce") != self.command_nonce:
            self.command_nonce = cmd.get("nonce")
            self.run_command(cmd)

    def on_playlist_changed(self):
        log.info("Playlist or settings changed")
        self.apply_settings()
        items = self.playlist()
        if self.current is None:
            if self.mpv.get("idle-active", False):
                self.play_from(self.last_position)
            return
        still_there = next((i for i in items if i["id"] == self.current["id"]), None)
        if still_there is None:
            # The file on screen was deleted or switched off: move on now.
            self.last_position = self.current["position"]
            self.current = None
            if not self.play_from(self.last_position):
                self.mpv.command("stop")
            return
        self.current = still_there
        self.queue_next()

    def run_command(self, cmd):
        log.info("Command from admin: %s", cmd.get("cmd"))
        try:
            if cmd["cmd"] == "next":
                after = self.current["position"] if self.current else self.last_position
                self.play_from(after)
            elif cmd["cmd"] == "play":
                item = db.get_media(self.conn, cmd.get("id"))
                if item:
                    self.load(item, "replace")
            elif cmd["cmd"] == "restart":
                self.mpv.command("quit")
        except MpvError as exc:
            log.warning("Command failed: %s", exc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    player = Player(Config())

    def _term(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    try:
        sys.exit(player.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
