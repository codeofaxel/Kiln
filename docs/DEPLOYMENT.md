# Kiln Deployment Guide

Reference for running Kiln yourself. Covers all environment variables, Docker deployment, and health verification.

---

## Environment Variables

### Printer Connection (Required for printer control)

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_PRINTER_HOST` | Yes | `""` | Base URL of the printer server (e.g. `http://octopi.local`, `http://192.168.1.50`). Works for both Ethernet and Wi-Fi LAN printers |
| `KILN_PRINTER_API_KEY` | Depends | `""` | API key for OctoPrint/Moonraker/Prusa Link authentication |
| `KILN_PRINTER_TYPE` | No | `octoprint` | Printer backend: `octoprint`, `moonraker`, `bambu`, `prusalink` |
| `KILN_PRINTER_SERIAL` | Bambu only | `""` | Bambu printer serial number (required when type is `bambu`) |
| `KILN_PRINTER_ACCESS_CODE` | Bambu only | `""` | Bambu printer access code (required when type is `bambu`) |
| `KILN_BAMBU_TLS_MODE` | Bambu only | `pin` | Bambu TLS policy: `pin` (TOFU pinning), `ca` (strict CA/hostname verification), or `insecure` (legacy no cert verification) |
| `KILN_BAMBU_TLS_FINGERPRINT` | Bambu only | `""` | Optional explicit SHA-256 certificate fingerprint pin |
| `KILN_BAMBU_TLS_PIN_FILE` | Bambu only | `~/.kiln/bambu_tls_pins.json` | Location of persisted TOFU certificate pins |
| `KILN_PRINTER_MODEL` | No | `""` | Printer model name for auto-loading safety/slicer profiles |
| `KILN_PRINTER` | No | `""` | Named printer from `~/.kiln/config.yaml` (CLI flag equivalent) |

### Authentication & Security

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_AUTH_ENABLED` | No | `false` | Enable API key authentication (`1`, `true`, `yes`) |
| `KILN_AUTH_KEY` | No | auto-generated | API key for client authentication. If omitted while auth is enabled, Kiln creates an ephemeral session key (value is not logged) |
| `KILN_MCP_AUTH_TOKEN` | No | `""` | Bearer token for MCP transport-level auth |
| `KILN_API_AUTH_TOKEN` | No | `""` | Alternate name for the MCP bearer token, accepted for compatibility |
| `KILN_WEBHOOK_ALLOW_REDIRECTS` | No | `false` | Allow webhook HTTP redirects. Disabled by default for SSRF safety |
| `KILN_WEBHOOK_MAX_REDIRECTS` | No | `3` | Max redirect hops when redirects are enabled (capped at 10) |
| `KILN_PLUGIN_POLICY` | No | `strict` | Third-party plugin policy: `strict` (default deny) or `permissive` |
| `KILN_ALLOWED_PLUGINS` | No | `""` | Comma-separated plugin entry-point names allowed under strict policy |

### Storage & Database

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_DB_PATH` | No | `~/.kiln/kiln.db` | Path to SQLite database for jobs, events, print history, agent memory |

### Licensing

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_LICENSE_KEY` | No | `""` | License key for Pro/Business tier features. Prefix `kiln_pro_` for Pro, `kiln_biz_` for Business |

### Logging

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_LOG_FORMAT` | No | `text` | Log output format: `text` (human-readable) or `json` (structured, recommended for production) |

### Marketplace Integrations

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_THINGIVERSE_TOKEN` | No | `""` | Thingiverse API app token for model search/download. *Deprecated — Thingiverse was acquired by MyMiniFactory (Feb 2026). Prefer `KILN_MMF_API_KEY`.* |
| `KILN_MMF_API_KEY` | No | `""` | MyMiniFactory API key |
| `KILN_CULTS3D_USERNAME` | No | `""` | Cults3D account username |
| `KILN_CULTS3D_API_KEY` | No | `""` | Cults3D API key |

### Fulfillment Providers

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_FULFILLMENT_PROVIDER` | No | auto-detect | Explicit fulfillment provider: `craftcloud` |
| `KILN_CRAFTCLOUD_API_KEY` | No | `""` | Craftcloud API key |

### Payment Providers

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_STRIPE_SECRET_KEY` | No | `""` | Stripe secret API key for payment processing |
| `KILN_STRIPE_WEBHOOK_SECRET` | No | `""` | Stripe webhook signing secret for event verification |
| `KILN_CIRCLE_API_KEY` | No | `""` | Circle API key for crypto payments |

### AI / Agent

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_OPENROUTER_KEY` | No | `""` | OpenRouter API key for the agent loop and REPL |
| `KILN_MESHY_API_KEY` | No | `""` | Meshy API key for AI 3D model generation |
| `KILN_AGENT_ID` | No | `default` | Agent identifier for event attribution and memory |
| `KILN_LLM_PRIVACY_MODE` | No | `1` (enabled) | Redact secrets from LLM context. Set `0` to disable |

### Safety & Confirmation

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_CONFIRM_UPLOAD` | No | `false` | Require confirmation before file uploads (`1`, `true`, `yes`) |
| `KILN_CONFIRM_MODE` | No | `false` | Require confirmation before destructive operations (`1`, `true`, `yes`) |
| `KILN_STRICT_MATERIAL_CHECK` | No | `true` | Enforce strict material compatibility checks |
| `KILN_HEATER_TIMEOUT` | No | `30` | Minutes before heater auto-cooldown watchdog triggers (0 to disable) |
| `KILN_VISION_AUTO_PAUSE` | No | `false` | Auto-pause print on vision-detected failures |

### Auto-Print (Use with caution)

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_AUTO_PRINT_MARKETPLACE` | No | `false` | Auto-start printing after downloading marketplace models. Moderate risk |
| `KILN_AUTO_PRINT_GENERATED` | No | `false` | Auto-start printing AI-generated models. Higher risk -- experimental geometry |

### Billing / Spend Limits

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_BILLING_MAX_PER_ORDER` | No | `500.0` | Maximum fee per outsourced order (USD) |
| `KILN_BILLING_MONTHLY_CAP` | No | `2000.0` | Monthly fee cap for outsourced orders (USD) |

### Slicer

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_SLICER_PATH` | No | auto-detect | Path to PrusaSlicer/OrcaSlicer binary |

### Plugins

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_ALLOWED_PLUGINS` | No | `""` | Comma-separated list of allowed plugin names |

### Network Proxies

| Variable | Required | Default | Description |
|---|---|---|---|
| `HTTP_PROXY` | No | `""` | HTTP proxy for outbound requests |
| `HTTPS_PROXY` | No | `""` | HTTPS proxy for outbound requests |

---

## Minimal `.env` Example

```env
# === Required: Printer Connection ===
KILN_PRINTER_HOST=http://octopi.local
KILN_PRINTER_API_KEY=CHANGE_ME_your_octoprint_api_key
KILN_PRINTER_TYPE=octoprint

# === Recommended: Security ===
KILN_MCP_AUTH_TOKEN=CHANGE_ME_generate_a_strong_random_key

# === Optional: Database persistence ===
KILN_DB_PATH=/data/kiln.db

# === Optional: Structured logging for production ===
KILN_LOG_FORMAT=json

# === Optional: Marketplace access ===
# KILN_MMF_API_KEY=CHANGE_ME_your_myminifactory_key          # Recommended (primary marketplace)
# KILN_THINGIVERSE_TOKEN=CHANGE_ME_your_thingiverse_token   # Deprecated — acquired by MMF, Feb 2026

# === Optional: License key for Pro/Business features ===
# KILN_LICENSE_KEY=kiln_pro_CHANGE_ME

# === Optional: Agent / AI ===
# KILN_OPENROUTER_KEY=sk-or-CHANGE_ME

# === Optional: Fulfillment (outsourced manufacturing) ===
# KILN_CRAFTCLOUD_API_KEY=CHANGE_ME
# KILN_STRIPE_SECRET_KEY=sk_live_CHANGE_ME

# === Optional: LLM privacy (enabled by default) ===
# KILN_LLM_PRIVACY_MODE=1
```

---

## Docker Deployment

### Standard Docker (MCP Server)

Build and run using the root `Dockerfile`:

```bash
docker build -t kiln .
docker run -d \
  --name kiln \
  -p 8000:8000 \
  --env-file .env \
  --restart unless-stopped \
  kiln
```

### Hosted REST API

If you'd rather not run any of this yourself, Kiln is also available as a
managed service — no servers to deploy, patch, or keep alive. See
[kiln3d.com/pricing](https://kiln3d.com/pricing).

### Docker Compose

Use the provided `docker-compose.yml` for the standard MCP server:

```bash
# Copy and fill in .env
cp .env.example .env
# Edit .env with your values

docker compose up -d
```

---

## Health Check & Verification

One command checks the whole install — Python version, the Kiln package, your
slicer, your printer config, and whether the printer actually answers — and
tells you what to fix:

```bash
kiln doctor
```

Add `--deep` when the printer is the part that isn't answering, or `--json`
if something else needs to read the result.

---

## Security Checklist

- [ ] Set a strong `KILN_MCP_AUTH_TOKEN` if the server is reachable beyond localhost
- [ ] Use `KILN_LOG_FORMAT=json` for production logging
- [ ] Ensure config files have `0600` permissions (automatic on Linux/macOS)
- [ ] Mount `/data` as a persistent volume for database durability
- [ ] Run the container as non-root
- [ ] Never commit `.env` files or API keys to version control
- [ ] Set `KILN_LLM_PRIVACY_MODE=1` (default) to redact secrets from LLM context
- [ ] Set `KILN_CONFIRM_MODE=true` for unattended deployments to require confirmation for destructive operations
