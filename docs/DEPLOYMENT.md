# Kiln Deployment Guide

Comprehensive reference for deploying Kiln as a hosted REST API service or local MCP server. Covers all environment variables, Docker/Railway deployment, and health verification.

---

## Environment Variables

### Printer Connection (Required for printer control)

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_PRINTER_HOST` | Yes | `""` | Base URL of the printer server (e.g. `http://octopi.local`, `192.168.1.50`) |
| `KILN_PRINTER_API_KEY` | Depends | `""` | API key for OctoPrint/Moonraker/Prusa Link authentication |
| `KILN_PRINTER_TYPE` | No | `octoprint` | Printer backend: `octoprint`, `moonraker`, `bambu`, `prusaconnect` |
| `KILN_PRINTER_SERIAL` | Bambu only | `""` | Bambu printer serial number (required when type is `bambu`) |
| `KILN_PRINTER_ACCESS_CODE` | Bambu only | `""` | Bambu printer access code (required when type is `bambu`) |
| `KILN_PRINTER_MODEL` | No | `""` | Printer model name for auto-loading safety/slicer profiles |
| `KILN_PRINTER` | No | `""` | Named printer from `~/.kiln/config.yaml` (CLI flag equivalent) |

### Authentication & Security

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_AUTH_ENABLED` | No | `false` | Enable API key authentication (`1`, `true`, `yes`) |
| `KILN_AUTH_KEY` | No | auto-generated | API key for client authentication. Auto-generated session key if auth is enabled but no key is set |
| `KILN_MCP_AUTH_TOKEN` | No | `""` | Bearer token for MCP transport-level auth |
| `KILN_API_AUTH_TOKEN` | No | `""` | REST API bearer token (checked at credential audit) |

### Storage & Database

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_DB_PATH` | No | `~/.kiln/kiln.db` | Path to SQLite database for jobs, events, print history, agent memory |
| `KILN_DATA_DIR` | No | `~/.kiln` | Base data directory (used in hosted Dockerfile for `/data`) |

### Rate Limiting

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_RATE_LIMIT` | No | `60` | Maximum requests per minute per client (REST API). Set to `0` to disable |

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
| `KILN_FULFILLMENT_PROVIDER` | No | auto-detect | Explicit fulfillment provider: `craftcloud`, `sculpteo` |
| `KILN_CRAFTCLOUD_API_KEY` | No | `""` | Craftcloud API key |
| `KILN_CRAFTCLOUD_BASE_URL` | No | Craftcloud default | Override Craftcloud API base URL |
| `KILN_SCULPTEO_API_KEY` | No | `""` | Sculpteo API key |
| `KILN_SCULPTEO_BASE_URL` | No | Sculpteo default | Override Sculpteo API base URL |

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

### 3DOS Gateway

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_3DOS_API_KEY` | No | `""` | 3DOS gateway API key |
| `KILN_3DOS_BASE_URL` | No | 3DOS default | Override 3DOS API base URL |

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
KILN_AUTH_ENABLED=true
KILN_AUTH_KEY=CHANGE_ME_generate_a_strong_random_key

# === Optional: Database persistence ===
KILN_DB_PATH=/data/kiln.db

# === Optional: Structured logging for production ===
KILN_LOG_FORMAT=json

# === Optional: Rate limiting (requests per minute) ===
KILN_RATE_LIMIT=60

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

### Hosted REST API (Dockerfile.hosted)

For deploying Kiln as an HTTP REST API service (no MCP transport):

```bash
cd kiln
docker build -f Dockerfile.hosted -t kiln-hosted .
docker run -d \
  --name kiln-hosted \
  -p 8420:8420 \
  -v kiln-data:/data \
  --env-file .env \
  --restart unless-stopped \
  kiln-hosted
```

Key differences from standard Docker:
- Installs `kiln[rest]` (includes FastAPI + uvicorn)
- Runs as non-root `kiln` user
- Persistent volume at `/data` for SQLite DB and license cache
- Exposes port 8420 (REST API default)
- Runs `kiln rest --host 0.0.0.0 --port $PORT`
- Built-in healthcheck on `/api/health`

### Docker Compose

Use the provided `docker-compose.yml` for the standard MCP server:

```bash
# Copy and fill in .env
cp .env.example .env
# Edit .env with your values

docker compose up -d
```

---

## Railway Deployment

Kiln can be deployed on Railway using the hosted Dockerfile:

1. **Create a new Railway project** and connect your Git repository.

2. **Set the Dockerfile path** to `kiln/Dockerfile.hosted` in your Railway service settings.

3. **Configure environment variables** in the Railway dashboard:
   - All `KILN_*` variables from the table above
   - Railway auto-sets `PORT`; the hosted Dockerfile respects this via `$PORT`

4. **Persistent storage**: Attach a Railway volume mounted at `/data` to persist the SQLite database and license cache across deploys.

5. **Health checks**: Railway auto-detects the `HEALTHCHECK` directive. The endpoint is `GET /api/health`.

6. **Recommended Railway variables**:
   ```
   KILN_PRINTER_HOST=http://your-printer-ip
   KILN_PRINTER_API_KEY=your-api-key
   KILN_AUTH_ENABLED=true
   KILN_AUTH_KEY=your-strong-random-key
   KILN_DB_PATH=/data/kiln.db
   KILN_LOG_FORMAT=json
   KILN_RATE_LIMIT=60
   ```

---

## Health Check & Verification

### Health Endpoint

```bash
curl http://localhost:8420/api/health
# Expected: {"status": "ok", "version": "0.1.0"}
```

### Verify Authentication

```bash
# Should return 401 if auth is enabled
curl http://localhost:8420/api/tools

# Should return 200 with tool list
curl -H "Authorization: Bearer YOUR_AUTH_KEY" http://localhost:8420/api/tools
```

### Verify Printer Connectivity

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_AUTH_KEY" \
  -H "Content-Type: application/json" \
  http://localhost:8420/api/tools/printer_status
```

### Check Rate Limiting

Responses include rate limit headers:

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 59
X-RateLimit-Reset: 1700000060
```

When rate limited, the server returns HTTP 429 with a `Retry-After` header.

### Docker Health Check

The hosted Dockerfile includes a built-in healthcheck:

```bash
docker inspect --format='{{json .State.Health}}' kiln-hosted
```

---

## Security Checklist

- [ ] Set `KILN_AUTH_ENABLED=true` and provide a strong `KILN_AUTH_KEY`
- [ ] Use `KILN_LOG_FORMAT=json` for production logging
- [ ] Set `KILN_RATE_LIMIT` to an appropriate value
- [ ] Ensure config files have `0600` permissions (automatic on Linux/macOS)
- [ ] Mount `/data` as a persistent volume for database durability
- [ ] Run the container as non-root (Dockerfile.hosted does this automatically)
- [ ] Never commit `.env` files or API keys to version control
- [ ] Set `KILN_LLM_PRIVACY_MODE=1` (default) to redact secrets from LLM context
- [ ] Set `KILN_CONFIRM_MODE=true` for hosted deployments to require confirmation for destructive operations
