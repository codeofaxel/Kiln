# Security Policy

## Reporting a Vulnerability

**DO NOT** open a public GitHub issue for security vulnerabilities.

If you discover a security issue in Kiln, please report it privately:

**Email:** security@kilnmcp.dev

Include in your report:

- Description of the vulnerability
- Steps to reproduce
- Potential impact (e.g. unauthorized printer control, credential exposure)
- Suggested fix (if any)

We will acknowledge your report within 48 hours and provide a timeline for a fix.

## Scope

Kiln controls physical 3D printers. Security issues with direct hardware impact
are treated with the highest priority:

- **Critical:** Unauthorized print commands, temperature manipulation, G-code
  injection, credential theft from config files
- **High:** Authentication bypass, API key exposure, arbitrary file read/write
- **Medium:** Denial of service, information disclosure, privilege escalation
  within the tool

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Security Practices

- API keys are stored in `~/.kiln/config.yaml` with restricted file permissions
- G-code commands are validated before execution
- Preflight safety checks run automatically before prints
- Network requests to printers use configurable timeouts and retry limits
- All MCP tool actions that modify printer state require authentication when enabled
