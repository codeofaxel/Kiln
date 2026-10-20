"""Community print data sync — local registry ↔ Supabase.

Anonymous sharing of print outcomes to build collective intelligence.
**Enabled by default.**  Users who want to opt out set
``KILN_COMMUNITY_OPT_IN=false`` (or ``0`` / ``no`` / ``off``) in their
environment; any other value (or unset) keeps sharing on.

Only geometric signatures, printer model, material, settings hash,
outcome, and failure mode are shared.  No file paths, no user IDs,
no PII.

Two directions, and they do NOT use the same door:

* **Contribute** — :func:`sync_community_print` /
  :func:`sync_community_print_async` hand one anonymized outcome to the
  Kiln API (:func:`send_contribution`), anonymously: no sign-in, no
  credential, nothing that names this machine.  The server checks it and
  writes it to the pool, so when the pool refuses a write the refusal
  reaches Kiln rather than a log on this disk.  Every install does this,
  on every tier; nothing here is gated.
* **Read back** — :func:`fetch_community_insights` and
  :func:`fetch_community_insight_for_signature` ask the Kiln API for a
  *computed aggregate*, authenticated as this machine.  Nobody reads
  the raw table: the server holds the credentials, reduces the matching
  rows to counts and rates, and returns only that.

Every read degrades to ``None`` — signed out, offline, or a plan that
doesn't include community insights all land in the same place, and every
caller already treats ``None`` as "use local knowledge alone".  Community
data has always been a bonus layer on top of Kiln's own answer, never a
prerequisite for one.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Kiln releases before this one wrote contributions straight into the pool
# with the database's public key.  Those installs still do, and the database
# keeps accepting their exact request (kiln-pro's client-write contracts pin
# it); this release and later go through ``_CONTRIBUTE_ROUTE`` instead.

# Read side — the Kiln API computes the aggregate; we never see rows.
_HOSTED_API_URL = "https://api.kiln3d.com"
_INSIGHT_ROUTE = "/api/community/insight"
_STATS_ROUTE = "/api/community/stats"
# Write side — one anonymous contribution, checked and stored by the server.
_CONTRIBUTE_ROUTE = "/api/community/contribute"
_CONTRIBUTE_TIMEOUT = 10.0

# Pull-side cache: community aggregates rarely change within an hour,
# and network hops on every generation would be a silent performance
# tax.  Cache to disk with a short TTL; skip the network on cache hit.
# Resolved at call time so ``HOME`` overrides (tests, sandboxes) work.
_COMMUNITY_CACHE_TTL_SECONDS = 3600  # 1 hour
_COMMUNITY_FETCH_TIMEOUT = 8.0


def _community_cache_dir() -> Path:
    return Path.home() / ".kiln" / "community_cache"


def community_sharing_enabled() -> bool:
    """Check if this install participates in the anonymous community
    learning loop.

    Default is ON — the data shared is geometric hashes + printer model
    + material + outcome + failure_mode, with no file paths, no user
    identifiers, and no PII.  The network effect of participating
    (better cross-installation intelligence for everyone) outweighs the
    opt-in friction.

    Users who want to disable sharing can set
    ``KILN_COMMUNITY_OPT_IN=false`` (or ``0``, ``no``, ``off``) in their
    environment.
    """
    val = os.environ.get("KILN_COMMUNITY_OPT_IN", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


# Backward-compat alias.  The original name suggested "opt-in" semantics
# but the behavior was always opt-out by default; ``community_sharing_enabled``
# is the honest name.  Kept so existing callers don't break.
community_opt_in_enabled = community_sharing_enabled


def network_sends_suppressed() -> bool:
    """True when this process must not POST to the shared community corpus.

    The write-side guards each protected a STORE: ``daily_stats``
    protects the real stats file, ``community_outbox`` protects the real
    per-user outbox DB — and both stand down when a test points them at
    its own path, because a custom path means "I want to exercise this
    store."  The hole (found 2026-08-05, seven fixture rows live in the
    production corpus): isolating the store is exactly what DISARMS the
    outbox guard, and the sender under it had no guard of its own — so a
    suite with a relocated HOME could exercise the outbox "safely" and
    still publish ``sig123`` to every real user.  Store isolation and
    network safety are different questions; this answers the second one,
    independent of any path.

    A test that really wants the send path (with a mocked or sandboxed
    network) says so explicitly: ``KILN_COMMUNITY_TEST_SEND=1``.  An
    accidental send can never opt itself in.
    """
    if os.environ.get("KILN_COMMUNITY_TEST_SEND") == "1":
        return False
    import sys as _sys

    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in _sys.modules:
        return True
    try:
        from kiln.heartbeat import _is_ci_environment

        return _is_ci_environment()
    except Exception:  # noqa: BLE001 — heartbeat absent, fall back to env
        return any(os.environ.get(v) for v in ("CI", "PYTEST_CURRENT_TEST"))


#: What the server's answer means for a queued contribution.  A refusal
#: (400/422) is about THIS row and spends one of its retries.  Everything
#: else that is not a delivery is about the door, not the row: too many sends
#: from here (429), the pool unavailable or our server not answering (5xx), a
#: server that predates the route (404), or this machine being offline.  Those
#: come back as :data:`kiln.community_outbox.DEFERRED`, and the queue keeps
#: the row without spending a retry -- spending them on a shut door is how
#: twelve weeks of contributions were deleted unsent in 2026.
_REFUSED_STATUSES = frozenset({400, 409, 413, 422})


def send_contribution(kind: str, record: dict[str, Any], send_id: str | None = None):
    """Hand one anonymous contribution to the Kiln API.

    :param kind: ``"print"`` or ``"recovery"`` -- which pool it joins.
    :param record: the anonymized fields, exactly as the pool stores them.
    :param send_id: random per-contribution token; a replay with the same
        token is stored once.  It names the contribution, never the sender.
    :returns: ``True`` when the server took it, ``False`` when it refused
        this record, or :data:`kiln.community_outbox.DEFERRED` (falsy) when
        it should simply be sent again later.  Never raises.

    No credential is attached, on purpose: a contribution is anonymous, and
    the server never looks at who sent it.
    """
    from kiln.community_outbox import DEFERRED

    if not community_sharing_enabled():
        return False
    if network_sends_suppressed():
        _logger.debug("Community contribution refused — test/CI runner (wire guard)")
        return False

    import urllib.error
    import urllib.request

    envelope: dict[str, Any] = {"kind": kind, "record": record}
    if send_id:
        envelope["send_id"] = send_id
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        from kiln.version_check import _current_version

        headers["X-Kiln-Client-Version"] = _current_version()
    except Exception:
        pass
    req = urllib.request.Request(
        f"{_api_base()}{_CONTRIBUTE_ROUTE}",
        data=json.dumps(envelope).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_CONTRIBUTE_TIMEOUT) as resp:
            if resp.status < 300:
                return True
            return DEFERRED
    except urllib.error.HTTPError as exc:
        if exc.code in _REFUSED_STATUSES:
            _logger.debug("Community contribution refused (%s)", exc.code)
            return False
        _logger.debug("Community contribution deferred (%s)", exc.code)
        return DEFERRED
    except Exception as exc:  # noqa: BLE001 — offline, DNS, timeout
        _logger.debug("Community contribution deferred (non-fatal): %s", exc)
        return DEFERRED


def sync_community_print(record: dict[str, Any], send_id: str | None = None):
    """Send a single community print record to the shared pool.

    :param record: Dict with keys matching the ``community_prints`` table
        (geometric_signature, printer_model, material, settings_hash,
        settings, outcome, quality_grade, failure_mode, print_time_seconds).
    :param send_id: Optional random, per-contribution idempotency token (set
        by the durable outbox), so a crash-replayed row is stored once.  It
        is random per contribution (not a user/install identifier), so it
        carries no cross-row linkability.
    :returns: ``True`` if the server took it; otherwise falsy -- ``False``
        for a refusal, :data:`kiln.community_outbox.DEFERRED` for "send it
        again later" (see :func:`send_contribution`).
    """
    if not community_opt_in_enabled():
        _logger.debug("Community sync skipped — opt-in not enabled")
        return False
    if network_sends_suppressed():
        _logger.debug("Community sync refused — test/CI runner (wire guard)")
        return False

    body: dict[str, Any] = {
        "geometric_signature": record.get("geometric_signature", ""),
        "printer_model": record.get("printer_model", ""),
        "material": record.get("material", ""),
        "settings_hash": record.get("settings_hash", ""),
        "settings": record.get("settings"),
        "outcome": record.get("outcome", ""),
        "quality_grade": record.get("quality_grade"),
        "failure_mode": record.get("failure_mode"),
        "print_time_seconds": record.get("print_time_seconds"),
    }
    # Shipped only when present: the v2 signature and the raw firmware code
    # the print tripped.  The second is evidence, not a verdict --
    # ``failure_mode`` says what Kiln concluded, ``print_error`` what the
    # machine actually reported, and only that one can be compared across
    # models and firmware by anybody counting them later.
    if record.get("geometric_signature_v2"):
        body["geometric_signature_v2"] = record["geometric_signature_v2"]
    if record.get("print_error"):
        body["print_error"] = record["print_error"]
    return send_contribution("print", body, send_id)


def sync_community_print_async(record: dict[str, Any]) -> None:
    """Fire community sync in a daemon thread — never blocks."""
    if not community_opt_in_enabled() or network_sends_suppressed():
        return
    t = threading.Thread(
        target=sync_community_print,
        args=(record,),
        daemon=True,
        name="kiln-community-sync",
    )
    t.start()


# ---------------------------------------------------------------------------
# Pull side — the Kiln API answers one scoped question with a computed
# aggregate.  No raw rows, no table-level access, no key in this process
# that could read the corpus directly.
# ---------------------------------------------------------------------------


def _api_base() -> str:
    return (os.environ.get("KILN_API_URL") or _HOSTED_API_URL).rstrip("/")


def _ask_community_api(route: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST one scoped question to the Kiln API; return the aggregate.

    ``None`` covers every way this can come back empty — no sign-in, no
    network, a plan without community insights, or a server that had
    nothing to say.  They are the same outcome for a caller: use what you
    know locally.  Nothing here raises, and nothing here blocks a print.
    """
    try:
        from kiln.auth_session import resolve_api_bearer

        bearer = resolve_api_bearer().token
    except Exception:
        bearer = ""
    if not bearer:
        _logger.debug("Community read skipped — no Kiln session on this machine")
        return None

    try:
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        }
        try:
            from kiln.version_check import _current_version

            headers["X-Kiln-Client-Version"] = _current_version()
        except Exception:
            pass

        req = urllib.request.Request(
            f"{_api_base()}{route}",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=_COMMUNITY_FETCH_TIMEOUT
        ) as resp:
            if resp.status >= 300:
                return None
            body = json.loads(resp.read().decode())
    except Exception as exc:
        # A 403 (plan without community insights) arrives here as an
        # HTTPError, alongside every offline case — all of them mean the
        # same thing to a caller, so none of them are worth a warning.
        _logger.debug("Community read unavailable (non-fatal): %s", exc)
        return None

    if not isinstance(body, dict) or not body.get("has_data"):
        return None
    return body


def _cache_path(printer_model: str, material: str) -> Path:
    # Sanitize — only alphanumerics and dashes in filenames.
    def _safe(s: str) -> str:
        return "".join(c if c.isalnum() or c == "-" else "_" for c in s.lower())

    return _community_cache_dir() / f"{_safe(printer_model)}__{_safe(material)}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        age = time.time() - path.stat().st_mtime
        if age > _COMMUNITY_CACHE_TTL_SECONDS:
            return None
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:
        _logger.debug("Community cache write failed: %s", exc)


def fetch_community_insights(
    printer_model: str,
    material: str,
    *,
    use_cache: bool = True,
) -> dict[str, Any] | None:
    """Fetch aggregate community failure statistics for a printer+material.

    Asks the Kiln API for a computed summary of community prints matching
    this printer model and material.  Disk-cached for
    ``_COMMUNITY_CACHE_TTL_SECONDS`` so repeated generation calls within
    the same hour do not re-hit the network.

    Returns ``None`` when sharing is off, this machine has no Kiln
    session, the network fails, the plan does not include community
    insights, or nobody has printed anything like this yet.  Callers
    should always tolerate ``None`` and fall back to local/static
    sources — they already do.

    Community insights come with Kiln Pro (https://kiln3d.com/pricing).

    :param printer_model: e.g. ``"bambu_x1c"``, ``"prusa_mk4"``.
    :param material: e.g. ``"PLA"``, ``"PETG"``.
    :param use_cache: When False, bypasses the disk cache (useful for
        tests or forced refresh).
    :returns: Dict with keys:
        - ``failure_breakdown``: ``{failure_mode: count}`` ordered by count desc
        - ``sample_size``: total matching community prints
        - ``success_count``: rows where ``outcome == "success"``
        - ``source``: always ``"community"``
        - ``fetched_at``: unix timestamp of fetch
    """
    if not community_opt_in_enabled():
        return None
    if not printer_model or not material:
        return None

    # One noun per machine, applied BEFORE both the query and the cache
    # key.  Previously the cache key lowercased/sanitized while the query
    # went out raw — so "Bambu A1" and "bambu a1" shared one cache file
    # while querying two different cohorts, and whichever fired first
    # answered for the other (1-hour TTL).  The door normalizes writes
    # with this same function; the read must speak the same language.
    from kiln.community_outbox import canonical_printer_model

    printer_model = canonical_printer_model(printer_model)

    cache_path = _cache_path(printer_model, material)
    if use_cache:
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

    body = _ask_community_api(
        _INSIGHT_ROUTE,
        {"printer_model": printer_model, "material": material},
    )
    if body is None:
        return None

    breakdown = body.get("failure_breakdown")
    result = {
        "failure_breakdown": breakdown if isinstance(breakdown, dict) else {},
        "sample_size": int(body.get("sample_size") or 0),
        "success_count": int(body.get("success_count") or 0),
        "source": "community",
        "fetched_at": time.time(),
    }
    _write_cache(cache_path, result)
    return result


def fetch_community_insight_for_signature(
    geometric_signature: str,
    geometric_signature_v2: str = "",
) -> dict[str, Any] | None:
    """Community aggregate for ONE model geometry, or ``None``.

    The returned dict matches what
    :func:`kiln.community_registry.get_community_insight` produces from
    local history — same fields, same meaning — so a caller can present
    either without special-casing where it came from.

    ``None`` covers every empty outcome: sharing off, no Kiln session,
    offline, a plan without community insights, or nobody has printed
    this shape yet.  Community insights come with Kiln Pro
    (https://kiln3d.com/pricing).
    """
    if not community_opt_in_enabled():
        return None
    signature = (geometric_signature or "").strip()
    if not signature:
        return None

    request_body: dict[str, Any] = {"geometric_signature": signature}
    # The v2 signature lets an upgraded server match precisely (v2 rows on
    # v2, pre-v2 rows through v1).  A server that predates it reads only the
    # keys it knows, so sending it early is harmless.
    signature_v2 = (geometric_signature_v2 or "").strip()
    if signature_v2:
        request_body["geometric_signature_v2"] = signature_v2

    body = _ask_community_api(_INSIGHT_ROUTE, request_body)
    if body is None:
        return None

    insight = body.get("insight")
    if not isinstance(insight, dict):
        return None
    groups = body.get("top_settings_groups")
    if isinstance(groups, list) and groups:
        insight = {**insight, "top_settings_groups": groups}
    return insight


def fetch_community_corpus_stats() -> dict[str, Any] | None:
    """Totals for the whole community pool, or ``None``.

    Counts only — how many prints the pool holds and how often they
    worked.  Available to anyone signed in, on any plan: knowing the pool
    is real shouldn't cost anything.
    """
    body = _ask_community_api(_STATS_ROUTE, {})
    if body is None:
        return None
    stats = body.get("stats")
    return stats if isinstance(stats, dict) else None
