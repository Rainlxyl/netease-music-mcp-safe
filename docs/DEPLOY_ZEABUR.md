# Deploy on Zeabur

Do not place a real NetEase cookie or access token in GitHub files. Add both through Zeabur's
environment-variable settings only.

## 1. Import the repository

In Zeabur, create a project, add a service from GitHub, and select this repository. The service root
is the repository root. `zbpack.json` runs the tests during build and starts `python server.py`.

## 2. Add environment variables

| Variable | First-deployment value |
| --- | --- |
| `NETEASE_COOKIE` | `MUSIC_U=...; __csrf=...` |
| `MCP_ACCESS_TOKEN` | A random value of at least 24 characters |
| `MCP_PUBLIC_URL` | `https://YOUR-DOMAIN.zeabur.app` |
| `MCP_OAUTH_PASSWORD` | A different random password of at least 16 characters |
| `MCP_HOST` | `0.0.0.0` |
| `MCP_READ_ONLY` | `true` |
| `LOG_LEVEL` | `INFO` |

Zeabur supplies `PORT`; the server reads it automatically.

Before enabling writes, attach a persistent volume mounted at `/data` and add:

| Variable | Recommended value |
| --- | --- |
| `MCP_STORAGE_PATH` | `/data/netease-music-mcp.sqlite3` |
| `MCP_WRITE_PREVIEW_POLICY` | `strict` |
| `MCP_REQUIRE_WRITE_PREVIEW` | `true` |
| `MCP_PREVIEW_TTL_SECONDS` | `300` |
| `MCP_OPERATION_RETENTION_DAYS` | `90` |
| `MCP_MAX_OPERATION_LOGS` | `1000` |
| `MCP_MAX_IMAGE_BYTES` | `5242880` |
| `MCP_MAX_IMAGE_PIXELS` | `25000000` |

The SQLite file contains private interaction notes, pending preview state, and sanitized operation
history. The application filesystem without a volume is ephemeral and must not be used for this
data. Run one service instance against one SQLite file. Back up the volume or use a
SQLite-consistent backup before migration or uninstall.

## 3. Create a domain

Generate a Zeabur domain for the service. Open `https://YOUR-DOMAIN/health`; the expected response
is:

```json
{"status": "ok", "mode": "read-only"}
```

The MCP endpoint is `https://YOUR-DOMAIN/mcp`. Opening it in a normal browser is not a valid MCP
test because the endpoint accepts authenticated JSON-RPC POST requests.

`MCP_OAUTH_PASSWORD` is entered only in the server's authorization page. Do not put it in GitHub,
plugin files, screenshots or chat. A successful OAuth login issues a one-hour access token and a
30-day refresh token, so users are not expected to sign in daily.

## 4. Keep the first deployment read-only

Do not change `MCP_READ_ONLY` until search, playlists, history and daily recommendations have all
been tested. When write mode is later enabled, clients should prompt before every account-changing
tool.

To enable write tools, confirm the persistent volume and database path first, then set
`MCP_READ_ONLY=false`, redeploy, refresh the app's action definitions, and disconnect and reconnect
the ChatGPT app to grant `netease.write` and reload the current tool schemas. Keep
`MCP_WRITE_PREVIEW_POLICY=strict` for the original behavior: ChatGPT must call
`preview_operation`, show the proposed state, and pass its short-lived token to the matching write.
The legacy `MCP_REQUIRE_WRITE_PREVIEW` is consulted only when the new variable is absent; if both
are set, `MCP_WRITE_PREVIEW_POLICY` wins and a conflict is reported in startup logs without secrets.

After strict mode has been verified, `MCP_WRITE_PREVIEW_POLICY=risk_based` may be used to permit
only owned-playlist additions of at most 10 songs, likes (`like=true`), and validated private-note
creation as single-call audited writes. All other writes still require preview. Do not set the
legacy boolean to `false`; that selects the old unaudited compatibility behavior only when the new
policy variable is absent.

On the first deployment of this version, the existing SQLite database automatically gains
`upstream_action_started`, a hashed idempotency-key column, and its partial unique index. Take a
SQLite-consistent backup or volume snapshot before redeploying. Confirm that the authorization page
lists the account changes before entering the private OAuth password. Do not approve write access if
the page still describes the connection as read-only.
