"""Anonymous daily usage heartbeat.

Sends one row per install per day to Supabase: installation UUID, Kiln
version, printer model.  No PII, no file paths, no user identity.
Runs in a daemon thread on server startup — never blocks, never errors
visibly, never delays anything.

Disable with ``KILN_TELEMETRY=false`` in environment.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date
from pathlib import Path

_logger = logging.getLogger(__name__)

_SUPABASE_URL = "https://nomzokpscfshjjzezplr.supabase.co"
_SUPABASE_ANON_KEY = "sb_publishable_ZCJyEL0qeveSwgqv7dry3A_YI26Yw6S"

_LAST_BEAT_PATH = Path.home() / ".kiln" / ".last_heartbeat"
_lock = threading.Lock()
_sent_today = False


def _telemetry_enabled() -> bool:
    """Check if telemetry is enabled (default: yes)."""
    val = os.environ.get("KILN_TELEMETRY", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def _already_sent_today() -> bool:
    """File-based guard — avoid duplicate pings on restarts."""
    try:
        if _LAST_BEAT_PATH.is_file():
            stored = _LAST_BEAT_PATH.read_text(encoding="utf-8").strip()
            return stored == str(date.today())
    except OSError:
        pass
    return False


def _mark_sent() -> None:
    """Record that today's heartbeat was sent."""
    try:
        _LAST_BEAT_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _LAST_BEAT_PATH.write_text(str(date.today()), encoding="utf-8")
    except OSError:
        pass


def _get_printer_model() -> str | None:
    """Best-effort resolve of the default printer model."""
    try:
        from kiln.registry import get_registry
        reg = get_registry()
        adapter = reg.get("default")
        if adapter is not None:
            info = adapter.get_printer_info()
            return getattr(info, "model", None) or getattr(info, "printer_model", None)
    except Exception:
        pass
    return None


def _send_heartbeat() -> None:
    """Send a single heartbeat to Supabase."""
    global _sent_today

    with _lock:
        if _sent_today or _already_sent_today():
            _sent_today = True
            return

    try:
        import json
        import urllib.request

        from kiln.installation import get_installation_id

        installation_id = get_installation_id()

        kiln_version: str | None = None
        try:
            import kiln
            kiln_version = getattr(kiln, "__version__", None)
        except Exception:
            pass

        printer_model = _get_printer_model()

        rpc_url = f"{_SUPABASE_URL}/rest/v1/rpc/record_heartbeat"
        payload = json.dumps({
            "p_installation_id": installation_id,
            "p_kiln_version": kiln_version,
            "p_printer_model": printer_model,
        }).encode()

        req = urllib.request.Request(
            rpc_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "apikey": _SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {_SUPABASE_ANON_KEY}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status < 300:
                _mark_sent()
                with _lock:
                    _sent_today = True
                _logger.debug("Heartbeat sent (install=%s)", installation_id[:8])

    except Exception as exc:
        _logger.debug("Heartbeat failed (non-fatal): %s", exc)


def send_heartbeat_async() -> None:
    """Fire the heartbeat in a daemon thread — never blocks startup."""
    if not _telemetry_enabled():
        return
    if _sent_today or _already_sent_today():
        return
    t = threading.Thread(target=_send_heartbeat, daemon=True, name="kiln-heartbeat")
    t.start()
