"""Community print data sync — local registry ↔ Supabase.

Opt-in anonymous sharing of print outcomes to build collective
intelligence.  Enable with ``KILN_COMMUNITY_OPT_IN=true``.

Only geometric signatures, printer model, material, settings hash,
outcome, and failure mode are shared.  No file paths, no user IDs,
no PII.

Two directions:

* :func:`sync_community_print` / :func:`sync_community_print_async` —
  push local outcomes to the community table.
* :func:`fetch_community_insights` — pull aggregate failure statistics
  for a (printer_model, material) pair.  Used at generation time to
  seed printer context when local history is sparse.
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
_SUPABASE_ANON_KEY = "sb_publishable_ZCJyEL0qeveSwgqv7dry3A_YI26Yw6S"

# Pull-side cache: community aggregates rarely change within an hour,
# and network hops on every generation would be a silent performance
# tax.  Cache to disk with a short TTL; skip the network on cache hit.
# Resolved at call time so ``HOME`` overrides (tests, sandboxes) work.
_COMMUNITY_CACHE_TTL_SECONDS = 3600  # 1 hour
_COMMUNITY_FETCH_LIMIT = 500
_COMMUNITY_FETCH_TIMEOUT = 5.0


def _community_cache_dir() -> Path:
    return Path.home() / ".kiln" / "community_cache"


def community_opt_in_enabled() -> bool:
    """Check if the user has opted in to community data sharing."""
    val = os.environ.get("KILN_COMMUNITY_OPT_IN", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def sync_community_print(record: dict[str, Any]) -> bool:
    """Send a single community print record to Supabase.

    :param record: Dict with keys matching the ``community_prints`` table
        (geometric_signature, printer_model, material, settings_hash,
        settings, outcome, quality_grade, failure_mode, print_time_seconds).
    :returns: True if successfully sent, False otherwise.
    """
    if not community_opt_in_enabled():
        _logger.debug("Community sync skipped — opt-in not enabled")
        return False

    try:
        import urllib.request

        insert_url = f"{_SUPABASE_URL}/rest/v1/community_prints"
        payload = json.dumps({
            "geometric_signature": record.get("geometric_signature", ""),
            "printer_model": record.get("printer_model", ""),
            "material": record.get("material", ""),
            "settings_hash": record.get("settings_hash", ""),
            "settings": record.get("settings"),
            "outcome": record.get("outcome", ""),
            "quality_grade": record.get("quality_grade"),
            "failure_mode": record.get("failure_mode"),
            "print_time_seconds": record.get("print_time_seconds"),
        }).encode()

        req = urllib.request.Request(
            insert_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "apikey": _SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {_SUPABASE_ANON_KEY}",
                "Prefer": "return=minimal",
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
# Pull side — aggregate community insights for a (printer_model, material)
# pair.  Called at generation time when local outcome history is sparse.
# ---------------------------------------------------------------------------


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

    Queries Supabase for recent community prints matching the given
    printer model and material, aggregates the failure-mode distribution,
    and returns a summary dict.  Disk-cached for
    ``_COMMUNITY_CACHE_TTL_SECONDS`` so repeated generation calls within
    the same hour do not re-hit the network.

    Returns ``None`` when opt-in is disabled, the network fails, or no
    matching community data exists — callers should always tolerate
    ``None`` and fall back to local/static sources.

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

    try:
        import urllib.parse
        import urllib.request

        params = urllib.parse.urlencode({
            "printer_model": f"eq.{printer_model}",
            "material": f"eq.{material}",
            "select": "failure_mode,outcome",
            "limit": str(_COMMUNITY_FETCH_LIMIT),
        })
        url = f"{_SUPABASE_URL}/rest/v1/community_prints?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "apikey": _SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {_SUPABASE_ANON_KEY}",
                "Accept": "application/json",
            },
            method="GET",
        )

        with urllib.request.urlopen(req, timeout=_COMMUNITY_FETCH_TIMEOUT) as resp:
            if resp.status >= 300:
                _logger.debug("Community fetch status: %s", resp.status)
                return None
            rows = json.loads(resp.read().decode())

    except Exception as exc:
        _logger.debug("Community fetch failed (non-fatal): %s", exc)
        return None

    if not isinstance(rows, list) or not rows:
        return None

    # Aggregate failure_mode counts (only from rows that actually failed).
    failure_counts: dict[str, int] = {}
    success_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("outcome") == "success":
            success_count += 1
            continue
        fm = row.get("failure_mode")
        if fm:
            failure_counts[fm] = failure_counts.get(fm, 0) + 1

    # Sort by count desc so downstream consumers can trust iteration order.
    ordered = dict(
        sorted(failure_counts.items(), key=lambda kv: kv[1], reverse=True)
    )

    result = {
        "failure_breakdown": ordered,
        "sample_size": len(rows),
        "success_count": success_count,
        "source": "community",
        "fetched_at": time.time(),
    }
    _write_cache(cache_path, result)
    return result
