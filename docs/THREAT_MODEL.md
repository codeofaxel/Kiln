# Kiln Threat Model

**Version 1.0 — February 2026**
**Methodology: STRIDE (Microsoft Threat Modeling Framework)**

## 1. Scope

This threat model covers the following components of the Kiln system:

| Component | Interface | Transport |
|---|---|---|
| MCP Server (`kiln serve`) | 197 MCP tools via FastMCP | stdio (local) |
| REST API (`kiln rest`) | HTTP endpoints via FastAPI/uvicorn | TCP (local or network) |
| CLI (`kiln`, `octoprint-cli`) | Click commands | Local process |
| Printer Adapters | OctoPrint (HTTP), Moonraker (HTTP), Bambu Lab (MQTT/FTPS), Prusa Link (HTTP) | LAN or WAN |
| Payment Processing | Stripe (HTTPS), Circle USDC (HTTPS), on-chain (Solana, Ethereum, Base L2) | HTTPS / RPC |
| Fulfillment Providers | Craftcloud (HTTPS), Sculpteo (HTTPS), 3DOS (HTTPS) | HTTPS |
| Webhook Delivery | Outbound HTTP POST to registered endpoints | HTTPS |
| Cloud Sync | Outbound HTTPS to remote API | HTTPS |
| Plugin System | Python entry points (`kiln.plugins` group) | In-process |
| Persistence Layer | SQLite database (`~/.kiln/kiln.db`) | Local filesystem |
| Credential Store | SQLite with PBKDF2+XOR encryption | Local filesystem |

**Out of scope:** Third-party printer firmware, third-party slicer binaries, third-party marketplace APIs (except as integration points), and attacks requiring physical access to the host machine.

## 2. Trust Boundaries

```
                    TRUST BOUNDARY 1: Agent/User ↔ Kiln
                    ====================================

  ┌─────────────┐        ┌──────────────────────────────────┐
  │  AI Agent   │──MCP──→│          Kiln MCP Server          │
  │ (untrusted  │  stdio │  ┌────────────┐ ┌──────────────┐ │
  │  output)    │←───────│  │ AuthManager │ │ Tool Tiering │ │
  └─────────────┘        │  └────────────┘ └──────────────┘ │
                         │  ┌────────────┐ ┌──────────────┐ │
  ┌─────────────┐        │  │  G-code    │ │  Safety      │ │
  │    User     │──CLI──→│  │  Validator │ │  Profiles    │ │
  │ (trusted)   │        │  └────────────┘ └──────────────┘ │
  └─────────────┘        └──────────┬───────────┬───────────┘
                                    │           │
                    TRUST BOUNDARY 2: Kiln ↔ Physical Hardware
                    ==========================================
                                    │           │
                    ┌───────────────┘           └────────────────┐
                    │                                            │
          ┌─────────────────┐                          ┌────────────────┐
          │ Printer Adapters │                          │  Webhooks Out  │
          │ OP / MR / BL / PC│                          │  (SSRF risk)   │
          └────────┬────────┘                          └────────────────┘
                   │
                   │ HTTP / MQTT / FTPS
                   │
          ┌────────────────┐
          │  3D Printers   │
          │  (physical hw) │
          └────────────────┘

                    TRUST BOUNDARY 3: Kiln ↔ External Services
                    ==========================================

  ┌──────────────────────────────────┐
  │          Kiln Server             │
  │  ┌──────────────┐ ┌───────────┐ │
  │  │ PaymentMgr   │ │ Fulfillment│ │
  │  │ SpendLimits  │ │ Monitor   │ │
  │  └──────┬───────┘ └─────┬─────┘ │
  └─────────┼───────────────┼───────┘
            │               │
     ┌──────┴───────┐  ┌───┴──────────┐
     │ Stripe/Circle│  │ Craftcloud/  │
     │ Solana/Base  │  │ Sculpteo/3DOS│
     └──────────────┘  └──────────────┘

                    TRUST BOUNDARY 4: Kiln ↔ Local Filesystem
                    ==========================================

  ┌──────────────────────────────────┐
  │          Kiln Server             │
  │  ┌──────────────┐ ┌───────────┐ │
  │  │ KilnDB       │ │ Credential│ │
  │  │ (SQLite)     │ │ Store     │ │
  │  └──────┬───────┘ └─────┬─────┘ │
  └─────────┼───────────────┼───────┘
            │               │
     ┌──────┴───────────────┴──────┐
     │  ~/.kiln/kiln.db            │
     │  ~/.kiln/config.yaml        │
     │  (file permissions matter)  │
     └─────────────────────────────┘
```

## 3. STRIDE Analysis

### 3.1 Spoofing

| ID | Threat | Component | Description |
|---|---|---|---|
| S-1 | API key brute force | AuthManager | An attacker with network access to the REST API attempts to guess API keys. Keys use the `sk_kiln_` prefix followed by 48 hex characters (192 bits of entropy), making brute force computationally infeasible. Rate limiting (default 60 req/min) provides a secondary barrier. |
| S-2 | Auth bypass via disabled auth | MCP Server | Authentication is **disabled by default**. Any process that can connect to the MCP stdio transport or REST API port operates with full access. This is by design for local-only deployments but becomes critical when exposed to a network. |
| S-3 | Agent impersonation | Agent Loop | No mechanism exists to cryptographically authenticate which agent or model is making tool calls. An agent ID is self-reported and can be trivially spoofed. Tool tier enforcement is based on configuration, not agent identity verification. |
| S-4 | Webhook source spoofing | Webhook receivers | Receivers that do not validate HMAC-SHA256 signatures cannot distinguish authentic Kiln events from forged payloads. Signature verification is opt-in (requires a shared secret). |
| S-5 | Printer impersonation on LAN | Printer Adapters | Kiln connects to printers by hostname/IP. An attacker on the same LAN could ARP-spoof or DNS-poison to redirect printer connections. Bambu adapter disables TLS certificate verification (`verify_mode = ssl.CERT_NONE`) due to self-signed certificates, eliminating server authentication. OctoPrint/Moonraker adapters default to `verify_ssl=True` but accept an override. |

### 3.2 Tampering

| ID | Threat | Component | Description |
|---|---|---|---|
| T-1 | G-code injection via tool results | Agent Loop | If a compromised printer API returns malicious strings in status responses, those strings pass through `_sanitize_tool_output()` before reaching the agent. The sanitizer strips known injection patterns but uses a deny-list approach, which cannot guarantee completeness against novel payloads. |
| T-2 | G-code command injection via agent | G-code Validator | An agent (or compromised agent) could attempt to send dangerous G-code. The validator blocks known dangerous commands (M502 firmware reset, unsafe movements beyond axis limits) and enforces temperature ceilings. However, the validator operates on a blocklist of known-bad commands; novel or undocumented firmware commands may not be caught. |
| T-3 | Webhook payload modification | Webhook Delivery | Webhook payloads are HMAC-SHA256 signed when a secret is configured. Without a secret, payloads are unsigned and susceptible to man-in-the-middle modification. HTTP (non-TLS) webhook URLs are permitted by the URL validator. |
| T-4 | SQLite database tampering | Persistence | The SQLite database at `~/.kiln/kiln.db` is protected only by filesystem permissions. An attacker with local filesystem access can modify job records, event history, payment records, and audit logs directly. |
| T-5 | Audit log tampering | Persistence | Safety audit entries are HMAC-signed individually, and `verify_audit_log()` detects modified entries. However, an attacker with database access could delete rows entirely (reducing the `total` count) without triggering integrity failures on remaining rows. The HMAC scheme is per-row, not hash-chained; there is no mechanism to detect deletion of entries. |
| T-6 | Plugin code injection | Plugin System | Plugins are loaded via Python entry points. Although only explicitly allow-listed plugin names are activated, the plugin code itself executes in the same process with full access to memory, filesystem, and network. A malicious plugin that passes the allow-list check has unrestricted capabilities. |
| T-7 | Config file manipulation | CLI Config | `~/.kiln/config.yaml` controls printer URLs, API keys, billing settings, and spend limits. Tampering with this file could redirect printer connections to attacker-controlled hosts, modify spend limits, or disable authentication. File permission warnings are emitted but not enforced. |

### 3.3 Repudiation

| ID | Threat | Component | Description |
|---|---|---|---|
| R-1 | Print commands without attribution | MCP Server | Agent IDs in audit log entries are self-reported strings. An agent can claim any identity or provide no identity. There is no cryptographic binding between an authenticated API key and the agent ID recorded in the audit log. |
| R-2 | Fee disputes on outsourced orders | Payment Manager | Platform fees (5% on fulfillment orders) are calculated locally and recorded in the `BillingLedger`. The fee calculation is deterministic and auditable, but there is no third-party attestation or signed receipt from the payment provider that the specific fee amount was authorized by the user. |
| R-3 | Deleted event history | Persistence | Events persisted to SQLite can be deleted by any process with filesystem access. While the safety audit log has HMAC signatures, the general event log does not. Event deletion leaves no trace. |
| R-4 | Unsigned cloud sync payloads | Cloud Sync | Cloud sync payloads are HMAC-signed for integrity, but the signing key is locally held. The remote API cannot independently verify that the data originated from a legitimate Kiln instance versus a replay or fabrication using a compromised key. |

### 3.4 Information Disclosure

| ID | Threat | Component | Description |
|---|---|---|---|
| I-1 | Auto-generated session key in logs | AuthManager | When auth is enabled without an explicit `KILN_AUTH_KEY`, a session key is auto-generated and logged via `logger.warning()`. If logs are collected by a centralized logging system, the key is exposed in plaintext. |
| I-2 | Printer credentials in config | CLI Config | Printer API keys are stored in `~/.kiln/config.yaml`. The system warns on world-readable permissions but does not enforce restrictive permissions. The credential store provides encrypted storage as an alternative, but the config file path remains the primary configuration mechanism. |
| I-3 | Payment provider keys in environment | Payment Manager | Stripe API keys, Circle API keys, and entity secrets are loaded from environment variables. These may be visible in process listings (`/proc/*/environ`), shell history, or CI/CD logs. |
| I-4 | Agent memory leaking secrets | Agent Loop | Privacy mode (enabled by default) redacts private IPs, Bearer tokens, Authorization headers, and known key patterns from tool results before they reach the LLM. However, secrets in non-standard formats may bypass the regex-based redaction. Disabling privacy mode (`KILN_LLM_PRIVACY_MODE=0`) removes all redaction. |
| I-5 | Webcam snapshot exposure | Streaming/Snapshots | Webcam snapshots are returned as raw bytes or base64-encoded JSON via MCP tools and CLI. The MJPEG proxy serves video without authentication. Any client that can reach the proxy endpoint can view the printer's camera feed. |
| I-6 | SQLite database as single exfiltration target | Persistence | `~/.kiln/kiln.db` contains job history, event logs, payment records, printer configurations, and audit logs. Exfiltrating this single file provides comprehensive operational intelligence. |
| I-7 | Bambu TLS certificate bypass | Bambu Adapter | The Bambu adapter sets `verify_mode = ssl.CERT_NONE` for both MQTT and FTPS connections due to self-signed printer certificates. This disables TLS server verification, allowing a man-in-the-middle attacker to intercept credentials and G-code traffic. |

### 3.5 Denial of Service

| ID | Threat | Component | Description |
|---|---|---|---|
| D-1 | Queue flooding | Job Queue | An agent can submit unlimited jobs to the queue. The free tier limits queue depth to 10 jobs, and the Pro/Business tier is unlimited. A compromised agent on a paid tier could flood the queue with thousands of jobs, monopolizing printer time. |
| D-2 | Event bus saturation | Event Bus | The pub/sub event bus persists all events to SQLite. A high-frequency event emitter (e.g., rapid printer state polling) could grow the database unboundedly. There is no event rate limiting or retention policy. |
| D-3 | Webhook delivery thread exhaustion | Webhook Manager | Webhook delivery uses a background thread and queue. A webhook endpoint that hangs (accepts connection but never responds) ties up the delivery thread for the duration of the HTTP timeout. Configuring many slow endpoints could exhaust delivery capacity. |
| D-4 | REST API rate limit bypass | Rate Limiter | The sliding-window rate limiter keys on client IP or API key. An attacker with many IPs (botnet, cloud instances) can trivially exceed the per-client limit by distributing requests. There is no global rate limit. |
| D-5 | Printer adapter connection exhaustion | Printer Adapters | Each printer adapter maintains an HTTP session. Rapid concurrent tool calls targeting the same printer could overwhelm the printer's embedded web server (OctoPrint/Moonraker typically run on Raspberry Pi hardware with limited resources). |
| D-6 | Large file upload | File Upload | G-code files can be arbitrarily large. Uploading a multi-gigabyte file through the MCP tool or REST API could exhaust memory or disk on both the Kiln host and the target printer. There is no file size limit enforced at the Kiln layer. |

### 3.6 Elevation of Privilege

| ID | Threat | Component | Description |
|---|---|---|---|
| E-1 | Tool tier bypass | Tool Tiering | Tool tiers are enforced by configuration — the server selectively exposes tools based on the `KILN_TOOL_TIER` setting. However, this is a server-side configuration, not a per-request authorization check. If the server is configured for `full` tier, all connected agents receive all tools regardless of model capability. There is no runtime verification that the calling model matches the configured tier. |
| E-2 | Auth scope escalation | AuthManager | API keys carry scopes (`read`, `write`, `admin`). Scope enforcement is implemented per-tool in the MCP server. A missing scope check on any single tool among 197 would constitute a privilege escalation vulnerability. The large tool surface area increases the probability of inconsistent enforcement. |
| E-3 | Plugin privilege escalation | Plugin System | Plugins execute in-process with the same privileges as the Kiln server. A plugin can access the `AuthManager`, `PaymentManager`, `KilnDB`, and all printer adapters. The allow-list controls which plugins load, but loaded plugins have no sandbox or capability restriction. |
| E-4 | Local filesystem to full access | Persistence | An attacker with write access to `~/.kiln/kiln.db` can insert API keys directly into the database, bypassing the `AuthManager.create_key()` flow. Since keys are stored as SHA-256 hashes, the attacker can compute the hash of a known key and insert it with `admin` scope. |
| E-5 | Environment variable manipulation | Server Config | Many security-critical behaviors are controlled by environment variables: `KILN_AUTH_ENABLED`, `KILN_RATE_LIMIT` (set to `0` to disable), `KILN_LLM_PRIVACY_MODE`, and `KILN_TOOL_TIER`. A process that can modify the environment of the Kiln server process can disable authentication, rate limiting, privacy mode, and tool restrictions. |
| E-6 | Spend limit override via config | Payment Manager | Spend limits (`max_per_order_usd`, `monthly_cap_usd`) are loaded from config and environment variables (`KILN_BILLING_MAX_PER_ORDER`, `KILN_BILLING_MONTHLY_CAP`). An attacker who can modify environment variables or the config file can raise spend limits arbitrarily, enabling unbounded charges. |

## 4. Risk Matrix

| ID | Threat | Severity | Likelihood | Existing Mitigation | Residual Risk |
|---|---|---|---|---|---|
| S-1 | API key brute force | Medium | Low | 192-bit key entropy, rate limiting (60 req/min) | Low — computationally infeasible |
| S-2 | Auth bypass (disabled by default) | Critical | Medium | Auth auto-generates session key when enabled without explicit key; documented as local-only default | High — any network-exposed deployment without explicit auth configuration is fully open |
| S-3 | Agent impersonation | Medium | Medium | Tool tiering limits blast radius of weak models | Medium — no cryptographic agent identity |
| S-4 | Webhook source spoofing | Medium | Low | HMAC-SHA256 signatures when secret configured | Low — mitigated when secrets are used |
| S-5 | Printer impersonation (LAN) | High | Low | Bambu: TLS used but no cert verification. OctoPrint/Moonraker: verify_ssl default true | Medium — Bambu adapter has no server authentication |
| T-1 | G-code injection via tool results | High | Low | `_sanitize_tool_output()` deny-list, output truncation | Medium — deny-list cannot guarantee completeness |
| T-2 | G-code command injection via agent | Critical | Medium | G-code validator blocklist, temperature ceilings, per-printer safety profiles (28 models) | Medium — blocklist approach cannot cover all firmware-specific commands |
| T-3 | Webhook payload modification | Medium | Low | HMAC-SHA256 when secret configured; HTTP URLs permitted | Medium — unsigned webhooks are vulnerable |
| T-4 | SQLite database tampering | High | Low | Filesystem permissions (not enforced, only warned) | Medium — no integrity protection beyond filesystem |
| T-5 | Audit log tampering | High | Low | Per-row HMAC signatures, `verify_audit_log()` | Medium — row deletion is undetectable |
| T-6 | Plugin code injection | Critical | Low | Explicit allow-list in user config | Medium — allow-listed plugins run unrestricted |
| T-7 | Config file manipulation | High | Low | Permission warnings on world-readable files | Medium — warnings not enforced |
| R-1 | Print commands without attribution | Medium | Medium | Agent ID recorded in audit log | Medium — IDs are self-reported |
| R-2 | Fee disputes | Low | Low | Deterministic fee calculation, billing ledger | Low — auditable but no third-party attestation |
| R-3 | Deleted event history | Medium | Low | Safety audit has HMAC; general events do not | Medium — general events have no integrity protection |
| R-4 | Unsigned cloud sync | Medium | Low | HMAC-signed payloads | Low — key compromise enables fabrication |
| I-1 | Session key in logs | High | Medium | Key is only auto-generated as fallback when no explicit key set | High — key in plaintext in log output |
| I-2 | Printer credentials in config | Medium | Low | Permission check with warning; credential store alternative | Medium — primary path still uses plaintext config |
| I-3 | Payment keys in environment | High | Low | Standard practice; no Kiln-specific mitigation | Medium — inherent to environment variable approach |
| I-4 | Agent memory leaking secrets | Medium | Low | Privacy mode enabled by default; regex redaction of known patterns | Low — effective for common patterns |
| I-5 | Webcam snapshot exposure | Medium | Medium | No authentication on MJPEG proxy endpoint | High — unauthenticated video stream on network |
| I-6 | SQLite as exfiltration target | High | Low | Filesystem permissions | Medium — single file contains all operational data |
| I-7 | Bambu TLS bypass | High | Low | TLS transport used, but `CERT_NONE` disables verification | Medium — MITM possible on LAN |
| D-1 | Queue flooding | Medium | Medium | Free tier: 10-job limit; paid tiers: unlimited | Medium — paid tiers have no queue cap |
| D-2 | Event bus saturation | Medium | Low | None | Medium — no retention policy or rate limit |
| D-3 | Webhook thread exhaustion | Low | Low | Best-effort delivery; configurable retries | Low — limited practical impact |
| D-4 | Rate limit bypass (distributed) | Medium | Low | Per-client sliding window rate limiter | Medium — no global limit |
| D-5 | Printer connection exhaustion | Medium | Low | None at Kiln layer; printers have their own limits | Medium — could crash embedded printer servers |
| D-6 | Large file upload | Medium | Low | None | Medium — no file size enforcement |
| E-1 | Tool tier bypass | Medium | Low | Server-side configuration | Low — requires server reconfiguration |
| E-2 | Auth scope escalation | High | Medium | Per-tool scope checks; 197 tools to audit | High — large surface area for missed checks |
| E-3 | Plugin privilege escalation | High | Low | Allow-list; fault isolation for exceptions | Medium — no capability sandbox |
| E-4 | DB write to full access | High | Low | Filesystem permissions | Medium — trivial if FS access obtained |
| E-5 | Env var manipulation | Critical | Low | None — standard process model | Medium — OS-level process isolation is only defense |
| E-6 | Spend limit override | High | Low | Spend limits checked before every charge | Medium — limits are configurable via env/config |

## 5. Open Items

The following are known gaps that have not been addressed in the current implementation:

1. **No formal penetration test.** The security model has not been validated by an external security firm. All mitigations are self-assessed.

2. **No fuzz testing of MCP tools.** The 197 MCP tool handlers have not been subjected to automated fuzz testing for input validation edge cases, malformed JSON, or type confusion.

3. **No mTLS or certificate pinning for printer connections.** The Bambu adapter explicitly disables TLS certificate verification. OctoPrint and Moonraker connections rely on system CA stores. No certificate pinning is implemented for any adapter.

4. **No PGP/GPG signing of releases.** Software releases are not cryptographically signed. Users cannot verify the authenticity of downloaded binaries or source archives.

5. **Auth disabled by default.** The security model assumes local-only deployment when auth is off. No warning is emitted when the REST API binds to `0.0.0.0` (all interfaces) without authentication enabled.

6. **No per-tool scope audit.** With 197 MCP tools, there is no automated verification that every tool correctly enforces its required auth scope. Manual review is the current approach.

7. **Audit log does not detect deletion.** The HMAC scheme signs individual rows but does not chain hashes. An attacker with database access can delete audit entries without detection. A Merkle tree or hash chain would address this.

8. **No sandbox for plugins.** Loaded plugins execute with full process privileges. There is no capability-based restriction, seccomp profile, or separate process isolation.

9. **MJPEG proxy has no authentication.** The webcam streaming proxy serves video to any client that can reach the HTTP port. This should be gated behind the same auth mechanism as the REST API.

10. **No webhook URL scheme enforcement for HTTPS.** The SSRF validator checks for private IP ranges but permits `http://` URLs. Webhook payloads sent over unencrypted HTTP are susceptible to interception and modification.

11. **Credential store uses XOR stream cipher.** The encrypted credential store derives a keystream via PBKDF2 and applies XOR. This is not a standard authenticated encryption scheme (e.g., AES-GCM). It lacks ciphertext integrity verification, meaning a targeted bit-flip attack on the stored ciphertext would produce a modified plaintext without detection.

12. **No formal bug bounty program.** The SECURITY.md acknowledges this. Credit-based recognition is offered but no financial incentive exists for external researchers.

13. **No SBOM (Software Bill of Materials).** No machine-readable inventory of dependencies is published, making it harder for operators to assess exposure to upstream vulnerabilities.

14. **No automated dependency scanning in CI.** No evidence of Dependabot, Snyk, or equivalent scanning for known vulnerabilities in Python dependencies.

15. **Cloud sync key compromise has no revocation mechanism.** If the HMAC signing key used for cloud sync payloads is compromised, there is no documented procedure to rotate it or invalidate previously-signed data.

---

*This document should be reviewed and updated whenever the system's security boundary changes, new components are added, or vulnerabilities are discovered and mitigated.*
