# Security Policy

## Reporting

Use GitHub Issues to report a security concern. Do not include credentials, private repository contents, cached test logs, or exploit data that would put other users at immediate risk. If sensitive coordination is required, open an issue containing only a minimal impact summary and request a private follow-up channel.

Report vulnerabilities in Codex itself through OpenAI's security process rather than this repository.

## Security boundaries

- Repository configuration contains shell commands and must be treated as executable code.
- Cached results and logs remain local; the plugin does not upload them.
- Cache-key changes must fail closed: uncertainty produces a cache miss.
- Only successful test exits are cached.

The latest released minor version receives security fixes.
