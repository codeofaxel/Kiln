"""Terms of use acceptance tracking.

Stores acceptance state in the SQLite settings table — the local floor, and the
only gate for a no-account install.  When the install has a hosted-API bearer
(a license key or a paired sign-in), acceptance is also mirrored to the account
so it is honored across the user's devices and surfaces (web, MCP, CLI).  The
current terms version is bumped whenever TERMS_OF_USE.md changes materially; a
version mismatch triggers re-acceptance.
"""

from __future__ import annotations

import os
import time

_CURRENT_TERMS_VERSION = "3.0"

_SETTINGS_KEY_VERSION = "terms_accepted_version"
_SETTINGS_KEY_TIMESTAMP = "terms_accepted_at"

_TERMS_SUMMARY = """\
  By using Kiln, you're agreeing to a few things:

  1. Safety stays with you. Kiln's checks lower the risk of a print
     going wrong — they don't remove it. Supervise what an AI agent
     runs on your printer, and don't run prints unattended without
     smoke/fire precautions.
  2. What you make is yours — and your responsibility. You own your
     designs and outputs, and you're responsible for following the
     laws that apply to you. Kiln itself doesn't monitor or restrict
     your files, though the AI assistant you use may decline a
     request under its own policies.
  3. Free and Pro are for personal projects. Selling what you print —
     or fulfilling client and custom orders — is a Business-tier
     feature.
  4. Fees are shown up front. Fulfillment orders carry a 5%
     orchestration fee (min $0.25, max $200); your first 3 each month
     are free. Printing on your own printer is always free.
  5. Third parties set their own rules. Marketplaces and fulfillment
     partners are governed by their terms, not Kiln's.
  6. Kiln is provided "as is", without warranty.

  Please read the full Terms before you accept: https://kiln3d.com/terms
  Privacy policy: https://kiln3d.com/privacy"""


# Forcing function: this marker MUST equal _CURRENT_TERMS_VERSION (enforced by
# test_summary_reviewed_for_current_version).  When you bump the terms version,
# that test stays red until you have re-read _TERMS_SUMMARY above AND the
# matching acceptance copy on the other surfaces -- the web sign-up and the MCP
# first-run gate -- updated whatever materially changed, then set this to match.
# It makes "did we refresh every place the user accepts the terms?" a conscious
# step on every change instead of something we remember to do by luck.
_SUMMARY_REVIEWED_FOR_VERSION = "3.0"


# --- Account-scoped acceptance (honored across the user's devices) ----------
#
# The local record above is the floor: authoritative, fast, offline-safe, and
# the ONLY gate for a no-account install.  When this install has a hosted-API
# bearer (a license key or a paired sign-in), acceptance is ALSO mirrored to the
# account so it is honored on the user's other devices and surfaces.  All of it
# is best-effort — a network failure never blocks accepting or using Kiln; the
# local record carries the user through.

_SETTINGS_KEY_SERVER_CHECK = "terms_server_checked_at"

# Don't re-poll the account for a cross-device acceptance more than once per this
# interval — the local record covers the common case, so this only paces the
# "did I accept on another machine?" lookup for a not-yet-accepted install.
_SERVER_RECHECK_TTL_S = 300.0

_REQUEST_TIMEOUT_S = 5.0


def _hosted_api_base() -> str:
    """The hosted API base — ``KILN_API_URL`` override else the default.

    Mirrors ``server._pro_api_call`` (lazily imported so this low-level module
    never drags in the heavy server module at import time).
    """
    override = (os.environ.get("KILN_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    try:
        from kiln.server import _HOSTED_KILN_API_URL

        return _HOSTED_KILN_API_URL.rstrip("/")
    except Exception:
        return "https://api.kiln3d.com"


def _account_bearer() -> str:
    """The install's hosted-API bearer: license key or paired sign-in token.

    Same resolution order as ``server._pro_api_call``.  Empty string when this
    is a no-account install — the local record is then the only gate, by design.
    """
    bearer = (os.environ.get("KILN_LICENSE_KEY") or "").strip()
    if bearer:
        return bearer
    try:
        from kiln.server import _paired_access_token

        return _paired_access_token() or ""
    except Exception:
        return ""


def _is_safe_base(base: str) -> bool:
    """Only put the bearer on the wire over https (localhost exempt for dev)."""
    return (
        base.startswith("https://")
        or base.startswith("http://127.0.0.1")
        or base.startswith("http://localhost")
    )


def _server_request(path: str, method: str, bearer: str, payload: dict | None = None):
    """Best-effort call to a hosted terms endpoint; ``None`` on any failure.

    Stdlib-only (urllib) so terms.py — imported early in CLI startup — never
    forces an httpx dependency on the free tier.
    """
    base = _hosted_api_base()
    if not _is_safe_base(base):
        return None
    import json
    import urllib.error  # noqa: F401  (urlopen raises urllib.error subclasses)
    import urllib.request

    try:
        headers = {"Authorization": f"Bearer {bearer}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base}{path}", data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except Exception:
        # Offline / unreachable / rejected — the local record is the fallback.
        return None


def get_accepted_version(*, db=None) -> str | None:
    """Return the accepted terms version, or ``None`` if never accepted."""
    if db is None:
        from kiln.persistence import get_db

        db = get_db()
    return db.get_setting(_SETTINGS_KEY_VERSION)


def is_current(*, db=None) -> bool:
    """Return ``True`` if the user has accepted the current terms version.

    Account-aware: the local record is authoritative (fast, offline-safe).  When
    it is stale AND this install has an account bearer, the server is consulted
    at most once per :data:`_SERVER_RECHECK_TTL_S` to import an acceptance made
    on another device, backfilling the local record on success.  No bearer ->
    local-only (the no-account floor).
    """
    if db is None:
        from kiln.persistence import get_db

        db = get_db()

    if get_accepted_version(db=db) == _CURRENT_TERMS_VERSION:
        return True

    bearer = _account_bearer()
    if not bearer:
        return False

    # Throttle the cross-device lookup so a not-yet-accepted install doesn't poll
    # on every gated call.
    # Throttle bookkeeping is best-effort: a settings-DB hiccup (e.g. a transient
    # write-lock) must NOT abort the acceptance check — that would fail the gate
    # OPEN over benign infra — so both the read and the write are guarded.
    try:
        last = float(db.get_setting(_SETTINGS_KEY_SERVER_CHECK) or 0)
    except Exception:
        last = 0.0
    now = time.time()
    # Throttle only when the last check is in the recent PAST.  A future-dated
    # stamp (system clock rolled back — NTP correction, VM snapshot, manual set)
    # would make (now - last) negative and otherwise lock out the cross-device
    # import until real time caught back up; the lower bound prevents that.
    if 0 <= now - last < _SERVER_RECHECK_TTL_S:
        return False
    try:
        db.set_setting(_SETTINGS_KEY_SERVER_CHECK, str(now))
    except Exception:
        pass  # couldn't stamp the throttle — fine, we just re-poll next time

    resp = _server_request("/api/terms/acceptance", "GET", bearer)
    if (
        isinstance(resp, dict)
        and resp.get("accepted")
        and resp.get("version") == _CURRENT_TERMS_VERSION
    ):
        # Accepted on another device — backfill local so future checks are fast
        # and offline-safe (the server already has it, so don't re-POST).
        db.set_setting(_SETTINGS_KEY_VERSION, _CURRENT_TERMS_VERSION)
        db.set_setting(_SETTINGS_KEY_TIMESTAMP, str(now))
        return True
    return False


def record_acceptance(*, db=None, method: str = "setup", verbatim_text: str | None = None) -> None:
    """Record acceptance of the current terms version.

    Always writes the local record (the floor for every install).  When an
    account bearer is present, ALSO best-effort mirrors the acceptance to the
    account so it is honored on the user's other devices and surfaces.  ``method``
    names the surface (``setup`` / ``cli`` / ``cli_noninteractive`` / ``env`` /
    ``mcp_in_chat`` / ``web_checkbox``); ``verbatim_text`` carries the exact
    phrase a user typed for the in-chat MCP path (the consent proof there).
    """
    if db is None:
        from kiln.persistence import get_db

        db = get_db()
    db.set_setting(_SETTINGS_KEY_VERSION, _CURRENT_TERMS_VERSION)
    db.set_setting(_SETTINGS_KEY_TIMESTAMP, str(time.time()))

    bearer = _account_bearer()
    if bearer:
        _server_request(
            "/api/terms/accept",
            "POST",
            bearer,
            {"method": method, "verbatim_text": verbatim_text},
        )


def prompt_acceptance(method: str = "setup") -> bool:
    """Display the terms summary and prompt for acceptance.

    Returns ``True`` if the user accepted, ``False`` otherwise.  ``method`` names
    the surface that prompted (``setup`` / ``cli`` / ...) and is recorded with
    the acceptance.  Uses click for consistent CLI prompting.
    """
    import click

    click.echo()
    click.echo(click.style("  Terms of Use", bold=True))
    click.echo(click.style("  ------------", bold=True))
    click.echo(_TERMS_SUMMARY)
    click.echo()
    accepted = click.confirm("  Do you accept these terms?", default=True)
    if accepted:
        record_acceptance(method=method)
        click.echo(click.style("  Terms accepted.", fg="green"))
    click.echo()
    return accepted
