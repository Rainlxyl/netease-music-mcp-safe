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

ChatGPT Work on the web uses a plugin containing this remote MCP configuration. That plugin is
generated only after the final Zeabur domain is known, so it does not ship with a fake endpoint.

