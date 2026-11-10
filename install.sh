#!/bin/sh
# Kiln installer for macOS, Linux and WSL.
#
#   curl -fsSL https://www.kiln3d.com/install.sh | sh
#
# Installs Kiln and connects it to every AI app on this computer.  Run it
# again to update.  Every change to an app's settings is backed up and
# written to a receipt at ~/.kiln/install/receipt.json, and --uninstall
# takes back exactly what the receipt says.  Windows: install.ps1.
#
# The whole script is one function called on the last line, so a download
# cut short runs nothing.

set -eu

# The Kiln release this installer installs by default.  A test keeps it
# equal to the package version.
KILN_DEFAULT_VERSION="1.5.1"
PKG="kiln3d"
# The Python Kiln runs on, which uv fetches when needed: the one Kiln's own
# tests run on, so every part Kiln depends on has a ready-made build for it.
KILN_PYTHON="3.12"

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '  x %s\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
Install Kiln and connect it to the AI apps on this computer
(Claude Code, Claude Desktop, Codex, Cursor, Gemini CLI, Grok Build, OpenClaw).

  curl -fsSL https://www.kiln3d.com/install.sh | sh
  curl -fsSL https://www.kiln3d.com/install.sh | sh -s -- [options]

  --dry-run          show what would happen, change nothing
  --only <apps>      only these apps, comma-separated: claude-code, claude-desktop,
                     codex, cursor, gemini, grok, openclaw
  --uninstall        take Kiln back out, exactly as the receipt says
  --version <x.y.z>  install this Kiln release
  --help             show this help

Run it again any time to update.
EOF
}

# A person is at a terminal when /dev/tty opens.  Under `curl | sh` stdin is
# the download, so every question is read from /dev/tty, never from stdin.
has_tty() { ( : </dev/tty ) 2>/dev/null; }

ask() {
    has_tty || return 1
    printf '  %s [Y/n] ' "$1" >/dev/tty
    read -r reply </dev/tty || return 1
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

TMP_DIR=""
cleanup() { if [ -n "$TMP_DIR" ]; then rm -rf "$TMP_DIR"; fi; }

main() {
    DRY_RUN=0
    UNINSTALL=0
    ONLY=""
    VERSION="$KILN_DEFAULT_VERSION"
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) DRY_RUN=1 ;;
            --uninstall) UNINSTALL=1 ;;
            --only) [ $# -ge 2 ] || die "--only needs a list of apps"; ONLY="$2"; shift ;;
            --only=*) ONLY="${1#--only=}" ;;
            --version) [ $# -ge 2 ] || die "--version needs a version"; VERSION="$2"; shift ;;
            --version=*) VERSION="${1#--version=}" ;;
            -h|--help) usage; return 0 ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
    case "$VERSION" in
        ''|*[!0-9A-Za-z.+-]*) die "not a version: $VERSION" ;;
    esac
    case "$ONLY" in
        *[!a-zA-Z0-9_,-]*) die "not a list of apps: $ONLY" ;;
    esac
    # KILN_PACKAGE lets a release candidate (a wheel file) be tried before it
    # is published; normally it is the pinned release.
    SPEC="${KILN_PACKAGE:-$PKG==$VERSION}"

    case "$(uname -s)" in
        Darwin) OS="macOS" ;;
        Linux) if grep -qi microsoft /proc/version 2>/dev/null; then OS="WSL"; else OS="Linux"; fi ;;
        *) die "this installer is for macOS, Linux and WSL; on Windows use install.ps1" ;;
    esac

    TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t kiln)"
    trap cleanup EXIT
    trap 'cleanup; exit 130' INT TERM

    if [ "$DRY_RUN" = 1 ]; then
        say "Kiln installer, dry run on $OS. Nothing on this computer will change."
        # Anything a dry run downloads stays in a folder deleted on exit.
        UV_CACHE_DIR="$TMP_DIR/cache"
        UV_PYTHON_INSTALL_DIR="$TMP_DIR/python"
        UV_TOOL_DIR="$TMP_DIR/tools"
        export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR UV_TOOL_DIR
    else
        say "Kiln installer on $OS"
    fi

    find_uv
    if [ "$UNINSTALL" = 1 ]; then
        uninstall
    else
        install
    fi
}

# Sets UV to a working uv, installing it when missing (into the throwaway
# folder on a dry run).
find_uv() {
    if has uv; then UV="uv"; return 0; fi
    if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; return 0; fi
    has curl || die "curl is needed to fetch uv, the tool that installs Kiln"
    if [ "$DRY_RUN" = 1 ]; then
        say "  Would install uv (it installs Kiln and the Python Kiln runs on)."
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_UNMANAGED_INSTALL="$TMP_DIR/uv" sh >/dev/null 2>&1 \
            || die "couldn't fetch uv to plan the install"
        UV="$TMP_DIR/uv/uv"
        return 0
    fi
    step "Installing uv (it installs Kiln and the Python Kiln runs on)"
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "couldn't install uv"
    UV="$HOME/.local/bin/uv"
    "$UV" --version >/dev/null 2>&1 || die "uv didn't install; see https://docs.astral.sh/uv"
}

# Runs kiln: the installed one, or (dry run / missing) the release itself
# straight from the package index without installing it.
kiln_run() {
    if [ "$DRY_RUN" = 0 ] && [ -x "$KILN" ]; then
        "$KILN" "$@"
    else
        "$UV" tool run --quiet --python "$KILN_PYTHON" --from "$SPEC" kiln "$@"
    fi
}

install() {
    BIN_DIR="$("$UV" tool dir --bin)" || die "uv couldn't name its program folder"
    KILN="$BIN_DIR/kiln"

    if [ "$DRY_RUN" = 1 ]; then
        step "Kiln $VERSION"
        if [ -x "$KILN" ]; then
            say "  Would update Kiln at $KILN to $VERSION"
        else
            say "  Would install Kiln $VERSION at $KILN"
        fi
        step "Your AI apps"
        if [ -n "$ONLY" ]; then
            kiln_run install-mcp --dry-run --command "$KILN" --only "$ONLY" || die "couldn't plan the AI apps"
        else
            kiln_run install-mcp --dry-run --command "$KILN" || die "couldn't plan the AI apps"
        fi
        step "Then"
        say "  Would offer to install OpenSCAD (Kiln's design engine) if it's missing."
        say "  Would ask you to accept Kiln's terms of use, if you haven't yet."
        say ""
        say "Dry run finished. Nothing was changed."
        return 0
    fi

    step "Installing Kiln $VERSION"
    "$UV" tool install --force --python "$KILN_PYTHON" "$SPEC" || die "couldn't install Kiln $VERSION"
    [ -x "$KILN" ] || die "Kiln installed, but $KILN is missing"

    step "Connecting Kiln to your AI apps"
    if [ -n "$ONLY" ]; then
        "$KILN" install-mcp --command "$KILN" --only "$ONLY" || warn "some apps weren't connected; see above"
    else
        "$KILN" install-mcp --command "$KILN" || warn "some apps weren't connected; see above"
    fi

    step "OpenSCAD (Kiln's design engine)"
    if "$KILN" install-openscad --check >/dev/null 2>&1; then
        say "  Already installed."
    elif ask "Install OpenSCAD now? Kiln designs parts with it."; then
        "$KILN" install-openscad </dev/tty || warn "OpenSCAD didn't install; run 'kiln install-openscad' later"
    else
        say "  Skipped. Install it any time with: kiln install-openscad"
    fi

    step "Kiln's terms of use"
    if has_tty; then
        # The person types their own acceptance, at the terminal.
        "$KILN" accept-terms --if-needed </dev/tty || warn "not accepted yet; run 'kiln accept-terms' when ready"
    else
        say "  There's no terminal here to ask you in. Run this yourself: kiln accept-terms"
    fi

    step "Done"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) say "  Open a new terminal (or run: $UV tool update-shell) so the 'kiln' command is found." ;;
    esac
    say "  Restart the apps listed above, then ask one of them to design something."
    say "  Run this again any time to update. To remove Kiln:"
    say "    curl -fsSL https://www.kiln3d.com/install.sh | sh -s -- --uninstall"
}

uninstall() {
    BIN_DIR="$("$UV" tool dir --bin 2>/dev/null || true)"
    KILN="$BIN_DIR/kiln"
    if [ "$DRY_RUN" = 1 ]; then flag="--dry-run"; else flag=""; fi

    step "Taking Kiln out of your AI apps"
    if [ -f "$HOME/.kiln/install/receipt.json" ]; then
        if [ -n "$ONLY" ]; then
            kiln_run uninstall-mcp --receipt-only $flag --only "$ONLY" || warn "some apps weren't restored; see above"
        else
            kiln_run uninstall-mcp --receipt-only $flag || warn "some apps weren't restored; see above"
        fi
    else
        say "  No receipt at ~/.kiln/install/receipt.json, so there are no app settings to take back."
    fi

    if [ -n "$ONLY" ]; then
        say ""
        say "Kiln itself stays installed (you chose --only)."
        return 0
    fi

    step "Removing Kiln"
    if [ "$DRY_RUN" = 1 ]; then
        say "  Would remove the Kiln program ($PKG)."
    elif "$UV" tool list 2>/dev/null | grep -q "^$PKG "; then
        "$UV" tool uninstall "$PKG"
    else
        say "  uv doesn't list Kiln, so there's no program to remove."
    fi
    say ""
    say "Your printers, prints and settings in ~/.kiln stay. Delete that folder yourself if you want them gone."
    if [ "$DRY_RUN" = 1 ]; then say "Dry run finished. Nothing was changed."; fi
}

main "$@"
