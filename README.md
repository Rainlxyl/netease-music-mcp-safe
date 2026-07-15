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
- `daily_recommend()`: read personalized daily recommendations.

Read-write mode additionally provides `create_playlist`, `update_playlist`, `add_to_playlist`,
`remove_from_playlist`, `reorder_playlist_tracks`, and `like_song`.

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

## Local development

1. Copy `.env.example` to `.env` and fill it locally. Do not commit `.env`.
2. Export the variables using a method appropriate for your shell.
3. Run `python server.py`.
4. Check `http://127.0.0.1:3456/health`.

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

The MCP endpoint will be `https://YOUR-SERVICE.zeabur.app/mcp`.

### Optional write mode

After all read tools have been tested, set `MCP_READ_ONLY=false` to expose six account-changing
tools: create or edit a playlist, add, remove, or reorder tracks, and like or unlike a song. The
OAuth page then shows a separate write-access warning and requires explicit confirmation. Keep
ChatGPT's write-action confirmations enabled while testing.

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
