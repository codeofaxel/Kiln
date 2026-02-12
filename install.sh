#!/usr/bin/env bash
# Kiln installer — works on macOS, Linux, and WSL
# Usage: git clone https://github.com/codeofaxel/Kiln.git ~/.kiln/src && ~/.kiln/src/install.sh
set -euo pipefail

REPO="https://github.com/codeofaxel/Kiln.git"
INSTALL_DIR="${KILN_INSTALL_DIR:-$HOME/.kiln/src}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ✓\033[0m  %s\n' "$*"; }
warn()  { printf '\033[1;33m ⚠\033[0m  %s\n' "$*"; }
fail()  { printf '\033[1;31m ✗\033[0m  %s\n' "$*" >&2; exit 1; }

has() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Detect OS
# ---------------------------------------------------------------------------

OS="unknown"
if [ "$(uname -s)" = "Darwin" ]; then
    OS="macos"
elif [ "$(uname -s)" = "Linux" ]; then
    if grep -qi microsoft /proc/version 2>/dev/null; then
        OS="wsl"
    else
        OS="linux"
    fi
fi

info "Detected platform: $OS"

# ---------------------------------------------------------------------------
# Check Python 3.10+
# ---------------------------------------------------------------------------

if ! has python3; then
    fail "Python 3 not found. Install Python 3.10+ first."
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Python 3.10+ required (found $PY_VERSION)"
fi
ok "Python $PY_VERSION"

# ---------------------------------------------------------------------------
# Install pipx if needed
# ---------------------------------------------------------------------------

if ! has pipx; then
    info "Installing pipx..."
    if [ "$OS" = "macos" ]; then
        if has brew; then
            brew install pipx
        else
            python3 -m pip install --user pipx 2>/dev/null || pip3 install --user pipx
        fi
    else
        # Linux / WSL
        if has apt; then
            sudo apt update -qq && sudo apt install -y -qq pipx
        elif has dnf; then
            sudo dnf install -y pipx
        else
            python3 -m pip install --user pipx 2>/dev/null || pip3 install --user pipx
        fi
    fi
    pipx ensurepath 2>/dev/null || true
    # Source updated PATH
    export PATH="$HOME/.local/bin:$PATH"
fi

if ! has pipx; then
    fail "Could not install pipx. Install it manually: https://pipx.pypa.io"
fi
ok "pipx available"

# ---------------------------------------------------------------------------
# Clone or update Kiln
# ---------------------------------------------------------------------------

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating Kiln source..."
    git -C "$INSTALL_DIR" pull --ff-only -q
    ok "Updated to latest"
else
    info "Cloning Kiln..."
    if ! has git; then
        fail "git not found. Install git first."
    fi
    git clone --depth 1 -q "$REPO" "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# Install via pipx
# ---------------------------------------------------------------------------

info "Installing Kiln via pipx..."

# Uninstall previous version if exists (ignore errors)
pipx uninstall kiln3d 2>/dev/null || true

pipx install "$INSTALL_DIR/kiln"
ok "Kiln installed"

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

# Ensure pipx bin is on PATH for this session
export PATH="$HOME/.local/bin:$PATH"

if has kiln; then
    KILN_VERSION=$(kiln --version 2>/dev/null || echo "unknown")
    ok "kiln command available ($KILN_VERSION)"
else
    warn "kiln not on PATH — restart your terminal or run: pipx ensurepath"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
info "Kiln is installed! Next steps:"
echo ""
echo "  kiln setup          # Interactive wizard — finds printers, saves config"
echo "  kiln verify         # Check everything is working"
echo "  kiln status --json  # See what your printer is doing"
echo ""

if [ "$OS" = "wsl" ]; then
    warn "WSL detected: mDNS printer discovery won't work."
    echo "  Use your printer's IP address directly in kiln setup."
    echo ""
fi
