# NetEase Music MCP — safe deployment edition

An authenticated, read-only-by-default MCP server for connecting ChatGPT or Codex to a NetEase
Cloud Music account. It is derived from
[Vael-KY/netease-music-mcp](https://github.com/Vael-KY/netease-music-mcp) under the MIT license.

## Safety changes

- Bearer-token authentication is mandatory; the server refuses to start without it.
- Optional OAuth 2.1 authorization supports hosted MCP clients without embedding a bearer token.
- Read-only mode is enabled by default and hides all account-changing tools.
- Local use binds to `127.0.0.1` by default.
- Public wildcard CORS is removed.
- Inputs, request sizes and upstream responses are validated.
- Upstream tool failures are returned as MCP error content so one failed action does not break the chat stream.
- Streamable HTTP runs statelessly and does not advertise a session or SSE stream that the server cannot maintain.
- Cookies are read only from environment variables and are never returned by `/health`.
- Destructive tools are clearly identified and require `MCP_READ_ONLY=false`.
- Production writes use a short-lived, state-bound preview token and a bounded persistent audit log.
- Private interaction notes and audit records are isolated by the current NetEase user ID.

## Available tools

Read-only mode provides:

- `search_song(query, limit=5)`: search without changing the account.
- `list_my_playlists()`: list owned and collected playlists.
- `get_playlist_songs(playlist_id, limit=50, offset=0)`: read a playlist page. `limit` must be
  1–100 and `offset` must be non-negative. The result includes returned count, total track count,
  `has_next`, and `next_offset`.
- `get_song_details(song_id=...)` or `get_song_details(song_ids=[...])`: return metadata for one
  song or up to 50 songs, including artists, album, duration, publish time, aliases/translations,
  and upstream version metadata. Live/remix/remaster/cover flags are reported only when explicit
  upstream tags support them; the server never guesses from a title.
- `get_play_history(limit=30, all_time=false)`: return NetEase's weekly or all-time aggregated
  per-song play ranking. These are counts, not individual listening events.
- `get_recent_plays(limit=100)`: return the upstream recent-song event list without reordering it,
  with raw millisecond and ISO 8601 timestamps when available.
- `list_my_subscribed_podcasts(limit=30, offset=0)`: list subscribed podcast/radio containers.
- `get_podcast_programs(radio_id, limit=30, offset=0, order="newest")`: list programs/episodes in
  one podcast container. `order` may be `newest` or `oldest`.
- `search_podcasts(query, limit=20, offset=0)`: search podcast/radio containers.
- `search_podcast_programs(query, limit=20, offset=0)`: search programs/episodes.
- `get_recent_podcast_plays(limit=50)`: return recent podcast-program resources in upstream order,
  with a timestamp only when NetEase supplies one. It is not presented as a complete event stream.
- `daily_recommend()`: read personalized daily recommendations.
- `preview_operation(operation, arguments)`: read the current state and proposed state without
  modifying anything, then issue a short-lived token bound to that exact operation and state.
- `get_operation_log(...)`: read sanitized, bounded audit records with pagination and optional
  operation, status, and ISO 8601 time filters.
- `list_interaction_notes(playlist_id, song_id?, author?, limit=50, offset=0)`: read private
  plugin-owned playlist or track notes.

Read-write mode additionally provides `create_playlist`, `update_playlist`, `add_to_playlist`,
`remove_from_playlist`, `reorder_playlist_tracks`, `like_song`, `undo_operation`,
`update_playlist_cover`, `create_interaction_note`, `update_interaction_note`, and
`delete_interaction_note`.

### Experimental podcast read-only dry run

The five podcast tools above are an experimental, read-only dry-run surface. Their request and
response handling is covered by synthetic fixtures with every network call mocked; this version has
not read a real account. Before publishing it, test the authenticated response shapes with a
non-sensitive account and review the normalized output. No podcast write action is included.

NetEase uses several related but distinct objects. This server keeps their identifiers separate:

- `radio_id` identifies the podcast/radio container (called `djRadio` or `voicelist` upstream);
- `program_id` identifies a program/episode (called `program` or `voice` upstream);
- `main_track_id`, when present, is NetEase's audio carrier for that program. It is deliberately not
  returned as `song_id`, because a podcast program is not a normal song resource.

Example page request:

```json
{
  "name": "get_podcast_programs",
  "arguments": {"radio_id": 123456, "limit": 20, "offset": 20, "order": "newest"}
}
```

`radio.playCount` and `program.listenerCount` are normalized as
`public_total_play_count` and `public_listener_count`. They are public aggregates, not the current
user's personal listening count. The investigated upstream API does not expose a reliable count of
how many times the current user played one program, so this server does not provide or infer one.

The recent-program endpoint accepts only `limit` in the investigated implementation. It does not
offer offset or time-range paging and is not documented as a complete per-play event ledger. The
server preserves the upstream order, emits `played_at` only from an actual `playTime`, and returns
`personal_play_count_supported: false`. An unrecognized or aggregate-only response produces no fake
events. The container-level recent-radio endpoint was investigated but is not exposed because it is
less precise than the program-level endpoint.

After deploying this version, refresh the app's action definitions and disconnect/reconnect the
ChatGPT app before expecting the new podcast tool schemas to appear.

### Preview, execute, audit, and undo

The safe production flow is always two calls. First preview the intended write:

```json
{
  "name": "preview_operation",
  "arguments": {
    "operation": "update_playlist",
    "arguments": {"playlist_id": 123456, "name": "Night Signals"}
  }
}
```

After reviewing `before_state`, `expected_after_state`, risk, and reversibility, call the original
tool with exactly the same arguments plus the returned token:

```json
{
  "name": "update_playlist",
  "arguments": {
    "playlist_id": 123456,
    "name": "Night Signals",
    "preview_token": "RETURNED_SHORT_LIVED_TOKEN"
  }
}
```

Tokens expire after five minutes by default, are stored only as SHA-256 hashes, are single-use,
and are bound to the NetEase user, operation, normalized arguments, and resource-state hash. A
duplicate request with an already successful token returns the recorded result instead of writing
again. If the playlist, like state, note version, or target permission changes after preview, the
write is rejected as a conflict. A process interruption leaves an old in-progress record as
`unknown`; it is never retried automatically because the upstream result may be ambiguous.

Every previewed write records sanitized arguments, target, timestamps, before/after state, status,
reversibility, undo state, and a redacted error summary. `get_operation_log` supports `limit`,
`offset`, `operation`, `status`, `created_after`, and `created_before`. Logs are retained for at
most 90 days and 1,000 records by default. Direct legacy writes can be enabled with
`MCP_REQUIRE_WRITE_PREVIEW=false` for migration testing, but they do not provide the audit/undo
guarantees and must not be used in production.

`undo_operation` itself must be previewed. It permits only a successful record marked reversible,
checks that the recorded after-state is still current, writes its own audit record, and refuses a
second undo. Supported restoration paths are:

- playlist name and description;
- tracks added by one operation;
- removed tracks, followed by best-effort restoration of their complete old order;
- a previous complete playlist order;
- the previous liked/unliked state;
- private-note creation, update, and soft deletion.

Playlist creation is not undone by deleting a playlist. Cover replacement is also marked
irreversible because NetEase does not reliably provide the original uploaded cover file.

### Playlist pagination example

The original call remains valid and returns the first page with the default size:

```json
{"name":"get_playlist_songs","arguments":{"playlist_id":123456}}
```

Request the third 25-song page with:

```json
{"name":"get_playlist_songs","arguments":{"playlist_id":123456,"limit":25,"offset":50}}
```

NetEase's playlist-detail response may contain only a partial `tracks` array, so the server uses
the complete upstream `trackIds` list and fetches details only for the requested page.

### Aggregated history versus recent play events

`get_play_history` uses NetEase's listening-rank endpoint. It groups data by song and may provide a
play count or score, but no timestamp for each listen. `get_recent_plays` uses the separate recent
song endpoint, which can provide individual `playTime` values and terminal labels. That endpoint
accepts only `limit` (up to 100); it does not expose a supported time-range filter or offset cursor.
If NetEase returns only aggregate data, the tool labels it `aggregated_play_counts` and does not
fabricate event timestamps.

### Reordering playlist tracks

`reorder_playlist_tracks(playlist_id, song_ids)` is a write operation. It accepts the complete new
order for an owned playlist only. Before writing, the server verifies ownership and requires every
existing song ID exactly once—no omissions, replacements, duplicates, or empty lists. It uses
NetEase's order-update operation and verifies the resulting full order. It never simulates sorting
by deleting and re-adding tracks. For request-size safety, at most 10,000 IDs are accepted.

### Updating a playlist cover

`update_playlist_cover(playlist_id, image, preview_token)` is a high-risk write for owned playlists.
In ChatGPT, attach a PNG or JPEG as the `image` file parameter; the tool declares the top-level file
field through `_meta["openai/fileParams"]` as described in the
[official Apps SDK file-handling guide](https://developers.openai.com/apps-sdk/build/mcp-server#file-handling).
The server receives ChatGPT's temporary file reference,
not an arbitrary `image_url`. Plain URL strings are rejected, and every HTTPS download and redirect
is checked against private, loopback, and link-local destinations.

The input defaults to a 5 MiB compressed-size limit and 25 million pixels. MIME type, extension,
and decoded image format must agree. The server decodes the image with Pillow, applies EXIF
orientation, center-crops it to a square, resizes it to 300x300, converts it to JPEG, and writes a
new image without EXIF or other source metadata. Processing is in memory; no upload temporary file
is created. The processed preview image is removed from SQLite after execution or expiry.

NetEase's undocumented flow allocates a short-lived NOS upload credential, uploads the JPEG, then
updates the owned playlist cover. The NOS credential and ChatGPT download URL are never logged.

### Private interaction notes

Interaction notes are this plugin's extension data; NetEase does not expose a native per-track
private-note API. A note may target a playlist or one current track and contains `author`, `content`,
private visibility, timestamps, and a monotonically increasing `version`. Updates require the
current version, so concurrent edits fail instead of overwriting each other. Deletion is a soft
delete and can be undone while the matching audit record remains available.

Notes are stored in the SQLite file configured by `MCP_STORAGE_PATH`, partitioned by the current
NetEase user ID. Any client authorized with this server's `netease.read` scope for that account can
read them. Notes are never copied into the public playlist description. Listing notes resolves the
current song name and artists; a track removed from the playlist is marked `stale`.

Back up the SQLite database with a volume snapshot or SQLite-consistent backup while the service is
stopped. To migrate, move that database together with the service configuration and mount it at the
new `MCP_STORAGE_PATH`. Before uninstalling, export or retain the database if the notes and audit
history should survive. Removing the database permanently removes plugin notes and logs but does
not modify NetEase data.

## Local development

1. Copy `.env.example` to `.env` and fill it locally. Do not commit `.env`.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. For read-write mode, set `MCP_STORAGE_PATH` to a durable local SQLite path.
4. Export the variables using a method appropriate for your shell.
5. Run `python server.py`.
6. Check `http://127.0.0.1:3456/health`.

Run the test suite without NetEase credentials:

```bash
python -m unittest discover -s tests -v
```

## Zeabur

Deploy the repository as a Python service with these settings:

- Root directory: repository root
- Start command: `python server.py`
- Port: supplied through Zeabur's `PORT` variable (see `zbpack.json`)
- Health endpoint: `/health`

Set the following environment variables in Zeabur's dashboard, never in Git:

- `NETEASE_COOKIE`: `MUSIC_U=...; __csrf=...`
- `MCP_ACCESS_TOKEN`: a random secret of at least 24 characters
- `MCP_READ_ONLY`: keep `true` for the first deployment
- `MCP_HOST`: `0.0.0.0`
- `MCP_PUBLIC_URL`: public HTTPS origin, for example `https://YOUR-SERVICE.zeabur.app`
- `MCP_OAUTH_PASSWORD`: a separate random password of at least 16 characters for one-time browser authorization
- `MCP_STORAGE_PATH`: for example `/data/netease-music-mcp.sqlite3` on a mounted persistent volume

The MCP endpoint will be `https://YOUR-SERVICE.zeabur.app/mcp`.

### Optional write mode

After all read tools have been tested, mount a persistent volume at `/data`, keep
`MCP_REQUIRE_WRITE_PREVIEW=true`, and set `MCP_READ_ONLY=false` to expose account and private-note
writes. The OAuth page then shows a separate write-access warning and requires explicit
confirmation. Keep ChatGPT's write-action confirmations enabled while testing. Use one service
instance per SQLite file; horizontal multi-writer deployment requires a database backend that this
version does not yet provide.

After deploying a version with new or changed tools, refresh the app's action definitions, then
disconnect and reconnect the ChatGPT app so it reloads the tool schema and obtains the current
OAuth scopes. Existing refresh tokens do not silently gain broader permissions.

Remote MCP cannot make a particular phone or computer start playback. Playback requires a local
player or device-control integration and is intentionally outside this hosted server.

When both OAuth variables are set, compatible remote clients discover OAuth automatically from the
server's protected-resource metadata. The user enters `MCP_OAUTH_PASSWORD` in the authorization
page once; access tokens last one hour and refresh automatically for up to 30 days. Reauthorization
is normally required only after the refresh token expires, credentials are changed, or authorization
is revoked. Keep the original `MCP_ACCESS_TOKEN`: local Codex clients can continue using it.

## Important limitations

This project calls undocumented NetEase web endpoints. They may change, expire, or trigger account
risk controls. Test read-only tools first. A NetEase cookie grants account access: do not paste it
into a chat, commit it, place it in screenshots, or expose it in logs.

Song detail and version fields are limited to metadata actually returned by NetEase. Recent plays
may omit device, completion state, or timestamps when the upstream omits them. Playlist paging and
safe reordering require the complete `trackIds` list; the server refuses to guess when that list is
incomplete. The reorder endpoint is undocumented and may stop working if NetEase changes it.

Podcast/radio interfaces are likewise undocumented. Subscription listing requires an authenticated
NetEase session. Search and public program metadata can expose public aggregate play/listener counts,
but the investigated upstream data does not provide a reliable current-user per-program play count.
Recent podcast resources may omit timestamps and are not guaranteed to represent every play event.

Cover upload and cover update are also undocumented NetEase web interfaces. The project cannot
guarantee restoration of an overwritten cover or distinguish every upstream partial-success case.
An interrupted write is recorded as failed, partial, or eventually `unknown` and is never retried
automatically. SQLite protects local concurrency with transactions and WAL mode, but a single
persistent SQLite file is not intended for horizontally scaled service replicas.
