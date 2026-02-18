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
- **Per-printer safety profiles** enforce temperature, feedrate, and flow limits for 28
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
- Printer adapters (OctoPrint, Moonraker, Bambu, Prusa Link)
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

## PGP Public Key

```
-----BEGIN PGP PUBLIC KEY BLOCK-----

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
=luPw
-----END PGP PUBLIC KEY BLOCK-----
```
