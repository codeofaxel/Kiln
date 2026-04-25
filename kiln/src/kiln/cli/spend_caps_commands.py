"""``kiln spend-caps`` — view, raise, and one-shot-approve over the agent flow.

The web app at ``app.kiln3d.com/settings/billing/spend-caps`` is the
canonical surface for cap changes — it always works, requires AAL2,
and is where the audit log + revert email links live.  This subcommand
is the CLI mirror that opt-in power users reach for when they live in
a terminal.

Discipline (Terms §5.8 v2.5):

  1. **Opt-in** (default off).  ``kiln spend-caps show`` reports
     whether the agent flow is enabled; ``raise`` and ``approve-order``
     refuse with a clear pointer to the web-app toggle when it isn't.

  2. **Two-beat**: the command POSTs ``request-*``, prints the intent
     block (current vs requested), prompts ``Authenticator code:``,
     reads stdin, then POSTs ``confirm-*``.  The CLI NEVER mints a
     TOTP; it only ferries what the user types.

  3. **Rate-limited** server-side: 3 attempts per rolling 24h window
     across web/cli/mcp.  The third locks all surfaces for 24h and
     emails the user.

  4. **Source-surface tagged**: every change-log row carries
     ``via='cli'`` so the audit trail records the path.

All commands talk only to the public Kiln REST API at
``https://api.kiln3d.com`` (override with ``KILN_API_URL``).  No
proprietary logic lives here — kiln-pro is not required for this
CLI to work; the user just needs a valid bearer in
``~/.kiln/auth_tokens.json`` (written by ``kiln signin`` or
``kiln pair``).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click


# Reuse the shared HTTP helpers from auth_commands.py rather than
# duplicating the bearer-write / response-shape code.  The point is to
# keep ONE place where the CLI's request envelope + error shape lives.
from kiln.cli.auth_commands import (
    _api_base,
    _http_get,
    _http_post,
    _read_tokens,
)


# Where the user toggles the opt-in flag — printed in every "opt-in
# off" error message so they know exactly where to go next.
_ENABLE_URL = "https://app.kiln3d.com/settings/billing/spend-caps"


# ---------------------------------------------------------------------------
# Helpers — bearer + structured-error shape
# ---------------------------------------------------------------------------


def _require_signin_bearer() -> str:
    """Pull the user's access_token from ~/.kiln/auth_tokens.json.

    Raises a ClickException pointing at ``kiln signin`` when no token
    is present.  Fail-fast: every cap-change command requires a real
    Supabase JWT — license-key bearers cannot run TOTP verify.
    """
    tokens = _read_tokens()
    bearer = str(tokens.get("access_token") or "")
    if not bearer:
        raise click.ClickException(
            "Not signed in.  Run `kiln signin` first, then re-run "
            "this command."
        )
    return bearer


def _http_post_authed(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST authed; never throws on 4xx — returns the JSON envelope so
    we can pattern-match on ``error`` (OPT_IN_REQUIRED / RATE_LIMITED /
    below_floor / etc.)."""
    bearer = _require_signin_bearer()
    return _http_post(path, body, bearer=bearer)


def _format_caps_summary(caps: dict[str, Any]) -> str:
    """Return ``per-order $1,000 / monthly $5,000`` style summary."""
    per = caps.get("per_order_dollars")
    mon = caps.get("monthly_dollars")
    if per is None and "per_order_cents" in caps:
        per = (caps.get("per_order_cents") or 0) / 100.0
    if mon is None and "monthly_cents" in caps:
        mon = (caps.get("monthly_cents") or 0) / 100.0
    return (
        f"per-order ${float(per or 0):,.0f} / monthly ${float(mon or 0):,.0f}"
    )


def _surface_envelope_error(resp: dict[str, Any], default: str) -> click.ClickException:
    """Map a non-success REST envelope to a click.ClickException with
    a human-friendly message that calls out OPT_IN_REQUIRED /
    RATE_LIMITED specially."""
    code = str(resp.get("error") or "ERROR").upper()
    msg = resp.get("message") or resp.get("error") or default
    if code == "OPT_IN_REQUIRED":
        url = resp.get("enable_url") or _ENABLE_URL
        return click.ClickException(
            "Agent-driven cap changes are disabled for your account.\n"
            f"  Open  {url}\n"
            "  and enable 'Allow CLI / MCP cap changes' (requires 2FA) "
            "to use this command."
        )
    if code == "RATE_LIMITED":
        retry = resp.get("retry_after_seconds")
        retry_line = ""
        try:
            r = int(retry or 0)
            if r > 0:
                hrs = r // 3600
                if hrs:
                    retry_line = f"  Lock auto-clears in ~{hrs}h."
                else:
                    retry_line = f"  Lock auto-clears in ~{r // 60}m."
        except (TypeError, ValueError):
            pass
        return click.ClickException(
            "Cap-change attempts are paused for 24 hours (3 attempts in "
            "the last 24h).\n"
            "  This is rate-limit protection across all surfaces "
            "(web/cli/mcp).\n"
            f"{retry_line}\n"
            "  Check your email for a notification with the attempt "
            "details, and write to adam@kiln3d.com if it wasn't you."
        )
    if code == "BELOW_FLOOR":
        cap = resp.get("cap") or "?"
        floor = resp.get("floor_cents")
        return click.ClickException(
            f"That value is below the protective floor.  cap={cap}, "
            f"floor_cents={floor}.  You can RAISE caps; you cannot "
            "LOWER them below the default ($500 / $2,000)."
        )
    return click.ClickException(f"{msg}  (code={code})")


# ---------------------------------------------------------------------------
# kiln spend-caps  (group)
# ---------------------------------------------------------------------------


@click.group("spend-caps")
def spend_caps() -> None:
    """View, raise, and one-shot-approve Kiln spend caps from the CLI.

    The web app at app.kiln3d.com/settings/billing/spend-caps is the
    canonical surface; this group is the CLI mirror, opt-in.  Every
    cap change still requires a 6-digit code from your authenticator
    app.
    """


# ---------------------------------------------------------------------------
# `kiln spend-caps show`
# ---------------------------------------------------------------------------


@spend_caps.command("show")
@click.option(
    "--json", "as_json", is_flag=True, default=False,
    help="Emit the full caps response as JSON (for piping into scripts).",
)
def cmd_show(as_json: bool) -> None:
    """Print your current per-order and monthly spend caps + opt-in status.

    Read-only; no AAL2 or TOTP required.
    """
    bearer = _require_signin_bearer()
    code, body = _http_get("/api/billing/spend-caps", bearer=bearer)
    if code == 401:
        raise click.ClickException(
            "Session expired.  Run `kiln signin` to sign in again."
        )
    if code >= 400 or not body.get("success"):
        raise click.ClickException(
            body.get("error") or f"Could not load spend caps (HTTP {code})."
        )
    if as_json:
        click.echo(json.dumps(body, indent=2, sort_keys=True))
        return
    caps = body.get("caps") or {}
    defaults = body.get("defaults") or {}
    is_default = bool(caps.get("is_default"))
    opt_in = bool(body.get("agent_flow_enabled"))
    click.echo("")
    click.echo(f"  Current caps:  {_format_caps_summary(caps)}"
               + ("  (default)" if is_default else "  (custom)"))
    click.echo(f"  Defaults:      "
               f"per-order ${float(defaults.get('per_order_dollars') or 500):,.0f} / "
               f"monthly ${float(defaults.get('monthly_dollars') or 2000):,.0f}")
    click.echo(
        "  Agent flow:    "
        + ("enabled" if opt_in else "disabled (CLI/MCP cap changes are off)")
    )
    if not opt_in:
        click.echo("")
        click.echo(
            "  To enable CLI/MCP cap changes:")
        click.echo(f"    {_ENABLE_URL}")
        click.echo(
            "  (toggling the flag itself requires 2FA from the web app)"
        )


# ---------------------------------------------------------------------------
# `kiln spend-caps raise`
# ---------------------------------------------------------------------------


def _prompt_totp(prompt: str = "Authenticator code") -> str:
    """Prompt the user for a 6-digit TOTP, normalize, validate.

    Strips spaces (so "123 456" works), refuses anything that doesn't
    parse as a 6-digit numeric.  ClickException on bad input — the
    user can re-run the command for a fresh challenge.
    """
    raw = click.prompt(
        f"  {prompt}", default="", show_default=False,
        prompt_suffix=": ",
    )
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) != 6:
        raise click.ClickException(
            "That didn't look like a 6-digit code from your "
            "authenticator app.  Re-run this command for a fresh "
            "challenge."
        )
    return digits


@spend_caps.command("raise")
@click.option(
    "--per-order", "per_order_dollars", type=float, default=None,
    help="New per-order cap in dollars (floor $500).  Optional.",
)
@click.option(
    "--monthly", "monthly_dollars", type=float, default=None,
    help="New monthly cap in dollars (floor $2,000).  Optional.",
)
@click.option(
    "--reason", type=str, default=None,
    help="Free-form rationale stored in the audit log row.  Optional.",
)
def cmd_raise(
    per_order_dollars: float | None,
    monthly_dollars: float | None,
    reason: str | None,
) -> None:
    """Raise your per-order and/or monthly cap above the protective default.

    Two beats:

      1. Posts /api/billing/spend-caps/request-change — server stashes
         the requested change and returns a 5-minute challenge_id.
      2. You read the 6-digit code from your authenticator app; CLI
         posts /api/billing/spend-caps/confirm-change.  On success,
         the new caps are committed and you receive a confirmation
         email with a 24-hour revert link.

    Refuses with a clear pointer to the web-app toggle when the
    agent-flow opt-in is off.  Rate-limited across all surfaces:
    3 attempts in 24h pauses further changes for 24h.
    """
    if per_order_dollars is None and monthly_dollars is None:
        raise click.ClickException(
            "Specify at least one of --per-order or --monthly.  "
            "(`kiln spend-caps show` to see your current caps.)"
        )

    body: dict[str, Any] = {"surface": "cli"}
    if per_order_dollars is not None:
        body["per_order_dollars"] = float(per_order_dollars)
    if monthly_dollars is not None:
        body["monthly_dollars"] = float(monthly_dollars)
    if reason:
        body["reason"] = str(reason)[:200]

    # Beat 1: request-change.
    resp = _http_post_authed("/api/billing/spend-caps/request-change", body)
    if not resp.get("success"):
        raise _surface_envelope_error(
            resp, default="Could not request a cap change.",
        )

    challenge_id = str(resp.get("challenge_id") or "")
    current = resp.get("current_caps") or {}
    requested = resp.get("requested_caps") or {}
    expires_at = str(resp.get("expires_at") or "")

    click.echo("")
    click.echo(f"  Current caps:    {_format_caps_summary(current)}")
    click.echo(f"  Requested caps:  {_format_caps_summary(requested)}")
    if reason:
        click.echo(f"  Reason:          {reason}")
    click.echo(f"  Challenge expires: {expires_at}")
    click.echo("")
    click.echo("  Open your authenticator app and read the current 6-digit code.")
    click.echo("")

    code = _prompt_totp()

    # Beat 2: confirm-change.
    confirm = _http_post_authed(
        "/api/billing/spend-caps/confirm-change",
        {
            "challenge_id": challenge_id,
            "totp_code": code,
            "surface": "cli",
        },
    )
    if not confirm.get("success"):
        raise _surface_envelope_error(
            confirm, default="Cap change denied.",
        )

    new_caps = confirm.get("new_caps") or {}
    cl_id = confirm.get("change_log_id")
    click.echo("")
    click.echo(f"✓ Caps updated — {_format_caps_summary(new_caps)}.")
    if cl_id:
        click.echo(f"  Audit log id: #{cl_id}")
    click.echo(
        "  A confirmation email with a 24-hour revert link is on its way."
    )


# ---------------------------------------------------------------------------
# `kiln spend-caps approve-order`
# ---------------------------------------------------------------------------


@spend_caps.command("approve-order")
@click.argument("order_id", required=True)
@click.option(
    "--max", "max_dollars", type=float, required=True,
    help="Order total being approved, in dollars.  Must be >= the "
         "actual order total.",
)
def cmd_approve_order(order_id: str, max_dollars: float) -> None:
    """Mint a one-shot, 5-minute approval token for a single over-cap order.

    Use this when an order is refused with SPEND_CAP_REACHED and you
    want to allow JUST THIS order without permanently raising the cap.
    The token is single-use, bound to (you, order, max_cents).

    Two beats — same shape as `kiln spend-caps raise`.
    """
    order_id = (order_id or "").strip()
    if not order_id or max_dollars <= 0:
        raise click.ClickException(
            "Need a non-empty <order_id> and --max > 0."
        )

    resp = _http_post_authed(
        "/api/billing/spend-caps/request-order-approval",
        {
            "order_id": order_id,
            "max_dollars": float(max_dollars),
            "surface": "cli",
        },
    )
    if not resp.get("success"):
        raise _surface_envelope_error(
            resp, default="Could not request an order approval.",
        )

    challenge_id = str(resp.get("challenge_id") or "")
    summary = resp.get("order_summary") or {}
    expires_at = str(resp.get("expires_at") or "")

    click.echo("")
    click.echo(f"  Order:          {summary.get('order_id', order_id)}")
    click.echo(f"  Approving up to ${float(summary.get('max_dollars') or max_dollars):,.2f}")
    click.echo(f"  Challenge expires: {expires_at}")
    click.echo("")
    click.echo("  Open your authenticator app and read the current 6-digit code.")
    click.echo("")

    code = _prompt_totp()

    confirm = _http_post_authed(
        "/api/billing/spend-caps/confirm-order-approval",
        {
            "challenge_id": challenge_id,
            "totp_code": code,
            "surface": "cli",
        },
    )
    if not confirm.get("success"):
        raise _surface_envelope_error(
            confirm, default="Order approval denied.",
        )

    token = str(confirm.get("approval_token") or "")
    expires = str(confirm.get("expires_at") or "")
    click.echo("")
    click.echo("✓ Approval token minted (single-use, 5-minute TTL).")
    click.echo("")
    click.echo(f"    {token}")
    click.echo("")
    click.echo(f"  Expires: {expires}")
    click.echo(
        "  Re-run the order with this token to bypass the cap for THIS "
        "order only."
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_spend_caps_cli(cli_group: click.Group) -> None:
    """Attach `kiln spend-caps {show,raise,approve-order}`.

    Called from ``kiln.cli.main`` unconditionally — these commands
    talk only to the public Kiln REST API and don't depend on
    kiln-pro being installed locally.  The agent-flow opt-in lives
    server-side, so a free-tier user with the opt-in enabled can
    drive cap changes from the CLI just fine.
    """
    cli_group.add_command(spend_caps)
