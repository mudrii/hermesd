# Security Policy

## Supported Versions

Only the latest release of hermesd is supported with security fixes. Please upgrade to the newest version before reporting an issue.

| Version | Supported |
|---------|-----------|
| Latest release | Yes |
| Older releases | No |

## Reporting a Vulnerability

Please report vulnerabilities privately via [GitHub private vulnerability reporting](https://github.com/mudrii/hermesd/security/advisories/new) rather than opening a public issue. If private reporting is unavailable, open a minimal issue asking for a private contact channel and do not include exploit details.

Please include your hermesd version (`hermesd --version`), the Hermes Agent version, and steps to reproduce.

## Security Model Notes

- **Read-only by design** — hermesd never writes to `~/.hermes/` and opens all SQLite databases with `mode=ro`. The only write path is the explicit `--snapshot-file PATH` export, which is rejected when the target is under the Hermes home.
- **Secret redaction** — `~/.hermes/` contains real user secrets (API keys, auth tokens, credentials). hermesd redacts secret-bearing config values, MCP server arguments/URLs, and credential pool metadata before rendering, but snapshots may still include sensitive paths and identifiers from your environment — review output before sharing.
- **No network access** — hermesd makes no network requests; it only reads local files.
