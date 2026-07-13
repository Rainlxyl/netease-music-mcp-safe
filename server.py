#!/usr/bin/env python3
"""A small, authenticated Streamable HTTP MCP server for NetEase Cloud Music.

Derived from Vael-KY/netease-music-mcp (MIT). This version adds authentication,
read-only-by-default behavior, safer networking defaults, validation and tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from typing import Any


LOG = logging.getLogger("netease_music_mcp")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

NETEASE_COOKIE = os.environ.get("NETEASE_COOKIE", "").strip()
ACCESS_TOKEN = os.environ.get("MCP_ACCESS_TOKEN", "").strip()
HOST = os.environ.get("MCP_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("MCP_PORT") or os.environ.get("PORT") or "3456")
READ_ONLY = os.environ.get("MCP_READ_ONLY", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ALLOWED_ORIGIN = os.environ.get("MCP_ALLOWED_ORIGIN", "").strip()
MAX_REQUEST_BYTES = int(os.environ.get("MCP_MAX_REQUEST_BYTES", "1048576"))
PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")
OAUTH_PASSWORD = os.environ.get("MCP_OAUTH_PASSWORD", "").strip()
OAUTH_SCOPE = "netease.read" if READ_ONLY else "netease.read netease.write"
SESSION_ID = str(uuid.uuid4())
USED_AUTHORIZATION_CODES: dict[str, int] = {}
USED_CODES_LOCK = threading.Lock()
FAILED_LOGINS: dict[str, list[int]] = {}
FAILED_LOGINS_LOCK = threading.Lock()

READ_TOOL_NAMES = {
    "search_song",
    "list_my_playlists",
    "get_playlist_songs",
    "get_play_history",
    "daily_recommend",
}
WRITE_TOOL_NAMES = {
    "create_playlist",
    "add_to_playlist",
    "remove_from_playlist",
    "like_song",
}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


READ_TOOLS = [
    _tool(
        "search_song",
        "Search NetEase Cloud Music. Read-only; it does not start playback or modify the account.",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        ["query"],
    ),
    _tool("list_my_playlists", "List playlists owned or collected by the logged-in user."),
    _tool(
        "get_playlist_songs",
        "List up to 50 songs in a playlist.",
        {"playlist_id": {"type": "integer", "minimum": 1}},
        ["playlist_id"],
    ),
    _tool(
        "get_play_history",
        "Get recent NetEase listening history.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "all_time": {"type": "boolean", "default": False},
        },
    ),
    _tool("daily_recommend", "Get today's personalized song recommendations."),
]

WRITE_TOOLS = [
    _tool(
        "create_playlist",
        "Create a playlist. This changes the NetEase account.",
        {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": {"type": "string", "maxLength": 1000},
            "privacy": {"type": "integer", "enum": [0, 10], "default": 10},
        },
        ["name"],
    ),
    _tool(
        "add_to_playlist",
        "Add one or more song IDs to a playlist. This changes the NetEase account.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "song_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "maxItems": 50,
            },
        },
        ["playlist_id", "song_ids"],
    ),
    _tool(
        "remove_from_playlist",
        "Remove one or more song IDs from a playlist. This is destructive.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "song_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "maxItems": 50,
            },
        },
        ["playlist_id", "song_ids"],
    ),
    _tool(
        "like_song",
        "Like or unlike a song. This changes the NetEase account.",
        {
            "song_id": {"type": "integer", "minimum": 1},
            "like": {"type": "boolean", "default": True},
        },
        ["song_id"],
    ),
]


class NetEaseError(RuntimeError):
    pass


def _require_cookie() -> None:
    if not NETEASE_COOKIE or "MUSIC_U=" not in NETEASE_COOKIE:
        raise NetEaseError("NetEase credentials are not configured.")


def netease_request(url: str, data: dict[str, Any] | str | None = None) -> dict[str, Any]:
    _require_cookie()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://music.163.com/",
        "Cookie": NETEASE_COOKIE,
        "Content-Type": "application/x-www-form-urlencoded" if data is not None else "application/json",
    }
    encoded: bytes | None = None
    if isinstance(data, dict):
        encoded = urllib.parse.urlencode(data).encode()
    elif isinstance(data, str):
        encoded = data.encode()
    request = urllib.request.Request(url, data=encoded, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(2_000_000).decode("utf-8")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise NetEaseError("NetEase returned an unexpected response.")
            return result
    except urllib.error.HTTPError as exc:
        raise NetEaseError(f"NetEase request failed with HTTP {exc.code}.") from None
    except urllib.error.URLError:
        raise NetEaseError("NetEase could not be reached.") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NetEaseError("NetEase returned an unreadable response.") from None


def get_csrf() -> str:
    for part in NETEASE_COOKIE.split(";"):
        part = part.strip()
        if part.startswith("__csrf="):
            return part.split("=", 1)[1]
    return ""


def get_uid() -> int:
    response = netease_request("https://music.163.com/api/nuser/account/get")
    profile = response.get("profile") or {}
    account = response.get("account") or {}
    uid = profile.get("userId") or account.get("id")
    if not isinstance(uid, int):
        raise NetEaseError("Could not identify the NetEase user. The cookie may have expired.")
    return uid


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer.")
    return value


def _song_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError("song_ids must contain between 1 and 50 IDs.")
    return [_positive_int(item, "song_id") for item in value]


def search_song(query: Any, limit: Any = 5) -> str:
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("query must be between 1 and 200 characters.")
    limit = _positive_int(limit, "limit")
    limit = min(limit, 10)
    url = (
        "https://music.163.com/api/search/get?s="
        + urllib.parse.quote(query.strip())
        + "&type=1&limit="
        + str(limit)
    )
    songs = netease_request(url).get("result", {}).get("songs", [])
    if not songs:
        return f"No results for {query.strip()!r}."
    lines = []
    for index, song in enumerate(songs[:limit], 1):
        artists = ", ".join(a.get("name", "") for a in song.get("artists", []))
        lines.append(f"{index}. {song.get('name', '')} - {artists} (ID:{song.get('id', '')})")
    return "\n".join(lines)


def list_my_playlists() -> str:
    uid = get_uid()
    response = netease_request(
        f"https://music.163.com/api/user/playlist?uid={uid}&limit=50&offset=0"
    )
    playlists = response.get("playlist", [])
    if not playlists:
        return "No playlists found."
    lines = []
    for playlist in playlists:
        creator = playlist.get("creator") or {}
        ownership = "mine" if creator.get("userId") == uid else "collected"
        lines.append(
            f"ID:{playlist.get('id', '')} | {playlist.get('name', '')} | "
            f"{playlist.get('trackCount', 0)} songs ({ownership})"
        )
    return "\n".join(lines)


def get_playlist_songs(playlist_id: Any) -> str:
    playlist_id = _positive_int(playlist_id, "playlist_id")
    response = netease_request(
        f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}"
    )
    playlist = response.get("playlist") or {}
    tracks = playlist.get("tracks") or []
    if not tracks and playlist.get("trackIds"):
        ids = [item["id"] for item in playlist["trackIds"][:50] if "id" in item]
        tracks = netease_request(
            "https://music.163.com/api/song/detail?ids=" + urllib.parse.quote(json.dumps(ids))
        ).get("songs", [])
    if not tracks:
        return f"Playlist {playlist_id} is empty or unavailable."
    lines = [f"Playlist: {playlist.get('name', '')} ({len(tracks[:50])} shown)"]
    for index, track in enumerate(tracks[:50], 1):
        artists = track.get("ar", track.get("artists", [])) or []
        artist_text = ", ".join(a.get("name", "") for a in artists)
        lines.append(
            f"{index}. {track.get('name', '')} - {artist_text} (ID:{track.get('id', '')})"
        )
    return "\n".join(lines)


def get_play_history(limit: Any = 30, all_time: Any = False) -> str:
    limit = min(_positive_int(limit, "limit"), 100)
    if not isinstance(all_time, bool):
        raise ValueError("all_time must be a boolean.")
    uid = get_uid()
    record_type = "0" if all_time else "1"
    response = netease_request(
        f"https://music.163.com/api/v1/play/record?uid={uid}&type={record_type}&limit={limit}"
    )
    records = response.get("allData" if all_time else "weekData") or []
    if not records:
        return "No play history found."
    lines = ["Recent play history:"]
    for index, record in enumerate(records[:limit], 1):
        song = record.get("song") or {}
        artists = song.get("ar", song.get("artists", [])) or []
        artist_text = ", ".join(a.get("name", "") for a in artists)
        count = record.get("playCount", record.get("score", ""))
        lines.append(
            f"{index}. {song.get('name', '')} - {artist_text} "
            f"(plays:{count}, ID:{song.get('id', '')})"
        )
    return "\n".join(lines)


def daily_recommend() -> str:
    csrf = get_csrf()
    response = netease_request(
        "https://music.163.com/api/v3/discovery/recommend/songs?csrf_token=" + csrf,
        data="{}",
    )
    songs = (response.get("data") or {}).get("dailySongs", [])
    if not songs:
        return "Could not fetch daily recommendations."
    lines = ["Today's recommendations:"]
    for index, song in enumerate(songs[:30], 1):
        artists = song.get("ar", song.get("artists", [])) or []
        artist_text = ", ".join(a.get("name", "") for a in artists)
        reason = f" [{song['reason']}]" if song.get("reason") else ""
        lines.append(
            f"{index}. {song.get('name', '')} - {artist_text} "
            f"(ID:{song.get('id', '')}){reason}"
        )
    return "\n".join(lines)


def create_playlist(name: Any, description: Any = "", privacy: Any = 10) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("name must be between 1 and 80 characters.")
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError("description must be a string up to 1000 characters.")
    if privacy not in (0, 10):
        raise ValueError("privacy must be 0 (public) or 10 (private).")
    response = netease_request(
        "https://music.163.com/api/playlist/create?csrf_token=" + get_csrf(),
        data={"name": name.strip(), "privacy": str(privacy), "type": "NORMAL", "description": description},
    )
    if response.get("code") != 200:
        raise NetEaseError(response.get("message") or "Playlist creation failed.")
    playlist = response.get("playlist") or {}
    return f"Created playlist {name.strip()!r} (ID:{playlist.get('id', '')})."


def manipulate_playlist(operation: str, playlist_id: Any, song_ids: Any) -> str:
    playlist_id = _positive_int(playlist_id, "playlist_id")
    ids = _song_ids(song_ids)
    response = netease_request(
        "https://music.163.com/api/playlist/manipulate/tracks?csrf_token=" + get_csrf(),
        data={"op": operation, "pid": str(playlist_id), "trackIds": json.dumps(ids)},
    )
    if response.get("code") == 502 and operation == "add":
        return "One or more songs were already in the playlist."
    if response.get("code") != 200:
        raise NetEaseError(response.get("message") or "Playlist update failed.")
    verb = "Added" if operation == "add" else "Removed"
    return f"{verb} {len(ids)} song(s) {'to' if operation == 'add' else 'from'} playlist {playlist_id}."


def like_song(song_id: Any, like: Any = True) -> str:
    song_id = _positive_int(song_id, "song_id")
    if not isinstance(like, bool):
        raise ValueError("like must be a boolean.")
    response = netease_request(
        "https://music.163.com/api/radio/like?alg=itembased"
        f"&trackId={song_id}&like={'true' if like else 'false'}&time=25&csrf_token={get_csrf()}"
    )
    if response.get("code") != 200:
        raise NetEaseError(response.get("message") or "Like operation failed.")
    return f"{'Liked' if like else 'Unliked'} song {song_id}."


def available_tools() -> list[dict[str, Any]]:
    return READ_TOOLS if READ_ONLY else READ_TOOLS + WRITE_TOOLS


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    if name in WRITE_TOOL_NAMES and READ_ONLY:
        raise PermissionError("Write tools are disabled. Set MCP_READ_ONLY=false to enable them.")
    if name == "search_song":
        return search_song(arguments.get("query"), arguments.get("limit", 5))
    if name == "list_my_playlists":
        return list_my_playlists()
    if name == "get_playlist_songs":
        return get_playlist_songs(arguments.get("playlist_id"))
    if name == "get_play_history":
        return get_play_history(arguments.get("limit", 30), arguments.get("all_time", False))
    if name == "daily_recommend":
        return daily_recommend()
    if name == "create_playlist":
        return create_playlist(
            arguments.get("name"), arguments.get("description", ""), arguments.get("privacy", 10)
        )
    if name == "add_to_playlist":
        return manipulate_playlist("add", arguments.get("playlist_id"), arguments.get("song_ids"))
    if name == "remove_from_playlist":
        return manipulate_playlist("del", arguments.get("playlist_id"), arguments.get("song_ids"))
    if name == "like_song":
        return like_song(arguments.get("song_id"), arguments.get("like", True))
    raise ValueError(f"Unknown tool: {name}")


def handle_jsonrpc(body: dict[str, Any]) -> dict[str, Any] | None:
    method = body.get("method")
    request_id = body.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "netease-music-mcp-safe", "version": "3.0.0"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": available_tools()}}
    if method == "tools/call":
        params = body.get("params") or {}
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        result = call_tool(str(params.get("name", "")), arguments)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": result}]},
        }
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def oauth_enabled() -> bool:
    """Return whether browser-based OAuth is configured for remote MCP clients."""
    return bool(PUBLIC_URL and OAUTH_PASSWORD)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _oauth_key() -> bytes:
    return hmac.new(ACCESS_TOKEN.encode(), b"netease-mcp-oauth-v1", hashlib.sha256).digest()


def _sign_claims(claims: dict[str, Any]) -> str:
    payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64url(hmac.new(_oauth_key(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature


def _verify_claims(token: str, expected_type: str) -> dict[str, Any] | None:
    try:
        payload, signature = token.split(".", 1)
        expected = _b64url(hmac.new(_oauth_key(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_b64url_decode(payload))
        if not isinstance(claims, dict) or claims.get("typ") != expected_type:
            return None
        if int(claims.get("exp", 0)) < int(time.time()):
            return None
        return claims
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _valid_redirect_uri(uri: str) -> bool:
    if len(uri) > 2048:
        return False
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if parsed.fragment or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def register_oauth_client(payload: dict[str, Any]) -> dict[str, Any]:
    redirect_uris = payload.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not 1 <= len(redirect_uris) <= 10
        or any(not isinstance(uri, str) or not _valid_redirect_uri(uri) for uri in redirect_uris)
    ):
        raise ValueError("redirect_uris must contain valid HTTPS or loopback callback URLs")
    now = int(time.time())
    client_id = _sign_claims(
        {
            "typ": "client",
            "redirect_uris": redirect_uris,
            "iat": now,
            "exp": now + 365 * 24 * 60 * 60,
            "jti": secrets.token_urlsafe(12),
        }
    )
    return {
        "client_id": client_id,
        "client_id_issued_at": now,
        "token_endpoint_auth_method": "none",
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


def validate_authorization_request(params: dict[str, str]) -> tuple[dict[str, Any], str]:
    client_id = params.get("client_id", "")
    client = _verify_claims(client_id, "client")
    if client is None:
        raise ValueError("invalid client_id")
    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri not in client.get("redirect_uris", []):
        raise ValueError("redirect_uri is not registered")
    if params.get("response_type") != "code":
        raise ValueError("response_type must be code")
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        raise ValueError("PKCE with S256 is required")
    resource = params.get("resource", PUBLIC_URL + "/mcp")
    if resource.rstrip("/") != (PUBLIC_URL + "/mcp").rstrip("/"):
        raise ValueError("invalid resource")
    requested = set(params.get("scope", "netease.read").split())
    allowed = set(OAUTH_SCOPE.split())
    if not requested or not requested.issubset(allowed):
        raise ValueError("invalid scope")
    return client, " ".join(sorted(requested))


def issue_authorization_code(params: dict[str, str]) -> str:
    _, scope = validate_authorization_request(params)
    now = int(time.time())
    return _sign_claims(
        {
            "typ": "code",
            "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"],
            "code_challenge": params["code_challenge"],
            "scope": scope,
            "aud": PUBLIC_URL + "/mcp",
            "iat": now,
            "exp": now + 300,
            "jti": secrets.token_urlsafe(16),
        }
    )


def _mark_code_used(jti: str, exp: int) -> bool:
    now = int(time.time())
    with USED_CODES_LOCK:
        for key, expiry in list(USED_AUTHORIZATION_CODES.items()):
            if expiry < now:
                USED_AUTHORIZATION_CODES.pop(key, None)
        if jti in USED_AUTHORIZATION_CODES:
            return False
        USED_AUTHORIZATION_CODES[jti] = exp
    return True


def _login_allowed(address: str) -> bool:
    cutoff = int(time.time()) - 15 * 60
    with FAILED_LOGINS_LOCK:
        attempts = [stamp for stamp in FAILED_LOGINS.get(address, []) if stamp >= cutoff]
        FAILED_LOGINS[address] = attempts
        return len(attempts) < 5


def _record_failed_login(address: str) -> None:
    with FAILED_LOGINS_LOCK:
        FAILED_LOGINS.setdefault(address, []).append(int(time.time()))


def _clear_failed_logins(address: str) -> None:
    with FAILED_LOGINS_LOCK:
        FAILED_LOGINS.pop(address, None)


def _token_pair(client_id: str, scope: str, include_refresh: bool = True) -> dict[str, Any]:
    now = int(time.time())
    access_token = _sign_claims(
        {
            "typ": "access",
            "client_id": client_id,
            "scope": scope,
            "aud": PUBLIC_URL + "/mcp",
            "iat": now,
            "exp": now + 3600,
            "jti": secrets.token_urlsafe(12),
        }
    )
    response: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
    }
    if include_refresh:
        response["refresh_token"] = _sign_claims(
            {
                "typ": "refresh",
                "client_id": client_id,
                "scope": scope,
                "aud": PUBLIC_URL + "/mcp",
                "iat": now,
                "exp": now + 30 * 24 * 60 * 60,
                "jti": secrets.token_urlsafe(16),
            }
        )
    return response


def exchange_oauth_token(params: dict[str, str]) -> dict[str, Any]:
    grant_type = params.get("grant_type")
    client_id = params.get("client_id", "")
    if _verify_claims(client_id, "client") is None:
        raise ValueError("invalid_client")
    if grant_type == "authorization_code":
        claims = _verify_claims(params.get("code", ""), "code")
        if claims is None or claims.get("client_id") != client_id:
            raise ValueError("invalid_grant")
        if claims.get("redirect_uri") != params.get("redirect_uri"):
            raise ValueError("invalid_grant")
        verifier = params.get("code_verifier", "")
        if not 43 <= len(verifier) <= 128:
            raise ValueError("invalid_grant")
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        if not hmac.compare_digest(challenge, str(claims.get("code_challenge", ""))):
            raise ValueError("invalid_grant")
        if not _mark_code_used(str(claims.get("jti", "")), int(claims["exp"])):
            raise ValueError("invalid_grant")
        return _token_pair(client_id, str(claims["scope"]))
    if grant_type == "refresh_token":
        claims = _verify_claims(params.get("refresh_token", ""), "refresh")
        if claims is None or claims.get("client_id") != client_id:
            raise ValueError("invalid_grant")
        return _token_pair(client_id, str(claims["scope"]))
    raise ValueError("unsupported_grant_type")


class MCPHandler(http.server.BaseHTTPRequestHandler):
    server_version = "NetEaseMusicMCP/3.0"

    def _cors(self) -> None:
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Mcp-Session-Id")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(
        self,
        payload: dict[str, Any],
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Mcp-Session-Id", SESSION_ID)
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _html(self, content: str, status: int = 200) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid_content_length") from None
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("invalid_request_size")
        return self.rfile.read(content_length)

    def _read_form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self._read_body().decode("utf-8"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _static_token_authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and bool(supplied) and hmac.compare_digest(supplied, ACCESS_TOKEN)

    def _oauth_access_claims(self) -> dict[str, Any] | None:
        header = self.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            return None
        if not oauth_enabled():
            return None
        claims = _verify_claims(supplied, "access")
        if claims is None or claims.get("aud") != PUBLIC_URL + "/mcp":
            return None
        return claims

    def _authorized(self) -> bool:
        if self._static_token_authorized():
            return True
        claims = self._oauth_access_claims()
        return claims is not None and "netease.read" in str(claims.get("scope", "")).split()

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        headers = {}
        if oauth_enabled():
            metadata = PUBLIC_URL + "/.well-known/oauth-protected-resource"
            headers["WWW-Authenticate"] = (
                f'Bearer resource_metadata="{metadata}", scope="netease.read"'
            )
        self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED, headers)
        return False

    def _authorization_page(self, params: dict[str, str], error: str = "") -> None:
        try:
            validate_authorization_request(params)
        except ValueError as exc:
            self._html("<h1>Invalid authorization request</h1><p>" + html.escape(str(exc)) + "</p>", 400)
            return
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
            for key, value in params.items()
            if key != "password"
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        self._html(
            "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width\">"
            "<title>Authorize Rain Music Room</title><style>"
            "body{font:16px system-ui;max-width:34rem;margin:10vh auto;padding:1.5rem;color:#202124}"
            "form{display:grid;gap:1rem}input,button{font:inherit;padding:.75rem}button{cursor:pointer}"
            ".error{color:#b3261e}</style></head><body>"
            "<h1>Rain Music Room</h1>"
            "<p>Authorize read-only access to your NetEase playlists, history, search and recommendations.</p>"
            + error_html
            + '<form method="post" action="/authorize">'
            + hidden
            + '<label>Private login password <input type="password" name="password" required autocomplete="current-password"></label>'
            + '<button type="submit">Authorize once</button></form></body></html>'
        )

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            self._json({"status": "ok", "mode": "read-only" if READ_ONLY else "read-write"})
            return
        if oauth_enabled() and path in {
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            self._json(
                {
                    "resource": PUBLIC_URL + "/mcp",
                    "authorization_servers": [PUBLIC_URL],
                    "scopes_supported": OAUTH_SCOPE.split(),
                    "bearer_methods_supported": ["header"],
                },
                headers={"Cache-Control": "no-store"},
            )
            return
        if oauth_enabled() and path == "/.well-known/oauth-authorization-server":
            self._json(
                {
                    "issuer": PUBLIC_URL,
                    "authorization_endpoint": PUBLIC_URL + "/authorize",
                    "token_endpoint": PUBLIC_URL + "/token",
                    "registration_endpoint": PUBLIC_URL + "/register",
                    "scopes_supported": OAUTH_SCOPE.split(),
                    "response_types_supported": ["code"],
                    "response_modes_supported": ["query"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
                headers={"Cache-Control": "no-store"},
            )
            return
        if oauth_enabled() and path == "/authorize":
            params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            self._authorization_page(params)
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if oauth_enabled() and path == "/register":
            try:
                payload = json.loads(self._read_body())
                if not isinstance(payload, dict):
                    raise ValueError("registration body must be an object")
                self._json(register_oauth_client(payload), HTTPStatus.CREATED, {"Cache-Control": "no-store"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": "invalid_client_metadata", "error_description": str(exc)}, 400)
            return
        if oauth_enabled() and path == "/authorize":
            try:
                params = self._read_form()
                supplied = params.pop("password", "")
                validate_authorization_request(params)
                address = self.client_address[0]
                if not _login_allowed(address):
                    self._html("<h1>Too many attempts</h1><p>Try again in 15 minutes.</p>", 429)
                    return
                if not hmac.compare_digest(supplied, OAUTH_PASSWORD):
                    _record_failed_login(address)
                    self._authorization_page(params, "Incorrect password")
                    return
                _clear_failed_logins(address)
                code = issue_authorization_code(params)
                query = {"code": code}
                if params.get("state"):
                    query["state"] = params["state"]
                separator = "&" if "?" in params["redirect_uri"] else "?"
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", params["redirect_uri"] + separator + urllib.parse.urlencode(query))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except (ValueError, UnicodeDecodeError) as exc:
                self._html("<h1>Authorization failed</h1><p>" + html.escape(str(exc)) + "</p>", 400)
            return
        if oauth_enabled() and path == "/token":
            try:
                params = self._read_form()
                self._json(exchange_oauth_token(params), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            except (ValueError, UnicodeDecodeError) as exc:
                error = str(exc)
                status = HTTPStatus.UNAUTHORIZED if error == "invalid_client" else HTTPStatus.BAD_REQUEST
                self._json({"error": error}, status, {"Cache-Control": "no-store", "Pragma": "no-cache"})
            return
        if path != "/mcp":
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._require_auth():
            return
        try:
            body = json.loads(self._read_body())
            if not isinstance(body, dict):
                raise ValueError("JSON-RPC request must be an object.")
            if body.get("method") == "tools/call":
                params = body.get("params") or {}
                tool_name = str(params.get("name", "")) if isinstance(params, dict) else ""
                if tool_name in WRITE_TOOL_NAMES and not self._static_token_authorized():
                    claims = self._oauth_access_claims() or {}
                    if "netease.write" not in str(claims.get("scope", "")).split():
                        self._json(
                            {
                                "jsonrpc": "2.0",
                                "id": body.get("id"),
                                "error": {"code": -32003, "message": "netease.write scope is required"},
                            },
                            HTTPStatus.FORBIDDEN,
                        )
                        return
            if body.get("id") is None or str(body.get("method", "")).startswith("notifications/"):
                self.send_response(HTTPStatus.NO_CONTENT)
                self._cors()
                self.end_headers()
                return
            result = handle_jsonrpc(body)
            if result is None:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._json(result)
        except json.JSONDecodeError:
            self._json(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                HTTPStatus.BAD_REQUEST,
            )
        except (ValueError, PermissionError) as exc:
            self._json(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": str(exc)}},
                HTTPStatus.BAD_REQUEST,
            )
        except NetEaseError as exc:
            LOG.warning("NetEase operation failed: %s", exc)
            self._json(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": str(exc)}},
                HTTPStatus.BAD_GATEWAY,
            )
        except Exception:
            LOG.exception("Unhandled MCP request failure")
            self._json(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error"}},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.info("%s - %s", self.client_address[0], format_string % args)


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def validate_startup() -> None:
    if not ACCESS_TOKEN:
        raise SystemExit("MCP_ACCESS_TOKEN is required. Refusing to start without authentication.")
    if len(ACCESS_TOKEN) < 24:
        raise SystemExit("MCP_ACCESS_TOKEN must be at least 24 characters.")
    if ACCESS_TOKEN.lower() in {"replace_with_a_long_random_secret", "change-me", "changeme"}:
        raise SystemExit("Replace the example MCP_ACCESS_TOKEN before starting.")
    if MAX_REQUEST_BYTES < 1024:
        raise SystemExit("MCP_MAX_REQUEST_BYTES must be at least 1024.")
    if bool(PUBLIC_URL) != bool(OAUTH_PASSWORD):
        raise SystemExit("Set both MCP_PUBLIC_URL and MCP_OAUTH_PASSWORD, or neither.")
    if PUBLIC_URL and not PUBLIC_URL.startswith("https://"):
        raise SystemExit("MCP_PUBLIC_URL must use HTTPS.")
    if PUBLIC_URL:
        parsed_public_url = urllib.parse.urlsplit(PUBLIC_URL)
        if parsed_public_url.path not in {"", "/"} or parsed_public_url.query or parsed_public_url.fragment:
            raise SystemExit("MCP_PUBLIC_URL must be an HTTPS origin without a path, query or fragment.")
    if OAUTH_PASSWORD and len(OAUTH_PASSWORD) < 16:
        raise SystemExit("MCP_OAUTH_PASSWORD must be at least 16 characters.")
    if OAUTH_PASSWORD.lower() in {
        "replace_with_a_different_random_password",
        "change-me",
        "changeme",
    }:
        raise SystemExit("Replace the example MCP_OAUTH_PASSWORD before starting.")
    if OAUTH_PASSWORD and hmac.compare_digest(OAUTH_PASSWORD, ACCESS_TOKEN):
        raise SystemExit("MCP_OAUTH_PASSWORD must be different from MCP_ACCESS_TOKEN.")


def main() -> None:
    validate_startup()
    server = ThreadingHTTPServer((HOST, PORT), MCPHandler)
    LOG.info(
        "NetEase Music MCP listening on http://%s:%s/mcp (%s)",
        HOST,
        PORT,
        "read-only" if READ_ONLY else "read-write",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
