# Security Policy

Kiln is agentic infrastructure that controls physical 3D printers. Vulnerabilities in this
software can result in hardware damage, fire, injury, or unauthorized access to physical
machines. We take security reports seriously.

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | Yes                |
| < 0.1   | No                 |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email **security@kiln3d.com** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The affected component (MCP server, CLI, REST API, printer adapter, payment processing)
- Your preferred attribution name (for credit in release notes)

### Response Timeline

| Stage                  | Target    |
|------------------------|-----------|
| Acknowledgement        | 48 hours  |
| Initial assessment     | 7 days    |
| Fix development        | 30 days   |
| Public disclosure      | 90 days   |

We will work with you to understand and validate the report. If we cannot reproduce the
issue, we will ask for additional information. Critical vulnerabilities affecting physical
safety (e.g., bypassing temperature limits or preflight checks) will be prioritized above
all other work.

## Security Model

Kiln implements defense in depth across five layers:

### Physical Safety

- **Preflight checks** validate printer state, loaded material, and file existence before
  every print. Bypassing `preflight_check()` is treated as a security defect.
- **G-code validation** blocks dangerous commands (firmware reset, unsafe motion, raw
  stepper control) before they reach hardware.
- **Per-printer safety profiles** enforce temperature, feedrate, and flow limits for 27
  printer models. Limits are validated server-side and cannot be overridden by agents.
- **Heater watchdog** automatically cools idle heaters to prevent thermal runaway.

### Authentication and Authorization

- Optional API key authentication with scope-based access control (`print`, `files`,
  `queue`, `temperature`, `admin`).
- Read-only tools (status, listing) never require authentication.
- Keys support rotation and expiry. Scopes follow the principle of least privilege.

### Network Security

- **SSRF prevention** in webhook delivery: URLs are validated and private/internal
  network ranges are blocked.
- **Rate limiting** on API endpoints to prevent abuse.
- **CORS controls** on the REST API server.
- **HMAC-SHA256 webhook signatures** for payload integrity verification.

### Agent Safety

- **Prompt injection defense**: tool results are sanitized before returning to agents.
  Raw printer API responses are never passed through to the MCP layer.
- **Privacy mode**: agent memory can be cleaned, and secrets are masked in all log output.
- **Tool tiering**: smaller models receive a reduced tool set, limiting the blast radius
  of a compromised or confused agent.

### Data Safety

- **Parameterized SQL** for all database operations (SQLite). No string concatenation in
  queries.
- **No `eval()` or `exec()`** anywhere in the codebase.
- **Secret masking** in logs: API keys, tokens, and credentials are redacted before
  logging.
- **Audit log integrity**: hash-chained audit logs with tamper detection
  (`verify_audit_integrity`).

## Scope

### In Scope

- The Kiln MCP server (`kiln serve`)
- The Kiln CLI (`kiln`)
- The REST API server (`kiln rest`)
- Printer adapters (OctoPrint, Moonraker, Bambu, Prusa Connect)
- Payment processing (Stripe, Circle)
- Fulfillment service integrations (Craftcloud, Sculpteo)
- 3DOS network gateway
- G-code validation and safety profiles
- Authentication and authorization logic
- Webhook delivery and signature verification
- The `octoprint-cli` package

### Out of Scope

- Third-party printer firmware (OctoPrint, Klipper, Bambu firmware)
- Third-party marketplace APIs (MyMiniFactory, Cults3D, Thingiverse)
- Third-party slicer software (PrusaSlicer, OrcaSlicer)
- Vulnerabilities that require physical access to the host machine
- Social engineering attacks against project maintainers
- Denial of service against third-party services

## Bug Bounty

We do not currently operate a formal bug bounty program. However, we will credit
reporters by name (or pseudonym) in our CHANGELOG and release notes for any confirmed
vulnerability. If you would like to be credited differently, let us know in your report.

## Disclosure Policy

We follow a **90-day coordinated disclosure** timeline:

1. Reporter emails **security@kiln3d.com** with the vulnerability details.
2. We acknowledge receipt within 48 hours and begin assessment.
3. We work with the reporter to validate and develop a fix.
4. We release the fix and publish a security advisory.
5. 90 days after the initial report, the reporter may publicly disclose the vulnerability
   regardless of fix status. We ask that reporters coordinate with us on disclosure timing
   when a fix is imminent.

If a vulnerability is being actively exploited in the wild, we may accelerate this
timeline and issue an emergency patch.

## Contact

- **Security reports**: security@kiln3d.com
- **General bugs**: [GitHub Issues](https://github.com/codeofaxel/Kiln/issues)
- **Project documentation**: [docs/PROJECT_DOCS.md](docs/PROJECT_DOCS.md)
- **Automated scanners**: [`.well-known/security.txt`](.well-known/security.txt) (RFC 9116)
