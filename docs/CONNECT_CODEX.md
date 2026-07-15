# Connect Codex after deployment

The ChatGPT desktop app, Codex CLI and IDE extension share their MCP configuration. Add a
Streamable HTTP server with:

- URL: `https://YOUR-DOMAIN/mcp`
- Bearer-token environment variable: `NETEASE_MCP_TOKEN`

Set `NETEASE_MCP_TOKEN` locally to the same value as Zeabur's `MCP_ACCESS_TOKEN`. Do not put the
literal token in a committed config file.

Equivalent project-scoped configuration:

```toml
[mcp_servers.netease_music]
url = "https://YOUR-DOMAIN/mcp"
bearer_token_env_var = "NETEASE_MCP_TOKEN"
default_tools_approval_mode = "writes"
tool_timeout_sec = 30
```

Restart the client after saving, then use `/mcp` to confirm the server is connected.

## Connect ChatGPT Work on the web

Configure `MCP_PUBLIC_URL` and `MCP_OAUTH_PASSWORD` on the server first. Then in ChatGPT:

1. Open **Settings → Security and login** and enable **Developer mode**.
2. Open **Settings → Plugins**, select the plus button, and create a developer-mode app.
3. Enter `https://YOUR-DOMAIN/mcp` and choose OAuth/discovered authentication.
4. Complete the browser authorization page with `MCP_OAUTH_PASSWORD`.

If the server is later changed from read-only to write mode, refresh the app's tool definitions,
disconnect it, and reconnect it. Existing refresh tokens retain their original read-only scope and
must not be silently upgraded. The new authorization page will explicitly request write access.

Also refresh, disconnect, and reconnect after deploying new tools or changing their input schema;
otherwise ChatGPT may continue using a cached tool list from the previous deployment.

Hosted Work cannot read an environment variable from the user's computer, so do not add a static
`Authorization` header to a plugin manifest. The bearer-token configuration above remains available
for local Codex clients.
