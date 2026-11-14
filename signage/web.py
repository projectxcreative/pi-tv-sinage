"""Web admin: log in, upload, delete, reorder and tune the playlist."""
import functools
import hmac
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, media
from .config import Config
from .wifi_setup import NM, DemoNM

log = logging.getLogger("signage.web")

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


class LoginThrottle:
    """Lock an IP out for a few minutes after repeated bad passwords."""

    def __init__(self):
        self._lock = threading.Lock()
        self._failures = {}

    def locked(self, ip):
        with self._lock:
            count, until = self._failures.get(ip, (0, 0))
            return count >= MAX_FAILURES and time.time() < until

    def fail(self, ip):
        with self._lock:
            count, until = self._failures.get(ip, (0, 0))
            if time.time() > until:
                count = 0
            self._failures[ip] = (count + 1, time.time() + LOCKOUT_SECONDS)

    def clear(self, ip):
        with self._lock:
            self._failures.pop(ip, None)


def set_credentials(conn, username, password):
    if not username or not password:
        raise ValueError("username and password are required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    db.set_setting(conn, "admin_username", username)
    db.set_setting(conn, "admin_password_hash", generate_password_hash(password))


class WifiSwitcher:
    """Joins a Wi-Fi network in the background.

    Switching networks can cut off the browser that asked for it, so the work
    happens on a thread and the page polls for the result. NM.connect() falls
    back to the previous network if the new one fails.
    """

    def __init__(self, nm):
        self.nm = nm
        self._lock = threading.Lock()
        self.job = {"state": "idle"}

    def busy(self):
        return self.job.get("state") == "connecting"

    def start(self, ssid, password, hidden):
        with self._lock:
            if self.busy():
                return False
            self.job = {"state": "connecting", "ssid": ssid, "started": time.time()}
        threading.Thread(target=self._run, args=(ssid, password, hidden), daemon=True).start()
        return True

    def _run(self, ssid, password, hidden):
        try:
            ok, err = self.nm.connect(ssid, password, hidden)
        except Exception as exc:  # never leave the job stuck in "connecting"
            log.exception("Wi-Fi connect failed")
            ok, err = False, str(exc)
        log.info("Wi-Fi switch to %r: %s", ssid, "ok" if ok else err)
        self.job = {"state": "ok" if ok else "failed", "ssid": ssid, "message": err,
                    "finished": time.time()}


def power_action(action):
    """Ask systemd to restart or shut down the Pi (allowed by a polkit rule)."""
    import subprocess
    verb = {"shutdown": "poweroff", "restart": "reboot"}[action]
    subprocess.run(["systemctl", verb], check=True, capture_output=True, text=True, timeout=30)


def create_app(config=None, nm=None, power=None):
    config = config or Config()
    config.ensure_dirs()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.secret_key,
        MAX_CONTENT_LENGTH=config.max_upload_mb * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.session_hours),
    )
    app.signage_config = config
    throttle = LoginThrottle()
    if nm is None:
        nm = DemoNM() if os.environ.get("SIGNAGE_WIFI_DEMO") else NM()
    wifi = WifiSwitcher(nm)
    app.wifi = wifi
    power = power or power_action

    def get_db():
        if "db" not in g:
            g.db = db.connect(config.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    def check_csrf():
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Bad or missing CSRF token. Reload the page and try again.")

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user"):
                if request.path.startswith("/api/"):
                    return jsonify(error="Not logged in"), 401
                return redirect(url_for("login", next=request.path))
            if request.method not in ("GET", "HEAD"):
                check_csrf()
            return view(*args, **kwargs)
        return wrapped

    def credentials_configured():
        return bool(db.get_setting(get_db(), "admin_password_hash"))

    # ---- pages ---------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        conn = get_db()
        if not credentials_configured():
            return render_template("login.html", not_configured=True), 503
        error = None
        if request.method == "POST":
            check_csrf()
            ip = request.remote_addr or "?"
            if throttle.locked(ip):
                error = "Too many failed attempts. Wait a few minutes and try again."
            else:
                user = request.form.get("username", "")
                pw = request.form.get("password", "")
                good_user = db.get_setting(conn, "admin_username", "")
                good_hash = db.get_setting(conn, "admin_password_hash", "")
                user_ok = hmac.compare_digest(user.encode(), good_user.encode())
                pw_ok = check_password_hash(good_hash, pw)
                if user_ok and pw_ok:
                    throttle.clear(ip)
                    session.clear()
                    session.permanent = True
                    session["user"] = user
                    nxt = request.args.get("next", "")
                    if not nxt.startswith("/") or nxt.startswith("//"):
                        nxt = url_for("index")
                    return redirect(nxt)
                throttle.fail(ip)
                error = "Wrong username or password."
        return render_template("login.html", error=error)

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        return render_template("index.html", user=session["user"])

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        conn = get_db()
        message = error = None
        if request.method == "POST":
            form = request.form
            if form.get("action") == "playback":
                try:
                    secs = float(form.get("image_duration", "10"))
                    if not 1 <= secs <= 3600:
                        raise ValueError
                except ValueError:
                    error = "Photo duration must be between 1 and 3600 seconds."
                else:
                    db.set_setting(conn, "image_duration", f"{secs:g}")
                    db.set_setting(conn, "mute", "1" if form.get("mute") else "0")
                    fit = form.get("fit_mode")
                    db.set_setting(conn, "fit_mode", fit if fit in ("fit", "fill") else "fit")
                    db.bump_version(conn)
                    message = "Playback settings saved."
            elif form.get("action") == "password":
                current = form.get("current_password", "")
                good_hash = db.get_setting(conn, "admin_password_hash", "")
                if not check_password_hash(good_hash, current):
                    error = "Current password is wrong."
                elif form.get("new_password") != form.get("confirm_password"):
                    error = "The new passwords don't match."
                else:
                    try:
                        set_credentials(conn, form.get("username") or session["user"],
                                        form.get("new_password", ""))
                        session["user"] = form.get("username") or session["user"]
                        message = "Login details updated."
                    except ValueError as exc:
                        error = str(exc).capitalize() + "."
        values = {
            "image_duration": db.get_setting(conn, "image_duration", "10"),
            "mute": db.get_setting(conn, "mute", "0") == "1",
            "fit_mode": db.get_setting(conn, "fit_mode", "fit"),
            "username": db.get_setting(conn, "admin_username", ""),
        }
        return render_template("settings.html", user=session["user"], v=values,
                               message=message, error=error)

    # ---- API -----------------------------------------------------------------

    def media_json(m, default_duration):
        return {
            "id": m["id"],
            "name": m["original_name"],
            "kind": m["kind"],
            "enabled": bool(m["enabled"]),
            "duration": m["duration"],
            "effective_duration": (m["duration"] or default_duration) if m["kind"] == "image"
            else m["media_length"],
            "width": m["width"],
            "height": m["height"],
            "codec": m["codec"],
            "size_bytes": m["size_bytes"],
            "warning": m["warning"],
            "thumb": url_for("thumb", media_id=m["id"]),
            "preview": url_for("preview", media_id=m["id"]),
        }

    @app.get("/api/media")
    @login_required
    def api_list():
        conn = get_db()
        default = float(db.get_setting(conn, "image_duration", "10"))
        return jsonify(items=[media_json(m, default) for m in db.list_media(conn)],
                       image_duration=default)

    @app.post("/api/upload")
    @login_required
    def api_upload():
        conn = get_db()
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="No file received."), 400
        created, errors = [], []
        for f in files:
            original = os.path.basename(f.filename or "").strip() or "upload"
            kind = media.kind_for(original)
            if not kind:
                errors.append(f"{original}: unsupported file type.")
                continue
            ext = Path(original).suffix.lower()
            stored = f"{uuid.uuid4().hex}{ext}"
            dest = config.media_dir / stored
            partial = config.tmp_dir / (stored + ".part")
            f.save(partial)
            info = {"codec": None, "width": None, "height": None, "length": None}
            warning = None
            try:
                if kind == "image":
                    info["width"], info["height"] = media.normalise_image(partial)
                else:
                    codec, w, h, length = media.probe(partial)
                    info.update(codec=codec, width=w, height=h, length=length)
                    warning = media.playback_warning(codec, w, h)
            except Exception as exc:
                partial.unlink(missing_ok=True)
                errors.append(f"{original}: could not read file ({exc}).")
                continue
            os.replace(partial, dest)
            media.make_thumbnail(dest, config.thumb_dir / f"{stored}.jpg", kind)
            media_id = db.add_media(
                conn, filename=stored, original_name=original, kind=kind,
                width=info["width"], height=info["height"], codec=info["codec"],
                media_length=info["length"], size_bytes=dest.stat().st_size,
                warning=warning,
            )
            created.append(media_id)
        status = 200 if created else 400
        return jsonify(created=created, errors=errors), status

    @app.patch("/api/media/<int:media_id>")
    @login_required
    def api_update(media_id):
        conn = get_db()
        if not db.get_media(conn, media_id):
            abort(404)
        body = request.get_json(silent=True) or {}
        fields = {}
        if "enabled" in body:
            fields["enabled"] = 1 if body["enabled"] else 0
        if "duration" in body:
            d = body["duration"]
            if d in (None, ""):
                fields["duration"] = None
            else:
                try:
                    d = float(d)
                except (TypeError, ValueError):
                    return jsonify(error="Duration must be a number."), 400
                if not 1 <= d <= 3600:
                    return jsonify(error="Duration must be 1 to 3600 seconds."), 400
                fields["duration"] = d
        if "name" in body and str(body["name"]).strip():
            fields["original_name"] = str(body["name"]).strip()[:200]
        db.update_media(conn, media_id, **fields)
        return jsonify(ok=True)

    @app.delete("/api/media/<int:media_id>")
    @login_required
    def api_delete(media_id):
        conn = get_db()
        m = db.get_media(conn, media_id)
        if not m:
            abort(404)
        db.delete_media(conn, media_id)
        (config.media_dir / m["filename"]).unlink(missing_ok=True)
        (config.thumb_dir / f"{m['filename']}.jpg").unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.post("/api/reorder")
    @login_required
    def api_reorder():
        body = request.get_json(silent=True) or {}
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            return jsonify(error="ids must be a list of numbers"), 400
        return jsonify(order=db.reorder(get_db(), ids))

    @app.get("/api/status")
    @login_required
    def api_status():
        conn = get_db()
        now = db.get_json(conn, "now_playing") or {}
        heartbeat = float(db.get_setting(conn, "player_heartbeat", "0") or 0)
        current = db.get_media(conn, now.get("id")) if now.get("id") else None
        playback = db.get_json(conn, "playback") or {}
        if not current or playback.get("id") != current["id"]:
            playback = {}
        return jsonify(
            player_online=time.time() - heartbeat < 15,
            now_playing={"id": current["id"], "name": current["original_name"],
                         "kind": current["kind"], "since": now.get("since"),
                         "pos": playback.get("pos"), "duration": playback.get("duration"),
                         "paused": bool(playback.get("paused"))} if current else None,
            snapshot_at=float(db.get_setting(conn, "snapshot_at", "0") or 0),
        )

    @app.post("/api/command")
    @login_required
    def api_command():
        body = request.get_json(silent=True) or {}
        cmd = body.get("cmd")
        if cmd not in ("next", "play", "restart", "seek", "pause"):
            return jsonify(error="unknown command"), 400
        payload = {"cmd": cmd, "nonce": uuid.uuid4().hex}
        if cmd == "seek":
            try:
                pos = float(body.get("pos"))
            except (TypeError, ValueError):
                return jsonify(error="pos must be a number of seconds"), 400
            if not 0 <= pos < 24 * 3600:
                return jsonify(error="pos out of range"), 400
            payload["pos"] = pos
        if cmd == "pause":
            payload["paused"] = bool(body.get("paused"))
        if cmd == "play":
            try:
                payload["id"] = int(body.get("id"))
            except (TypeError, ValueError):
                return jsonify(error="id required"), 400
        db.set_json(get_db(), "player_command", payload)
        return jsonify(ok=True)

    # ---- power -----------------------------------------------------------------

    @app.post("/api/power")
    @login_required
    def api_power():
        action = (request.get_json(silent=True) or {}).get("action")
        if action not in ("shutdown", "restart"):
            return jsonify(error="action must be shutdown or restart"), 400

        def go():
            time.sleep(1)  # let the reply reach the browser first
            try:
                power(action)
            except Exception as exc:
                log.error("Power %s failed: %s", action, getattr(exc, "stderr", None) or exc)

        log.warning("%s requested from the web admin by %s", action.capitalize(), session.get("user"))
        threading.Thread(target=go, daemon=True).start()
        return jsonify(ok=True), 202

    # ---- live view of the screen -------------------------------------------------

    @app.post("/api/screen/refresh")
    @login_required
    def api_screen_refresh():
        """Ask the player for a fresh snapshot (at most one a second)."""
        conn = get_db()
        last = float(db.get_setting(conn, "snapshot_requested_at", "0") or 0)
        if time.time() - last >= 1:
            db.set_setting(conn, "snapshot_requested_at", f"{time.time():.2f}")
            db.set_setting(conn, "snapshot_request", uuid.uuid4().hex)
        return jsonify(ok=True), 202

    @app.get("/api/screen")
    @login_required
    def api_screen():
        path = config.run_dir / "screen.jpg"
        if not path.exists():
            return jsonify(error="Nothing on screen"), 404
        resp = send_from_directory(config.run_dir, "screen.jpg", max_age=0)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- Wi-Fi -------------------------------------------------------------------

    @app.get("/api/wifi")
    @login_required
    def api_wifi():
        return jsonify(available=nm.has_wifi(), status=nm.status(),
                       saved=nm.wifi_profiles(), job=wifi.job)

    @app.get("/api/wifi/scan")
    @login_required
    def api_wifi_scan():
        if wifi.busy():
            return jsonify(error="Busy switching networks. Try again in a moment."), 409
        return jsonify(networks=[{"ssid": n.ssid, "signal": n.signal, "secure": n.secure,
                                  "in_use": n.in_use} for n in nm.scan()])

    @app.post("/api/wifi/connect")
    @login_required
    def api_wifi_connect():
        body = request.get_json(silent=True) or {}
        ssid = str(body.get("ssid") or "").strip()
        password = str(body.get("password") or "")
        hidden = bool(body.get("hidden"))
        if not 1 <= len(ssid.encode()) <= 32:
            return jsonify(error="Enter the network name (up to 32 characters)."), 400
        if password and not 8 <= len(password) <= 63:
            return jsonify(error="Wi-Fi passwords are 8 to 63 characters."), 400
        if not wifi.start(ssid, password, hidden):
            return jsonify(error="Already switching networks. Wait for it to finish."), 409
        return jsonify(ok=True), 202

    @app.post("/api/wifi/forget")
    @login_required
    def api_wifi_forget():
        body = request.get_json(silent=True) or {}
        uuid_ = str(body.get("uuid") or "")
        profile = next((p for p in nm.wifi_profiles() if p["uuid"] == uuid_), None)
        if not profile:
            return jsonify(error="That network isn't saved."), 404
        if profile["active"]:
            return jsonify(error="The Pi is using this network now. Join another one first."), 409
        if not nm.forget(uuid_):
            return jsonify(error="Could not forget that network."), 500
        return jsonify(ok=True)

    # ---- files -----------------------------------------------------------------

    @app.get("/thumb/<int:media_id>")
    @login_required
    def thumb(media_id):
        m = db.get_media(get_db(), media_id) or abort(404)
        name = f"{m['filename']}.jpg"
        if not (config.thumb_dir / name).exists():
            return send_from_directory(app.static_folder, f"{m['kind']}.svg")
        return send_from_directory(config.thumb_dir, name, max_age=3600)

    @app.get("/preview/<int:media_id>")
    @login_required
    def preview(media_id):
        m = db.get_media(get_db(), media_id) or abort(404)
        return send_from_directory(config.media_dir, m["filename"], conditional=True)

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify(error=f"File is larger than the {config.max_upload_mb} MB limit."), 413

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from waitress import serve

    config = Config()
    app = create_app(config)
    log.info("Signage admin listening on http://%s:%s", config.host, config.port)
    serve(
        app, host=config.host, port=config.port, threads=8,
        max_request_body_size=config.max_upload_mb * 1024 * 1024,
        channel_timeout=600,
    )


if __name__ == "__main__":
    main()
