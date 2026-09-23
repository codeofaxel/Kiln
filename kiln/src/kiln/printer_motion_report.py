"""Every registered Klipper-family printer's motion settings, once a day.

The daily heartbeat (:mod:`kiln.heartbeat`) says which printers an install
has.  This sends, right after it, the parts of each Klipper-family
printer's own settings that decide how its head moves on its own --
:func:`kiln.machine_motion.motion_settings`, with nothing that identifies
the machine or its owner -- so Kiln learns how printers behave in the
wild from every install, not only from the ones that print beside a part.
Same switch as the heartbeat (``KILN_TELEMETRY``), same once-a-day
cadence, and the same posture: best effort, never blocks, never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)

#: Home fleets are 1-3 printers; a farm has the fleet tools.
MAX_PRINTERS = 8
_RPC = "record_printer_motion_settings"
_TIMEOUT_S = 5

__all__ = ["MAX_PRINTERS", "fingerprint_of", "gather", "send", "send_after_heartbeat"]


def fingerprint_of(sections: dict[str, Any]) -> str:
    """One fingerprint per distinct document, the same way the placement
    service names it: the sections, canonically."""
    text = json.dumps(sections, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gather() -> list[dict[str, Any]]:
    """One report per registered printer whose settings can be read, as the
    arguments the service's door takes.  Never raises."""
    reports: list[dict[str, Any]] = []
    try:
        from kiln import plate_state
        from kiln.machine_motion import motion_settings_of
        from kiln.registry import get_registry

        reg = get_registry()
        for name in reg.list_names():
            if len(reports) >= MAX_PRINTERS:
                break
            try:
                adapter = reg.get(name)
                printer_id = plate_state.declared_model_of(adapter)
                doc = motion_settings_of(adapter) if printer_id else None
            except Exception:  # noqa: BLE001 -- a machine that cannot be asked sends nothing
                continue
            if not doc:
                continue
            reports.append({
                "p_printer_id": str(printer_id).strip().lower()[:80],
                "p_fingerprint": fingerprint_of(doc["sections"]),
                "p_sections": doc["sections"],
                "p_unit": doc.get("unit"),
                "p_chip": doc.get("chip"),
            })
    except Exception as exc:  # noqa: BLE001
        _logger.debug("printer motion report: nothing gathered (%s)", exc)
    return reports


def send(reports: list[dict[str, Any]], supabase_url: str, anon_key: str) -> int:
    """Post each report to the service's door; how many landed."""
    import urllib.request

    landed = 0
    for report in reports:
        try:
            req = urllib.request.Request(
                f"{supabase_url.rstrip('/')}/rest/v1/rpc/{_RPC}",
                data=json.dumps(report).encode("utf-8"),
                headers={"Content-Type": "application/json", "apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                if resp.status < 300:
                    landed += 1
        except Exception as exc:  # noqa: BLE001 -- non-fatal, like the heartbeat
            _logger.debug("printer motion report failed (non-fatal): %s", exc)
    return landed


def send_after_heartbeat(supabase_url: str, anon_key: str) -> int:
    """What the heartbeat calls once it has been sent for the day."""
    reports = gather()
    return send(reports, supabase_url, anon_key) if reports else 0
