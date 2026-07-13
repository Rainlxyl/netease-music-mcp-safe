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
- Cookies are read only from environment variables and are never returned by `/health`.
- Destructive tools are clearly identified and require `MCP_READ_ONLY=false`.

## Available tools

Read-only mode provides search, playlist listing, playlist contents, play history and daily
recommendations. Read-write mode additionally provides playlist creation, track addition/removal,
and like/unlike.

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

When both OAuth variables are set, compatible remote clients discover OAuth automatically from the
server's protected-resource metadata. The user enters `MCP_OAUTH_PASSWORD` in the authorization
page once; access tokens last one hour and refresh automatically for up to 30 days. Reauthorization
is normally required only after the refresh token expires, credentials are changed, or authorization
is revoked. Keep the original `MCP_ACCESS_TOKEN`: local Codex clients can continue using it.

## Important limitations

This project calls undocumented NetEase web endpoints. They may change, expire, or trigger account
risk controls. Test read-only tools first. A NetEase cookie grants account access: do not paste it
into a chat, commit it, place it in screenshots, or expose it in logs.
