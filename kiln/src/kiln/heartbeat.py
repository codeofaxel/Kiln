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


def _get_printer_info() -> tuple[str | None, str | None, int]:
    """Best-effort resolve of printer model, adapter type, and printer count."""
    model: str | None = None
    adapter_type: str | None = None
    printer_count = 0
    try:
        from kiln.registry import get_registry
        reg = get_registry()
        printer_count = reg.count
        adapter = reg.get("default")
        if adapter is not None:
            info = adapter.get_printer_info()
            model = getattr(info, "model", None) or getattr(info, "printer_model", None)
            # Derive adapter type from class name
            cls_name = type(adapter).__name__.lower()
            if "bambu" in cls_name:
                adapter_type = "bambu"
            elif "octoprint" in cls_name:
                adapter_type = "octoprint"
            elif "moonraker" in cls_name:
                adapter_type = "moonraker"
            elif "serial" in cls_name:
                adapter_type = "serial"
            else:
                adapter_type = cls_name.replace("adapter", "").strip("_") or None
    except Exception:
        pass
    return model, adapter_type, printer_count


def _get_daily_counts() -> tuple[int, int]:
    """Best-effort count of prints and generations completed today."""
    prints = 0
    generations = 0
    try:
        import time

        from kiln.persistence import get_db
        db = get_db()
        today_start = time.mktime(date.today().timetuple())
        outcomes = db.list_print_outcomes(limit=100)
        prints = sum(1 for o in outcomes if o.get("created_at", 0) >= today_start)
        # Generation count from job history if available
        if hasattr(db, "list_jobs"):
            jobs = db.list_jobs(limit=100)
            generations = sum(
                1 for j in jobs
                if j.get("created_at", 0) >= today_start
                and "generat" in (j.get("type") or "").lower()
            )
    except Exception:
        pass
    return prints, generations


def _is_pro_installed() -> bool:
    """Check if kiln-pro is installed."""
    try:
        import kiln_pro  # noqa: F401
        return True
    except ImportError:
        return False


def _send_heartbeat() -> None:
    """Send a single heartbeat to Supabase."""
    global _sent_today

    with _lock:
        if _sent_today or _already_sent_today():
            _sent_today = True
            return

    try:
        import json
        import platform
        import urllib.request

        from kiln.installation import get_installation_id

        installation_id = get_installation_id()

        kiln_version: str | None = None
        try:
            import kiln
            kiln_version = getattr(kiln, "__version__", None)
        except Exception:
            pass

        printer_model, adapter_type, printer_count = _get_printer_info()
        prints_today, generations_today = _get_daily_counts()

        rpc_url = f"{_SUPABASE_URL}/rest/v1/rpc/record_heartbeat"
        payload = json.dumps({
            "p_installation_id": installation_id,
            "p_kiln_version": kiln_version,
            "p_printer_model": printer_model,
            "p_adapter_type": adapter_type,
            "p_printer_count": printer_count,
            "p_prints_today": prints_today,
            "p_generations_today": generations_today,
            "p_pro_installed": _is_pro_installed(),
            "p_os_platform": platform.system().lower(),
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
