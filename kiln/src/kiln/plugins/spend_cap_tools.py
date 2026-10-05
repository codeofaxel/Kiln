"""Spend caps on fulfillment orders, from inside an agent chat.

Every signed-in account has a per-order and a monthly ceiling on what an
agent may spend on manufacturing orders.  The web app's billing settings are
where they are managed; these five tools let a person who lives in agent chat
read them, raise them, or approve one order over the ceiling without leaving
the conversation.

They run LOCALLY, like the sign-in tools beside them: each one calls the Kiln
API as the person signed in on THIS machine, with the session ``kiln signin``
stored here.  A hosted proxy has no such session to call with, which is why
they live in public Kiln.  They are transport only; every rule is the
server's:

* Changing caps from chat is off until the account owner turns it on in the
  web app.  A refusal carries ``enable_url``.
* A change is two calls.  The first states the change and returns a
  ``challenge_id``; the second carries the six-digit code the person reads
  from their own authenticator app.  These tools never produce that code.
* Attempts are limited per day across every surface; a refusal carries
  ``retry_after_seconds``.

No plan is needed: an account has spend caps from the moment it signs in.
"""

from __future__ import annotations

from typing import Any

#: Where the account owner turns chat-driven cap changes on.
_ENABLE_URL = "https://app.kiln3d.com/settings/billing/spend-caps"

_TIMEOUT_S = 15.0

#: Keys of a refusal that are the envelope's own and are not repeated as
#: detail.
_ENVELOPE_KEYS = frozenset({"success", "error", "message"})


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "status": "success", **payload, "data": payload}


def _err(message: str, code: str = "ERROR") -> dict[str, Any]:
    return {"success": False, "status": "error", "error": message, "code": code}


def _session() -> tuple[str, dict[str, Any] | None]:
    """``(token, None)`` for the person signed in here, else ``("", refusal)``.

    The session and nothing else: these routes act on a person's account,
    and a license key names a plan, not a person.
    """
    from kiln.auth_session import resolve_session_bearer

    session = resolve_session_bearer()
    if session.token:
        return session.token, None
    return "", _err(
        (session.detail + "  " if session.detail else "")
        + "Sign in to Kiln before reading or changing spend caps: call "
        "`kiln_signin` in this chat (or run `kiln signin`), then call this "
        "tool again.",
        code="SIGNIN_REQUIRED",
    )


def _call(
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One request to the Kiln API as the signed-in person.

    Returns the parsed body whatever the status, because a refusal's body
    carries the code and the fields the person needs.  Raises when the
    servers could not be reached at all.
    """
    import httpx

    from kiln.auth_session import _api_base

    resp = httpx.request(
        method,
        f"{_api_base()}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "kiln-mcp-spend-caps/1.0",
        },
        json=body if method != "GET" else None,
        timeout=_TIMEOUT_S,
    )
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed:
        return parsed
    return {
        "success": False,
        "error": "non_json_response",
        "status": resp.status_code,
        "text": (resp.text or "")[:200],
    }


def _ask(tool: str, method: str, path: str, body: dict[str, Any] | None = None):
    """``(response, None)`` or ``(None, refusal)`` for one call."""
    token, refusal = _session()
    if refusal:
        return None, refusal
    try:
        return _call(token, method, path, body), None
    except Exception as exc:  # noqa: BLE001 — said to the person, not raised
        return None, _err(f"{tool}: could not reach Kiln's servers: {exc}")


def _refusal(resp: dict[str, Any], *, default: str) -> dict[str, Any]:
    """A refusal from the servers, with every field they sent kept readable.

    ``enable_url``, ``retry_after_seconds`` and the like are folded into the
    message as ``key=value`` so they reach the person even through a client
    that shows only the message.
    """
    code = str(resp.get("error") or "ERROR").upper()
    message = resp.get("message") or resp.get("error") or default
    extras = [f"{k}={v}" for k, v in resp.items() if k not in _ENVELOPE_KEYS]
    if extras:
        message = f"{message}  ({'; '.join(extras)})"
    return _err(message, code=code)


def _caps_summary(caps: dict[str, Any]) -> str:
    """One line for a caps record, in dollars."""
    per = caps.get("per_order_dollars")
    monthly = caps.get("monthly_dollars")
    if per is None and "per_order_cents" in caps:
        per = (caps.get("per_order_cents") or 0) / 100.0
    if monthly is None and "monthly_cents" in caps:
        monthly = (caps.get("monthly_cents") or 0) / 100.0
    return f"per-order ${float(per or 0):,.0f} / monthly ${float(monthly or 0):,.0f}"


class _SpendCapToolsPlugin:
    """Read, raise and one-time-approve spend caps as five MCP tools."""

    @property
    def name(self) -> str:
        return "spend_cap_tools"

    @property
    def description(self) -> str:
        return (
            "Spend caps on fulfillment orders from inside an agent chat: "
            "read your caps, raise either ceiling, or approve one order "
            "over the ceiling, each confirmed with a six-digit code from "
            "your own authenticator app."
        )

    def register(self, mcp: Any) -> None:
        @mcp.tool()
        def kiln_spend_caps_show() -> dict[str, Any]:
            """Show your current Kiln spend caps and whether chat may change them.

            Returns the per-order and monthly caps, the defaults, and
            ``agent_flow_enabled``.  When that is off the answer carries a
            ``next_step`` with the web page where the account owner turns it
            on; give the person that link as written, because turning it on
            cannot be done from chat.

            Needs a signed-in session: call ``kiln_signin`` first.
            """
            resp, refusal = _ask("kiln_spend_caps_show", "GET", "/api/billing/spend-caps")
            if refusal:
                return refusal
            if not resp.get("success"):
                return _err(
                    resp.get("error") or "Could not load spend caps.",
                    code=str(resp.get("error") or "UNKNOWN").upper(),
                )
            caps = resp.get("caps") or {}
            enabled = bool(resp.get("agent_flow_enabled"))
            payload: dict[str, Any] = {
                "caps": caps,
                "defaults": resp.get("defaults") or {},
                "agent_flow_enabled": enabled,
                "summary": _caps_summary(caps),
            }
            if not enabled:
                payload["next_step"] = {
                    "action": "enable_agent_flow",
                    "enable_url": _ENABLE_URL,
                    "message": (
                        "Changing caps from chat is turned off for this "
                        "account.  Open the link and turn on 'Allow CLI / "
                        "MCP cap changes' (it asks for your second factor); "
                        "the other kiln_spend_caps_* tools work after that."
                    ),
                }
            return _ok(payload)

        @mcp.tool()
        def kiln_spend_caps_request_change(
            per_order_dollars: float | None = None,
            monthly_dollars: float | None = None,
            reason: str | None = None,
        ) -> dict[str, Any]:
            """Ask to raise the per-order and/or monthly spend cap (step 1 of 2).

            The servers hold the requested change for a few minutes and
            return a ``challenge_id``.  Show the person ``current_caps`` and
            ``requested_caps`` so they can review the change, then ask them
            for the six-digit code from their authenticator app and pass it
            to ``kiln_spend_caps_confirm_change``.

            Args:
                per_order_dollars: New per-order cap; leave unset to keep it.
                monthly_dollars: New monthly cap; leave unset to keep it.
                reason: Optional note kept with the change.

            A refusal says which case it is (chat changes turned off, with
            ``enable_url``; too many attempts, with ``retry_after_seconds``;
            a cap below the minimum).  Relay it as written.
            """
            import kiln.server as _srv

            if denied := _srv._check_auth("write"):
                return denied
            if per_order_dollars is None and monthly_dollars is None:
                return _err(
                    "Specify at least one of per_order_dollars or monthly_dollars.",
                    code="INVALID_INPUT",
                )
            body: dict[str, Any] = {"surface": "mcp"}
            if per_order_dollars is not None:
                body["per_order_dollars"] = float(per_order_dollars)
            if monthly_dollars is not None:
                body["monthly_dollars"] = float(monthly_dollars)
            if reason:
                body["reason"] = str(reason)[:200]
            resp, refusal = _ask(
                "kiln_spend_caps_request_change",
                "POST",
                "/api/billing/spend-caps/request-change",
                body,
            )
            if refusal:
                return refusal
            if not resp.get("success"):
                return _refusal(resp, default="Could not request a cap change.")
            return _ok({
                "challenge_id": resp.get("challenge_id"),
                "current_caps": resp.get("current_caps"),
                "requested_caps": resp.get("requested_caps"),
                "reason": resp.get("reason"),
                "expires_at": resp.get("expires_at"),
                "instructions": resp.get("instructions") or (
                    "Show the person the current and requested caps.  Then "
                    "ask for the six-digit code from their authenticator app "
                    "and call kiln_spend_caps_confirm_change(challenge_id=…, "
                    "totp_code=…)."
                ),
            })

        @mcp.tool()
        def kiln_spend_caps_confirm_change(
            challenge_id: str,
            totp_code: str,
        ) -> dict[str, Any]:
            """Confirm a cap change with the person's six-digit code (step 2 of 2).

            Args:
                challenge_id: From ``kiln_spend_caps_request_change``.
                totp_code: The six-digit code the PERSON reads from their own
                    authenticator app.  Never produce or guess it.

            Returns the new caps on success.  A refusal names its case: a
            wrong code (they may try again within the daily limit), a
            challenge that expired or was already used (ask again from step
            1), no authenticator set up on the account, or too many attempts.
            """
            import kiln.server as _srv

            if denied := _srv._check_auth("write"):
                return denied
            challenge_id = (challenge_id or "").strip()
            totp_code = (totp_code or "").strip()
            if not challenge_id or not totp_code:
                return _err(
                    "challenge_id and totp_code are both required.",
                    code="INVALID_INPUT",
                )
            resp, refusal = _ask(
                "kiln_spend_caps_confirm_change",
                "POST",
                "/api/billing/spend-caps/confirm-change",
                {"challenge_id": challenge_id, "totp_code": totp_code, "surface": "mcp"},
            )
            if refusal:
                return refusal
            if not resp.get("success"):
                return _refusal(resp, default="Cap change denied.")
            return _ok({
                "status": "success",
                "new_caps": resp.get("new_caps"),
                "change_log_id": resp.get("change_log_id"),
                "summary": "Caps updated: " + _caps_summary(resp.get("new_caps") or {}),
            })

        @mcp.tool()
        def kiln_spend_caps_request_order_approval(
            order_id: str,
            max_dollars: float,
        ) -> dict[str, Any]:
            """Ask to approve ONE order over the cap (step 1 of 2).

            For an order Kiln refused as over the person's spend cap, when
            they want this order to go ahead without raising the cap for
            good.  Returns a ``challenge_id``; step 2 is
            ``kiln_spend_caps_confirm_order_approval`` with their code.

            Args:
                order_id: The order Kiln refused.
                max_dollars: The order total being approved, in dollars.
            """
            import kiln.server as _srv

            if denied := _srv._check_auth("write"):
                return denied
            order_id = (order_id or "").strip()
            try:
                amount = float(max_dollars)
            except (TypeError, ValueError):
                return _err("max_dollars must be a number.", code="INVALID_INPUT")
            if not order_id or amount <= 0:
                return _err(
                    "order_id and max_dollars > 0 are required.",
                    code="INVALID_INPUT",
                )
            resp, refusal = _ask(
                "kiln_spend_caps_request_order_approval",
                "POST",
                "/api/billing/spend-caps/request-order-approval",
                {"order_id": order_id, "max_dollars": amount, "surface": "mcp"},
            )
            if refusal:
                return refusal
            if not resp.get("success"):
                return _refusal(resp, default="Could not request an order approval.")
            return _ok({
                "challenge_id": resp.get("challenge_id"),
                "order_summary": resp.get("order_summary"),
                "expires_at": resp.get("expires_at"),
                "instructions": resp.get("instructions") or (
                    "Show the person the order summary.  Then ask for the "
                    "six-digit code from their authenticator app and call "
                    "kiln_spend_caps_confirm_order_approval(challenge_id=…, "
                    "totp_code=…)."
                ),
            })

        @mcp.tool()
        def kiln_spend_caps_confirm_order_approval(
            challenge_id: str,
            totp_code: str,
        ) -> dict[str, Any]:
            """Confirm a one-order approval with the person's code (step 2 of 2).

            On success returns an ``approval_token``: pass it to the order
            tool when placing that order again.  It works once, for that
            order and amount, and expires within minutes.

            Args:
                challenge_id: From ``kiln_spend_caps_request_order_approval``.
                totp_code: The six-digit code the PERSON reads from their own
                    authenticator app.  Never produce or guess it.
            """
            import kiln.server as _srv

            if denied := _srv._check_auth("write"):
                return denied
            challenge_id = (challenge_id or "").strip()
            totp_code = (totp_code or "").strip()
            if not challenge_id or not totp_code:
                return _err(
                    "challenge_id and totp_code are both required.",
                    code="INVALID_INPUT",
                )
            resp, refusal = _ask(
                "kiln_spend_caps_confirm_order_approval",
                "POST",
                "/api/billing/spend-caps/confirm-order-approval",
                {"challenge_id": challenge_id, "totp_code": totp_code, "surface": "mcp"},
            )
            if refusal:
                return refusal
            if not resp.get("success"):
                return _refusal(resp, default="Order approval denied.")
            return _ok({
                "status": "success",
                "approval_token": resp.get("approval_token"),
                "expires_at": resp.get("expires_at"),
                "order_id": resp.get("order_id"),
                "max_dollars": resp.get("max_dollars"),
                "next_step": (
                    "Place the order again with "
                    f"approval_token='{resp.get('approval_token') or ''}'. "
                    "It covers this order only."
                ),
            })


plugin = _SpendCapToolsPlugin()


def register(mcp: Any) -> None:
    plugin.register(mcp)
