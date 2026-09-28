# Deploy on Zeabur

Do not place a real NetEase cookie, session database, or access token in GitHub files. The
recommended NetEase login flow uses the MCP QR tools and a persistent volume; `NETEASE_COOKIE`
remains an optional compatibility fallback.

## 1. Import the repository

In Zeabur, create a project, add a service from GitHub, and select this repository. The service root
is the repository root. `zbpack.json` runs the tests during build and starts `python server.py`.

## 2. Add environment variables

| Variable | First-deployment value |
| --- | --- |
| `MCP_ACCESS_TOKEN` | A random value of at least 24 characters |
| `MCP_PUBLIC_URL` | `https://YOUR-DOMAIN.zeabur.app` |
| `MCP_OAUTH_PASSWORD` | A different random password of at least 16 characters |
| `MCP_HOST` | `0.0.0.0` |
| `MCP_READ_ONLY` | `true` |
| `LOG_LEVEL` | `INFO` |
| `MCP_STORAGE_PATH` | `/data/netease-music-mcp.sqlite3` |

Zeabur supplies `PORT`; the server reads it automatically.

Before the first QR login, attach a persistent volume mounted at `/data`. The following limits can
keep their defaults unless the deployment needs different bounds:

| Variable | Recommended value |
| --- | --- |
| `MCP_OPERATION_RETENTION_DAYS` | `90` |
| `MCP_MAX_OPERATION_LOGS` | `1000` |
| `MCP_MAX_IMAGE_BYTES` | `5242880` |
| `MCP_MAX_IMAGE_PIXELS` | `25000000` |

The SQLite file contains the NetEase runtime session, private interaction notes, and sanitized
operation history. The application filesystem without a volume is ephemeral and must not be used
for this data. Treat the database and its backups as credentials. Run one service instance against
one SQLite file. Back up the volume or use a SQLite-consistent backup before migration or uninstall.

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

After connecting the MCP client, run `start_netease_qr_login`. Its MCP result includes a direct
NetEase HTTPS `qr_url` and a PNG image content block. Scan and confirm the image with the NetEase
App, then call `check_netease_qr_login` with the returned `login_id`.
On `confirmed`, the session is already in SQLite. Future NetEase re-login does not require changing
Zeabur environment variables or redeploying. An old `NETEASE_COOKIE` may remain during migration;
after a verified runtime session exists, SQLite takes precedence.

## 4. Keep the first deployment read-only

Do not change `MCP_READ_ONLY` until search, playlists, history and daily recommendations have all
been tested. When write mode is later enabled, clients should prompt before every account-changing
tool.

To enable write tools, confirm the persistent volume and database path first, then set
`MCP_READ_ONLY=false`, redeploy, refresh the app's action definitions, and disconnect and reconnect
the ChatGPT app to grant `netease.write` and reload the current tool schemas. Every enabled write
tool executes in one call after backend argument, ownership, state, and file-safety checks. Audit
logs, before/after snapshots, idempotency, undo, and partial/unknown status handling remain active.

Delete these deprecated variables from Zeabur after deploying this version:

- `MCP_WRITE_PREVIEW_POLICY`
- `MCP_REQUIRE_WRITE_PREVIEW`
- `MCP_PREVIEW_TTL_SECONDS`
- `MCP_MAX_PENDING_PREVIEWS`

If they remain temporarily, the server logs their names as deprecated and ignores their values;
they cannot restore strict or risk-based behavior and do not cause startup rejection.

On the first deployment of this version, the existing SQLite database automatically gains the
singleton `netease_session` table. No manual migration command is required. Take a SQLite-consistent
backup or volume snapshot before redeploying. Reconnect the ChatGPT app so the new
`netease.session` scope and login tools are authorized. Confirm that the authorization page lists
session management before continuing; when write mode is enabled it must also list account changes.
