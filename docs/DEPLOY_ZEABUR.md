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
| `MCP_HOST` | `0.0.0.0` |
| `MCP_READ_ONLY` | `true` |
| `LOG_LEVEL` | `INFO` |

Zeabur supplies `PORT`; the server reads it automatically.

## 3. Create a domain

Generate a Zeabur domain for the service. Open `https://YOUR-DOMAIN/health`; the expected response
is:

```json
{"status": "ok", "mode": "read-only"}
```

The MCP endpoint is `https://YOUR-DOMAIN/mcp`. Opening it in a normal browser is not a valid MCP
test because the endpoint accepts authenticated JSON-RPC POST requests.

## 4. Keep the first deployment read-only

Do not change `MCP_READ_ONLY` until search, playlists, history and daily recommendations have all
been tested. When write mode is later enabled, clients should prompt before every account-changing
tool.

