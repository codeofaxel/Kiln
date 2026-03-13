#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# --- Pre-flight checks ----------------------------------------------------

if [ ! -f .env ]; then
    echo "ERROR: .env file not found in $SCRIPT_DIR"
    echo "Copy .env.example to .env and fill in the required values:"
    echo "  cp .env.example .env"
    exit 1
fi

# --- Deploy ----------------------------------------------------------------

echo "==> Pulling latest code..."
git -C "$SCRIPT_DIR/.." pull --ff-only

echo "==> Building and starting containers..."
docker compose --env-file .env up --build -d

echo "==> Deployment complete. Container status:"
docker compose ps
