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
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from image_safety import normalize_cover_image, validate_file_reference
from persistence import PersistentStore, utc_now


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
STORAGE_PATH = os.environ.get("MCP_STORAGE_PATH", "").strip()
REQUIRE_WRITE_PREVIEW = os.environ.get("MCP_REQUIRE_WRITE_PREVIEW", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
PREVIEW_TTL_SECONDS = int(os.environ.get("MCP_PREVIEW_TTL_SECONDS", "300"))
OPERATION_RETENTION_DAYS = int(os.environ.get("MCP_OPERATION_RETENTION_DAYS", "90"))
MAX_OPERATION_LOGS = int(os.environ.get("MCP_MAX_OPERATION_LOGS", "1000"))
MAX_PENDING_PREVIEWS = int(os.environ.get("MCP_MAX_PENDING_PREVIEWS", "200"))
MAX_IMAGE_BYTES = int(os.environ.get("MCP_MAX_IMAGE_BYTES", "5242880"))
MAX_IMAGE_PIXELS = int(os.environ.get("MCP_MAX_IMAGE_PIXELS", "25000000"))
OAUTH_SCOPE = "netease.read" if READ_ONLY else "netease.read netease.write"
USED_AUTHORIZATION_CODES: dict[str, int] = {}
USED_CODES_LOCK = threading.Lock()
FAILED_LOGINS: dict[str, list[int]] = {}
FAILED_LOGINS_LOCK = threading.Lock()
STORE_INSTANCE: PersistentStore | None = None
STORE_LOCK = threading.Lock()

READ_TOOL_NAMES = {
    "search_song",
    "list_my_playlists",
    "get_playlist_songs",
    "get_song_details",
    "get_play_history",
    "get_recent_plays",
    "list_my_subscribed_podcasts",
    "get_podcast_programs",
    "search_podcasts",
    "search_podcast_programs",
    "get_recent_podcast_plays",
    "daily_recommend",
    "preview_operation",
    "get_operation_log",
    "list_interaction_notes",
}
WRITE_TOOL_NAMES = {
    "create_playlist",
    "update_playlist",
    "add_to_playlist",
    "remove_from_playlist",
    "reorder_playlist_tracks",
    "like_song",
    "undo_operation",
    "update_playlist_cover",
    "create_interaction_note",
    "update_interaction_note",
    "delete_interaction_note",
}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    read_only: bool = True,
    destructive: bool = False,
    min_properties: int | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    if min_properties is not None:
        schema["minProperties"] = min_properties
    result = {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "openWorldHint": True,
        },
    }
    if meta:
        result["_meta"] = meta
    return result


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
        "Read one page of playlist tracks. Returns pagination metadata and does not modify the account.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        ["playlist_id"],
    ),
    _tool(
        "get_song_details",
        "Read metadata for one song ID or up to 50 song IDs. Version flags use explicit upstream metadata only; the title is never guessed.",
        {
            "song_id": {"type": "integer", "minimum": 1},
            "song_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "maxItems": 50,
            },
        },
        min_properties=1,
    ),
    _tool(
        "get_play_history",
        "Read NetEase's aggregated weekly or all-time play ranking. Counts are grouped by song and are not individual play events or timestamps.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "all_time": {"type": "boolean", "default": False},
        },
    ),
    _tool(
        "get_recent_plays",
        "Read actual recent song play events in upstream order, including per-play timestamps when NetEase supplies them. The upstream endpoint supports only a limit, not time-range or offset pagination.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100}},
    ),
    _tool(
        "list_my_subscribed_podcasts",
        "List podcast/radio containers subscribed to by the logged-in user. Read-only. Public play counts are labelled as public aggregates, never as the user's listening count.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
    ),
    _tool(
        "get_podcast_programs",
        "Read one page of programs (episodes) from a podcast/radio container. Uses radio_id for the container and returns program_id plus an optional main_track_id audio carrier; it never calls either one song_id.",
        {
            "radio_id": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "order": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "default": "newest",
            },
        },
        ["radio_id"],
    ),
    _tool(
        "search_podcasts",
        "Search podcast/radio containers. Read-only; result IDs are radio_id values and public play counts are not personal listening counts.",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        ["query"],
    ),
    _tool(
        "search_podcast_programs",
        "Search podcast programs/episodes. Read-only; result IDs are program_id values. An optional main_track_id identifies the audio carrier and is not a normal song resource ID.",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        ["query"],
    ),
    _tool(
        "get_recent_podcast_plays",
        "Read the recent podcast-program resources reported by NetEase, preserving upstream order and timestamps only when supplied. This is not guaranteed to be a complete event stream and does not provide a reliable per-user play count.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
    ),
    _tool("daily_recommend", "Get today's personalized song recommendations."),
    _tool(
        "preview_operation",
        "Read-only preview for an account or private-note write. It never changes data and returns a short-lived token bound to the arguments and current resource state.",
        {
            "operation": {"type": "string", "enum": sorted(WRITE_TOOL_NAMES)},
            "arguments": {"type": "object", "additionalProperties": True},
        },
        ["operation", "arguments"],
    ),
    _tool(
        "get_operation_log",
        "Read sanitized, bounded operation audit records from persistent storage.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "operation": {"type": "string"},
            "status": {"type": "string"},
            "created_after": {"type": "string"},
            "created_before": {"type": "string"},
        },
    ),
    _tool(
        "list_interaction_notes",
        "Read private plugin-owned notes for an accessible playlist. These are not native NetEase comments.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "song_id": {"type": "integer", "minimum": 1},
            "author": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        ["playlist_id"],
    ),
]

WRITE_TOOLS = [
    _tool(
        "create_playlist",
        "Create a playlist. This changes the NetEase account.",
        {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": {"type": "string", "maxLength": 1000},
            "privacy": {"type": "integer", "enum": [0, 10], "default": 10},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["name"],
        read_only=False,
    ),
    _tool(
        "update_playlist",
        "Update the name and/or description of a playlist owned by the user. At least one of name or description must be provided. This changes the NetEase account.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "description": {"type": "string", "maxLength": 1000},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["playlist_id"],
        read_only=False,
        min_properties=2,
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
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["playlist_id", "song_ids"],
        read_only=False,
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
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["playlist_id", "song_ids"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "reorder_playlist_tracks",
        "Replace the track order of a playlist owned by the current user. Requires the complete existing set of unique song IDs in the desired order and modifies account data.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "song_ids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "maxItems": 10000,
            },
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["playlist_id", "song_ids"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "like_song",
        "Like or unlike a song. This changes the NetEase account.",
        {
            "song_id": {"type": "integer", "minimum": 1},
            "like": {"type": "boolean", "default": True},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["song_id"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "undo_operation",
        "Undo one successful reversible operation after verifying that its recorded after-state is still current. This modifies data and is itself logged.",
        {
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["operation_id", "preview_token"],
        read_only=False,
        destructive=True,
    ),
    _tool(
        "update_playlist_cover",
        "Replace an owned playlist cover with an uploaded PNG or JPEG. The image is validated, center-cropped, resized, stripped of metadata, and uploaded as JPEG. This is a destructive write and cannot be reliably undone.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "image": {
                "type": "object",
                "properties": {
                    "download_url": {"type": "string", "format": "uri"},
                    "file_id": {"type": "string"},
                    "mime_type": {"type": "string", "enum": ["image/png", "image/jpeg"]},
                    "file_name": {"type": "string"},
                },
                "required": ["download_url", "file_id"],
                "additionalProperties": False,
            },
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["playlist_id", "image", "preview_token"],
        read_only=False,
        destructive=True,
        meta={"openai/fileParams": ["image"]},
    ),
    _tool(
        "create_interaction_note",
        "Create a private plugin-owned playlist or track note in persistent storage. This does not create a native NetEase comment.",
        {
            "playlist_id": {"type": "integer", "minimum": 1},
            "song_id": {"type": "integer", "minimum": 1},
            "author": {"type": "string", "minLength": 1, "maxLength": 80},
            "content": {"type": "string", "minLength": 1, "maxLength": 2000},
            "visibility": {"type": "string", "enum": ["private"], "default": "private"},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["playlist_id", "author", "content", "preview_token"],
        read_only=False,
    ),
    _tool(
        "update_interaction_note",
        "Update a private plugin-owned note using its current version for optimistic concurrency control.",
        {
            "note_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "version": {"type": "integer", "minimum": 1},
            "author": {"type": "string", "minLength": 1, "maxLength": 80},
            "content": {"type": "string", "minLength": 1, "maxLength": 2000},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["note_id", "version", "preview_token"],
        read_only=False,
        min_properties=4,
    ),
    _tool(
        "delete_interaction_note",
        "Soft-delete a private plugin-owned note using its current version. The deletion is reversible while the audit record is retained.",
        {
            "note_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "version": {"type": "integer", "minimum": 1},
            "preview_token": {"type": "string", "minLength": 20, "maxLength": 200},
        },
        ["note_id", "version", "preview_token"],
        read_only=False,
        destructive=True,
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


def netease_binary_request(url: str, data: bytes, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        raise NetEaseError(f"NetEase image upload failed with HTTP {exc.code}.") from None
    except urllib.error.URLError:
        raise NetEaseError("NetEase image storage could not be reached.") from None
    if not payload:
        return {"code": 200}
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NetEaseError("NetEase image storage returned an unreadable response.") from None
    if not isinstance(result, dict):
        raise NetEaseError("NetEase image storage returned an unexpected response.")
    return result


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


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}.")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer.")
    return value


def _redact_secrets(message: Any) -> str:
    text = str(message)
    for secret in (NETEASE_COOKIE, ACCESS_TOKEN, OAUTH_PASSWORD):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    for cookie_name in ("MUSIC_U", "__csrf", "NMTID"):
        marker = cookie_name + "="
        start = text.find(marker)
        while start >= 0:
            value_start = start + len(marker)
            end_candidates = [
                position
                for position in (text.find(";", value_start), text.find(" ", value_start))
                if position >= 0
            ]
            value_end = min(end_candidates) if end_candidates else len(text)
            text = text[:value_start] + "[REDACTED]" + text[value_end:]
            start = text.find(marker, value_start + len("[REDACTED]"))
    return text[:500]


def _upstream_error(response: dict[str, Any], fallback: str) -> str:
    message = response.get("message") or response.get("msg")
    return _redact_secrets(message) if isinstance(message, str) and message.strip() else fallback


def _raise_for_upstream_code(response: dict[str, Any], fallback: str) -> None:
    code = response.get("code")
    if code is not None and code != 200:
        raise NetEaseError(_upstream_error(response, fallback))


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _preview_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _store() -> PersistentStore:
    global STORE_INSTANCE
    if not STORAGE_PATH:
        raise RuntimeError(
            "Persistent storage is not configured. Set MCP_STORAGE_PATH to a SQLite file on a persistent volume."
        )
    if STORE_INSTANCE is None or STORE_INSTANCE.path != os.path.abspath(STORAGE_PATH):
        with STORE_LOCK:
            if STORE_INSTANCE is None or STORE_INSTANCE.path != os.path.abspath(STORAGE_PATH):
                STORE_INSTANCE = PersistentStore(
                    STORAGE_PATH,
                    retention_days=OPERATION_RETENTION_DAYS,
                    max_operations=MAX_OPERATION_LOGS,
                    max_previews=MAX_PENDING_PREVIEWS,
                )
                STORE_INSTANCE.initialize()
    return STORE_INSTANCE


def _sanitize_value(value: Any, key: str = "") -> Any:
    lowered = key.casefold()
    sensitive_markers = (
        "cookie",
        "token",
        "password",
        "authorization",
        "secret",
        "download_url",
        "environment",
    )
    if any(marker in lowered for marker in sensitive_markers):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact_secrets(value)
    return value


def _validate_iso8601(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _milliseconds_to_iso8601(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _playlist_detail(playlist_id: int) -> dict[str, Any]:
    response = netease_request(
        f"https://music.163.com/api/v6/playlist/detail?id={playlist_id}&n=100000&s=0"
    )
    _raise_for_upstream_code(response, "Playlist lookup failed.")
    playlist = response.get("playlist")
    if not isinstance(playlist, dict):
        raise NetEaseError(f"Playlist {playlist_id} is unavailable.")
    return playlist


def _playlist_track_ids(playlist: dict[str, Any]) -> tuple[list[int], int]:
    raw_track_ids = playlist.get("trackIds")
    track_ids: list[int] = []
    if isinstance(raw_track_ids, list):
        for item in raw_track_ids:
            value = item.get("id") if isinstance(item, dict) else item
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                track_ids.append(value)

    tracks = playlist.get("tracks")
    if not track_ids and isinstance(tracks, list):
        track_ids = [
            item["id"]
            for item in tracks
            if isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and not isinstance(item.get("id"), bool)
            and item["id"] > 0
        ]

    raw_total = playlist.get("trackCount")
    total = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else len(track_ids)
    total = max(total, len(track_ids))
    if total > len(track_ids):
        raise NetEaseError(
            "NetEase did not return the complete playlist track ID list; pagination or reordering cannot be performed safely."
        )
    return track_ids, total


def _fetch_song_records(song_ids: list[int]) -> list[dict[str, Any]]:
    if not song_ids:
        return []
    response = netease_request(
        "https://music.163.com/api/v3/song/detail",
        data={
            "c": json.dumps(
                [{"id": song_id} for song_id in song_ids], separators=(",", ":")
            )
        },
    )
    _raise_for_upstream_code(response, "Song detail lookup failed.")
    songs = response.get("songs")
    if not isinstance(songs, list):
        return []
    by_id = {
        song.get("id"): song
        for song in songs
        if isinstance(song, dict) and isinstance(song.get("id"), int)
    }
    return [by_id[song_id] for song_id in song_ids if song_id in by_id]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _explicit_version_labels(song: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for field in ("tags", "entertainmentTags", "awardTags"):
        value = song.get(field)
        if isinstance(value, str) and value.strip():
            labels.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                label = item.get("name") if isinstance(item, dict) else item
                if isinstance(label, str) and label.strip():
                    labels.append(label.strip())
    return list(dict.fromkeys(labels))


def _song_summary(song: dict[str, Any]) -> dict[str, Any]:
    artists = song.get("ar", song.get("artists", []))
    if not isinstance(artists, list):
        artists = []
    return {
        "song_id": song.get("id"),
        "name": song.get("name"),
        "artists": [
            {"id": artist.get("id"), "name": artist.get("name")}
            for artist in artists
            if isinstance(artist, dict)
        ],
    }


def _song_detail_payload(song: dict[str, Any]) -> dict[str, Any]:
    summary = _song_summary(song)
    album = song.get("al", song.get("album"))
    if not isinstance(album, dict):
        album = {}
    duration_ms = song.get("dt", song.get("duration"))
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        duration_ms = None
    publish_time_ms = song.get("publishTime")
    if isinstance(publish_time_ms, bool) or not isinstance(publish_time_ms, int):
        publish_time_ms = None
    labels = _explicit_version_labels(song)
    normalized_labels = " ".join(labels).casefold()
    version_flags = {
        "live": True if "live" in normalized_labels else None,
        "remix": True if "remix" in normalized_labels else None,
        "remastered": True if "remaster" in normalized_labels else None,
        "cover": True if "cover" in normalized_labels else None,
    }
    summary.update(
        {
            "album": {
                "id": album.get("id"),
                "name": album.get("name"),
                "translated_names": _string_list(album.get("tns")),
            }
            if album
            else None,
            "duration_ms": duration_ms,
            "duration_seconds": round(duration_ms / 1000, 3) if duration_ms is not None else None,
            "publish_time_ms": publish_time_ms,
            "release_time": _milliseconds_to_iso8601(publish_time_ms),
            "aliases": _string_list(song.get("alia", song.get("alias"))),
            "translated_names": _string_list(song.get("tns")),
            "version_labels": labels,
            "version_flags": version_flags,
            "version_detection": "explicit_upstream_metadata_only; title_not_parsed",
            "upstream_version_metadata": {
                "version": song.get("version", song.get("v")),
                "song_type": song.get("t"),
                "file_type": song.get("ftype"),
                "mark": song.get("mark"),
                "origin_cover_type": song.get("originCoverType"),
                "origin_song": song.get("originSongSimpleData"),
                "resource_state": song.get("resourceState"),
            },
            "disc_number": song.get("cd"),
            "track_number": song.get("no"),
            "mv_id": song.get("mv"),
            "copyright": song.get("copyright"),
            "fee": song.get("fee"),
        }
    )
    return summary


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


def get_playlist_songs(playlist_id: Any, limit: Any = 50, offset: Any = 0) -> str:
    playlist_id = _positive_int(playlist_id, "playlist_id")
    # The reverse-engineered upstream's playlist helper slices complete
    # trackIds before requesting song details. Keep pages at 100 or fewer so
    # the follow-up detail request and MCP result remain bounded.
    limit = _bounded_int(limit, "limit", 1, 100)
    offset = _non_negative_int(offset, "offset")
    playlist = _playlist_detail(playlist_id)
    track_ids, total = _playlist_track_ids(playlist)
    page_ids = track_ids[offset : offset + limit]
    tracks = _fetch_song_records(page_ids)
    returned_ids = {
        track.get("id") for track in tracks if isinstance(track.get("id"), int)
    }
    has_next = offset + len(page_ids) < total
    payload = {
        "record_type": "playlist_track_page",
        "playlist": {
            "playlist_id": playlist_id,
            "name": playlist.get("name"),
            "total_tracks": total,
        },
        "pagination": {
            "returned": len(tracks),
            "requested_track_ids": len(page_ids),
            "limit": limit,
            "offset": offset,
            "has_next": has_next,
            "next_offset": offset + len(page_ids) if has_next else None,
        },
        "songs": [
            {"position": offset + index, **_song_summary(track)}
            for index, track in enumerate(tracks, 1)
        ],
        "missing_song_ids": [song_id for song_id in page_ids if song_id not in returned_ids],
    }
    return _json_text(payload)


def get_song_details(song_ids: Any) -> str:
    if isinstance(song_ids, int) and not isinstance(song_ids, bool):
        ids = [_positive_int(song_ids, "song_id")]
    elif isinstance(song_ids, list):
        if not 1 <= len(song_ids) <= 50:
            raise ValueError("song_ids must contain between 1 and 50 IDs.")
        ids = [_positive_int(song_id, "song_id") for song_id in song_ids]
    else:
        raise ValueError("Provide one positive song_id or a list of 1 to 50 song_ids.")
    if len(set(ids)) != len(ids):
        raise ValueError("song_ids must not contain duplicates.")
    songs = _fetch_song_records(ids)
    returned_ids = {
        song.get("id") for song in songs if isinstance(song.get("id"), int)
    }
    return _json_text(
        {
            "record_type": "song_details",
            "requested_song_ids": ids,
            "songs": [_song_detail_payload(song) for song in songs],
            "missing_song_ids": [song_id for song_id in ids if song_id not in returned_ids],
        }
    )


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
    lines = ["Aggregated play history (not individual play events):"]
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


def _aggregated_play_payload(response: dict[str, Any], limit: int) -> dict[str, Any] | None:
    records = response.get("weekData")
    period = "week"
    if not isinstance(records, list):
        records = response.get("allData")
        period = "all_time"
    if not isinstance(records, list):
        return None
    aggregated = []
    for record in records[:limit]:
        if not isinstance(record, dict):
            continue
        song = record.get("song") if isinstance(record.get("song"), dict) else {}
        aggregated.append(
            {
                **_song_summary(song),
                "play_count": record.get("playCount"),
                "score": record.get("score"),
            }
        )
    return {
        "record_type": "aggregated_play_counts",
        "period": period,
        "events": [],
        "aggregated_tracks": aggregated,
        "limitation": "The upstream response contains per-song aggregates and no per-play timestamps; it is not presented as a recent-play event stream.",
    }


def get_recent_plays(limit: Any = 100) -> str:
    limit = _bounded_int(limit, "limit", 1, 100)
    response = netease_request(
        "https://music.163.com/api/play-record/song/list",
        data={"limit": str(limit)},
    )
    _raise_for_upstream_code(response, "Recent play lookup failed.")
    data = response.get("data")
    entries = data.get("list") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        aggregate_payload = _aggregated_play_payload(response, limit)
        if aggregate_payload is not None:
            return _json_text(aggregate_payload)
        entries = []

    events = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        song = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        play_time_ms = entry.get("playTime")
        terminal = entry.get("multiTerminalInfo")
        if not isinstance(terminal, dict):
            terminal = {}
        events.append(
            {
                **_song_summary(song),
                "song_id": song.get("id", entry.get("resourceId")),
                "play_time_ms": play_time_ms
                if isinstance(play_time_ms, int) and not isinstance(play_time_ms, bool)
                else None,
                "played_at": _milliseconds_to_iso8601(play_time_ms),
                "source_device": terminal.get("osText"),
                "banned": entry.get("banned") if isinstance(entry.get("banned"), bool) else None,
            }
        )
    raw_total = data.get("total") if isinstance(data, dict) else None
    return _json_text(
        {
            "record_type": "recent_play_events",
            "order": "upstream_order_preserved",
            "returned": len(events),
            "upstream_total": raw_total if isinstance(raw_total, int) else None,
            "limit": limit,
            "events": events,
            "limitations": {
                "time_range_supported": False,
                "offset_pagination_supported": False,
                "completion_status_available": False,
            },
        }
    )


def _upstream_positive_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _upstream_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clean_search_query(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError("query must be between 1 and 200 characters.")
    return value.strip()


def _pagination_payload(
    *, limit: int, offset: int, returned: int, total: Any, has_more: Any
) -> dict[str, Any]:
    normalized_total = _upstream_int(total)
    normalized_more = has_more if isinstance(has_more, bool) else None
    if normalized_more is None and normalized_total is not None:
        normalized_more = offset + returned < normalized_total
    return {
        "returned": returned,
        "limit": limit,
        "offset": offset,
        "upstream_total": normalized_total,
        "has_next": normalized_more,
        "next_offset": offset + returned if normalized_more is True else None,
    }


def _podcast_radio_payload(radio: dict[str, Any]) -> dict[str, Any]:
    creator = radio.get("dj") if isinstance(radio.get("dj"), dict) else {}
    create_time_ms = _upstream_int(radio.get("createTime"))
    fee_scope = _upstream_int(radio.get("feeScope"))
    return {
        "resource_type": "podcast_radio",
        "radio_id": _upstream_positive_id(radio.get("id")),
        "name": radio.get("name"),
        "description": radio.get("desc", radio.get("description")),
        "cover_url": radio.get("picUrl", radio.get("coverUrl")),
        "creator": {
            "user_id": _upstream_positive_id(creator.get("userId")),
            "nickname": creator.get("nickname"),
        }
        if creator
        else None,
        "category": radio.get("category"),
        "second_category": radio.get("secondCategory"),
        "program_count": _upstream_int(radio.get("programCount")),
        "subscriber_count": _upstream_int(radio.get("subCount")),
        "public_total_play_count": _upstream_int(radio.get("playCount")),
        "public_count_semantics": (
            "NetEase public aggregate; not the current user's listening count."
        ),
        "created_time_ms": create_time_ms,
        "created_at": _milliseconds_to_iso8601(create_time_ms),
        "is_paid": fee_scope != 0 if fee_scope is not None else None,
    }


def _podcast_program_payload(program: dict[str, Any]) -> dict[str, Any]:
    radio = program.get("radio") if isinstance(program.get("radio"), dict) else {}
    creator = program.get("dj") if isinstance(program.get("dj"), dict) else {}
    main_song = program.get("mainSong") if isinstance(program.get("mainSong"), dict) else {}
    main_track_id = _upstream_positive_id(program.get("mainTrackId"))
    if main_track_id is None:
        main_track_id = _upstream_positive_id(main_song.get("id"))
    duration_ms = _upstream_int(program.get("duration"))
    if duration_ms is None:
        duration_ms = _upstream_int(main_song.get("duration"))
    create_time_ms = _upstream_int(program.get("createTime"))
    return {
        "resource_type": "podcast_program",
        "program_id": _upstream_positive_id(program.get("id")),
        "radio_id": _upstream_positive_id(radio.get("id", program.get("radioId"))),
        "main_track_id": main_track_id,
        "main_track_semantics": (
            "Audio carrier returned by NetEase; it is not exposed as a normal song_id."
            if main_track_id is not None
            else None
        ),
        "name": program.get("name"),
        "description": program.get("description", program.get("desc")),
        "cover_url": program.get("coverUrl", program.get("blurCoverUrl")),
        "radio_name": radio.get("name"),
        "creator": {
            "user_id": _upstream_positive_id(creator.get("userId")),
            "nickname": creator.get("nickname"),
        }
        if creator
        else None,
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3) if duration_ms is not None else None,
        "published_time_ms": create_time_ms,
        "published_at": _milliseconds_to_iso8601(create_time_ms),
        "serial_number": _upstream_int(program.get("serialNum")),
        "program_type": program.get("type"),
        "public_listener_count": _upstream_int(program.get("listenerCount")),
        "public_liked_count": _upstream_int(program.get("likedCount")),
        "public_comment_count": _upstream_int(program.get("commentCount")),
        "public_share_count": _upstream_int(program.get("shareCount")),
        "public_count_semantics": (
            "NetEase public aggregates; none are the current user's personal play count."
        ),
    }


def list_my_subscribed_podcasts(limit: Any = 30, offset: Any = 0) -> str:
    limit = _bounded_int(limit, "limit", 1, 100)
    offset = _non_negative_int(offset, "offset")
    response = netease_request(
        "https://music.163.com/api/djradio/get/subed",
        data={"limit": str(limit), "offset": str(offset), "total": "true"},
    )
    _raise_for_upstream_code(response, "Subscribed podcast lookup failed.")
    radios = response.get("djRadios")
    if not isinstance(radios, list):
        radios = []
    normalized = [_podcast_radio_payload(item) for item in radios if isinstance(item, dict)]
    return _json_text(
        {
            "record_type": "subscribed_podcast_radio_page",
            "pagination": _pagination_payload(
                limit=limit,
                offset=offset,
                returned=len(normalized),
                total=response.get("count", response.get("total")),
                has_more=response.get("hasMore", response.get("more")),
            ),
            "podcasts": normalized,
        }
    )


def get_podcast_programs(
    radio_id: Any, limit: Any = 30, offset: Any = 0, order: Any = "newest"
) -> str:
    radio_id = _positive_int(radio_id, "radio_id")
    limit = _bounded_int(limit, "limit", 1, 100)
    offset = _non_negative_int(offset, "offset")
    if order not in ("newest", "oldest"):
        raise ValueError("order must be 'newest' or 'oldest'.")
    response = netease_request(
        "https://music.163.com/api/dj/program/byradio",
        data={
            "radioId": str(radio_id),
            "limit": str(limit),
            "offset": str(offset),
            "asc": "true" if order == "oldest" else "false",
        },
    )
    _raise_for_upstream_code(response, "Podcast program lookup failed.")
    programs = response.get("programs")
    if not isinstance(programs, list):
        programs = []
    normalized = [_podcast_program_payload(item) for item in programs if isinstance(item, dict)]
    return _json_text(
        {
            "record_type": "podcast_program_page",
            "radio_id": radio_id,
            "order": order,
            "pagination": _pagination_payload(
                limit=limit,
                offset=offset,
                returned=len(normalized),
                total=response.get("count"),
                has_more=response.get("more", response.get("hasMore")),
            ),
            "programs": normalized,
        }
    )


def _search_resources(response: dict[str, Any], legacy_key: str) -> tuple[list[Any], Any, Any]:
    data = response.get("data")
    if isinstance(data, dict) and isinstance(data.get("resources"), list):
        return data["resources"], data.get("totalCount"), data.get("hasMore")
    result = response.get("result")
    if isinstance(result, dict) and isinstance(result.get(legacy_key), list):
        return result[legacy_key], result.get(f"{legacy_key}Count"), result.get("hasMore")
    return [], None, None


def search_podcasts(query: Any, limit: Any = 20, offset: Any = 0) -> str:
    query = _clean_search_query(query)
    limit = _bounded_int(limit, "limit", 1, 50)
    offset = _non_negative_int(offset, "offset")
    response = netease_request(
        "https://music.163.com/api/search/voicelist/get",
        data={
            "keyword": query,
            "scene": "normal",
            "limit": str(limit),
            "offset": str(offset),
            "e_r": "true",
        },
    )
    _raise_for_upstream_code(response, "Podcast search failed.")
    resources, total, has_more = _search_resources(response, "djRadios")
    radios = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        base = resource.get("baseInfo") if isinstance(resource.get("baseInfo"), dict) else resource
        radios.append(_podcast_radio_payload(base))
    return _json_text(
        {
            "record_type": "podcast_radio_search_page",
            "query": query,
            "pagination": _pagination_payload(
                limit=limit, offset=offset, returned=len(radios), total=total, has_more=has_more
            ),
            "podcasts": radios,
        }
    )


def search_podcast_programs(query: Any, limit: Any = 20, offset: Any = 0) -> str:
    query = _clean_search_query(query)
    limit = _bounded_int(limit, "limit", 1, 50)
    offset = _non_negative_int(offset, "offset")
    response = netease_request(
        "https://music.163.com/api/search/voice/get",
        data={
            "keyword": query,
            "scene": "normal",
            "limit": str(limit),
            "offset": str(offset),
        },
    )
    _raise_for_upstream_code(response, "Podcast program search failed.")
    resources, total, has_more = _search_resources(response, "djprograms")
    programs = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        base = resource.get("baseInfo") if isinstance(resource.get("baseInfo"), dict) else resource
        program = _podcast_program_payload(base)
        if program["program_id"] is None:
            program["program_id"] = _upstream_positive_id(resource.get("resourceId"))
        programs.append(program)
    return _json_text(
        {
            "record_type": "podcast_program_search_page",
            "query": query,
            "pagination": _pagination_payload(
                limit=limit,
                offset=offset,
                returned=len(programs),
                total=total,
                has_more=has_more,
            ),
            "programs": programs,
        }
    )


def get_recent_podcast_plays(limit: Any = 50) -> str:
    limit = _bounded_int(limit, "limit", 1, 100)
    response = netease_request(
        "https://music.163.com/api/play-record/voice/list",
        data={"limit": str(limit)},
    )
    _raise_for_upstream_code(response, "Recent podcast play lookup failed.")
    data = response.get("data")
    entries = data.get("list") if isinstance(data, dict) else None
    response_shape = "recent_program_resource_list" if isinstance(entries, list) else "unsupported"
    if not isinstance(entries, list):
        entries = []
    records = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        raw_program = entry.get("data")
        if not isinstance(raw_program, dict):
            raw_program = entry.get("baseInfo") if isinstance(entry.get("baseInfo"), dict) else {}
        program = _podcast_program_payload(raw_program)
        if program["program_id"] is None:
            program["program_id"] = _upstream_positive_id(entry.get("resourceId"))
        play_time_ms = _upstream_int(entry.get("playTime"))
        terminal = entry.get("multiTerminalInfo")
        if not isinstance(terminal, dict):
            terminal = {}
        records.append(
            {
                **program,
                "play_time_ms": play_time_ms,
                "played_at": _milliseconds_to_iso8601(play_time_ms),
                "source_device": terminal.get("osText"),
            }
        )
    raw_total = data.get("total") if isinstance(data, dict) else None
    return _json_text(
        {
            "record_type": "recent_podcast_program_resources",
            "response_shape": response_shape,
            "order": "upstream_order_preserved",
            "returned": len(records),
            "upstream_total": _upstream_int(raw_total),
            "limit": limit,
            "records": records,
            "personal_play_count_supported": False,
            "limitations": {
                "complete_event_stream_guaranteed": False,
                "time_range_supported": False,
                "offset_pagination_supported": False,
                "personal_program_play_count_available": False,
                "public_listener_count_is_personal": False,
            },
        }
    )


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
        raise NetEaseError(_upstream_error(response, "Playlist creation failed."))
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
        raise NetEaseError(_upstream_error(response, "Playlist update failed."))
    verb = "Added" if operation == "add" else "Removed"
    return f"{verb} {len(ids)} song(s) {'to' if operation == 'add' else 'from'} playlist {playlist_id}."


def update_playlist(
    playlist_id: Any,
    name: Any = None,
    description: Any = None,
) -> str:
    playlist_id = _positive_int(playlist_id, "playlist_id")
    if name is None and description is None:
        raise ValueError("At least one of name or description must be provided.")

    clean_name: str | None = None
    if name is not None:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("name must be between 1 and 80 characters.")
        clean_name = name.strip()
    if description is not None and (
        not isinstance(description, str) or len(description) > 1000
    ):
        raise ValueError("description must be a string up to 1000 characters.")

    updated: list[str] = []
    csrf = get_csrf()
    if clean_name is not None:
        response = netease_request(
            "https://music.163.com/api/playlist/update/name?csrf_token=" + csrf,
            data={"id": str(playlist_id), "name": clean_name},
        )
        if response.get("code") != 200:
            raise NetEaseError(_upstream_error(response, "Playlist name update failed."))
        updated.append("name")

    if description is not None:
        response = netease_request(
            "https://music.163.com/api/playlist/desc/update?csrf_token=" + csrf,
            data={"id": str(playlist_id), "desc": description},
        )
        if response.get("code") != 200:
            message = _upstream_error(response, "Playlist description update failed.")
            if updated:
                raise NetEaseError(
                    f"Playlist name was updated, but the description was not: {message}"
                )
            raise NetEaseError(message)
        updated.append("description")

    return f"Updated playlist {playlist_id}: {', '.join(updated)}."


def _validate_complete_track_order(current_ids: list[int], requested: Any) -> list[int]:
    if not isinstance(requested, list) or not 1 <= len(requested) <= 10000:
        raise ValueError("song_ids must contain the complete order of 1 to 10000 IDs.")
    requested_ids = [_positive_int(song_id, "song_id") for song_id in requested]
    if len(set(requested_ids)) != len(requested_ids):
        raise ValueError("song_ids must not contain duplicates.")
    if len(set(current_ids)) != len(current_ids):
        raise NetEaseError("The existing playlist contains duplicate IDs and cannot be reordered safely.")
    if len(requested_ids) != len(current_ids) or set(requested_ids) != set(current_ids):
        raise ValueError(
            "song_ids must contain every existing playlist track exactly once; no tracks may be added, omitted, or replaced."
        )
    return requested_ids


def reorder_playlist_tracks(playlist_id: Any, song_ids: Any) -> str:
    playlist_id = _positive_int(playlist_id, "playlist_id")
    uid = get_uid()
    playlist = _playlist_detail(playlist_id)
    creator = playlist.get("creator")
    creator_id = creator.get("userId") if isinstance(creator, dict) else None
    if creator_id is None or str(creator_id) != str(uid):
        raise PermissionError("Only playlists owned by the current NetEase user can be reordered.")
    current_ids, _ = _playlist_track_ids(playlist)
    requested_ids = _validate_complete_track_order(current_ids, song_ids)
    if requested_ids == current_ids:
        return _json_text(
            {
                "success": True,
                "changed": False,
                "playlist_id": playlist_id,
                "track_count": len(requested_ids),
                "message": "The playlist is already in the requested order.",
            }
        )

    response = netease_request(
        "https://music.163.com/api/playlist/manipulate/tracks?csrf_token=" + get_csrf(),
        data={
            "pid": str(playlist_id),
            "trackIds": json.dumps(requested_ids, separators=(",", ":")),
            "op": "update",
        },
    )
    _raise_for_upstream_code(response, "Playlist reorder failed.")

    verified_playlist = _playlist_detail(playlist_id)
    verified_ids, _ = _playlist_track_ids(verified_playlist)
    if verified_ids != requested_ids:
        raise NetEaseError(
            "NetEase accepted the reorder request, but the returned playlist order did not match. No automatic retry was attempted."
        )
    return _json_text(
        {
            "success": True,
            "changed": True,
            "verified": True,
            "playlist_id": playlist_id,
            "track_count": len(verified_ids),
            "order_summary": {
                "first_song_ids": verified_ids[:5],
                "last_song_ids": verified_ids[-5:],
            },
        }
    )


def like_song(song_id: Any, like: Any = True) -> str:
    song_id = _positive_int(song_id, "song_id")
    if not isinstance(like, bool):
        raise ValueError("like must be a boolean.")
    response = netease_request(
        "https://music.163.com/api/radio/like?alg=itembased"
        f"&trackId={song_id}&like={'true' if like else 'false'}&time=25&csrf_token={get_csrf()}"
    )
    if response.get("code") != 200:
        raise NetEaseError(_upstream_error(response, "Like operation failed."))
    return f"{'Liked' if like else 'Unliked'} song {song_id}."


def _owned_playlist_state(playlist_id: int, kind: str) -> dict[str, Any]:
    uid = get_uid()
    playlist = _playlist_detail(playlist_id)
    creator = playlist.get("creator")
    creator_id = creator.get("userId") if isinstance(creator, dict) else None
    if creator_id is None or str(creator_id) != str(uid):
        raise PermissionError("Only playlists owned by the current NetEase user can be modified.")
    state: dict[str, Any] = {"playlist_id": playlist_id, "creator_id": creator_id}
    if kind == "metadata":
        state.update({"name": playlist.get("name"), "description": playlist.get("description") or ""})
    elif kind == "tracks":
        state["track_ids"] = _playlist_track_ids(playlist)[0]
    elif kind == "cover":
        state.update(
            {
                "cover_image_id": playlist.get("coverImgId"),
                "cover_image_url": playlist.get("coverImgUrl") if playlist.get("coverImgId") is None else None,
            }
        )
    else:
        raise ValueError("Unknown playlist state kind.")
    return state


def _liked_song_state(song_id: int) -> dict[str, Any]:
    uid = get_uid()
    response = netease_request(
        "https://music.163.com/api/song/like/get",
        data={"uid": str(uid)},
    )
    _raise_for_upstream_code(response, "Liked-song lookup failed.")
    raw_ids = response.get("ids")
    ids = raw_ids if isinstance(raw_ids, list) else []
    return {"song_id": song_id, "liked": song_id in ids}


def _accessible_playlist(playlist_id: int) -> tuple[int, dict[str, Any], list[int]]:
    uid = get_uid()
    playlist = _playlist_detail(playlist_id)
    track_ids, _ = _playlist_track_ids(playlist)
    return uid, playlist, track_ids


def _note_snapshot(note: dict[str, Any] | None) -> dict[str, Any] | None:
    if note is None:
        return None
    return {
        "note_id": note.get("note_id"),
        "playlist_id": note.get("playlist_id"),
        "song_id": note.get("song_id"),
        "author": note.get("author"),
        "content": note.get("content"),
        "visibility": note.get("visibility"),
        "created_at": note.get("created_at"),
        "updated_at": note.get("updated_at"),
        "version": note.get("version"),
        "deleted_at": note.get("deleted_at"),
    }


def _clean_note_id(value: Any, field: str = "note_id") -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ValueError(f"{field} must be a non-empty string up to 100 characters.")
    return value


def _clean_note_author(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError("author must be between 1 and 80 characters.")
    return value.strip()


def _clean_note_content(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("content must be between 1 and 2000 characters.")
    return value.strip()


def _normalize_write_arguments(operation: str, arguments: Any) -> dict[str, Any]:
    if operation not in WRITE_TOOL_NAMES:
        raise ValueError("operation must name an available write tool.")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object.")
    arguments = {key: value for key, value in arguments.items() if key != "preview_token"}
    if operation == "create_playlist":
        name = arguments.get("name")
        description = arguments.get("description", "")
        privacy = arguments.get("privacy", 10)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("name must be between 1 and 80 characters.")
        if not isinstance(description, str) or len(description) > 1000:
            raise ValueError("description must be a string up to 1000 characters.")
        if privacy not in (0, 10):
            raise ValueError("privacy must be 0 (public) or 10 (private).")
        return {"name": name.strip(), "description": description, "privacy": privacy}
    if operation == "update_playlist":
        playlist_id = _positive_int(arguments.get("playlist_id"), "playlist_id")
        has_name = "name" in arguments
        has_description = "description" in arguments
        if not has_name and not has_description:
            raise ValueError("At least one of name or description must be provided.")
        normalized: dict[str, Any] = {"playlist_id": playlist_id}
        if has_name:
            name = arguments.get("name")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                raise ValueError("name must be between 1 and 80 characters.")
            normalized["name"] = name.strip()
        if has_description:
            description = arguments.get("description")
            if not isinstance(description, str) or len(description) > 1000:
                raise ValueError("description must be a string up to 1000 characters.")
            normalized["description"] = description
        return normalized
    if operation in {"add_to_playlist", "remove_from_playlist"}:
        playlist_id = _positive_int(arguments.get("playlist_id"), "playlist_id")
        ids = _song_ids(arguments.get("song_ids"))
        if len(set(ids)) != len(ids):
            raise ValueError("song_ids must not contain duplicates.")
        return {"playlist_id": playlist_id, "song_ids": ids}
    if operation == "reorder_playlist_tracks":
        playlist_id = _positive_int(arguments.get("playlist_id"), "playlist_id")
        requested = arguments.get("song_ids")
        if not isinstance(requested, list) or not 1 <= len(requested) <= 10000:
            raise ValueError("song_ids must contain the complete order of 1 to 10000 IDs.")
        ids = [_positive_int(item, "song_id") for item in requested]
        if len(set(ids)) != len(ids):
            raise ValueError("song_ids must not contain duplicates.")
        return {"playlist_id": playlist_id, "song_ids": ids}
    if operation == "like_song":
        song_id = _positive_int(arguments.get("song_id"), "song_id")
        like = arguments.get("like", True)
        if not isinstance(like, bool):
            raise ValueError("like must be a boolean.")
        return {"song_id": song_id, "like": like}
    if operation == "update_playlist_cover":
        playlist_id = _positive_int(arguments.get("playlist_id"), "playlist_id")
        reference = validate_file_reference(arguments.get("image"))
        return {
            "playlist_id": playlist_id,
            "image": {
                "file_id": reference["file_id"],
                "mime_type": reference["mime_type"],
                "file_name": reference["file_name"],
            },
        }
    if operation == "create_interaction_note":
        playlist_id = _positive_int(arguments.get("playlist_id"), "playlist_id")
        song_id = arguments.get("song_id")
        if song_id is not None:
            song_id = _positive_int(song_id, "song_id")
        visibility = arguments.get("visibility", "private")
        if visibility != "private":
            raise ValueError("visibility currently supports only private.")
        return {
            "playlist_id": playlist_id,
            "song_id": song_id,
            "author": _clean_note_author(arguments.get("author")),
            "content": _clean_note_content(arguments.get("content")),
            "visibility": visibility,
        }
    if operation == "update_interaction_note":
        note_id = _clean_note_id(arguments.get("note_id"))
        version = _positive_int(arguments.get("version"), "version")
        if "author" not in arguments and "content" not in arguments:
            raise ValueError("At least one of author or content must be provided.")
        normalized = {"note_id": note_id, "version": version}
        if "author" in arguments:
            normalized["author"] = _clean_note_author(arguments.get("author"))
        if "content" in arguments:
            normalized["content"] = _clean_note_content(arguments.get("content"))
        return normalized
    if operation == "delete_interaction_note":
        return {
            "note_id": _clean_note_id(arguments.get("note_id")),
            "version": _positive_int(arguments.get("version"), "version"),
        }
    if operation == "undo_operation":
        return {"operation_id": _clean_note_id(arguments.get("operation_id"), "operation_id")}
    raise ValueError("Unsupported write operation.")


def _current_state_for_operation(
    operation: str, arguments: dict[str, Any], user_id: int
) -> dict[str, Any] | None:
    if operation == "create_playlist":
        return None
    if operation == "update_playlist":
        return _owned_playlist_state(arguments["playlist_id"], "metadata")
    if operation in {"add_to_playlist", "remove_from_playlist", "reorder_playlist_tracks"}:
        return _owned_playlist_state(arguments["playlist_id"], "tracks")
    if operation == "like_song":
        return _liked_song_state(arguments["song_id"])
    if operation == "update_playlist_cover":
        return _owned_playlist_state(arguments["playlist_id"], "cover")
    if operation == "create_interaction_note":
        _, _, track_ids = _accessible_playlist(arguments["playlist_id"])
        if arguments.get("song_id") is not None and arguments["song_id"] not in track_ids:
            raise ValueError("song_id is not currently in the specified playlist.")
        return None
    if operation in {"update_interaction_note", "delete_interaction_note"}:
        note = _store().get_note(arguments["note_id"], user_id, include_deleted=True)
        if note is None or note.get("deleted_at") is not None:
            raise ValueError("The note does not exist or is already deleted.")
        if note.get("version") != arguments["version"]:
            raise ValueError("The note version is stale; reload it before modifying it.")
        _accessible_playlist(int(note["playlist_id"]))
        return _note_snapshot(note)
    if operation == "undo_operation":
        original = _store().get_operation(arguments["operation_id"], user_id)
        if original is None:
            raise ValueError("The operation does not exist.")
        if original.get("status") != "success" or not original.get("reversible"):
            raise ValueError("Only successful operations marked reversible can be undone.")
        if original.get("undo_status") != "not_requested":
            raise ValueError("This operation has already been undone or an undo was attempted.")
        return _current_state_for_logged_operation(original, user_id)
    raise ValueError("Unsupported write operation.")


def _current_state_for_logged_operation(record: dict[str, Any], user_id: int) -> dict[str, Any] | None:
    operation = str(record["operation"])
    arguments = record.get("sanitized_arguments") or {}
    if operation == "create_interaction_note":
        after = record.get("after") or {}
        note = _store().get_note(str(after.get("note_id", "")), user_id, include_deleted=True)
        return _note_snapshot(note)
    if operation in {"update_interaction_note", "delete_interaction_note"}:
        note = _store().get_note(str(arguments.get("note_id", "")), user_id, include_deleted=True)
        return _note_snapshot(note)
    return _current_state_for_operation(operation, arguments, user_id)


def preview_operation(operation: Any, arguments: Any) -> str:
    if READ_ONLY:
        raise PermissionError("Write tools are disabled; previews are available in read-write mode.")
    if not isinstance(operation, str):
        raise ValueError("operation must be a string.")
    normalized = _normalize_write_arguments(operation, arguments)
    user_id = get_uid()
    before_state = _current_state_for_operation(operation, normalized, user_id)
    target: dict[str, Any]
    expected_after: Any
    reversible = False
    risk_level = "medium"
    artifact: bytes | None = None
    artifact_meta: dict[str, Any] | None = None

    if operation == "create_playlist":
        target = {"resource_type": "playlist", "playlist_id": None}
        expected_after = dict(normalized)
    elif operation == "update_playlist":
        target = {"resource_type": "playlist", "playlist_id": normalized["playlist_id"]}
        expected_after = dict(before_state or {})
        expected_after.update({key: value for key, value in normalized.items() if key in {"name", "description"}})
        reversible = expected_after != before_state
    elif operation == "add_to_playlist":
        target = {"resource_type": "playlist_tracks", "playlist_id": normalized["playlist_id"]}
        current_ids = list((before_state or {}).get("track_ids", []))
        added_ids = [song_id for song_id in normalized["song_ids"] if song_id not in current_ids]
        expected_after = {**(before_state or {}), "track_ids": current_ids + added_ids}
        reversible = bool(added_ids)
    elif operation == "remove_from_playlist":
        target = {"resource_type": "playlist_tracks", "playlist_id": normalized["playlist_id"]}
        current_ids = list((before_state or {}).get("track_ids", []))
        missing = [song_id for song_id in normalized["song_ids"] if song_id not in current_ids]
        if missing:
            raise ValueError(f"Cannot remove song IDs that are not in the playlist: {missing}.")
        expected_after = {
            **(before_state or {}),
            "track_ids": [song_id for song_id in current_ids if song_id not in normalized["song_ids"]],
        }
        reversible = expected_after != before_state
        risk_level = "high"
    elif operation == "reorder_playlist_tracks":
        target = {"resource_type": "playlist_tracks", "playlist_id": normalized["playlist_id"]}
        current_ids = list((before_state or {}).get("track_ids", []))
        requested_ids = _validate_complete_track_order(current_ids, normalized["song_ids"])
        expected_after = {**(before_state or {}), "track_ids": requested_ids}
        reversible = expected_after != before_state
        risk_level = "high"
    elif operation == "like_song":
        target = {"resource_type": "song_like", "song_id": normalized["song_id"]}
        expected_after = {"song_id": normalized["song_id"], "liked": normalized["like"]}
        reversible = expected_after != before_state
        if normalized["like"] is False:
            risk_level = "high"
    elif operation == "update_playlist_cover":
        target = {"resource_type": "playlist_cover", "playlist_id": normalized["playlist_id"]}
        raw_reference = arguments.get("image") if isinstance(arguments, dict) else None
        artifact, artifact_meta = normalize_cover_image(
            raw_reference,
            max_bytes=MAX_IMAGE_BYTES,
            max_pixels=MAX_IMAGE_PIXELS,
        )
        expected_after = {
            "playlist_id": normalized["playlist_id"],
            "processed_image": artifact_meta,
            "undo_limitation": "NetEase does not provide a reliable original-cover file for restoration.",
        }
        risk_level = "high"
    elif operation == "create_interaction_note":
        target = {
            "resource_type": "interaction_note",
            "playlist_id": normalized["playlist_id"],
            "song_id": normalized.get("song_id"),
        }
        expected_after = {**normalized, "version": 1, "deleted_at": None}
        reversible = True
        risk_level = "low"
    elif operation == "update_interaction_note":
        target = {"resource_type": "interaction_note", "note_id": normalized["note_id"]}
        expected_after = dict(before_state or {})
        expected_after.update({key: value for key, value in normalized.items() if key in {"author", "content"}})
        expected_after["version"] = normalized["version"] + 1
        reversible = expected_after != before_state
        risk_level = "low"
    elif operation == "delete_interaction_note":
        target = {"resource_type": "interaction_note", "note_id": normalized["note_id"]}
        expected_after = {**(before_state or {}), "deleted_at": "set_on_execution", "version": normalized["version"] + 1}
        reversible = True
        risk_level = "high"
    elif operation == "undo_operation":
        original = _store().get_operation(normalized["operation_id"], user_id)
        assert original is not None
        if _canonical_json(before_state) != _canonical_json(original.get("after")):
            raise ValueError("The resource changed after the recorded operation; undo is unsafe.")
        target = {"resource_type": "operation", "operation_id": normalized["operation_id"]}
        expected_after = original.get("before")
        risk_level = "high"
    else:
        raise ValueError("Unsupported write operation.")

    preview_token = secrets.token_urlsafe(32)
    created_at = utc_now()
    expires_at = time.time() + PREVIEW_TTL_SECONDS
    sanitized_arguments = _sanitize_value(normalized)
    _store().save_preview(
        {
            "token_hash": _preview_token_hash(preview_token),
            "user_id": user_id,
            "operation": operation,
            "arguments": normalized,
            "sanitized_arguments": sanitized_arguments,
            "target": target,
            "before_state": before_state,
            "expected_after_state": expected_after,
            "state_hash": _state_hash(before_state),
            "risk_level": risk_level,
            "reversible": reversible,
            "artifact": artifact,
            "artifact_meta": artifact_meta,
            "created_at": created_at,
            "expires_at": expires_at,
        }
    )
    return _json_text(
        {
            "operation": operation,
            "normalized_arguments": sanitized_arguments,
            "target": target,
            "before_state": before_state,
            "expected_after_state": expected_after,
            "risk_level": risk_level,
            "reversible": reversible,
            "preview_token": preview_token,
            "created_at": created_at,
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "expires_in_seconds": PREVIEW_TTL_SECONDS,
        }
    )


def get_operation_log(
    limit: Any = 50,
    offset: Any = 0,
    operation: Any = None,
    status: Any = None,
    created_after: Any = None,
    created_before: Any = None,
) -> str:
    limit = _bounded_int(limit, "limit", 1, 100)
    offset = _non_negative_int(offset, "offset")
    if operation is not None and (not isinstance(operation, str) or operation not in WRITE_TOOL_NAMES):
        raise ValueError("operation filter must name a write tool.")
    allowed_statuses = {
        "started",
        "success",
        "failed",
        "partial_success",
        "conflict",
        "unknown",
    }
    if status is not None and (not isinstance(status, str) or status not in allowed_statuses):
        raise ValueError("status filter is invalid.")
    after = _validate_iso8601(created_after, "created_after")
    before = _validate_iso8601(created_before, "created_before")
    if after and before and after > before:
        raise ValueError("created_after must not be later than created_before.")
    user_id = get_uid()
    records = _store().query_operations(
        user_id,
        limit=limit,
        offset=offset,
        operation=operation,
        status=status,
        created_after=after,
        created_before=before,
    )
    public_records = []
    for record in records:
        public_records.append(
            {
                "operation_id": record.get("operation_id"),
                "operation": record.get("operation"),
                "sanitized_arguments": record.get("sanitized_arguments"),
                "target": record.get("target"),
                "created_at": record.get("created_at"),
                "completed_at": record.get("completed_at"),
                "status": record.get("status"),
                "before_state": record.get("before"),
                "after_state": record.get("after"),
                "reversible": record.get("reversible"),
                "undo_status": record.get("undo_status"),
                "undo_operation_id": record.get("undo_operation_id"),
                "error_summary": record.get("error_summary"),
            }
        )
    return _json_text(
        {
            "storage": "persistent_sqlite",
            "retention": {"days": OPERATION_RETENTION_DAYS, "maximum_records": MAX_OPERATION_LOGS},
            "limit": limit,
            "offset": offset,
            "returned": len(public_records),
            "operations": public_records,
        }
    )


def list_interaction_notes(
    playlist_id: Any,
    song_id: Any = None,
    author: Any = None,
    limit: Any = 50,
    offset: Any = 0,
) -> str:
    playlist_id = _positive_int(playlist_id, "playlist_id")
    if song_id is not None:
        song_id = _positive_int(song_id, "song_id")
    if author is not None:
        author = _clean_note_author(author)
    limit = _bounded_int(limit, "limit", 1, 100)
    offset = _non_negative_int(offset, "offset")
    user_id, playlist, track_ids = _accessible_playlist(playlist_id)
    notes = _store().list_notes(
        user_id,
        playlist_id,
        song_id=song_id,
        author=author,
        limit=limit,
        offset=offset,
    )
    requested_song_ids = list(
        dict.fromkeys(int(note["song_id"]) for note in notes if note.get("song_id") is not None)
    )
    songs = _fetch_song_records(requested_song_ids)
    songs_by_id = {song.get("id"): _song_summary(song) for song in songs}
    output = []
    for note in notes:
        current_song = songs_by_id.get(note.get("song_id")) if note.get("song_id") else None
        output.append(
            {
                **_note_snapshot(note),
                "stale": note.get("song_id") is not None and note.get("song_id") not in track_ids,
                "current_song": current_song,
            }
        )
    return _json_text(
        {
            "storage": "plugin_owned_private_sqlite",
            "native_netease_data": False,
            "playlist": {"playlist_id": playlist_id, "name": playlist.get("name")},
            "limit": limit,
            "offset": offset,
            "returned": len(output),
            "notes": output,
        }
    )


def _upload_playlist_cover(
    playlist_id: int, image_bytes: bytes, image_meta: dict[str, Any]
) -> dict[str, Any]:
    allocation = netease_request(
        "https://music.163.com/api/nos/token/alloc",
        data={
            "bucket": "yyimgs",
            "ext": "jpg",
            "filename": f"playlist-{playlist_id}.jpg",
            "local": "false",
            "nos_product": "0",
            "return_body": '{"code":200,"size":"$(ObjectSize)"}',
            "type": "other",
        },
    )
    _raise_for_upstream_code(allocation, "NetEase image-upload allocation failed.")
    allocation_result = allocation.get("result")
    if not isinstance(allocation_result, dict):
        raise NetEaseError("NetEase did not return image-upload credentials.")
    object_key = allocation_result.get("objectKey")
    upload_token = allocation_result.get("token")
    image_id = allocation_result.get("docId")
    if not all(isinstance(value, (str, int)) and str(value) for value in (object_key, upload_token, image_id)):
        raise NetEaseError("NetEase returned incomplete image-upload credentials.")
    encoded_key = urllib.parse.quote(str(object_key), safe="/")
    upload_response = netease_binary_request(
        f"https://nosup-hz1.127.net/yyimgs/{encoded_key}?offset=0&complete=true&version=1.0",
        image_bytes,
        {"x-nos-token": str(upload_token), "Content-Type": "image/jpeg"},
    )
    if upload_response.get("code") not in (None, 200):
        raise NetEaseError("NetEase image storage rejected the cover upload.")
    update_response = netease_request(
        "https://music.163.com/api/playlist/cover/update?csrf_token=" + get_csrf(),
        data={"id": str(playlist_id), "coverImgId": str(image_id)},
    )
    _raise_for_upstream_code(update_response, "Playlist cover update failed.")
    return {
        "success": True,
        "playlist_id": playlist_id,
        "upstream_image_id": image_id,
        "final_format": image_meta.get("final_format"),
        "final_mime_type": image_meta.get("final_mime_type"),
        "final_width": image_meta.get("final_width"),
        "final_height": image_meta.get("final_height"),
        "metadata_removed": True,
        "reversible": False,
        "undo_limitation": "The original NetEase cover file cannot be recovered reliably.",
    }


def _execute_operation_action(
    operation: str,
    arguments: dict[str, Any],
    preview: dict[str, Any],
    user_id: int,
    operation_id: str,
) -> dict[str, Any]:
    if operation == "create_playlist":
        response = netease_request(
            "https://music.163.com/api/playlist/create?csrf_token=" + get_csrf(),
            data={
                "name": arguments["name"],
                "privacy": str(arguments["privacy"]),
                "type": "NORMAL",
                "description": arguments["description"],
            },
        )
        _raise_for_upstream_code(response, "Playlist creation failed.")
        playlist = response.get("playlist") if isinstance(response.get("playlist"), dict) else {}
        return {
            "success": True,
            "playlist_id": playlist.get("id"),
            "name": playlist.get("name", arguments["name"]),
            "description": playlist.get("description", arguments["description"]),
            "privacy": arguments["privacy"],
        }
    if operation == "update_playlist":
        message = update_playlist(
            arguments["playlist_id"],
            arguments.get("name"),
            arguments.get("description"),
        )
        return {"success": True, "message": message}
    if operation == "add_to_playlist":
        before_ids = list((preview.get("before") or {}).get("track_ids", []))
        actual_ids = [song_id for song_id in arguments["song_ids"] if song_id not in before_ids]
        if not actual_ids:
            return {"success": True, "changed": False, "message": "All songs were already present."}
        message = manipulate_playlist("add", arguments["playlist_id"], actual_ids)
        return {"success": True, "changed": True, "song_ids": actual_ids, "message": message}
    if operation == "remove_from_playlist":
        message = manipulate_playlist("del", arguments["playlist_id"], arguments["song_ids"])
        return {"success": True, "changed": True, "song_ids": arguments["song_ids"], "message": message}
    if operation == "reorder_playlist_tracks":
        return json.loads(reorder_playlist_tracks(arguments["playlist_id"], arguments["song_ids"]))
    if operation == "like_song":
        message = like_song(arguments["song_id"], arguments["like"])
        return {"success": True, "message": message}
    if operation == "update_playlist_cover":
        artifact = preview.get("artifact")
        image_meta = preview.get("artifact_meta")
        if not isinstance(artifact, bytes) or not isinstance(image_meta, dict):
            raise ValueError("The preview no longer contains the processed image; create a new preview.")
        return _upload_playlist_cover(arguments["playlist_id"], artifact, image_meta)
    if operation == "create_interaction_note":
        note = _store().create_note(
            {
                "note_id": str(uuid.uuid4()),
                "user_id": user_id,
                "playlist_id": arguments["playlist_id"],
                "song_id": arguments.get("song_id"),
                "author": arguments["author"],
                "content": arguments["content"],
                "visibility": arguments["visibility"],
                "created_at": utc_now(),
            }
        )
        return {"success": True, "note": _note_snapshot(note)}
    if operation == "update_interaction_note":
        note = _store().update_note(
            arguments["note_id"],
            user_id,
            arguments["version"],
            author=arguments.get("author"),
            content=arguments.get("content"),
        )
        return {"success": True, "note": _note_snapshot(note)}
    if operation == "delete_interaction_note":
        note = _store().soft_delete_note(
            arguments["note_id"], user_id, arguments["version"]
        )
        return {"success": True, "soft_deleted": True, "note": _note_snapshot(note)}
    if operation == "undo_operation":
        return _perform_undo(arguments["operation_id"], user_id, operation_id)
    raise ValueError("Unsupported write operation.")


def _undo_states_match(expected: Any, actual: Any, operation: str) -> bool:
    if operation not in {
        "create_interaction_note",
        "update_interaction_note",
        "delete_interaction_note",
    }:
        return _canonical_json(expected) == _canonical_json(actual)
    fields = ("note_id", "playlist_id", "song_id", "author", "content", "visibility", "deleted_at")
    return all((expected or {}).get(field) == (actual or {}).get(field) for field in fields)


def _perform_undo(operation_to_undo: str, user_id: int, undo_operation_id: str) -> dict[str, Any]:
    original = _store().get_operation(operation_to_undo, user_id)
    if original is None:
        raise ValueError("The operation does not exist.")
    if original.get("status") != "success" or not original.get("reversible"):
        raise ValueError("Only successful operations marked reversible can be undone.")
    if original.get("undo_status") != "not_requested":
        raise ValueError("This operation has already been undone or an undo was attempted.")
    current = _current_state_for_logged_operation(original, user_id)
    if _canonical_json(current) != _canonical_json(original.get("after")):
        raise ValueError("The resource changed after the original operation; undo is unsafe.")
    _store().set_undo_status(operation_to_undo, "in_progress", undo_operation_id)
    operation = str(original["operation"])
    before = original.get("before") or {}
    after = original.get("after") or {}
    arguments = original.get("sanitized_arguments") or {}
    try:
        if operation == "update_playlist":
            update_playlist(
                int(before["playlist_id"]),
                before.get("name"),
                before.get("description", ""),
            )
        elif operation == "add_to_playlist":
            before_ids = list(before.get("track_ids", []))
            added_ids = [song_id for song_id in after.get("track_ids", []) if song_id not in before_ids]
            if added_ids:
                manipulate_playlist("del", int(before["playlist_id"]), added_ids)
        elif operation == "remove_from_playlist":
            after_ids = list(after.get("track_ids", []))
            removed_ids = [song_id for song_id in before.get("track_ids", []) if song_id not in after_ids]
            if removed_ids:
                manipulate_playlist("add", int(before["playlist_id"]), removed_ids)
                reorder_playlist_tracks(int(before["playlist_id"]), list(before["track_ids"]))
        elif operation == "reorder_playlist_tracks":
            reorder_playlist_tracks(int(before["playlist_id"]), list(before["track_ids"]))
        elif operation == "like_song":
            like_song(int(before["song_id"]), bool(before["liked"]))
        elif operation == "create_interaction_note":
            note_id = str(after["note_id"])
            current_note = _store().get_note(note_id, user_id, include_deleted=True)
            if current_note is None:
                raise ValueError("The created note no longer exists.")
            _store().soft_delete_note(note_id, user_id, int(current_note["version"]))
        elif operation in {"update_interaction_note", "delete_interaction_note"}:
            note_id = str(arguments["note_id"])
            current_note = _store().get_note(note_id, user_id, include_deleted=True)
            if current_note is None:
                raise ValueError("The note no longer exists.")
            _store().restore_note_snapshot(before, user_id, int(current_note["version"]))
        else:
            raise ValueError("This operation does not have a safe undo implementation.")
        restored = _current_state_for_logged_operation(original, user_id)
        if not _undo_states_match(before, restored, operation):
            raise NetEaseError("The undo request completed, but the restored state did not match the audit record.")
    except Exception:
        _store().set_undo_status(operation_to_undo, "failed", undo_operation_id)
        raise
    _store().set_undo_status(operation_to_undo, "succeeded", undo_operation_id)
    return {
        "success": True,
        "undone_operation_id": operation_to_undo,
        "original_operation": operation,
        "before_undo": current,
        "after_undo": restored,
    }


def _after_state_for_operation(
    operation: str,
    arguments: dict[str, Any],
    result: dict[str, Any] | None,
    user_id: int,
) -> Any:
    if operation == "create_playlist":
        return result
    if operation in {"create_interaction_note", "update_interaction_note", "delete_interaction_note"}:
        return (result or {}).get("note")
    if operation == "undo_operation":
        return (result or {}).get("after_undo")
    return _current_state_for_operation(operation, arguments, user_id)


def _best_effort_after_state(
    operation: str, arguments: dict[str, Any], user_id: int
) -> Any:
    try:
        if operation == "create_interaction_note":
            return None
        if operation in {"update_interaction_note", "delete_interaction_note"}:
            return _note_snapshot(
                _store().get_note(arguments["note_id"], user_id, include_deleted=True)
            )
        if operation == "undo_operation":
            original = _store().get_operation(arguments["operation_id"], user_id)
            return _current_state_for_logged_operation(original, user_id) if original else None
        return _current_state_for_operation(operation, arguments, user_id)
    except Exception:
        return None


def _execute_previewed_operation(
    operation: str, raw_arguments: dict[str, Any], preview_token: Any
) -> str:
    if not isinstance(preview_token, str) or not 20 <= len(preview_token) <= 200:
        raise ValueError("A valid preview_token is required. Call preview_operation first.")
    normalized = _normalize_write_arguments(operation, raw_arguments)
    user_id = get_uid()
    token_hash = _preview_token_hash(preview_token)
    claim_status, preview = _store().claim_preview(
        token_hash, user_id, operation, normalized
    )
    if claim_status == "consumed":
        result = preview.get("result") or {}
        if result.get("status") != "success":
            raise NetEaseError(result.get("error_summary") or "The previous execution attempt failed.")
        result["idempotent_replay"] = True
        return _json_text(result)

    operation_id = str(uuid.uuid4())
    _store().start_operation(
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "operation": operation,
            "sanitized_arguments": preview.get("sanitized_arguments"),
            "target": preview.get("target"),
            "created_at": utc_now(),
            "before_state": preview.get("before"),
            "reversible": preview.get("reversible"),
            "parent_operation_id": normalized.get("operation_id") if operation == "undo_operation" else None,
        }
    )
    try:
        try:
            current_state = _current_state_for_operation(operation, normalized, user_id)
        except (ValueError, PermissionError) as exc:
            error = _redact_secrets(exc)
            conflict_result = {
                "operation_id": operation_id,
                "operation": operation,
                "status": "conflict",
                "error_summary": error,
            }
            _store().finish_operation(
                operation_id,
                status="conflict",
                after_state=_best_effort_after_state(operation, normalized, user_id),
                result=conflict_result,
                error_summary=error,
            )
            _store().finish_preview(
                token_hash, "conflict", operation_id=operation_id, result=conflict_result
            )
            if isinstance(exc, PermissionError):
                raise PermissionError(error) from None
            raise ValueError(error) from None
        if _state_hash(current_state) != preview.get("state_hash"):
            error = "The resource changed after preview; no write was performed. Create a new preview."
            conflict_result = {
                "operation_id": operation_id,
                "operation": operation,
                "status": "conflict",
                "error_summary": error,
            }
            _store().finish_operation(
                operation_id,
                status="conflict",
                after_state=current_state,
                result=conflict_result,
                error_summary=error,
            )
            _store().finish_preview(
                token_hash, "conflict", operation_id=operation_id, result=conflict_result
            )
            raise ValueError(error)
        action_result = _execute_operation_action(
            operation, normalized, preview, user_id, operation_id
        )
        after_state = _after_state_for_operation(operation, normalized, action_result, user_id)
        deterministic_operations = {
            "update_playlist",
            "add_to_playlist",
            "remove_from_playlist",
            "reorder_playlist_tracks",
            "like_song",
        }
        if operation in deterministic_operations and _canonical_json(after_state) != _canonical_json(
            preview.get("expected_after")
        ):
            raise NetEaseError(
                "NetEase accepted the write, but the resulting state did not match the preview. No automatic retry was attempted."
            )
        if operation == "update_playlist_cover":
            expected_image_id = action_result.get("upstream_image_id")
            actual_image_id = (after_state or {}).get("cover_image_id")
            if actual_image_id is not None and str(actual_image_id) != str(expected_image_id):
                raise NetEaseError(
                    "NetEase accepted the cover update, but the playlist still reports a different cover image."
                )
            action_result["verified"] = actual_image_id is not None
        final_result = {
            "operation_id": operation_id,
            "operation": operation,
            "status": "success",
            "result": action_result,
            "before_state": preview.get("before"),
            "after_state": after_state,
            "reversible": bool(preview.get("reversible")),
        }
        _store().finish_operation(
            operation_id,
            status="success",
            after_state=after_state,
            result=final_result,
        )
        _store().finish_preview(
            token_hash, "consumed", operation_id=operation_id, result=final_result
        )
        return _json_text(final_result)
    except Exception as exc:
        existing = _store().get_operation(operation_id, user_id)
        if existing and existing.get("status") in {"conflict", "success"}:
            raise
        safe_error = _redact_secrets(exc)
        after_state = _best_effort_after_state(operation, normalized, user_id)
        status = (
            "partial_success"
            if after_state is not None
            and _canonical_json(after_state) != _canonical_json(preview.get("before"))
            else "failed"
        )
        failure_result = {
            "operation_id": operation_id,
            "operation": operation,
            "status": status,
            "error_summary": safe_error,
        }
        _store().finish_operation(
            operation_id,
            status=status,
            after_state=after_state,
            result=failure_result,
            error_summary=safe_error,
        )
        _store().finish_preview(
            token_hash, "consumed", operation_id=operation_id, result=failure_result
        )
        if isinstance(exc, PermissionError):
            raise PermissionError(safe_error) from None
        if isinstance(exc, ValueError):
            raise ValueError(safe_error) from None
        raise NetEaseError(safe_error) from None


def available_tools() -> list[dict[str, Any]]:
    return READ_TOOLS if READ_ONLY else READ_TOOLS + WRITE_TOOLS


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    if name in WRITE_TOOL_NAMES and READ_ONLY:
        raise PermissionError("Write tools are disabled. Set MCP_READ_ONLY=false to enable them.")
    if name in WRITE_TOOL_NAMES:
        preview_token = arguments.get("preview_token")
        preview_only_tools = {
            "undo_operation",
            "update_playlist_cover",
            "create_interaction_note",
            "update_interaction_note",
            "delete_interaction_note",
        }
        if preview_token is not None:
            return _execute_previewed_operation(name, arguments, preview_token)
        if REQUIRE_WRITE_PREVIEW or name in preview_only_tools:
            raise ValueError(
                "This write requires a matching preview_token. Call preview_operation with the same operation and arguments first."
            )
    if name == "search_song":
        return search_song(arguments.get("query"), arguments.get("limit", 5))
    if name == "list_my_playlists":
        return list_my_playlists()
    if name == "get_playlist_songs":
        return get_playlist_songs(
            arguments.get("playlist_id"),
            arguments.get("limit", 50),
            arguments.get("offset", 0),
        )
    if name == "get_song_details":
        has_single = "song_id" in arguments
        has_many = "song_ids" in arguments
        if has_single == has_many:
            raise ValueError("Provide exactly one of song_id or song_ids.")
        return get_song_details(
            arguments.get("song_id") if has_single else arguments.get("song_ids")
        )
    if name == "get_play_history":
        return get_play_history(arguments.get("limit", 30), arguments.get("all_time", False))
    if name == "get_recent_plays":
        return get_recent_plays(arguments.get("limit", 100))
    if name == "list_my_subscribed_podcasts":
        return list_my_subscribed_podcasts(
            arguments.get("limit", 30), arguments.get("offset", 0)
        )
    if name == "get_podcast_programs":
        return get_podcast_programs(
            arguments.get("radio_id"),
            arguments.get("limit", 30),
            arguments.get("offset", 0),
            arguments.get("order", "newest"),
        )
    if name == "search_podcasts":
        return search_podcasts(
            arguments.get("query"),
            arguments.get("limit", 20),
            arguments.get("offset", 0),
        )
    if name == "search_podcast_programs":
        return search_podcast_programs(
            arguments.get("query"),
            arguments.get("limit", 20),
            arguments.get("offset", 0),
        )
    if name == "get_recent_podcast_plays":
        return get_recent_podcast_plays(arguments.get("limit", 50))
    if name == "daily_recommend":
        return daily_recommend()
    if name == "preview_operation":
        return preview_operation(arguments.get("operation"), arguments.get("arguments"))
    if name == "get_operation_log":
        return get_operation_log(
            arguments.get("limit", 50),
            arguments.get("offset", 0),
            arguments.get("operation"),
            arguments.get("status"),
            arguments.get("created_after"),
            arguments.get("created_before"),
        )
    if name == "list_interaction_notes":
        return list_interaction_notes(
            arguments.get("playlist_id"),
            arguments.get("song_id"),
            arguments.get("author"),
            arguments.get("limit", 50),
            arguments.get("offset", 0),
        )
    if name == "create_playlist":
        return create_playlist(
            arguments.get("name"), arguments.get("description", ""), arguments.get("privacy", 10)
        )
    if name == "update_playlist":
        return update_playlist(
            arguments.get("playlist_id"),
            arguments.get("name"),
            arguments.get("description"),
        )
    if name == "add_to_playlist":
        return manipulate_playlist("add", arguments.get("playlist_id"), arguments.get("song_ids"))
    if name == "remove_from_playlist":
        return manipulate_playlist("del", arguments.get("playlist_id"), arguments.get("song_ids"))
    if name == "reorder_playlist_tracks":
        return reorder_playlist_tracks(
            arguments.get("playlist_id"), arguments.get("song_ids")
        )
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
                "serverInfo": {"name": "netease-music-mcp-safe", "version": "4.0.0"},
                "instructions": (
                    "Before every write, call preview_operation with the target tool name and arguments, "
                    "review its before/after states, then call that same write tool with the returned preview_token. "
                    "Never reuse a preview for different arguments. Interaction notes are private plugin-owned data, "
                    "not native NetEase comments."
                ),
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


def tool_error_response(request_id: Any, message: str) -> dict[str, Any]:
    """Return an MCP tool-level error without breaking the HTTP message stream."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        },
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
    server_version = "NetEaseMusicMCP/4.0"

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
                f'Bearer resource_metadata="{metadata}", scope="{OAUTH_SCOPE}"'
            )
        self._json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED, headers)
        return False

    def _authorization_page(self, params: dict[str, str], error: str = "") -> None:
        try:
            validate_authorization_request(params)
        except ValueError as exc:
            self._html("<h1>Invalid authorization request</h1><p>" + html.escape(str(exc)) + "</p>", 400)
            return
        requested_scopes = set(params.get("scope", "netease.read").split())
        write_requested = "netease.write" in requested_scopes
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
            for key, value in params.items()
            if key not in {"password", "confirm_write"}
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        access_text = (
            "Authorize access to read your NetEase music data and to create playlists, edit playlist "
            "names, descriptions, or covers; add, remove, or reorder playlist tracks; like or unlike songs; "
            "and manage private plugin-owned notes. Writes require a short-lived matching preview. "
            "ChatGPT will still apply its confirmation settings before write actions."
            if write_requested
            else "Authorize read-only access to your NetEase playlists, history, search and recommendations."
        )
        write_confirmation = (
            '<label class="warning"><input type="checkbox" name="confirm_write" value="yes" required> '
            "I understand this grants permission to modify my NetEase account.</label>"
            if write_requested
            else ""
        )
        self._html(
            "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width\">"
            "<title>Authorize Rain Music Room</title><style>"
            "body{font:16px system-ui;max-width:34rem;margin:10vh auto;padding:1.5rem;color:#202124}"
            "form{display:grid;gap:1rem}input,button{font:inherit;padding:.75rem}button{cursor:pointer}"
            ".error,.warning{color:#b3261e}</style></head><body>"
            "<h1>Rain Music Room</h1>"
            "<p>" + access_text + "</p>"
            + error_html
            + '<form method="post" action="/authorize">'
            + hidden
            + write_confirmation
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
        if path == "/mcp":
            # This server is intentionally stateless and does not expose an SSE
            # listener. Streamable HTTP permits a 405 response when GET streams
            # are unsupported. Do not emit Mcp-Session-Id on POST responses.
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self._cors()
            self.send_header("Allow", "POST, OPTIONS")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
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
                confirm_write = params.pop("confirm_write", "")
                validate_authorization_request(params)
                if "netease.write" in params.get("scope", "").split() and confirm_write != "yes":
                    self._authorization_page(params, "Confirm write access before continuing")
                    return
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
        request_id: Any = None
        tool_call = False
        try:
            body = json.loads(self._read_body())
            if not isinstance(body, dict):
                raise ValueError("JSON-RPC request must be an object.")
            request_id = body.get("id")
            tool_call = body.get("method") == "tools/call"
            if tool_call:
                params = body.get("params") or {}
                tool_name = str(params.get("name", "")) if isinstance(params, dict) else ""
                if tool_name in WRITE_TOOL_NAMES and not self._static_token_authorized():
                    claims = self._oauth_access_claims() or {}
                    if "netease.write" not in str(claims.get("scope", "")).split():
                        self._json(
                            tool_error_response(
                                request_id, "netease.write scope is required"
                            )
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
            if tool_call:
                self._json(tool_error_response(request_id, str(exc)))
            else:
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": str(exc)},
                    },
                    HTTPStatus.BAD_REQUEST,
                )
        except NetEaseError as exc:
            safe_message = _redact_secrets(exc)
            LOG.warning("NetEase operation failed: %s", safe_message)
            if tool_call:
                self._json(tool_error_response(request_id, safe_message))
            else:
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32001, "message": safe_message},
                    },
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
    if not READ_ONLY and not STORAGE_PATH:
        raise SystemExit(
            "MCP_STORAGE_PATH is required in read-write mode. Point it to a SQLite file on a persistent volume."
        )
    if not 30 <= PREVIEW_TTL_SECONDS <= 3600:
        raise SystemExit("MCP_PREVIEW_TTL_SECONDS must be between 30 and 3600.")
    if not 1 <= OPERATION_RETENTION_DAYS <= 3650:
        raise SystemExit("MCP_OPERATION_RETENTION_DAYS must be between 1 and 3650.")
    if not 100 <= MAX_OPERATION_LOGS <= 100000:
        raise SystemExit("MCP_MAX_OPERATION_LOGS must be between 100 and 100000.")
    if not 10 <= MAX_PENDING_PREVIEWS <= 10000:
        raise SystemExit("MCP_MAX_PENDING_PREVIEWS must be between 10 and 10000.")
    if not 1_048_576 <= MAX_IMAGE_BYTES <= 20_971_520:
        raise SystemExit("MCP_MAX_IMAGE_BYTES must be between 1 MiB and 20 MiB.")
    if not 1_000_000 <= MAX_IMAGE_PIXELS <= 100_000_000:
        raise SystemExit("MCP_MAX_IMAGE_PIXELS must be between 1 and 100 million.")
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
