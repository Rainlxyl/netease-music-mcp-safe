# Security Policy

This is a personal, independent open-source project. Each deployment owner is responsible for
protecting the credentials and account configured for that deployment.

## Report issues without secrets

- Never include a real NetEase Cookie, MCP Bearer token, OAuth password, access/refresh token,
  private playlist data, personal account ID, or raw operation log in a public GitHub issue.
- Security problems that can be described without sensitive data may be reported through a GitHub
  issue. Redact account-specific values and use synthetic reproduction data.
- This repository does not publish a private security email or response-time SLA. Do not post a
  secret publicly in order to request private contact.

## If a credential was exposed

Treat it as compromised. Replace the deployment's `MCP_ACCESS_TOKEN` and `MCP_OAUTH_PASSWORD`,
invalidate or renew the exposed NetEase account session/Cookie through account controls, and remove
the secret from logs, screenshots, issue text, and deployment history where possible.

## Deployment boundary

Current architecture is **one deployment → one NetEase account**. The NetEase session belongs only
in the server deployment environment or its persistent SQLite database. OAuth and Bearer
authentication control access to that deployment; they do not provide separate NetEase logins for
different users. Share the repository, not a private deployment's credentials. Other users should
deploy their own instance with their own secrets.

The SQLite file, WAL/SHM files, and backups may contain the allowlisted `MUSIC_U` and `__csrf`
session fields. Protect persistent-volume and backup access as credential access. The session is not
printed to logs or returned through MCP tools. This personal single-instance deployment does not add
a second database-encryption key; service or platform administrators who can read both the volume
and process environment are inside the deployment's trusted administrative boundary.
