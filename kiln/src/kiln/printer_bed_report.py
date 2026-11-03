"""The beds found for this install's printers outside the catalogue, once a day.

A printer Kiln's catalogue has no row for gets its bed on this machine --
read off its own configuration, published by its maker and confirmed by its
owner, or taken from a slicer project (:func:`kiln.printers.bed_fit.printer_bed`).
This sends, right after the daily heartbeat (:mod:`kiln.heartbeat`), each such
printer's model, its bed and where the bed came from, so Kiln can see
how many installs agree on a model's bed before it is added to the catalogue.
Adding a model stays a person's decision; nothing here writes to the catalogue.

What leaves: the model key the heartbeat already sends, the three numbers, the
source's class, and -- for a maker's page -- its web host ("prusa3d.com"),
never the page's path, a file name or a configuration line.  The install id is
the heartbeat's own, so one install counts once per model.  Same switch as the
heartbeat (``KILN_TELEMETRY``), same once-a-day cadence, same posture: best
effort, never blocks, never raises.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

#: Home fleets are 1-3 printers; a farm has the fleet tools.
MAX_PRINTERS = 8
_RPC = "record_printer_bed"
_TIMEOUT_S = 5
_MODEL_KEY = re.compile(r"^[a-z0-9_]{1,80}$")
_HOST = re.compile(r"^(?=.{4,80}$)[a-z0-9-]+(\.[a-z0-9-]+)+$")

__all__ = ["MAX_PRINTERS", "gather", "send", "send_after_heartbeat", "source_host"]


def source_host(where: str) -> str | None:
    """The public web host of a maker's page, or ``None``: never a path, a
    query, a local name or an address."""
    try:
        parsed = urlparse(str(where or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if not _HOST.match(host) or re.fullmatch(r"[\d.]+", host) or host.endswith((".local", ".lan", ".home")):
        return None
    return host


def gather() -> list[dict[str, Any]]:
    """One report per model this install's printers are set up as whose bed
    is recorded here, not the catalogue's, as the arguments the service's
    door takes.  Never raises."""
    reports: list[dict[str, Any]] = []
    try:
        from kiln.installation import get_installation_id
        from kiln.printer_model_resolver import resolve_all_printer_models
        from kiln.printers.bed_fit import BED_FROM_CATALOGUE, printer_bed
        from kiln.safety_profiles import BED_FROM_MAKER_SPEC

        installation_id = get_installation_id()
        for model in dict.fromkeys(str(m).strip().lower() for m in resolve_all_printer_models() if m):
            if len(reports) >= MAX_PRINTERS:
                break
            bed = printer_bed(model)
            if bed is None or bed.source == BED_FROM_CATALOGUE or not _MODEL_KEY.match(bed.printer_id):
                continue
            reports.append({
                "p_installation_id": installation_id,
                "p_printer_id": bed.printer_id,
                "p_bed_mm": [round(float(side), 1) for side in bed.volume],
                "p_source": bed.source,
                "p_source_host": source_host(bed.where) if bed.source == BED_FROM_MAKER_SPEC else None,
            })
    except Exception as exc:  # noqa: BLE001
        _logger.debug("printer bed report: nothing gathered (%s)", exc)
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
            _logger.debug("printer bed report failed (non-fatal): %s", exc)
    return landed


def send_after_heartbeat(supabase_url: str, anon_key: str) -> int:
    """What the heartbeat calls once it has been sent for the day."""
    reports = gather()
    return send(reports, supabase_url, anon_key) if reports else 0
