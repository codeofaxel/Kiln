#!/usr/bin/env bash
# deploy.sh — One-command deploy of the Kiln REST API to Fly.io.
#
# Prerequisites (one-time):
#   brew install flyctl
#   fly auth login          # opens browser — create account or log in
#
# Then just run:
#   ./deploy.sh
#
# This script will:
#   1. Verify you're logged in to Fly.io
#   2. Create the app if it doesn't exist yet
#   3. Set secrets from your .env file
#   4. Deploy the Docker image
#   5. Print the live URL

set -euo pipefail

APP_NAME="kiln3d-api"
REGION="lax"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# ---------- helpers ----------
red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

die() { red "ERROR: $*" >&2; exit 1; }

# ---------- 1. Check flyctl is installed ----------
command -v flyctl >/dev/null 2>&1 || command -v fly >/dev/null 2>&1 || \
    die "flyctl not found. Install it with: brew install flyctl"

# Use whichever alias is available
if command -v fly >/dev/null 2>&1; then
    FLY=fly
else
    FLY=flyctl
fi

# ---------- 2. Check login ----------
blue "Checking Fly.io authentication..."
if ! $FLY auth whoami >/dev/null 2>&1; then
    red "Not logged in to Fly.io."
    echo ""
    bold "Run this first, then re-run ./deploy.sh:"
    echo ""
    echo "  fly auth login"
    echo ""
    exit 1
fi
ACCOUNT=$($FLY auth whoami 2>&1)
green "Logged in as: $ACCOUNT"
echo ""

# ---------- 3. Create app if needed ----------
blue "Checking if app '$APP_NAME' exists..."
if $FLY apps list 2>/dev/null | grep -q "$APP_NAME"; then
    green "App '$APP_NAME' already exists."
else
    blue "Creating app '$APP_NAME' in region $REGION..."
    $FLY apps create "$APP_NAME" --org personal || true
    green "App '$APP_NAME' created."
fi
echo ""

# ---------- 4. Set secrets from .env ----------
blue "Setting secrets from .env..."
if [[ ! -f "$ENV_FILE" ]]; then
    die ".env file not found at $ENV_FILE — copy .env.example to .env and fill in your keys."
fi

# Parse .env: skip comments, blank lines; extract KEY=VALUE pairs.
# Only set the secrets the API actually needs.
SECRETS_ARGS=()
SECRETS_NEEDED=(
    KILN_STRIPE_SECRET_KEY
    KILN_STRIPE_WEBHOOK_SECRET
    KILN_CIRCLE_API_KEY
)

for KEY in "${SECRETS_NEEDED[@]}"; do
    # Extract the value from .env (handles quotes, trailing whitespace)
    VALUE=$(grep -E "^${KEY}=" "$ENV_FILE" 2>/dev/null | head -1 | sed 's/^[^=]*=//' | sed 's/^["'"'"']//;s/["'"'"']$//' | xargs)
    if [[ -n "$VALUE" && "$VALUE" != "..." ]]; then
        SECRETS_ARGS+=("${KEY}=${VALUE}")
    fi
done

if [[ ${#SECRETS_ARGS[@]} -gt 0 ]]; then
    # fly secrets set is idempotent — safe to re-run
    $FLY secrets set "${SECRETS_ARGS[@]}" --app "$APP_NAME" --stage
    green "Set ${#SECRETS_ARGS[@]} secret(s)."
else
    echo "  No secrets found in .env (or all are still placeholder values)."
    echo "  You can set them later with:"
    echo "    fly secrets set KILN_STRIPE_SECRET_KEY=sk_live_... --app $APP_NAME"
fi
echo ""

# ---------- 5. Deploy ----------
blue "Deploying $APP_NAME (this takes 1-3 minutes)..."
echo ""
$FLY deploy --app "$APP_NAME" --remote-only

echo ""
green "========================================="
green "  Deployment complete!"
green "========================================="
echo ""
bold "Your API is live at:"
echo ""
echo "  https://${APP_NAME}.fly.dev"
echo ""
echo "Health check:"
echo "  https://${APP_NAME}.fly.dev/api/health"
echo ""
echo "To use a custom domain (api.kiln3d.com):"
echo "  fly certs create api.kiln3d.com --app $APP_NAME"
echo "  Then add the CNAME shown to your DNS."
echo ""
echo "To view logs:"
echo "  fly logs --app $APP_NAME"
echo ""
echo "To set up CI/CD (auto-deploy on push to main):"
echo "  fly tokens create deploy --app $APP_NAME"
echo "  Then add that token as FLY_API_TOKEN in GitHub repo secrets."
echo ""
