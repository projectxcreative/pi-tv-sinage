"""Minimal client for mpv's JSON IPC protocol over a Unix socket."""
import itertools
import json
import queue
import socket
import threading
import time


class MpvError(RuntimeError):
    pass


class MpvClient:
    def __init__(self, path, connect_timeout=20):
        self.events = queue.Queue()
        self._pending = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._sock = self._connect(str(path), connect_timeout)
        self.closed = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @staticmethod
    def _connect(path, timeout):
        deadline = time.monotonic() + timeout
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(path)
                return s
            except OSError:
                s.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def _read_loop(self):
        buf = b""
        try:
            while True:
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if "request_id" in msg and "event" not in msg:
                        with self._lock:
                            slot = self._pending.pop(msg["request_id"], None)
                        if slot:
                            slot.put(msg)
                    elif "event" in msg:
                        self.events.put(msg)
        except OSError:
            pass
        finally:
            self.closed.set()
            self.events.put({"event": "_disconnected"})

    def command(self, *args, timeout=10, **named):
        """Send a command. Positional args form the classic array syntax;
        keyword args use mpv's named-argument syntax (``name=`` required)."""
        if self.closed.is_set():
            raise MpvError("mpv connection closed")
        req_id = next(self._ids)
        slot = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[req_id] = slot
        cmd = dict(named) if named else list(args)
        payload = json.dumps({"command": cmd, "request_id": req_id}) + "\n"
        try:
            self._sock.sendall(payload.encode())
            reply = slot.get(timeout=timeout)
        except (OSError, queue.Empty) as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            raise MpvError(f"mpv did not answer {cmd!r}: {exc}") from exc
        if reply.get("error") != "success":
            raise MpvError(f"{cmd!r} failed: {reply.get('error')}")
        return reply.get("data")

    def get(self, prop, default=None):
        try:
            return self.command("get_property", prop)
        except MpvError:
            return default

    def set(self, prop, value):
        return self.command("set_property", prop, value)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
