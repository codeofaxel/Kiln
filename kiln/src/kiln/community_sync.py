"""Community print data sync — local registry → Supabase.

Opt-in anonymous sharing of print outcomes to build collective
intelligence.  Enable with ``KILN_COMMUNITY_OPT_IN=true``.

Only geometric signatures, printer model, material, settings hash,
outcome, and failure mode are shared.  No file paths, no user IDs,
no PII.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

_logger = logging.getLogger(__name__)

_SUPABASE_URL = "https://nomzokpscfshjjzezplr.supabase.co"
_SUPABASE_ANON_KEY = "sb_publishable_ZCJyEL0qeveSwgqv7dry3A_YI26Yw6S"


def community_opt_in_enabled() -> bool:
    """Check if the user has opted in to community data sharing."""
    val = os.environ.get("KILN_COMMUNITY_OPT_IN", "false").strip().lower()
    return val in ("true", "1", "yes", "on")


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
