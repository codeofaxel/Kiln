"""``kiln signin`` / ``kiln signout`` / ``kiln whoami`` / ``kiln pair`` /
``kiln link`` — the CLI's device-code OAuth flow + bidirectional pairing
against the Kiln REST API.

How the login flow works end-to-end:

  1. ``kiln signin`` POSTs ``/api/auth/device/start``.  Gets back a
     ``device_code`` (secret), a ``user_code`` (human-typeable), and a
     ``verification_uri`` (URL to open in the browser).
  2. CLI prints the ``user_code`` + URL, opens the URL in the user's
     default browser (unless ``--no-browser``), and starts polling
     ``/api/auth/device/poll`` every ``interval`` seconds.
  3. User picks a provider on the browser page (Google / Apple / GitHub),
     completes the OAuth flow with Supabase.  The Supabase redirect
     lands on our ``/auth/device/callback`` page, which JS-side fetches
     the user and POSTs ``/api/auth/device/claim`` so the server can
     mark the row as ``status=success`` + stash the tokens.
  4. The next poll from the CLI returns ``{status: success,
     access_token, refresh_token, tier, email}``.
  5. CLI writes the tokens to ``~/.kiln/auth_tokens.json`` (mode 0600)
     and prints ``Signed in as {email} ({tier})``.

``kiln signout`` deletes the token file.

``kiln whoami`` reads the file, hits ``GET /api/auth/whoami`` with the
access_token, and prints the resolved email + tier.

``kiln pair <code>`` is the web-initiated mirror of ``kiln signin`` —
the user has already signed in on app.kiln3d.com; this command claims
a short-lived pairing code and writes the tokens locally without a
second browser round-trip.

All four commands talk ONLY to the public Kiln REST API at
``https://api.kiln3d.com`` (override with ``KILN_API_URL`` for local
dev).  No proprietary logic lives here — they ship in the public
``kiln`` package so ``pip install kiln3d && kiln pair <code>`` works
out-of-the-box on a clean machine without requiring kiln-pro.

The file is JSON not YAML so that ops can eyeball it without a parser
dependency and so third-party tooling can read tier without depending
on any Kiln package at all.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

import click


# Default API base — override with ``KILN_API_URL`` for local dev.
def _api_base() -> str:
    return (os.environ.get("KILN_API_URL") or "https://api.kiln3d.com").rstrip("/")


def _tokens_path() -> Path:
    """Where we persist the access + refresh token.  Per-user, tight
    permissions.  ``$KILN_AUTH_HOME`` lets tests redirect the path
    without monkeypatching ``Path.home``."""
    home = os.environ.get("KILN_AUTH_HOME") or str(Path.home())
    return Path(home) / ".kiln" / "auth_tokens.json"


def _write_tokens(data: dict[str, Any]) -> None:
    """Atomic + 0600.  Create the directory with 0700 perms first so
    we never leave an intermediate mode-wide-open file on disk."""
    path = _tokens_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    tmp.replace(path)


def _read_tokens() -> dict[str, Any]:
    path = _tokens_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _delete_tokens() -> bool:
    path = _tokens_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def _http_post(
    path: str,
    body: dict[str, Any],
    *,
    bearer: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Thin requests.post wrapper that returns a dict or raises
    click.ClickException with a human message.  Keeping the HTTP
    surface tiny (no retry, no session reuse) because these endpoints
    are called at most a few dozen times across the whole flow.

    ``bearer`` is keyword-only so every existing call site (all
    unauthenticated pairing/login endpoints) keeps working unchanged;
    only authed callers like ``kiln link`` opt in."""
    try:
        import requests  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — requests is a hard dep
        raise click.ClickException(
            "The `requests` package is required for `kiln signin`. "
            "Install with `pip install requests`."
        ) from exc

    headers = {"User-Agent": f"kiln-cli/{_cli_version()}"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    try:
        resp = requests.post(
            f"{_api_base()}{path}",
            json=body,
            timeout=timeout,
            headers=headers,
        )
    except requests.exceptions.ConnectionError as exc:
        raise click.ClickException(
            f"Could not reach Kiln API at {_api_base()} — is the server up?"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise click.ClickException(
            f"Timed out reaching {_api_base()}{path}."
        ) from exc

    if bearer and resp.status_code == 401:
        # Only surface 401 as a terminal error when an Authorization
        # header was actually sent.  Unauthenticated callers (the
        # device-flow / pairing-claim endpoints) never expect 401 and
        # continue to signal success/failure via the JSON body — hiding
        # that under a blanket 401 branch would break the login flow.
        raise click.ClickException(
            "Your session is not accepted by the Kiln API (HTTP 401). "
            "Run `kiln signin` to refresh, then retry."
        )
    if resp.status_code >= 500:
        raise click.ClickException(
            f"Kiln API returned {resp.status_code} for {path}. "
            "Try again in a moment; if it persists, email adam@kiln3d.com."
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise click.ClickException(
            f"Kiln API returned non-JSON for {path}: {resp.text[:200]}"
        ) from exc


def _http_get(path: str, *, bearer: str | None = None, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    try:
        import requests  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise click.ClickException("The `requests` package is required.") from exc

    headers = {"User-Agent": f"kiln-cli/{_cli_version()}"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        resp = requests.get(f"{_api_base()}{path}", headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        raise click.ClickException(f"Could not reach {_api_base()}.") from exc
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def _cli_version() -> str:
    try:
        from kiln import __version__
        return __version__
    except Exception:
        return "dev"


# ═════════════════════════════════════════════════════════════════════
# `kiln signin`  (also aliased as `kiln signin` for backwards compat)
# ═════════════════════════════════════════════════════════════════════


@click.command("signin")
@click.option(
    "--no-browser", is_flag=True, default=False,
    help="Don't open the browser automatically. Print the URL and wait.",
)
@click.option(
    "--timeout", type=int, default=900, show_default=True,
    help="Max seconds to wait for the user to complete the browser step.",
)
def auth_login(no_browser: bool, timeout: int) -> None:
    """Sign in to your Kiln account via OAuth (Google / Apple / GitHub).

    This is KILN ACCOUNT AUTHENTICATION — use this to log in to the
    Kiln REST API + workshop as yourself.  Completely distinct from
    ``kiln identity link``, which attaches a provider identity (e.g.
    GitHub) to your tenant for design-release signing.  If you want
    to "log in to Kiln", this is the command.  If you want to
    "connect my GitHub account so I can sign design releases with my
    GitHub identity", that's ``kiln identity link``.

    Launches the device-code flow: opens your browser to a small
    provider-picker page, polls until you finish signing in, then
    writes your session token to ``~/.kiln/auth_tokens.json``.

    The rest of the CLI will pick up your session automatically — no
    license key needed for OAuth-linked Pro+ accounts.  Backwards-compat
    alias: ``kiln login`` (same command, same flow).
    """
    # 1) Start the device flow.
    start = _http_post("/api/auth/device/start", {})
    if not start.get("success"):
        raise click.ClickException(
            start.get("error") or "Could not start the sign-in flow."
        )

    device_code = start["device_code"]
    user_code = start["user_code"]
    verification_uri = start["verification_uri"]
    interval = max(1, int(start.get("interval") or 2))
    expires_in = int(start.get("expires_in") or 900)
    deadline = time.monotonic() + min(timeout, expires_in)

    # 2) Try to open the browser FIRST.  The verification URL carries
    #    the user_code as a query param, so when the browser opens the
    #    human lands on the provider picker with nothing to type.  We
    #    only fall back to showing the raw URL / code when auto-open
    #    fails — most users (normies, agents, devs alike) never see
    #    the code.  Hiding device-flow implementation details is the
    #    biggest single UX win over the classic "paste this code"
    #    pattern.
    browser_opened = False
    if not no_browser:
        try:
            browser_opened = bool(webbrowser.open(verification_uri, new=1, autoraise=True))
        except Exception:
            browser_opened = False

    click.echo("", err=True)
    if browser_opened:
        click.echo("  Opening your browser to sign in\u2026", err=True)
    else:
        # Fallback path — no browser auto-open (--no-browser, headless
        # machine, sandboxed CLI).  URL is primary; code is the
        # last-resort affordance shown in a muted secondary line.
        click.echo("  Open this URL to sign in:", err=True)
        click.echo("", err=True)
        click.echo(f"    {verification_uri}", err=True)
        click.echo("", err=True)
        click.echo(
            f"  If the page asks you to confirm a code, it\u2019s  {user_code}",
            err=True,
        )

    click.echo("", err=True)

    # 3) Poll with a subtle braille spinner so the terminal feels
    #    alive during the 5–30s most users take in the browser.
    #    TTY-gated so piping to a log file (or CI) produces static,
    #    greppable output instead of a carriage-return salad.
    result: dict[str, Any] = {}
    spinner_frames = "\u2807\u2811\u2813\u2817\u2837\u2836\u2834\u2830\u2820\u2800"
    is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
    tick = 0
    if is_tty:
        click.echo(
            f"  {spinner_frames[0]} Waiting for you to finish in the browser\u2026",
            nl=False, err=True,
        )
    else:
        click.echo("  Waiting for you to finish in the browser\u2026", err=True)
    while time.monotonic() < deadline:
        time.sleep(interval)
        poll = _http_post("/api/auth/device/poll", {"device_code": device_code})
        status = str(poll.get("status") or "pending").lower()
        if status == "pending":
            if is_tty:
                tick += 1
                frame = spinner_frames[tick % len(spinner_frames)]
                click.echo(
                    f"\r  {frame} Waiting for you to finish in the browser\u2026",
                    nl=False, err=True,
                )
            continue
        result = poll
        break
    else:
        if is_tty:
            click.echo("\r" + " " * 60 + "\r", nl=False, err=True)
        raise click.ClickException(
            "Sign-in timed out before the browser flow completed. "
            "Run `kiln signin` again."
        )

    # Clear the spinner line cleanly before printing the final verdict.
    if is_tty:
        click.echo("\r" + " " * 60 + "\r", nl=False, err=True)

    status = str(result.get("status") or "").lower()
    if status == "success":
        email = str(result.get("email") or "")
        tier = str(result.get("tier") or "free").lower()
        _write_tokens({
            "access_token": result.get("access_token") or "",
            "refresh_token": result.get("refresh_token") or "",
            "email": email,
            "auth_uid": result.get("auth_uid") or "",
            "tier": tier,
            "has_entitlement": bool(result.get("has_entitlement")),
            "signed_in_at": int(time.time()),
        })
        # Final confirmation on stdout (not stderr) so ``kiln login
        # | tail -1`` captures it.  Ember-coloured checkmark +
        # lowercase "you're in" matches the workshop's signin copy
        # and the /auth/device page's h1 — three surfaces, one voice.
        click.echo(f"\u2713 You\u2019re in \u2014 {email} ({tier}).")
        return
    if status == "denied":
        # User cancelled in the browser (e.g. picked a provider, got
        # to the consent screen, hit back).  Quiet exit, no scolding.
        raise click.ClickException(
            result.get("message") or "Sign-in was cancelled in the browser."
        )
    if status == "expired":
        raise click.ClickException(
            result.get("message") or "Sign-in code expired. Run `kiln signin` again."
        )
    raise click.ClickException(f"Unexpected sign-in status: {status!r}")


# ═════════════════════════════════════════════════════════════════════
# `kiln signout`  (also aliased as `kiln logout` for backwards compat)
# ═════════════════════════════════════════════════════════════════════


@click.command("signout")
def auth_logout() -> None:
    """Delete the locally-stored Kiln session token.

    This does not revoke the token upstream — Supabase-side revocation
    lives in the workshop (``/settings/account → Sign out of all
    sessions``).  For most users this command is what they want: it
    forgets the session on this laptop without touching the others.
    """
    if _delete_tokens():
        click.echo("Signed out. Token file removed.")
    else:
        click.echo("You weren't signed in.")


# ═════════════════════════════════════════════════════════════════════
# `kiln whoami`
# ═════════════════════════════════════════════════════════════════════


@click.command("whoami")
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Emit the full whoami response as JSON instead of a human summary.",
)
def auth_whoami(as_json: bool) -> None:
    """Show who you're signed in as + your current tier.

    Hits ``GET /api/auth/whoami`` with your saved access_token and
    prints the tier + email the server resolves.  Useful when the
    local cache might be out of date (e.g. right after an upgrade in
    the Stripe portal).
    """
    tokens = _read_tokens()
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise click.ClickException(
            "Not signed in. Run `kiln signin` to sign in."
        )

    code, body = _http_get("/api/auth/whoami", bearer=access_token)
    if code == 401:
        raise click.ClickException(
            "Session expired. Run `kiln signin` to sign in again."
        )
    if code >= 400 or not body.get("success"):
        raise click.ClickException(
            body.get("error") or f"whoami failed (HTTP {code})"
        )

    email = str(body.get("email") or "")
    tier = str(body.get("tier") or "free").lower()
    status = str(body.get("status") or "").lower()
    expires = str(body.get("expires_at") or "")

    # Write the fresh server-resolved tier back to the token cache so
    # any LicenseManager (which reads this file) picks up tier changes
    # immediately after ``kiln whoami`` instead of waiting for its own
    # 24h auto-refresh.  Useful right after a Stripe upgrade: run
    # ``kiln whoami``, then every local tool sees the new tier.
    if str(tokens.get("tier") or "").lower() != tier or not tokens.get("signed_in_at"):
        tokens["tier"] = tier
        tokens["signed_in_at"] = int(time.time())
        if email:
            tokens["email"] = email
        _write_tokens(tokens)

    if as_json:
        click.echo(json.dumps(body, indent=2, sort_keys=True))
        return

    click.echo(f"Email:   {email}")
    click.echo(f"Tier:    {tier}")
    if status and status not in ("none", "active"):
        click.echo(f"Status:  {status}")
    if expires:
        click.echo(f"Expires: {expires}")


# ═════════════════════════════════════════════════════════════════════
# `kiln pair <code>` — web-initiated pairing
# ═════════════════════════════════════════════════════════════════════
#
# The mirror image of ``kiln signin``: instead of this machine bootstrapping
# an OAuth session from cold, the user has already signed in on the web
# (usually right after upgrading their tier at app.kiln3d.com/checkout/
# success → /connect).  The workshop minted a short-lived code tied to
# their session; ``kiln pair <code>`` hands the tokens to this machine
# without any browser round-trip here.
#
# Why this is the preferred post-upgrade path (even though ``kiln signin``
# still works): after paying, the user is already in a browser, already
# signed in.  Asking them to run ``kiln signin`` which opens ANOTHER
# browser to sign in AGAIN is a jarring re-authentication.  Pairing
# reuses the session they already have in one command.


def _hostname_label() -> str:
    """Best-effort human label for this machine, used on the workshop's
    paired-machines list.  Never fatal — fall back to a generic string
    if hostname resolution fails (rare, but it happens in some container
    runtimes)."""
    try:
        import socket as _socket
        h = _socket.gethostname()
        # Strip ``.local`` and other zeroconf suffixes — users recognize
        # ``Adams-MacBook-Pro`` faster than ``Adams-MacBook-Pro.local``.
        for suf in (".local", ".lan", ".home"):
            if h.endswith(suf):
                h = h[: -len(suf)]
        return h or "this machine"
    except Exception:
        return "this machine"


# ---------------------------------------------------------------------
# AI-client identity detection
# ---------------------------------------------------------------------
#
# Two rows that both read "Adams-MBP" on /settings/agent is the exact
# ambiguity this helper exists to resolve.  A user running Claude
# Desktop + Claude Code + Cursor on one laptop needs to know which row
# is which before they can meaningfully revoke.  ``client_name``
# captures the AI client; ``machine_label`` captures the host.
#
# Priority ladder (highest → lowest):
#
#   1. Explicit ``--client`` flag on the command — user knows best,
#      so shortcut the ladder.
#   2. Environment-variable sniff.  Most LLM surfaces export a
#      distinguishing env var when they spawn an MCP subprocess.
#      Order matters: CLAUDE_DESKTOP_SESSION beats CLAUDE_CODE_* because
#      a Claude Desktop session that happens to set both is still
#      semantically "Claude Desktop."
#   3. Parent-process name (best-effort, Unix only).  If env sniff
#      missed (older client versions that don't export a session var)
#      we try to read the parent's executable name.  Only surface a
#      well-known set — anything else stays empty rather than leaking
#      a meaningless name like ``zsh``.
#   4. Headless detection.  If stdin isn't a TTY AND every sniff
#      returned nothing, assume this is an MCP-launched subprocess
#      with no distinguishing identity.  "Unspecified (MCP)" beats
#      an empty string for giving the user a handle to reason about.
#   5. Interactive prompt — the caller handles this when appropriate;
#      the helper itself only returns "" so the caller can decide
#      whether to prompt.
#
# None of this is security-bearing; it's identification only.  A
# malicious client that sets CLAUDE_DESKTOP_SESSION to impersonate a
# Claude Desktop row is defeated by /settings/agent's Revoke button —
# the user sees their OWN list and kicks off anything they don't
# recognise.  We just need the labels to be *usually correct* so the
# list is useful in the common case.

# Public map for testing — so test_auth_commands.py can assert the
# priority order without duplicating the env-var names.
_CLIENT_NAME_ENV_SIGNALS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_DESKTOP_SESSION", "Claude Desktop"),
    ("CLAUDE_CODE_SESSION_ID", "Claude Code"),
    ("CURSOR_SESSION", "Cursor"),
    ("CURSOR_EDITOR", "Cursor"),
    ("CONTINUE_SESSION_ID", "Continue"),
    ("CODEX_SESSION_ID", "Codex"),
)

# Parent-process name → canonical client label.  Lowercase match on
# the basename so we don't care about ``.app`` suffixes, capitalisation
# variants, or Electron helper names that embed the parent.
_CLIENT_NAME_PROC_SIGNALS: tuple[tuple[str, str], ...] = (
    ("claude.app", "Claude Desktop"),
    ("claude", "Claude Desktop"),
    ("cursor.app", "Cursor"),
    ("cursor", "Cursor"),
    ("codex", "Codex"),
    ("continue", "Continue"),
)


def _sniff_client_from_env() -> str:
    """Walk the env-var priority ladder and return the first match.

    Kept pure so tests can patch ``os.environ`` without worrying about
    process-name side effects.
    """
    for var, label in _CLIENT_NAME_ENV_SIGNALS:
        if os.environ.get(var):
            return label
    # Generic CLAUDE_CODE_* fallback — new session-variable names ship
    # faster than this ladder can track them, so any CLAUDE_CODE_ prefix
    # with a non-empty value counts as Claude Code.  Isolated to its own
    # branch (not a tuple entry) because we need the prefix-match, not
    # an equality-match.
    for key, value in os.environ.items():
        if key.startswith("CLAUDE_CODE_") and value:
            return "Claude Code"
    # VS Code without Cursor indicates the VS Code MCP host, which is
    # worth distinguishing from Cursor (they set similar pids but
    # different identity env).  Placed last so explicit Cursor/Claude
    # signals always win.
    if os.environ.get("VSCODE_PID") and not os.environ.get("CURSOR_EDITOR"):
        return "VS Code"
    return ""


def _sniff_client_from_parent_process() -> str:
    """Best-effort parent-process name lookup on Unix.

    Uses ``ps`` — present on every macOS and Linux install we care
    about, including minimal container images.  Never raises;
    returns empty string on any failure (Windows, locked-down
    sandbox, truncated stat, unknown parent name).
    """
    try:
        import subprocess as _sp
        ppid = os.getppid()
        if ppid <= 0:
            return ""
        # -p: filter by pid; -o comm=: just the executable name, no header.
        result = _sp.run(
            ["ps", "-p", str(ppid), "-o", "comm="],
            capture_output=True, text=True, timeout=2.0,
        )
        name = (result.stdout or "").strip().lower()
        if not name:
            return ""
        # ps returns the full path on some systems — take the basename.
        base = name.rsplit("/", 1)[-1]
        for match, label in _CLIENT_NAME_PROC_SIGNALS:
            if match in base:
                return label
        return ""
    except Exception:
        return ""


def _is_stdin_tty() -> bool:
    """Wrapper so tests can patch the TTY check without touching
    ``sys.stdin`` (which click's CliRunner rewires anyway)."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _detect_client_name() -> str:
    """Return a best-guess AI client name for this pairing.

    Priority: env-var signal > parent-process name > "Unspecified (MCP)"
    (headless fallback) > "" (caller decides whether to prompt).

    Explicit ``--client`` handling is the caller's responsibility; it
    takes precedence over this helper.
    """
    env_hit = _sniff_client_from_env()
    if env_hit:
        return env_hit
    proc_hit = _sniff_client_from_parent_process()
    if proc_hit:
        return proc_hit
    # Headless: no TTY and no env/proc signals → almost certainly an
    # MCP subprocess launched by a client we don't have a signature
    # for.  Better to label it "Unspecified (MCP)" than leave it
    # blank, which shows as "Unspecified client" on /settings/agent
    # and is indistinguishable from a legit-but-unknown pairing.
    if not _is_stdin_tty():
        return "Unspecified (MCP)"
    return ""


def _prompt_for_client_name() -> str:
    """Interactive TTY fallback — ask the user which agent this is for.

    Only called when env/proc sniffs came up empty AND stdin is a TTY.
    A headless caller never gets here (``_detect_client_name`` returns
    "Unspecified (MCP)" first).  The default on Enter is ``[1] Claude
    Desktop`` — the most common answer, and the safest miss (the user
    can always edit the label from /settings/agent later).
    """
    options = [
        ("Claude Desktop", "Claude Desktop"),
        ("Claude Code", "Claude Code"),
        ("Cursor", "Cursor"),
        ("Codex", "Codex"),
        ("Other", None),
    ]
    click.echo("  Which agent is this pairing for?", err=True)
    for i, (label, _value) in enumerate(options, start=1):
        click.echo(f"    [{i}] {label}", err=True)
    try:
        raw = click.prompt(
            "  Choice", default="1", show_default=True,
            prompt_suffix=" ", err=True,
        )
    except (click.Abort, EOFError, KeyboardInterrupt):
        return ""
    try:
        idx = int(str(raw).strip())
    except (TypeError, ValueError):
        idx = 1
    if idx < 1 or idx > len(options):
        idx = 1
    label, value = options[idx - 1]
    if value is not None:
        return value
    # "Other" — free-text label, cap at 40 chars, strip non-printable
    # + surrounding whitespace.  The server also caps at 40 (same as
    # machine_label); we trim here so the user sees their final label
    # without a server round-trip.
    try:
        free = click.prompt(
            "  Label", default="", show_default=False,
            prompt_suffix=" ", err=True,
        )
    except (click.Abort, EOFError, KeyboardInterrupt):
        return ""
    cleaned = "".join(c for c in str(free or "")[:40] if c.isprintable()).strip()
    return cleaned


def _resolve_client_name(explicit: str | None) -> str:
    """One entry point for both ``pair`` and ``invite`` so there's a
    single source of truth for the priority order.

    ``explicit`` is whatever ``--client`` captured, including the
    absent-flag sentinel ``None``.  An explicit empty string ("" via
    ``--client ""``) is treated as "user wants no label" and short-
    circuits both the detection and the TTY prompt.
    """
    if explicit is not None:
        # User said what they wanted; honour it verbatim, just tidy
        # the visible characters + cap length.  No prompt, no sniff.
        cleaned = "".join(c for c in str(explicit)[:40] if c.isprintable()).strip()
        return cleaned
    detected = _detect_client_name()
    if detected:
        return detected
    # Env + proc sniffs came up empty AND we're interactive — ask.
    # _detect_client_name already returned "Unspecified (MCP)" for
    # the non-TTY case, so reaching this branch means TTY is True.
    if _is_stdin_tty():
        return _prompt_for_client_name()
    return ""


@click.command("pair")
@click.argument("code", required=True)
@click.option(
    "--client", "client",
    type=str, default=None,
    metavar="NAME",
    help=(
        "Label this pairing by the AI client running it — e.g. "
        "\"Claude Desktop\", \"Cursor\", \"Codex\".  If omitted, Kiln "
        "auto-detects from your environment.  On /settings/agent this "
        "shows alongside the hostname so you can tell two paired "
        "machines apart."
    ),
)
def auth_pair(code: str, client: str | None) -> None:
    """Pair this machine with your signed-in Kiln workshop session.

    After you upgrade on app.kiln3d.com, the workshop shows a pairing
    code.  Run this command here to sync your tier to this machine —
    your MCP server picks it up immediately, no Claude Desktop restart
    required.

    The code is single-use and expires in 10 minutes.  Generate a new
    one any time at app.kiln3d.com/connect.

    Accepts either the full ``KLN-ABCD-EFGH`` shape or just
    ``ABCD-EFGH`` (the prefix is added for you).
    """
    raw = (code or "").strip().upper()
    if not raw:
        raise click.ClickException("Missing pairing code.  Usage: kiln pair <code>")

    # Strip any leading ``kiln pair`` the user may have accidentally
    # double-pasted from the copy button.  Small courtesy, saves a
    # "why isn't this working" moment.
    for prefix in ("KILN PAIR ", "KILN PAIR\u00A0"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):].strip()
            break

    # Tidy typos: some users drop the ``KLN-`` prefix.  The server
    # accepts both shapes, but normalizing here means the terminal
    # banner we print on success shows the user what the server saw.
    if not raw.startswith("KLN-"):
        raw = f"KLN-{raw}"

    # Resolve the AI-client identity BEFORE printing the pairing banner
    # so the interactive prompt (if any) comes first — the user
    # shouldn't see "Pairing..." then get stopped with a question.
    client_name = _resolve_client_name(client)

    click.echo("")
    click.echo(f"  Pairing {_hostname_label()} with your Kiln workshop\u2026", err=True)

    resp = _http_post(
        "/api/auth/pairing/claim",
        {
            "code": raw,
            "machine_label": _hostname_label(),
            "client_name": client_name,
        },
    )

    if not resp.get("success"):
        # Server sends a machine-readable ``code`` field alongside the
        # human ``error`` so we can print tailored next-step guidance
        # without string-sniffing the error message.
        reason = str(resp.get("code") or "").lower()
        msg = resp.get("error") or "Pairing failed."
        if reason in ("not_found", "expired"):
            raise click.ClickException(
                f"{msg}\n\n  Get a fresh code at app.kiln3d.com/connect."
            )
        if reason == "already_claimed":
            raise click.ClickException(
                f"{msg}\n\n  If you paired on another machine, that's fine — "
                f"this machine just needs its own code."
            )
        if reason == "rate_limited":
            raise click.ClickException(
                f"{msg}  Wait a minute and retry."
            )
        raise click.ClickException(msg)

    # Defensive response validation — the CLI MUST NOT write a partial
    # token file that silently degrades to free-tier on every tool call.
    # If the server says ``success`` but omits ``access_token`` (happened
    # in the 2026-04-23 failure mode that surfaced as stale empty tokens
    # in ~/.kiln/auth_tokens.json), fail loudly instead of writing a lie.
    access_token = str(resp.get("access_token") or "")
    refresh_token = str(resp.get("refresh_token") or "")
    auth_uid = str(resp.get("auth_uid") or "")
    if not access_token:
        raise click.ClickException(
            "Pairing claim succeeded but the server response carried no "
            "access_token.  Nothing was written locally.  This is a "
            "server-side bug — please retry with a fresh code from "
            "app.kiln3d.com/connect, and if it persists: adam@kiln3d.com."
        )

    email = str(resp.get("email") or "")
    tier = str(resp.get("tier") or "free").lower()
    _write_tokens({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "email": email,
        "auth_uid": auth_uid,
        "tier": tier,
        "has_entitlement": bool(resp.get("has_entitlement")),
        "signed_in_at": int(time.time()),
    })

    # Post-write smoke test — confirm the token the server just gave us
    # is accepted by /api/auth/whoami.  The token IS written regardless
    # of smoke result: rolling back on a single-endpoint 401 would
    # incorrectly block pairing when only whoami has an upstream issue
    # (we saw this on 2026-04-23: whoami returned 401 on a Supabase-valid
    # JWT because the server's SUPABASE_URL was misconfigured vs the
    # issuer).  Instead: write tokens, then WARN loudly if whoami rejects
    # them, so the operator has visibility without losing the pair.
    try:
        smoke_status, _ = _http_get("/api/auth/whoami", bearer=access_token, timeout=5.0)
    except Exception:
        smoke_status = 0  # network / unexpected — skip the warning
    if smoke_status == 401:
        click.echo(
            "  ⚠ Tokens written, but /api/auth/whoami rejected them "
            "(HTTP 401).  Tools may still work if the gate accepts this "
            "token shape — verify with a real call.  If every tool "
            "fails: adam@kiln3d.com (server-side issuer/validator "
            "mismatch).",
            err=True,
        )
    elif smoke_status and smoke_status >= 400:
        click.echo(
            f"  ⚠ Tokens written, but /api/auth/whoami returned HTTP "
            f"{smoke_status}.  This is non-fatal; tools will still be "
            "attempted.",
            err=True,
        )

    # Final one-line confirmation on stdout (so `kiln pair ... | tail
    # -1` captures it).  Matches the `kiln signin` voice for source
    # consistency.
    click.echo(f"\u2713 Paired \u2014 {email} ({tier}).")


# ═════════════════════════════════════════════════════════════════════
# `kiln link` — CLI-initiated pairing (the reverse of `kiln pair`)
# ═════════════════════════════════════════════════════════════════════
#
# The mirror image of ``kiln pair``: the user is already signed in on
# THIS terminal (``kiln signin`` or ``kiln pair``) and wants to open a
# fresh browser tab at app.kiln3d.com while carrying the same session.
# ``kiln link`` mints a one-shot code that the browser's
# /settings/agent page can claim.
#
# Why this matters: pairing is now symmetric.  No matter which surface
# you sign in on first — web or terminal — you can pair the other in
# one code.  Before this, CLI-first users had to sign in twice (once
# in the terminal, once in the browser).
#
# Naming history: this command shipped briefly as ``kiln invite``
# (2026-04-23 → 2026-04-25) before the rename.  ``invite`` read as
# "invite another USER to Kiln" (referral / team-invite semantics)
# rather than "link my own browser tab to my own CLI session" — and
# it boxed us out of the ``invite`` namespace for the eventual
# referral system.  ``kiln invite`` survives one release as a hidden
# deprecation alias that prints a stderr warning and forwards.


@click.command("link")
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Emit the link response as JSON (for piping into scripts).",
)
@click.option(
    "--client", "client",
    type=str, default=None,
    metavar="NAME",
    help=(
        "Label this pairing by the AI client running it — e.g. "
        "\"Claude Desktop\", \"Cursor\".  The browser's /settings/agent "
        "list shows it alongside the hostname so two paired machines "
        "on the same host are distinguishable.  Auto-detected if omitted."
    ),
)
def auth_link(as_json: bool, client: str | None) -> None:
    """Generate a one-shot code to sign in on another device.

    Use this when you're signed in on THIS terminal (``kiln signin`` or
    ``kiln pair``) and want to open a fresh browser tab at
    app.kiln3d.com carrying the same session — no re-signin.

    How it works:
        1. Run ``kiln link`` here.
        2. The terminal prints a short code + the URL to visit.
        3. Open app.kiln3d.com/settings/agent in a browser, sign in if
           you haven't already (SAME account), and paste the code into
           the "Link from another device" box.
        4. The browser tab is now paired with this terminal's session.

    The code is single-use and expires in 10 minutes.
    """
    tokens = _read_tokens()
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise click.ClickException(
            "Not signed in on this terminal.  Run `kiln signin` first, "
            "then `kiln link` to pair a browser tab."
        )
    refresh_token = str(tokens.get("refresh_token") or "")

    # Resolve AI-client identity BEFORE minting — if we need to prompt
    # the user, we do it before the "Minting..." spinner so the flow
    # reads: prompt → mint → print code.
    client_name = _resolve_client_name(client)

    click.echo("")
    click.echo("  Minting a one-shot code for your browser\u2026", err=True)

    # POST /api/auth/pairing/invite — authed with our access_token via
    # the shared _http_post helper.  401 is surfaced as a ClickException
    # by the helper itself when a bearer is supplied.
    #
    # NOTE: the SERVER endpoint is still /api/auth/pairing/invite — this
    # is internal CLI/web wiring that users never see.  Renaming it
    # would force every test + the web client to migrate in lockstep
    # for zero user-visible benefit, so we leave it.  The user-facing
    # name (this command) is what changed.
    body = _http_post(
        "/api/auth/pairing/invite",
        {
            "refresh_token": refresh_token,
            "client_name": client_name,
        },
        bearer=access_token,
    )

    if not body.get("success"):
        raise click.ClickException(body.get("error") or "Could not mint a link code.")

    code = str(body.get("code") or "")
    expires_at = str(body.get("expires_at") or "")
    verify_url = str(body.get("verify_url") or "https://app.kiln3d.com/settings/agent")

    if as_json:
        click.echo(json.dumps(body, indent=2, sort_keys=True))
        return

    # Human output — match the `kiln signin` / `kiln pair` voice.
    # The code is the hero; render it letter-spaced so it's easy to
    # read off the terminal and type into a browser.  A single blank
    # line of breathing room above and below.
    click.echo("")
    click.echo(f"    {code}")
    click.echo("")
    click.echo(f"  Enter this code at:  {verify_url}", err=True)

    # Countdown — same 10-min window as the forward flow.  Best-effort
    # parse; if the ISO timestamp doesn't round-trip (timezone weirdness
    # on some platforms) we just skip the countdown.  Users have the
    # absolute timestamp from the API if they need it.
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        exp_dt = _dt.fromisoformat(expires_at.replace("Z", "+00:00"))
        now = _dt.now(tz=_tz.utc)
        mins = max(0, int((exp_dt - now).total_seconds() // 60))
        if mins:
            click.echo(f"  Expires in ~{mins} minute{'s' if mins != 1 else ''}.", err=True)
    except Exception:
        pass

    click.echo("")
    click.echo(
        "  After you enter the code in the browser, that tab will be "
        "signed in with the same account as this terminal.",
        err=True,
    )


# ═════════════════════════════════════════════════════════════════════
# `kiln invite` — deprecated alias for `kiln link` (one-release grace)
# ═════════════════════════════════════════════════════════════════════


@click.command(
    "invite",
    hidden=True,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.pass_context
def _auth_invite_deprecated(ctx: click.Context) -> None:
    """[deprecated] alias for ``kiln link``.

    Forwards every flag through unchanged so existing scripts keep
    working.  Will be removed one PyPI release after the rename ships.
    """
    click.echo(
        "warning: `kiln invite` has been renamed to `kiln link` "
        "(this alias will be removed in the next release).",
        err=True,
    )
    ctx.invoke(auth_link, **{k: v for k, v in _parse_link_args(ctx.args).items()})


def _parse_link_args(args: list[str]) -> dict[str, Any]:
    """Tiny re-parser for the deprecation alias.

    Click's ``ctx.forward`` doesn't compose well with ``allow_extra_args``
    when the source and target commands have the same option set, so we
    reconstruct the kwargs by hand.  Keep this dumb on purpose — only
    the two flags ``auth_link`` actually accepts (``--json``,
    ``--client``).  Anything else is silently dropped (with a stderr
    warning above flagging the deprecation, the user already knows
    they're on a legacy code path).
    """
    out: dict[str, Any] = {"as_json": False, "client": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            out["as_json"] = True
            i += 1
        elif a == "--client" and i + 1 < len(args):
            out["client"] = args[i + 1]
            i += 2
        elif a.startswith("--client="):
            out["client"] = a.split("=", 1)[1]
            i += 1
        else:
            i += 1
    return out


# ═════════════════════════════════════════════════════════════════════
# registration
# ═════════════════════════════════════════════════════════════════════


def register_auth_cli(cli_group: click.Group) -> None:
    """Attach ``kiln signin`` / ``kiln signout`` / ``kiln whoami`` /
    ``kiln pair`` / ``kiln link``.

    If the group already has a command named ``login`` (the legacy
    identity-linking flow in kiln-pro's ``vcs_commands.cli_login``), we
    move it under ``kiln identity login`` so the top-level ``login``
    name is free for the OAuth device flow — which is the command the
    vast majority of users type first.

    Called from ``kiln.cli.main`` unconditionally so every install
    (free tier + Pro) gets ``kiln pair`` / ``kiln signin`` out of the
    box.
    """
    # NOTE: There used to be legacy-login relocation logic here —
    # kiln-pro would register ``cli_login`` as a top-level ``login``
    # command, and we'd pop it + move it into the ``identity`` group
    # to free the canonical name.  kiln-pro now registers its
    # identity-linking command natively as ``kiln identity link`` (see
    # ``kiln_pro.cli.vcs_commands.register_pro_cli``), so there's
    # nothing to relocate.  Keeping this comment as an audit trail in
    # case some future kiln-pro release regresses.

    # Canonical names: signin / signout (match the web workshop + MCP tools).
    cli_group.add_command(auth_login)
    cli_group.add_command(auth_logout)
    cli_group.add_command(auth_whoami)
    cli_group.add_command(auth_pair)
    cli_group.add_command(auth_link)
    # Legacy aliases: `kiln signin` / `kiln logout` keep working for existing
    # scripts + muscle memory.  Docs point at signin/signout.
    cli_group.add_command(auth_login, name="login")
    cli_group.add_command(auth_logout, name="logout")
    # Deprecation alias: `kiln invite` was the original name for this
    # command (2026-04-23 → 2026-04-25).  Hidden from --help so docs
    # point at `kiln link`, but still callable so any script / muscle
    # memory written in those two days keeps working for ONE release.
    # Drop after the next PyPI release ships and shows green metrics.
    cli_group.add_command(_auth_invite_deprecated, name="invite")
