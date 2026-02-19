# Kiln Deployment Guide

Comprehensive reference for deploying Kiln as a hosted REST API service or local MCP server. Covers all environment variables, Docker/Railway deployment, and health verification.

---

## Environment Variables

### Printer Connection (Required for printer control)

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_PRINTER_HOST` | Yes | `""` | Base URL of the printer server (e.g. `http://octopi.local`, `http://192.168.1.50`). Works for both Ethernet and Wi-Fi LAN printers |
| `KILN_PRINTER_API_KEY` | Depends | `""` | API key for OctoPrint/Moonraker/Prusa Link authentication |
| `KILN_PRINTER_TYPE` | No | `octoprint` | Printer backend: `octoprint`, `moonraker`, `bambu`, `prusaconnect` |
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
| `KILN_API_AUTH_TOKEN` | Yes for hosted REST | `""` | REST API bearer token. Required when binding REST to non-localhost addresses |
| `KILN_WEBHOOK_ALLOW_REDIRECTS` | No | `false` | Allow webhook HTTP redirects. Disabled by default for SSRF safety |
| `KILN_WEBHOOK_MAX_REDIRECTS` | No | `3` | Max redirect hops when redirects are enabled (capped at 10) |
| `KILN_PLUGIN_POLICY` | No | `strict` | Third-party plugin policy: `strict` (default deny) or `permissive` |
| `KILN_ALLOWED_PLUGINS` | No | `""` | Comma-separated plugin entry-point names allowed under strict policy |

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
| `KILN_FULFILLMENT_PROVIDER` | No | auto-detect | Explicit fulfillment provider: `craftcloud` |
| `KILN_CRAFTCLOUD_API_KEY` | No | `""` | Craftcloud API key |
| `KILN_CRAFTCLOUD_BASE_URL` | No | `https://api.craftcloud3d.com` | Override Craftcloud API base URL (staging: `https://api-stg.craftcloud3d.com`) |
| `KILN_CRAFTCLOUD_MATERIAL_CATALOG_URL` | No | `https://customer-api.craftcloud3d.com/material-catalog` | Craftcloud material catalog endpoint (used to retrieve `materialConfigId`s) |
| `KILN_CRAFTCLOUD_USE_WEBSOCKET` | No | `""` | Set to `1` to use WebSocket price polling (recommended by Craftcloud; requires `websockets` + `msgpack`) |
| `KILN_CRAFTCLOUD_PAYMENT_MODE` | No | `craftcloud` | `craftcloud` (Craftcloud handles payment) or `partner` (platform handles payment separately) |
| `KILN_SCULPTEO_API_KEY` | No | `""` | Sculpteo API key *(pending partner credentials)* |
| `KILN_SCULPTEO_BASE_URL` | No | Sculpteo default | Override Sculpteo API base URL *(pending partner credentials)* |

### Payment Providers

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_STRIPE_SECRET_KEY` | No | `""` | Stripe secret API key for payment processing |
| `KILN_STRIPE_WEBHOOK_SECRET` | No | `""` | Stripe webhook signing secret for event verification |
| `KILN_STRIPE_PRICE_PRO` | No | `""` | Stripe Price ID for Pro monthly. Falls back to lookup key `pro_monthly`. |
| `KILN_STRIPE_PRICE_PRO_ANNUAL` | No | `""` | Stripe Price ID for Pro annual. Falls back to lookup key `pro_annual`. |
| `KILN_STRIPE_PRICE_BUSINESS` | No | `""` | Stripe Price ID for Business monthly. Falls back to lookup key `business_monthly`. |
| `KILN_STRIPE_PRICE_BUSINESS_ANNUAL` | No | `""` | Stripe Price ID for Business annual. Falls back to lookup key `business_annual`. |
| `KILN_STRIPE_PRICE_ENTERPRISE` | No | `""` | Stripe Price ID for Enterprise monthly. Falls back to lookup key `enterprise_monthly`. |
| `KILN_STRIPE_PRICE_ENTERPRISE_ANNUAL` | No | `""` | Stripe Price ID for Enterprise annual. Falls back to lookup key `enterprise_annual`. |
| `KILN_STRIPE_PRICE_PRINTER_OVERAGE` | No | `""` | Stripe Price ID for metered printer overage ($15/printer/mo). Falls back to lookup key `enterprise_printer_overage`. |
| `KILN_CIRCLE_API_KEY` | No | `""` | Circle API key for crypto payments |

### AI / Agent

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_OPENROUTER_KEY` | No | `""` | OpenRouter API key for the agent loop and REPL |
| `KILN_MESHY_API_KEY` | No | `""` | Meshy API key for AI 3D model generation |
| `KILN_AGENT_ID` | No | `default` | Agent identifier for event attribution and memory |
| `KILN_LLM_PRIVACY_MODE` | No | `1` (enabled) | Redact secrets from LLM context. Set `0` to disable |

### Distributed Manufacturing Network *(Coming Soon)*

| Variable | Required | Default | Description |
|---|---|---|---|
| `KILN_NETWORK_API_KEY` | No | `""` | Distributed network gateway API key |
| `KILN_NETWORK_BASE_URL` | No | — | Override network API base URL |

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
KILN_API_AUTH_TOKEN=CHANGE_ME_generate_a_strong_random_key

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
# KILN_CRAFTCLOUD_BASE_URL=https://api.craftcloud3d.com
# KILN_CRAFTCLOUD_MATERIAL_CATALOG_URL=https://customer-api.craftcloud3d.com/material-catalog
# KILN_CRAFTCLOUD_USE_WEBSOCKET=1
# KILN_CRAFTCLOUD_PAYMENT_MODE=craftcloud
# KILN_CRAFTCLOUD_BASE_URL=https://api-stg.craftcloud3d.com   # staging
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

### Hosted REST API (Dockerfile.api)

For deploying Kiln as an HTTP REST API service (no MCP transport):

```bash
docker build -f Dockerfile.api -t kiln-hosted .
docker run -d \
  --name kiln-hosted \
  -p 8080:8080 \
  -v kiln-data:/data \
  --env-file .env \
  --restart unless-stopped \
  kiln-hosted
```

Key differences from standard Docker:
- Installs `kiln[rest]` (includes FastAPI + uvicorn)
- Runs as non-root `kiln` user
- Persistent volume at `/data` for SQLite DB and license cache
- Exposes port 8080 (hosted REST default)
- Runs `kiln rest --host 0.0.0.0 --port 8080`
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

2. **Set the Dockerfile path** to `Dockerfile.api` in your Railway service settings.

3. **Configure environment variables** in the Railway dashboard:
   - All `KILN_*` variables from the table above
   - Include `KILN_API_AUTH_TOKEN` (required for hosted REST on non-localhost binds)

4. **Persistent storage**: Attach a Railway volume mounted at `/data` to persist the SQLite database and license cache across deploys.

5. **Health checks**: Railway auto-detects the `HEALTHCHECK` directive. The endpoint is `GET /api/health`.

6. **Recommended Railway variables**:
   ```
   KILN_API_AUTH_TOKEN=change-me-long-random-token
   KILN_PRINTER_HOST=http://your-printer-ip
   KILN_PRINTER_API_KEY=your-api-key
   KILN_DB_PATH=/data/kiln.db
   KILN_LOG_FORMAT=json
   KILN_RATE_LIMIT=60
   ```

---

## Health Check & Verification

### Health Endpoint

```bash
curl http://localhost:8080/api/health
# Expected: {"status": "ok", "version": "0.1.0"}
```

### Verify Authentication

```bash
# Should return 401 if auth is enabled
curl http://localhost:8080/api/tools

# Should return 200 with tool list
curl -H "Authorization: Bearer YOUR_AUTH_KEY" http://localhost:8080/api/tools
```

### Verify Printer Connectivity

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_AUTH_KEY" \
  -H "Content-Type: application/json" \
  http://localhost:8080/api/tools/printer_status
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

- [ ] Set a strong `KILN_API_AUTH_TOKEN` for hosted REST deployments
- [ ] Use `KILN_LOG_FORMAT=json` for production logging
- [ ] Set `KILN_RATE_LIMIT` to an appropriate value
- [ ] Ensure config files have `0600` permissions (automatic on Linux/macOS)
- [ ] Mount `/data` as a persistent volume for database durability
- [ ] Run the container as non-root (Dockerfile.api does this automatically)
- [ ] Never commit `.env` files or API keys to version control
- [ ] Set `KILN_LLM_PRIVACY_MODE=1` (default) to redact secrets from LLM context
- [ ] Set `KILN_CONFIRM_MODE=true` for hosted deployments to require confirmation for destructive operations

---

## On-Premises Deployment (Enterprise)

Kiln Enterprise supports on-premises deployment via raw Kubernetes manifests or a Helm chart. All resources are in the `deploy/` directory.

### Prerequisites

- Kubernetes 1.25+ cluster
- Helm 3.x (for Helm-based deployment)
- `kubectl` configured with cluster access
- Container registry access to `ghcr.io/codeofaxel/kiln` (or a mirror for air-gapped environments)
- A valid Enterprise license key (`KILN_LICENSE_KEY=kiln_ent_...`)

### Quick Start: Raw Kubernetes Manifests

1. **Edit secrets and config:**

   ```bash
   # Copy and edit the secret template -- replace all CHANGE_ME values
   cp deploy/k8s/secret.yaml deploy/k8s/secret-local.yaml
   # Edit deploy/k8s/secret-local.yaml with real values

   # Edit the configmap to match your printer network
   vi deploy/k8s/configmap.yaml
   ```

2. **Apply all manifests:**

   ```bash
   kubectl apply -f deploy/k8s/namespace.yaml
   kubectl apply -f deploy/k8s/serviceaccount.yaml
   kubectl apply -f deploy/k8s/configmap.yaml
   kubectl apply -f deploy/k8s/secret-local.yaml
   kubectl apply -f deploy/k8s/pvc.yaml
   kubectl apply -f deploy/k8s/deployment.yaml
   kubectl apply -f deploy/k8s/service.yaml

   # Optional: ingress, HPA, network policy
   kubectl apply -f deploy/k8s/ingress.yaml
   kubectl apply -f deploy/k8s/hpa.yaml
   kubectl apply -f deploy/k8s/networkpolicy.yaml
   ```

3. **Verify:**

   ```bash
   kubectl get pods -n kiln
   kubectl port-forward svc/kiln 8741:8741 -n kiln
   curl http://localhost:8741/health
   ```

### Quick Start: Helm

1. **Create a custom values file:**

   ```bash
   cp deploy/helm/kiln/values.yaml my-values.yaml
   # Edit my-values.yaml with your configuration
   ```

2. **Install:**

   ```bash
   helm install kiln deploy/helm/kiln/ \
     --namespace kiln --create-namespace \
     -f my-values.yaml
   ```

3. **Upgrade after config changes:**

   ```bash
   helm upgrade kiln deploy/helm/kiln/ \
     --namespace kiln \
     -f my-values.yaml
   ```

4. **Verify:**

   ```bash
   helm status kiln -n kiln
   kubectl get pods -n kiln
   ```

### Key Configuration

#### Database Persistence

By default, Kiln uses SQLite stored on a PersistentVolumeClaim at `/data/kiln.db`. This is suitable for single-replica deployments.

- **PVC size**: 1Gi default (configurable via `persistence.size` in Helm or `pvc.yaml`)
- **Storage class**: Uses cluster default. Set `persistence.storageClass` for specific backends.
- **Existing PVC**: Set `persistence.existingClaim` in Helm values to reuse a pre-provisioned PVC.

#### TLS Termination

The included Ingress manifest supports TLS via cert-manager:

- Annotated with `cert-manager.io/cluster-issuer: letsencrypt-prod`
- Uses nginx ingress class by default
- Set `ingress.hosts[0].host` to your domain and `ingress.tls[0].secretName` to your TLS secret

For environments without cert-manager, provision a TLS secret manually:

```bash
kubectl create secret tls kiln-tls \
  --cert=tls.crt --key=tls.key \
  -n kiln
```

#### Network Policies

When `networkPolicy.enabled: true`, Kiln pods are restricted to:

- **Ingress**: Only from within the `kiln` namespace and the `ingress-nginx` namespace
- **Egress**: DNS, HTTPS to external APIs (443), HTTP/HTTPS/MQTT to printer networks (configurable CIDRs), and PostgreSQL (5432)

Adjust `networkPolicy.printerCIDRs` to match your printer subnet layout.

#### Air-Gapped Deployments

For clusters without internet access:

1. Mirror the container image to your internal registry:
   ```bash
   docker pull ghcr.io/codeofaxel/kiln:0.1.0
   docker tag ghcr.io/codeofaxel/kiln:0.1.0 registry.internal/kiln:0.1.0
   docker push registry.internal/kiln:0.1.0
   ```

2. Update the image reference:
   ```yaml
   # Helm values
   image:
     repository: registry.internal/kiln
     tag: "0.1.0"
     pullPolicy: IfNotPresent
   ```

3. If your registry requires authentication, create an image pull secret:
   ```bash
   kubectl create secret docker-registry ghcr-credentials \
     --docker-server=registry.internal \
     --docker-username=user \
     --docker-password=token \
     -n kiln
   ```
   Then set `imagePullSecrets` in your values file.

### Scaling Considerations

| Replicas | Database Backend | Deployment Strategy | Notes |
|---|---|---|---|
| 1 | SQLite (default) | `Recreate` | Simplest setup. No concurrent write issues. |
| 2-10 | PostgreSQL | `RollingUpdate` | Set `KILN_DB_URL` to a PostgreSQL connection string. |
| 10+ | PostgreSQL + read replicas | `RollingUpdate` | Consider connection pooling (PgBouncer). |

**SQLite limitations**: SQLite supports only a single writer at a time. Running multiple replicas with SQLite will cause write conflicts and data corruption. Always switch to PostgreSQL before enabling the HPA or setting `replicaCount > 1`.

To switch to PostgreSQL:

```yaml
# In secrets (Helm values or secret.yaml)
KILN_DB_URL: "postgresql://kiln:password@postgres.kiln.svc:5432/kiln"

# In config (remove SQLite path)
# KILN_DB_PATH is ignored when KILN_DB_URL is set

# Update deployment strategy
strategy:
  type: RollingUpdate
```

### Security Hardening Checklist

- [ ] Replace **all** `CHANGE_ME` values in secrets before deploying
- [ ] Generate strong keys with `openssl rand -hex 32`
- [ ] Use an external secret manager (Vault, Sealed Secrets, AWS Secrets Manager) instead of Kubernetes Secret manifests in production
- [ ] Enable network policies (`networkPolicy.enabled: true`)
- [ ] Enable TLS via Ingress -- never expose Kiln over plain HTTP in production
- [ ] Set `KILN_AUTH_ENABLED=true` and configure `KILN_API_AUTH_TOKEN`
- [ ] Set `KILN_ENCRYPTION_KEY` for data-at-rest encryption (Enterprise)
- [ ] Configure SSO via `KILN_SSO_*` environment variables (Enterprise)
- [ ] Run as non-root (default in both manifests and Helm chart)
- [ ] Enable read-only root filesystem (default in security context)
- [ ] Drop all capabilities (default in security context)
- [ ] Set resource limits to prevent noisy-neighbor issues
- [ ] Use `KILN_LOG_FORMAT=json` for structured logging to your SIEM
- [ ] Restrict printer network egress CIDRs to only the subnets containing your printers
- [ ] Regularly rotate `KILN_API_AUTH_TOKEN` and `KILN_ENCRYPTION_KEY`
- [ ] Audit Kubernetes RBAC -- Kiln pods need no special cluster permissions
- [ ] Create a dedicated ServiceAccount for Kiln pods (do not use the `default` ServiceAccount). Set `automountServiceAccountToken: false` on both the ServiceAccount and the pod spec to prevent unnecessary API server access.
- [ ] Enable a seccomp profile on all containers (`seccompProfile: type: RuntimeDefault`). This restricts the set of syscalls available to the container, reducing kernel attack surface.
- [ ] Rotate secrets regularly. Establish a rotation schedule for `KILN_API_AUTH_TOKEN`, `KILN_AUTH_KEY`, `KILN_ENCRYPTION_KEY`, and printer API keys. For zero-downtime rotation: create a new secret, update the Kubernetes Secret resource, then perform a rolling restart (`kubectl rollout restart deployment/kiln -n kiln`). Revoke the old credential after all pods have restarted.
