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
  :func:`sync_community_print_async` post one anonymized outcome
  straight to the community table with the publishable key.  Every
  install does this, on every tier; nothing here is gated.
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

_SUPABASE_URL = "https://nomzokpscfshjjzezplr.supabase.co"
# CONTRIBUTION ONLY — the publishable key inserts an anonymized outcome and
# nothing else.  It is deliberately absent from every read path in this
# module: reads go to the Kiln API (see ``_INSIGHT_ROUTE``), which holds the
# credentials the corpus actually answers to.  Pinned by
# ``tests/test_community_sync.py::test_publishable_key_is_contribution_only``.
_SUPABASE_ANON_KEY = "sb_publishable_ZCJyEL0qeveSwgqv7dry3A_YI26Yw6S"  # PLACEHOLDER: RLS-gated publishable key, insert-only

# Read side — the Kiln API computes the aggregate; we never see rows.
_HOSTED_API_URL = "https://api.kiln3d.com"
_INSIGHT_ROUTE = "/api/community/insight"
_STATS_ROUTE = "/api/community/stats"

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


def sync_community_print(record: dict[str, Any], send_id: str | None = None) -> bool:
    """Send a single community print record to Supabase.

    :param record: Dict with keys matching the ``community_prints`` table
        (geometric_signature, printer_model, material, settings_hash,
        settings, outcome, quality_grade, failure_mode, print_time_seconds).
    :param send_id: Optional random, per-contribution idempotency token (set
        by the durable outbox).  When present, the insert upserts on the
        ``send_id`` unique index with ``resolution=ignore-duplicates`` so a
        crash-replayed row is a server-side no-op instead of a duplicate row.
        It is random per contribution (not a user/install identifier), so it
        carries no cross-row linkability.  When absent (or before the
        federation column ships), the call is a plain insert.
    :returns: True if successfully sent, False otherwise.
    """
    if not community_opt_in_enabled():
        _logger.debug("Community sync skipped — opt-in not enabled")
        return False

    try:
        import urllib.request

        insert_url = f"{_SUPABASE_URL}/rest/v1/community_prints"
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
        prefer = "return=minimal"
        if send_id:
            body["send_id"] = send_id
            insert_url = f"{insert_url}?on_conflict=send_id"
            prefer = "return=minimal,resolution=ignore-duplicates"
        payload = json.dumps(body).encode()

        req = urllib.request.Request(
            insert_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "apikey": _SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {_SUPABASE_ANON_KEY}",
                "Prefer": prefer,
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status < 300:
                _logger.debug("Community print synced")
                return True
            _logger.debug("Community sync response: %s", resp.status)
            return False

    except Exception as exc:
        _logger.debug("Community sync failed (non-fatal): %s", exc)
        return False


def sync_community_print_async(record: dict[str, Any]) -> None:
    """Fire community sync in a daemon thread — never blocks."""
    if not community_opt_in_enabled():
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
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
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

    body = _ask_community_api(
        _INSIGHT_ROUTE, {"geometric_signature": signature}
    )
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
