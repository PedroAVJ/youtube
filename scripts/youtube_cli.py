#!/usr/bin/env python3
"""ytx — a thin, quota-aware CLI over the YouTube Data API v3.

Stdlib only. Reuses the Desktop OAuth client already configured for `gws`
(``~/.config/gws/client_secret.json``) unless told otherwise, but keeps its
own token store so YouTube scopes never disturb the Workspace credentials.

Reads are cached in SQLite so repeated queries cost zero quota.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

API_ROOT = "https://www.googleapis.com/youtube/v3"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

SCOPE_READ = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_WRITE = "https://www.googleapis.com/auth/youtube.force-ssl"

CONFIG_DIR = Path(os.environ.get("YTX_CONFIG_DIR", Path.home() / ".config" / "ytx"))
TOKEN_FILE = CONFIG_DIR / "credentials.json"
CACHE_DB = CONFIG_DIR / "cache.db"
DEFAULT_CLIENT_SECRET = Path.home() / ".config" / "gws" / "client_secret.json"

# Quota units per method. Source: developers.google.com/youtube/v3/determine_quota_cost
QUOTA_COSTS = {
    "playlists.list": 1,
    "playlists.insert": 50,
    "playlists.update": 50,
    "playlists.delete": 50,
    "playlistItems.list": 1,
    "playlistItems.insert": 50,
    "playlistItems.update": 50,
    "playlistItems.delete": 50,
    "videos.list": 1,
    "channels.list": 1,
    "subscriptions.list": 1,
    "subscriptions.delete": 50,
    "search.list": 100,
}

DAILY_QUOTA = 10_000
PACIFIC = ZoneInfo("America/Los_Angeles")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    raise SystemExit(f"ytx: {message}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def quota_day() -> str:
    """YouTube quota resets at midnight Pacific, not UTC."""
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def is_tty() -> bool:
    return sys.stdout.isatty()


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(value: str) -> str:
    """Accept a bare ID, a watch URL, a youtu.be link, a Short, or a music URL."""
    value = value.strip()
    if VIDEO_ID_RE.match(value):
        return value
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        if VIDEO_ID_RE.match(candidate):
            return candidate
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query and VIDEO_ID_RE.match(query["v"][0]):
            return query["v"][0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            if VIDEO_ID_RE.match(parts[1]):
                return parts[1]
    die(f"could not parse a video ID out of {value!r}")


def extract_playlist_id(value: str) -> str | None:
    value = value.strip()
    if re.match(r"^(PL|UU|LL|FL|RD|OL)[A-Za-z0-9_-]*$", value) or value in {"LL", "WL"}:
        return value
    if "://" in value or value.startswith("youtube.com"):
        parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
        query = urllib.parse.parse_qs(parsed.query)
        if "list" in query:
            return query["list"][0]
    return None


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def truncate(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def emit(rows: list[dict[str, Any]], columns: Sequence[tuple[str, str, int]], force_json: bool) -> None:
    """Print JSON when piped or asked; a padded table when a human is watching."""
    if force_json or not is_tty():
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("(no results)")
        return
    header = "  ".join(title.ljust(width) for _key, title, width in columns)
    print(header)
    print("  ".join("-" * width for _key, _title, width in columns))
    for row in rows:
        print("  ".join(truncate(str(row.get(key, "")), width).ljust(width) for key, _title, width in columns))


# --------------------------------------------------------------------------
# credential handling
# --------------------------------------------------------------------------


def client_config() -> tuple[str, str]:
    path = Path(os.environ.get("YTX_CLIENT_SECRET_FILE", DEFAULT_CLIENT_SECRET))
    if not path.exists():
        die(
            f"no OAuth client config at {path}.\n"
            "  Point YTX_CLIENT_SECRET_FILE at a Desktop-app client_secret.json,\n"
            "  or download one from console.cloud.google.com → Credentials."
        )
    try:
        blob = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc}")
    section = blob.get("installed") or blob.get("web")
    if not section:
        die(f"{path} has neither an 'installed' nor a 'web' client section")
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not client_id or not client_secret:
        die(f"{path} is missing client_id or client_secret")
    return client_id, client_secret


KEYCHAIN_SERVICE = "ytx-oauth"


def keychain_available() -> bool:
    return sys.platform == "darwin" and os.environ.get("YTX_FORCE_FILE_STORE") != "1"


def _decode_keychain_blob(raw: str) -> str:
    """`security` hex-encodes any value it considers non-printable (e.g. one
    containing newlines), so accept hex, base64, and plain JSON alike."""
    raw = raw.strip()
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", raw):
        try:
            candidate = bytes.fromhex(raw).decode()
            if candidate.lstrip().startswith("{"):
                return candidate
        except (ValueError, UnicodeDecodeError):
            pass
    try:
        candidate = base64.b64decode(raw, validate=True).decode()
        if candidate.lstrip().startswith("{"):
            return candidate
    except (ValueError, UnicodeDecodeError):
        pass
    return raw


def keychain_read() -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _decode_keychain_blob(result.stdout)


def keychain_write(blob: str) -> bool:
    # base64 keeps the stored value single-line printable ASCII, so `security`
    # hands it back verbatim instead of hex-encoding it.
    encoded = base64.b64encode(blob.encode()).decode()
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", os.environ.get("USER", "ytx"), "-w", encoded, "-U"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def keychain_delete() -> None:
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
        capture_output=True,
        text=True,
    )


def tokens_exist() -> bool:
    return bool(keychain_read()) if keychain_available() else TOKEN_FILE.exists()


def token_store() -> str:
    return f"macOS Keychain ({KEYCHAIN_SERVICE})" if keychain_available() else str(TOKEN_FILE)


def load_tokens() -> dict[str, Any]:
    if keychain_available():
        blob = keychain_read()
        if blob:
            return json.loads(blob)
        # fall through to the file store so an older login still works
    if not TOKEN_FILE.exists():
        die("not authenticated. Run:  ytx auth login")
    return json.loads(TOKEN_FILE.read_text())


def save_tokens(tokens: dict[str, Any]) -> None:
    blob = json.dumps(tokens, indent=2)
    if keychain_available() and keychain_write(blob):
        # never leave a stale plaintext copy behind once the Keychain holds it
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(blob)
    TOKEN_FILE.chmod(0o600)


def post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        die(f"token endpoint returned HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        die(f"could not reach {url}: {exc.reason}")


def refresh_access_token(tokens: dict[str, Any]) -> dict[str, Any]:
    client_id, client_secret = client_config()
    payload = post_form(
        TOKEN_URI,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    tokens["access_token"] = payload["access_token"]
    tokens["expires_at"] = (
        datetime.now(timezone.utc).timestamp() + int(payload.get("expires_in", 3600)) - 60
    )
    save_tokens(tokens)
    return tokens


def access_token() -> str:
    tokens = load_tokens()
    if tokens.get("expires_at", 0) <= datetime.now(timezone.utc).timestamp():
        tokens = refresh_access_token(tokens)
    return tokens["access_token"]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in query.items()}
        ok = "code" in _CallbackHandler.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "<h2>ytx is authorized.</h2><p>You can close this tab.</p>"
            if ok
            else f"<h2>Authorization failed.</h2><pre>{_CallbackHandler.result}</pre>"
        )
        self.wfile.write(f"<html><body style='font-family:system-ui;padding:3rem'>{message}</body></html>".encode())

    def log_message(self, *_args: Any) -> None:
        return


def do_login(scopes: list[str], open_browser: bool) -> None:
    client_id, client_secret = client_config()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)

    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"

    print("Open this URL and grant access:\n", file=sys.stderr)
    print(url, file=sys.stderr)
    print("\nWaiting for the redirect…", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=int(os.environ.get("YTX_LOGIN_TIMEOUT", "900")))
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        die("timed out waiting for the OAuth redirect")
    if result.get("state") != state:
        die("OAuth state mismatch — aborting")
    if "code" not in result:
        die(f"authorization failed: {result.get('error', 'no code returned')}")

    payload = post_form(
        TOKEN_URI,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if "refresh_token" not in payload:
        die("Google did not return a refresh token. Re-run with a fresh consent prompt.")

    save_tokens(
        {
            "refresh_token": payload["refresh_token"],
            "access_token": payload["access_token"],
            "expires_at": datetime.now(timezone.utc).timestamp() + int(payload.get("expires_in", 3600)) - 60,
            "scopes": scopes,
            "client_id": client_id,
            "obtained_at": now_iso(),
        }
    )
    print(f"\nAuthorized. Tokens stored in {token_store()}.", file=sys.stderr)


# --------------------------------------------------------------------------
# quota-accounted API layer
# --------------------------------------------------------------------------


def record_quota(method: str, units: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO quota_log(day, method, units, at) VALUES(?,?,?,?)",
            (quota_day(), method, units, now_iso()),
        )


def api(method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One YouTube Data API call, with quota accounted before it goes out."""
    verb = {"list": "GET", "insert": "POST", "update": "PUT", "delete": "DELETE"}[method.split(".")[-1]]
    units = QUOTA_COSTS.get(method, 1)
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{API_ROOT}/{path}" + (f"?{query}" if query else "")

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=verb)
    request.add_header("Authorization", f"Bearer {access_token()}")
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            record_quota(method, units)
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        record_quota(method, units)  # failed calls still bill
        detail = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)["error"]
            reason = parsed.get("errors", [{}])[0].get("reason", "")
            message = parsed.get("message", detail)
        except Exception:
            reason, message = "", detail
        if reason == "quotaExceeded":
            die("daily quota (10,000 units) exhausted. Resets at midnight Pacific. Try `ytx quota`.")
        if exc.code in (401, 403) and "insufficient" in message.lower():
            die(f"{message}\n  Missing scope? Re-run:  ytx auth login --write")
        die(f"API {method} failed (HTTP {exc.code}): {message}")
    except urllib.error.URLError as exc:
        die(f"could not reach the YouTube API: {exc.reason}")


def api_pages(method: str, path: str, params: dict[str, Any], max_pages: int = 50) -> Iterable[dict[str, Any]]:
    page_token = None
    for _ in range(max_pages):
        payload = api(method, path, {**params, "pageToken": page_token})
        yield from payload.get("items", [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return


# --------------------------------------------------------------------------
# SQLite cache
# --------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY, title TEXT, description TEXT, privacy TEXT,
    item_count INTEGER, published_at TEXT, synced_at TEXT
);
CREATE TABLE IF NOT EXISTS playlist_items (
    item_id TEXT PRIMARY KEY, playlist_id TEXT, video_id TEXT, title TEXT,
    channel_title TEXT, position INTEGER, published_at TEXT, synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_playlist ON playlist_items(playlist_id);
CREATE INDEX IF NOT EXISTS idx_items_video ON playlist_items(video_id);
CREATE TABLE IF NOT EXISTS quota_log (
    day TEXT, method TEXT, units INTEGER, at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_day ON quota_log(day);
"""


def db() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def resolve_playlist(value: str) -> str:
    """Accept an ID, a URL, or a cached title (case-insensitive substring)."""
    direct = extract_playlist_id(value)
    if direct:
        return direct
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title FROM playlists WHERE lower(title) LIKE ?", (f"%{value.lower()}%",)
        ).fetchall()
    if not rows:
        die(f"no playlist matched {value!r}. Run `ytx sync` first, or pass an ID.")
    if len(rows) > 1:
        options = "\n".join(f"  {r['id']}  {r['title']}" for r in rows[:10])
        die(f"{value!r} matched {len(rows)} playlists:\n{options}")
    return rows[0]["id"]


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_auth_login(args: argparse.Namespace) -> None:
    scopes = [SCOPE_WRITE] if args.write else [SCOPE_READ]
    do_login(scopes, args.open)


def cmd_auth_status(args: argparse.Namespace) -> None:
    if not tokens_exist():
        print(json.dumps({"authenticated": False, "token_store": token_store()}, indent=2))
        return
    tokens = load_tokens()
    expires = datetime.fromtimestamp(tokens.get("expires_at", 0), timezone.utc)
    channel = api("channels.list", "channels", {"part": "snippet", "mine": "true"})
    items = channel.get("items", [])
    print(
        json.dumps(
            {
                "authenticated": True,
                "channel": items[0]["snippet"]["title"] if items else None,
                "channel_id": items[0]["id"] if items else None,
                "scopes": tokens.get("scopes", []),
                "can_write": SCOPE_WRITE in tokens.get("scopes", []),
                "access_token_expires": expires.isoformat(timespec="seconds"),
                "obtained_at": tokens.get("obtained_at"),
                "token_store": token_store(),
            },
            indent=2,
        )
    )


def cmd_auth_logout(args: argparse.Namespace) -> None:
    if not tokens_exist():
        print("Already logged out.", file=sys.stderr)
        return
    tokens = load_tokens()
    post_form(REVOKE_URI, {"token": tokens.get("refresh_token", "")})
    keychain_delete()
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    print("Revoked and removed stored tokens.", file=sys.stderr)


def cmd_playlists_list(args: argparse.Namespace) -> None:
    if args.cached:
        with db() as conn:
            rows = [dict(r) for r in conn.execute("SELECT id, title, privacy, item_count FROM playlists ORDER BY title")]
    else:
        rows = [
            {
                "id": item["id"],
                "title": item["snippet"]["title"],
                "privacy": item.get("status", {}).get("privacyStatus", ""),
                "item_count": item.get("contentDetails", {}).get("itemCount", 0),
            }
            for item in api_pages(
                "playlists.list",
                "playlists",
                {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 50},
            )
        ]
    emit(rows, [("id", "ID", 36), ("title", "TITLE", 44), ("privacy", "PRIVACY", 8), ("item_count", "N", 5)], args.json)


def cmd_playlists_show(args: argparse.Namespace) -> None:
    playlist_id = resolve_playlist(args.playlist)
    if args.cached:
        with db() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT position, video_id, title, channel_title FROM playlist_items "
                    "WHERE playlist_id=? ORDER BY position",
                    (playlist_id,),
                )
            ]
    else:
        rows = [
            {
                "position": item["snippet"].get("position", 0),
                "video_id": item["snippet"]["resourceId"].get("videoId", ""),
                "title": item["snippet"]["title"],
                "channel_title": item["snippet"].get("videoOwnerChannelTitle", ""),
                "item_id": item["id"],
            }
            for item in api_pages(
                "playlistItems.list",
                "playlistItems",
                {"part": "snippet", "playlistId": playlist_id, "maxResults": 50},
            )
        ]
    emit(
        rows,
        [("position", "#", 4), ("video_id", "VIDEO", 11), ("title", "TITLE", 52), ("channel_title", "CHANNEL", 24)],
        args.json,
    )


def cmd_playlists_create(args: argparse.Namespace) -> None:
    payload = api(
        "playlists.insert",
        "playlists",
        {"part": "snippet,status"},
        {
            "snippet": {"title": args.title, "description": args.description or ""},
            "status": {"privacyStatus": args.privacy},
        },
    )
    print(json.dumps({"id": payload["id"], "title": payload["snippet"]["title"]}, indent=2))


def cmd_playlists_rename(args: argparse.Namespace) -> None:
    playlist_id = resolve_playlist(args.playlist)
    current = api("playlists.list", "playlists", {"part": "snippet,status", "id": playlist_id})
    if not current.get("items"):
        die(f"playlist {playlist_id} not found")
    snippet = current["items"][0]["snippet"]
    payload = api(
        "playlists.update",
        "playlists",
        {"part": "snippet"},
        {
            "id": playlist_id,
            "snippet": {
                "title": args.title,
                "description": args.description if args.description is not None else snippet.get("description", ""),
            },
        },
    )
    print(json.dumps({"id": payload["id"], "title": payload["snippet"]["title"]}, indent=2))


def cmd_playlists_delete(args: argparse.Namespace) -> None:
    playlist_id = resolve_playlist(args.playlist)
    if not args.yes:
        die(f"refusing to delete {playlist_id} without --yes")
    api("playlists.delete", "playlists", {"id": playlist_id})
    with db() as conn:
        conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    print(json.dumps({"deleted": playlist_id}, indent=2))


def cmd_items_add(args: argparse.Namespace) -> None:
    playlist_id = resolve_playlist(args.playlist)
    added = []
    for raw in args.videos:
        video_id = extract_video_id(raw)
        payload = api(
            "playlistItems.insert",
            "playlistItems",
            {"part": "snippet"},
            {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )
        added.append({"item_id": payload["id"], "video_id": video_id, "title": payload["snippet"]["title"]})
    print(json.dumps(added, indent=2, ensure_ascii=False))


def cmd_items_remove(args: argparse.Namespace) -> None:
    playlist_id = resolve_playlist(args.playlist)
    wanted = {extract_video_id(v) for v in args.videos}
    removed = []
    for item in api_pages("playlistItems.list", "playlistItems", {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}):
        video_id = item["snippet"]["resourceId"].get("videoId")
        if video_id in wanted:
            api("playlistItems.delete", "playlistItems", {"id": item["id"]})
            removed.append({"video_id": video_id, "title": item["snippet"]["title"]})
    if not removed:
        die("none of those videos were in that playlist")
    print(json.dumps(removed, indent=2, ensure_ascii=False))


def cmd_items_move(args: argparse.Namespace) -> None:
    playlist_id = resolve_playlist(args.playlist)
    video_id = extract_video_id(args.video)
    target = next(
        (
            item
            for item in api_pages(
                "playlistItems.list", "playlistItems", {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
            )
            if item["snippet"]["resourceId"].get("videoId") == video_id
        ),
        None,
    )
    if target is None:
        die(f"{video_id} is not in that playlist")
    api(
        "playlistItems.update",
        "playlistItems",
        {"part": "snippet"},
        {
            "id": target["id"],
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
                "position": args.position,
            },
        },
    )
    print(json.dumps({"moved": video_id, "to_position": args.position}, indent=2))


def cmd_liked(args: argparse.Namespace) -> None:
    channel = api("channels.list", "channels", {"part": "contentDetails", "mine": "true"})
    if not channel.get("items"):
        die("no channel found for this account")
    likes_id = channel["items"][0]["contentDetails"]["relatedPlaylists"].get("likes")
    if not likes_id:
        die("this account exposes no likes playlist")
    rows = []
    for item in api_pages(
        "playlistItems.list", "playlistItems", {"part": "snippet", "playlistId": likes_id, "maxResults": 50},
        max_pages=args.pages,
    ):
        rows.append(
            {
                "video_id": item["snippet"]["resourceId"].get("videoId", ""),
                "title": item["snippet"]["title"],
                "channel_title": item["snippet"].get("videoOwnerChannelTitle", ""),
            }
        )
    emit(rows, [("video_id", "VIDEO", 11), ("title", "TITLE", 56), ("channel_title", "CHANNEL", 26)], args.json)


def fetch_subscriptions(pages: int = 10) -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "channel_id": item["snippet"]["resourceId"]["channelId"],
            "title": item["snippet"]["title"],
            "subscribed_at": item["snippet"].get("publishedAt", ""),
        }
        for item in api_pages(
            "subscriptions.list",
            "subscriptions",
            {"part": "snippet", "mine": "true", "maxResults": 50, "order": "alphabetical"},
            max_pages=pages,
        )
    ]


def cmd_subs_list(args: argparse.Namespace) -> None:
    emit(
        fetch_subscriptions(args.pages),
        [("subscribed_at", "SUBSCRIBED", 20), ("title", "TITLE", 40), ("channel_id", "CHANNEL ID", 26)],
        args.json,
    )


def cmd_subs_remove(args: argparse.Namespace) -> None:
    subscriptions = fetch_subscriptions()
    removed = []
    for wanted in args.channels:
        needle = wanted.strip().lower()
        matches = [
            s
            for s in subscriptions
            if s["channel_id"].lower() == needle or needle in s["title"].lower()
        ]
        if not matches:
            die(f"no subscription matched {wanted!r}")
        if len(matches) > 1:
            options = "\n".join(f"  {m['channel_id']}  {m['title']}" for m in matches)
            die(f"{wanted!r} matched {len(matches)} subscriptions:\n{options}")
        target = matches[0]
        api("subscriptions.delete", "subscriptions", {"id": target["id"]})
        removed.append({"channel_id": target["channel_id"], "title": target["title"]})
    print(json.dumps(removed, indent=2, ensure_ascii=False))


def cmd_search(args: argparse.Namespace) -> None:
    if not args.yes:
        print(
            f"search.list costs {QUOTA_COSTS['search.list']} units "
            f"({DAILY_QUOTA // QUOTA_COSTS['search.list']} per day). Pass --yes to run it.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    payload = api(
        "search.list",
        "search",
        {"part": "snippet", "q": args.query, "type": args.type, "maxResults": args.limit},
    )
    rows = []
    for item in payload.get("items", []):
        ident = item["id"]
        rows.append(
            {
                "id": ident.get("videoId") or ident.get("playlistId") or ident.get("channelId", ""),
                "title": item["snippet"]["title"],
                "channel_title": item["snippet"].get("channelTitle", ""),
            }
        )
    emit(rows, [("id", "ID", 26), ("title", "TITLE", 50), ("channel_title", "CHANNEL", 24)], args.json)


def cmd_video(args: argparse.Namespace) -> None:
    ids = [extract_video_id(v) for v in args.videos]
    rows = []
    for batch in chunked(ids, 50):
        payload = api(
            "videos.list",
            "videos",
            {"part": "snippet,contentDetails,statistics", "id": ",".join(batch)},
        )
        for item in payload.get("items", []):
            rows.append(
                {
                    "id": item["id"],
                    "title": item["snippet"]["title"],
                    "channel_title": item["snippet"]["channelTitle"],
                    "duration": item["contentDetails"]["duration"],
                    "views": item.get("statistics", {}).get("viewCount", ""),
                    "published_at": item["snippet"]["publishedAt"],
                }
            )
    emit(
        rows,
        [("id", "ID", 11), ("title", "TITLE", 46), ("channel_title", "CHANNEL", 22), ("duration", "LEN", 10), ("views", "VIEWS", 10)],
        args.json,
    )


def cmd_sync(args: argparse.Namespace) -> None:
    stamp = now_iso()
    playlists = list(
        api_pages(
            "playlists.list",
            "playlists",
            {"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 50},
        )
    )
    with db() as conn:
        for item in playlists:
            conn.execute(
                "INSERT OR REPLACE INTO playlists(id,title,description,privacy,item_count,published_at,synced_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    item["id"],
                    item["snippet"]["title"],
                    item["snippet"].get("description", ""),
                    item.get("status", {}).get("privacyStatus", ""),
                    item.get("contentDetails", {}).get("itemCount", 0),
                    item["snippet"].get("publishedAt", ""),
                    stamp,
                ),
            )

    total_items = 0
    if not args.playlists_only:
        for item in playlists:
            playlist_id = item["id"]
            entries = list(
                api_pages(
                    "playlistItems.list",
                    "playlistItems",
                    {"part": "snippet", "playlistId": playlist_id, "maxResults": 50},
                )
            )
            with db() as conn:
                conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
                for entry in entries:
                    snippet = entry["snippet"]
                    conn.execute(
                        "INSERT OR REPLACE INTO playlist_items"
                        "(item_id,playlist_id,video_id,title,channel_title,position,published_at,synced_at)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (
                            entry["id"],
                            playlist_id,
                            snippet["resourceId"].get("videoId", ""),
                            snippet["title"],
                            snippet.get("videoOwnerChannelTitle", ""),
                            snippet.get("position", 0),
                            snippet.get("publishedAt", ""),
                            stamp,
                        ),
                    )
            total_items += len(entries)
            print(f"  {item['snippet']['title'][:50]:52} {len(entries):>4} items", file=sys.stderr)

    print(
        json.dumps({"playlists": len(playlists), "items": total_items, "synced_at": stamp, "cache": str(CACHE_DB)}, indent=2)
    )


def cmd_db_query(args: argparse.Namespace) -> None:
    statement = args.sql.strip()
    if not re.match(r"^(select|with)\b", statement, re.IGNORECASE):
        die("only SELECT/WITH statements are allowed here")
    with db() as conn:
        rows = [dict(r) for r in conn.execute(statement)]
    print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))


def cmd_quota(args: argparse.Namespace) -> None:
    day = quota_day()
    with db() as conn:
        total = conn.execute("SELECT COALESCE(SUM(units),0) AS u FROM quota_log WHERE day=?", (day,)).fetchone()["u"]
        breakdown = [
            dict(r)
            for r in conn.execute(
                "SELECT method, COUNT(*) AS calls, SUM(units) AS units FROM quota_log WHERE day=?"
                " GROUP BY method ORDER BY units DESC",
                (day,),
            )
        ]
    print(
        json.dumps(
            {
                "day_pacific": day,
                "used": total,
                "limit": DAILY_QUOTA,
                "remaining": DAILY_QUOTA - total,
                "by_method": breakdown,
            },
            indent=2,
        )
    )


# --------------------------------------------------------------------------
# argument wiring
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ytx", description="Quota-aware CLI for the YouTube Data API v3.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Shared by every table-producing command so `--json` works after the
    # subcommand, where people actually type it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="force JSON output even on a TTY")

    auth = subparsers.add_parser("auth", help="authentication").add_subparsers(dest="auth_command", required=True)
    login = auth.add_parser("login", help="run the OAuth loopback flow")
    login.add_argument("--write", action="store_true", help="request youtube.force-ssl instead of read-only")
    login.add_argument(
        "--open",
        action="store_true",
        help="also launch the default browser (off by default so nothing steals focus)",
    )
    login.set_defaults(func=cmd_auth_login)
    auth.add_parser("status", help="show token and channel state").set_defaults(func=cmd_auth_status)
    auth.add_parser("logout", help="revoke and delete local tokens").set_defaults(func=cmd_auth_logout)

    playlists = subparsers.add_parser("playlists", help="playlist operations").add_subparsers(
        dest="playlists_command", required=True
    )
    pl_list = playlists.add_parser("list", help="list your playlists", parents=[common])
    pl_list.add_argument("--cached", action="store_true", help="read the local cache (0 quota)")
    pl_list.set_defaults(func=cmd_playlists_list)
    pl_show = playlists.add_parser("show", help="list the contents of one playlist", parents=[common])
    pl_show.add_argument("playlist", help="playlist ID, URL, or cached title fragment")
    pl_show.add_argument("--cached", action="store_true", help="read the local cache (0 quota)")
    pl_show.set_defaults(func=cmd_playlists_show)
    pl_create = playlists.add_parser("create", help="create a playlist")
    pl_create.add_argument("title")
    pl_create.add_argument("--description")
    pl_create.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    pl_create.set_defaults(func=cmd_playlists_create)
    pl_rename = playlists.add_parser("rename", help="change a playlist title/description")
    pl_rename.add_argument("playlist")
    pl_rename.add_argument("title")
    pl_rename.add_argument("--description")
    pl_rename.set_defaults(func=cmd_playlists_rename)
    pl_delete = playlists.add_parser("delete", help="delete a playlist")
    pl_delete.add_argument("playlist")
    pl_delete.add_argument("--yes", action="store_true", help="required confirmation")
    pl_delete.set_defaults(func=cmd_playlists_delete)

    items = subparsers.add_parser("items", help="playlist membership").add_subparsers(dest="items_command", required=True)
    it_add = items.add_parser("add", help="add videos to a playlist")
    it_add.add_argument("playlist")
    it_add.add_argument("videos", nargs="+", help="video IDs or URLs")
    it_add.set_defaults(func=cmd_items_add)
    it_remove = items.add_parser("remove", help="remove videos from a playlist")
    it_remove.add_argument("playlist")
    it_remove.add_argument("videos", nargs="+")
    it_remove.set_defaults(func=cmd_items_remove)
    it_move = items.add_parser("move", help="reposition a video in a playlist")
    it_move.add_argument("playlist")
    it_move.add_argument("video")
    it_move.add_argument("--position", type=int, required=True)
    it_move.set_defaults(func=cmd_items_move)

    liked = subparsers.add_parser("liked", help="list liked videos", parents=[common])
    liked.add_argument("--pages", type=int, default=5, help="pages of 50 to fetch (default 5)")
    liked.set_defaults(func=cmd_liked)

    subs = subparsers.add_parser("subs", help="subscriptions").add_subparsers(
        dest="subs_command", required=True
    )
    subs_list = subs.add_parser("list", help="list your subscriptions", parents=[common])
    subs_list.add_argument("--pages", type=int, default=5)
    subs_list.set_defaults(func=cmd_subs_list)
    subs_remove = subs.add_parser("remove", help="unsubscribe from channels")
    subs_remove.add_argument("channels", nargs="+", help="channel IDs or title fragments")
    subs_remove.set_defaults(func=cmd_subs_remove)

    search = subparsers.add_parser("search", help="search YouTube (100 units per call)", parents=[common])
    search.add_argument("query")
    search.add_argument("--type", choices=["video", "channel", "playlist"], default="video")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--yes", action="store_true", help="acknowledge the 100-unit cost")
    search.set_defaults(func=cmd_search)

    video = subparsers.add_parser("video", help="metadata for one or more videos", parents=[common])
    video.add_argument("videos", nargs="+")
    video.set_defaults(func=cmd_video)

    sync = subparsers.add_parser("sync", help="mirror playlists + contents into SQLite")
    sync.add_argument("--playlists-only", action="store_true", help="skip item contents")
    sync.set_defaults(func=cmd_sync)

    database = subparsers.add_parser("db", help="query the local cache").add_subparsers(dest="db_command", required=True)
    query = database.add_parser("query", help="run a read-only SQL query against the cache")
    query.add_argument("sql")
    query.set_defaults(func=cmd_db_query)

    subparsers.add_parser("quota", help="today's quota spend").set_defaults(func=cmd_quota)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
