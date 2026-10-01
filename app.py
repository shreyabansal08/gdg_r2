import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, redirect, request

db_path = os.environ.get("DB_PATH", "shortener.db")
base_url = os.environ.get("BASE_URL", "").rstrip("/")

api_key = os.environ.get("API_KEY")
if not api_key:
    api_key = secrets.token_urlsafe(32)
    logging.warning("no API_KEY set, using temporary key: %s", api_key)

alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
max_url_length = 2048
code_length = 7
max_attempts = 20
max_ttl = 365 * 24 * 3600
alias_pattern = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
reserved_words = {"shorten", "stats", "health", "static", "api", "admin"}
allowed_schemes = {"http", "https"}

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(db_path, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            code       TEXT PRIMARY KEY,
            url        TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER,
            clicks     INTEGER NOT NULL DEFAULT 0,
            is_custom  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_links_expires ON links(expires_at)")
    db.commit()
    db.close()


def error_response(status, message):
    return jsonify({"error": message}), status


def to_iso(timestamp):
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def to_base62(number):
    if number == 0:
        return alphabet[0]
    digits = []
    while number:
        number, remainder = divmod(number, 62)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def make_code(url, attempt):
    nonce = secrets.token_hex(8)
    digest = hashlib.sha256(f"{url}|{attempt}|{nonce}".encode()).digest()
    length = code_length + attempt // 5
    return to_base62(int.from_bytes(digest, "big"))[:length]


def check_url(raw):
    if not isinstance(raw, str):
        return None, "'url' must be a string"

    url = raw.strip()
    if not url:
        return None, "'url' must not be empty"
    if len(url) > max_url_length:
        return None, f"'url' is longer than {max_url_length} characters"
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None, "'url' contains whitespace or control characters"

    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port
    except ValueError:
        return None, "'url' is malformed"

    if parts.scheme.lower() not in allowed_schemes:
        return None, "only http and https urls are allowed"
    if not host:
        return None, "'url' needs a host"
    if parts.username or parts.password:
        return None, "urls with embedded credentials are not allowed"

    if base_url:
        own_host = urlsplit(base_url).hostname
    else:
        own_host = request.host.split(":")[0]
    if own_host and host.lower() == own_host.lower():
        return None, "can't shorten a url that points back to this service"

    return url, None


def check_ttl(value):
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "'ttl_seconds' must be an integer"
    if value <= 0:
        return None, "'ttl_seconds' must be positive"
    if value > max_ttl:
        return None, f"'ttl_seconds' can't be more than {max_ttl}"
    return value, None


def save_link(db, code, url, now, expires_at, is_custom):
    try:
        db.execute(
            "DELETE FROM links WHERE code = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            (code, now),
        )
        db.execute(
            "INSERT INTO links (code, url, created_at, expires_at, is_custom) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, url, now, expires_at, int(is_custom)),
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        db.rollback()
        return False


def build_short_url(code):
    base = base_url or request.host_url.rstrip("/")
    return f"{base}/{code}"


def has_expired(row, now):
    return row["expires_at"] is not None and row["expires_at"] <= now


def find_link(code):
    if not alias_pattern.match(code):
        return None
    return get_db().execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/shorten")
def shorten():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error_response(400, "request body must be a json object")

    url, problem = check_url(body.get("url"))
    if problem:
        return error_response(400, problem)

    ttl, problem = check_ttl(body.get("ttl_seconds"))
    if problem:
        return error_response(400, problem)

    alias = body.get("alias")
    if alias is not None:
        if not isinstance(alias, str) or not alias_pattern.match(alias):
            return error_response(400, "alias must be 3-32 characters: letters, numbers, _ or -")
        if alias.lower() in reserved_words:
            return error_response(400, "that alias is reserved")

    db = get_db()
    now = int(time.time())
    expires_at = now + ttl if ttl else None

    if alias:
        if not save_link(db, alias, url, now, expires_at, True):
            return error_response(409, "alias is already taken")
        code = alias
    else:
        code = None
        for attempt in range(max_attempts):
            candidate = make_code(url, attempt)
            if candidate.lower() in reserved_words:
                continue
            if save_link(db, candidate, url, now, expires_at, False):
                code = candidate
                break
        if code is None:
            return error_response(503, "couldn't find a free code, please try again")

    return (
        jsonify(
            {
                "code": code,
                "short_url": build_short_url(code),
                "original_url": url,
                "created_at": to_iso(now),
                "expires_at": to_iso(expires_at),
            }
        ),
        201,
    )


@app.get("/stats/<code>")
def stats(code):
    row = find_link(code)
    if row is None:
        return error_response(404, "not found")
    if has_expired(row, int(time.time())):
        return error_response(410, "this link has expired")
    return jsonify(
        {
            "code": row["code"],
            "original_url": row["url"],
            "clicks": row["clicks"],
            "created_at": to_iso(row["created_at"]),
            "expires_at": to_iso(row["expires_at"]),
        }
    )


@app.delete("/<code>")
def delete(code):
    supplied = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(supplied.encode(), api_key.encode()):
        return error_response(401, "missing or invalid api key")

    db = get_db()
    result = db.execute("DELETE FROM links WHERE code = ?", (code,))
    db.commit()
    if result.rowcount == 0:
        return error_response(404, "not found")
    return jsonify({"deleted": code})


@app.get("/<code>")
def follow(code):
    row = find_link(code)
    if row is None:
        return error_response(404, "not found")

    now = int(time.time())
    if has_expired(row, now):
        return error_response(410, "this link has expired")

    db = get_db()
    db.execute(
        "UPDATE links SET clicks = clicks + 1 "
        "WHERE code = ? AND (expires_at IS NULL OR expires_at > ?)",
        (code, now),
    )
    db.commit()
    return redirect(row["url"], code=302)


@app.errorhandler(404)
def not_found(e):
    return error_response(404, "not found")


@app.errorhandler(405)
def method_not_allowed(e):
    return error_response(405, "method not allowed")


@app.errorhandler(500)
def server_error(e):
    return error_response(500, "something went wrong on our side")


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)