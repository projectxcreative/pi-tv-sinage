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


def create_app(config=None):
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
        return jsonify(
            player_online=time.time() - heartbeat < 15,
            now_playing={"id": current["id"], "name": current["original_name"],
                         "since": now.get("since")} if current else None,
        )

    @app.post("/api/command")
    @login_required
    def api_command():
        body = request.get_json(silent=True) or {}
        cmd = body.get("cmd")
        if cmd not in ("next", "play", "restart"):
            return jsonify(error="unknown command"), 400
        payload = {"cmd": cmd, "nonce": uuid.uuid4().hex}
        if cmd == "play":
            try:
                payload["id"] = int(body.get("id"))
            except (TypeError, ValueError):
                return jsonify(error="id required"), 400
        db.set_json(get_db(), "player_command", payload)
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
