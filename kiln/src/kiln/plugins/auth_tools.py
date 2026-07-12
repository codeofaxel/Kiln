"""MCP wrappers for the Kiln sign-in device-code flow.

Users increasingly live inside agent chats — Claude Code, Claude
Desktop, Cursor — and dropping out to a terminal to authenticate Kiln
is the papercut that turns "I'll try Kiln" into "I'll try it later".
This module exposes the same device-code flow the ``kiln signin`` CLI
uses as two MCP tools, so an agent can sign the user in without leaving
the chat.

* :func:`kiln_signin` — POSTs ``/api/auth/device/start`` and returns
  the ``verification_uri`` + ``device_code`` the agent surfaces.

* :func:`kiln_signin_poll` — POSTs ``/api/auth/device/poll``; on
  success writes the tokens to ``~/.kiln/auth_tokens.json`` (the same
  file the CLI writes, so every other Kiln tool picks the session up
  instantly).

Both tools run LOCALLY, on the user's machine — which is the whole
point: the poll writes the session token to the caller's own
``~/.kiln``.  A hosted proxy can't do that (it would write to the
server), so these live in public Kiln where they run beside the user's
session, not in the private overlay.  They are thin transport wrappers
over the already-public ``kiln.cli.auth_commands`` helpers; there is
deliberately no new logic here.

Both tools are free tier — sign-in must work before anyone has a tier.
"""
from __future__ import annotations

import time
from typing import Any


class _AuthToolsPlugin:
    """Agent-side sign-in: device-code OAuth as two MCP tools."""

    @property
    def name(self) -> str:
        return "auth_tools"

    @property
    def description(self) -> str:
        return (
            "Sign in to Kiln from inside an agent chat without dropping "
            "to a terminal.  ``kiln_signin`` starts the OAuth device flow "
            "and returns a browser URL; ``kiln_signin_poll`` waits for "
            "the user to finish and writes the session tokens."
        )

    def register(self, mcp: Any) -> None:
        @mcp.tool()
        def kiln_signin() -> dict[str, Any]:
            """Start a Kiln sign-in via OAuth.

            Returns a URL the user can open in their browser to sign in
            with Google / Apple / GitHub.  Relay the ``verification_uri``
            to the user and then call ``kiln_signin_poll(device_code)``
            every few seconds until the status is no longer ``"pending"``.

            Response fields (all strings unless noted):

            * ``verification_uri`` — URL to open in the browser; the
              ``user_code`` is already embedded as a query param so the
              user typically doesn't have to type anything.
            * ``user_code`` — the short human-readable code
              (``KLN-ABCD-EFGH``); show it only as a fallback in case
              the verification URL didn't pre-fill it.
            * ``device_code`` — secret; pass to ``kiln_signin_poll``,
              never show to the user.
            * ``interval`` (int) — seconds to wait between polls
              (default 2).
            * ``expires_in`` (int) — seconds the code is valid for
              (900 = 15 minutes).

            A free Kiln account adds a cloud design library with share
            links plus the free monthly allowance of Kiln's hosted
            tools.  Free tier — no license key required; this is the
            very first call an unauthenticated user makes.
            """
            import kiln.server as _srv

            try:
                from kiln.cli.auth_commands import _http_post
            except Exception as exc:  # pragma: no cover — helpers are in-package
                return _srv._error_dict(
                    f"kiln_signin: sign-in helpers unavailable: {exc}",
                    code="SIGNIN_UNAVAILABLE",
                )

            try:
                resp = _http_post("/api/auth/device/start", {})
            except Exception as exc:
                # _http_post raises click.ClickException on any HTTP / network
                # failure; surface its message rather than let it bubble.
                return _srv._error_dict(
                    f"kiln_signin: could not start the sign-in flow: {exc}",
                    code="SIGNIN_START_FAILED",
                )

            if not resp.get("success", True) and resp.get("error"):
                return _srv._error_dict(
                    resp.get("error") or "Could not start the sign-in flow.",
                    code="SIGNIN_START_FAILED",
                )

            return {
                "success": True,
                "verification_uri": resp.get("verification_uri") or "",
                "user_code": resp.get("user_code") or "",
                "device_code": resp.get("device_code") or "",
                "interval": int(resp.get("interval") or 2),
                "expires_in": int(resp.get("expires_in") or 900),
                "instructions": (
                    "Open verification_uri in your browser to finish "
                    "signing in.  The agent will poll every few seconds "
                    "with kiln_signin_poll until you're done."
                ),
            }

        @mcp.tool()
        def kiln_signin_poll(device_code: str) -> dict[str, Any]:
            """Check whether a sign-in started by ``kiln_signin`` is done.

            Call this repeatedly (every ``interval`` seconds) with the
            ``device_code`` that ``kiln_signin`` returned.  Each call is a
            single HTTP round-trip (it does NOT block), so the agent stays
            responsive and the user sees progress.

            Returns ``{"status": "pending" | "success" | "denied" |
            "expired", ...}``.

            * ``pending`` — user hasn't finished in the browser yet;
              wait ``interval`` seconds and call again.
            * ``success`` — tokens have been written to
              ``~/.kiln/auth_tokens.json`` (mode 0600); the response
              also echoes ``email`` and ``tier``.  Every other Kiln tool
              picks up the new session automatically.
            * ``denied`` — user cancelled in the browser.
            * ``expired`` — the device_code timed out (15 min window);
              call ``kiln_signin`` again for a fresh code.

            Free tier — no license key required.
            """
            import kiln.server as _srv

            code = (device_code or "").strip()
            if not code:
                return _srv._error_dict(
                    "kiln_signin_poll: device_code is required "
                    "(use the value from kiln_signin).",
                    code="INVALID_INPUT",
                )

            try:
                from kiln.cli.auth_commands import _http_post, _write_tokens
            except Exception as exc:  # pragma: no cover — helpers are in-package
                return _srv._error_dict(
                    f"kiln_signin_poll: sign-in helpers unavailable: {exc}",
                    code="SIGNIN_UNAVAILABLE",
                )

            try:
                resp = _http_post("/api/auth/device/poll", {"device_code": code})
            except Exception as exc:
                return _srv._error_dict(
                    f"kiln_signin_poll: could not reach Kiln API: {exc}",
                    code="SIGNIN_POLL_FAILED",
                )

            status = str(resp.get("status") or "pending").lower()

            if status == "pending":
                return {"success": True, "status": "pending"}

            if status == "success":
                access_token = str(resp.get("access_token") or "")
                if not access_token:
                    return _srv._error_dict(
                        "kiln_signin_poll: sign-in succeeded but the "
                        "server response carried no access_token.  "
                        "Nothing was written locally.",
                        code="TOKEN_RESPONSE_INCOMPLETE",
                    )
                email = str(resp.get("email") or "")
                tier = str(resp.get("tier") or "free").lower()
                try:
                    _write_tokens({
                        "access_token": access_token,
                        "refresh_token": resp.get("refresh_token") or "",
                        "email": email,
                        "auth_uid": resp.get("auth_uid") or "",
                        "tier": tier,
                        "has_entitlement": bool(resp.get("has_entitlement")),
                        "signed_in_at": int(time.time()),
                    })
                except Exception as exc:
                    return _srv._error_dict(
                        f"kiln_signin_poll: sign-in succeeded but the "
                        f"token file could not be written: {exc}",
                        code="TOKEN_WRITE_FAILED",
                    )
                return {
                    "success": True,
                    "status": "success",
                    "email": email,
                    "tier": tier,
                    "message": f"Signed in as {email} ({tier}).",
                }

            # denied / expired / anything else — echo the server's human
            # message if present, but never raise.
            out: dict[str, Any] = {"success": True, "status": status}
            msg = resp.get("message")
            if msg:
                out["message"] = msg
            return out


plugin = _AuthToolsPlugin()


def register(mcp: Any) -> None:
    plugin.register(mcp)
