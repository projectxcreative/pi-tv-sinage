"""SQLite storage for the playlist and settings.

Both the web admin and the player open the same database. WAL mode lets the
player read while the admin writes.
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT NOT NULL UNIQUE,
    original_name  TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('video', 'image')),
    position       INTEGER NOT NULL,
    duration       REAL,              -- seconds on screen (images); NULL = default
    enabled        INTEGER NOT NULL DEFAULT 1,
    width          INTEGER,
    height         INTEGER,
    codec          TEXT,
    media_length   REAL,              -- video length in seconds
    size_bytes     INTEGER,
    warning        TEXT,
    created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "image_duration": "10",
    "mute": "0",
    "fit_mode": "fit",          # fit = letterbox, fill = crop to fill screen
    "playlist_version": "0",
}


def connect(path):
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    return conn


# ---- settings ---------------------------------------------------------------

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)),
    )


def bump_version(conn):
    """Tell the player the playlist or settings changed."""
    conn.execute(
        "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'playlist_version'"
    )


def get_json(conn, key):
    raw = get_setting(conn, key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def set_json(conn, key, value):
    set_setting(conn, key, json.dumps(value))


# ---- media ------------------------------------------------------------------

def list_media(conn, enabled_only=False):
    sql = "SELECT * FROM media"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY position, id"
    return [dict(r) for r in conn.execute(sql)]


def get_media(conn, media_id):
    row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    return dict(row) if row else None


def add_media(conn, **fields):
    row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM media").fetchone()
    fields.setdefault("created_at", time.time())
    fields["position"] = row["p"]
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO media ({cols}) VALUES ({marks})", tuple(fields.values()))
    bump_version(conn)
    return cur.lastrowid


def update_media(conn, media_id, **fields):
    allowed = {"enabled", "duration", "original_name"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE media SET {sets} WHERE id = ?", (*fields.values(), media_id))
    bump_version(conn)


def delete_media(conn, media_id):
    conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
    bump_version(conn)


def reorder(conn, ordered_ids):
    """Set positions from a full list of ids. Ids not listed keep their
    relative order and go after the listed ones."""
    existing = [m["id"] for m in list_media(conn)]
    seen = set()
    order = []
    for i in ordered_ids:
        if i in existing and i not in seen:
            order.append(i)
            seen.add(i)
    order += [i for i in existing if i not in seen]
    conn.execute("BEGIN")
    try:
        for pos, media_id in enumerate(order):
            conn.execute("UPDATE media SET position = ? WHERE id = ?", (pos, media_id))
        bump_version(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return order
