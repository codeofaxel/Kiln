"""``kiln login`` / ``kiln logout`` / ``kiln whoami`` / ``kiln pair`` —
the CLI's device-code OAuth flow + web-initiated pairing against the
Kiln REST API.

How the login flow works end-to-end:

  1. ``kiln login`` POSTs ``/api/auth/device/start``.  Gets back a
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

``kiln logout`` deletes the token file.

``kiln whoami`` reads the file, hits ``GET /api/auth/whoami`` with the
access_token, and prints the resolved email + tier.

``kiln pair <code>`` is the web-initiated mirror of ``kiln login`` —
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
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
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


def _http_post(path: str, body: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """Thin requests.post wrapper that returns a dict or raises
    click.ClickException with a human message.  Keeping the HTTP
    surface tiny (no retry, no session reuse) because these endpoints
    are called at most a few dozen times across the whole flow."""
    try:
        import requests  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — requests is a hard dep
        raise click.ClickException(
            "The `requests` package is required for `kiln login`. "
            "Install with `pip install requests`."
        ) from exc

    try:
        resp = requests.post(
            f"{_api_base()}{path}",
            json=body,
            timeout=timeout,
            headers={"User-Agent": f"kiln-cli/{_cli_version()}"},
        )
    except requests.exceptions.ConnectionError as exc:
        raise click.ClickException(
            f"Could not reach Kiln API at {_api_base()} — is the server up?"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise click.ClickException(
            f"Timed out reaching {_api_base()}{path}."
        ) from exc

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
# `kiln login`
# ═════════════════════════════════════════════════════════════════════


@click.command("login")
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

    Launches the device-code flow: opens your browser to a small
    provider-picker page, polls until you finish signing in, then
    writes your session token to ``~/.kiln/auth_tokens.json``.

    The rest of the CLI (and the desktop app, when it reuses the same
    tokens file) will pick up your session automatically — no license
    key needed for OAuth-linked Pro+ accounts.
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
            "Run `kiln login` again."
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
            result.get("message") or "Sign-in code expired. Run `kiln login` again."
        )
    raise click.ClickException(f"Unexpected sign-in status: {status!r}")


# ═════════════════════════════════════════════════════════════════════
# `kiln logout`
# ═════════════════════════════════════════════════════════════════════


@click.command("logout")
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
            "Not signed in. Run `kiln login` to sign in."
        )

    code, body = _http_get("/api/auth/whoami", bearer=access_token)
    if code == 401:
        raise click.ClickException(
            "Session expired. Run `kiln login` to sign in again."
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
# The mirror image of ``kiln login``: instead of this machine bootstrapping
# an OAuth session from cold, the user has already signed in on the web
# (usually right after upgrading their tier at app.kiln3d.com/checkout/
# success → /welcome).  The workshop minted a short-lived code tied to
# their session; ``kiln pair <code>`` hands the tokens to this machine
# without any browser round-trip here.
#
# Why this is the preferred post-upgrade path (even though ``kiln login``
# still works): after paying, the user is already in a browser, already
# signed in.  Asking them to run ``kiln login`` which opens ANOTHER
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


@click.command("pair")
@click.argument("code", required=True)
def auth_pair(code: str) -> None:
    """Pair this machine with your signed-in Kiln workshop session.

    After you upgrade on app.kiln3d.com, the workshop shows a pairing
    code.  Run this command here to sync your tier to this machine —
    your MCP server picks it up immediately, no Claude Desktop restart
    required.

    The code is single-use and expires in 10 minutes.  Generate a new
    one any time at app.kiln3d.com/welcome.

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

    click.echo("")
    click.echo(f"  Pairing {_hostname_label()} with your Kiln workshop\u2026", err=True)

    resp = _http_post(
        "/api/auth/pairing/claim",
        {"code": raw, "machine_label": _hostname_label()},
    )

    if not resp.get("success"):
        # Server sends a machine-readable ``code`` field alongside the
        # human ``error`` so we can print tailored next-step guidance
        # without string-sniffing the error message.
        reason = str(resp.get("code") or "").lower()
        msg = resp.get("error") or "Pairing failed."
        if reason in ("not_found", "expired"):
            raise click.ClickException(
                f"{msg}\n\n  Get a fresh code at app.kiln3d.com/welcome."
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
            "app.kiln3d.com/welcome, and if it persists: adam@kiln3d.com."
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
    # -1` captures it).  Matches the `kiln login` voice for source
    # consistency.
    click.echo(f"\u2713 Paired \u2014 {email} ({tier}).")


# ═════════════════════════════════════════════════════════════════════
# registration
# ═════════════════════════════════════════════════════════════════════


def register_auth_cli(cli_group: click.Group) -> None:
    """Attach ``kiln login`` / ``kiln logout`` / ``kiln whoami`` /
    ``kiln pair``.

    If the group already has a command named ``login`` (the legacy
    identity-linking flow in kiln-pro's ``vcs_commands.cli_login``), we
    move it under ``kiln identity login`` so the top-level ``login``
    name is free for the OAuth device flow — which is the command the
    vast majority of users type first.

    Called from ``kiln.cli.main`` unconditionally so every install
    (free tier + Pro) gets ``kiln pair`` / ``kiln login`` out of the
    box.
    """
    # Relocate the legacy identity-linking login, if present.
    legacy = cli_group.commands.pop("login", None)
    if legacy is not None:
        identity = cli_group.commands.get("identity")
        if identity is not None and hasattr(identity, "add_command"):
            # ``kiln identity login`` becomes the new home for the
            # GitHub-identity-linking flow.  Rename via .name so help
            # text stays consistent.
            legacy.name = "login"
            try:
                identity.add_command(legacy)
            except Exception:
                # If the identity group already has a login subcommand
                # (future-proof), bail out without overwriting it —
                # better to silently skip than to shadow behaviour.
                pass

    cli_group.add_command(auth_login)
    cli_group.add_command(auth_logout)
    cli_group.add_command(auth_whoami)
    cli_group.add_command(auth_pair)
