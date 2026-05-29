"""Silent, geometry-based community auto-contribution for monitored prints.

When a monitored print reaches a terminal outcome, its (anonymous) result is
contributed to the community pool automatically — no manual
``record_print_outcome`` call, and keyed on the printed file's GEOMETRIC
signature (:func:`kiln.print_dna.fingerprint_model`), not its file hash, so the
same model re-exported still aggregates with its siblings.

Best-effort + non-blocking throughout: a missing manifest entry, a non-STL
source, or an offline federation endpoint never affects the print path.  The
geometry lookup is fail-safe (returns ``""`` → the contribution is skipped
rather than sending a file-hash stand-in), and the actual send goes through
:func:`kiln.community_outbox.contribute`, which persists locally first and
retries later, and is itself opt-in-gated.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Only outcomes that say something about whether the geometry+settings printed
# well are worth aggregating.  cancelled / timeout / paused are user/clock
# events, not model-quality signals — skip them.
_TERMINAL_OUTCOME_MAP = {"completed": "success", "failed": "failed"}


def geometric_signature_for(printer_file_name: str | None) -> str:
    """Resolve a printed file to its geometric signature.

    Pipeline: printer file name → upload manifest → local source path →
    :func:`fingerprint_model`.  Returns ``""`` on any failure (no manifest
    entry, missing source, non-STL/unparseable file) so callers skip the
    contribution rather than substitute a file hash.
    """
    if not printer_file_name or printer_file_name == "N/A":
        return ""
    try:
        from kiln.upload_manifest import resolve_source_path

        source_path = resolve_source_path(printer_file_name)
        if not source_path or not os.path.exists(source_path):
            return ""
        from kiln.print_dna import fingerprint_model

        return fingerprint_model(source_path).geometric_signature or ""
    except Exception:
        logger.debug("geometric signature unavailable (best-effort)", exc_info=True)
        return ""


def auto_contribute_completion(
    *,
    outcome: str,
    printer_file_name: str | None,
    job_id: str | None = None,
    printer_model: str | None = None,
    material: str | None = None,
    print_time_seconds: int | None = None,
) -> dict[str, Any]:
    """Contribute a monitored terminal outcome to the community pool.

    Silent, non-blocking, never raises.  Returns a small status dict for
    tests/maintainers (never surfaced to the user).  Skips non-quality
    outcomes and any print whose geometry can't be fingerprinted.
    """
    try:
        mapped = _TERMINAL_OUTCOME_MAP.get(outcome)
        if mapped is None:
            return {"contributed": False, "reason": "non_quality_outcome"}
        signature = geometric_signature_for(printer_file_name)
        if not signature:
            return {"contributed": False, "reason": "no_geometry"}
        record = {
            "geometric_signature": signature,
            "printer_model": printer_model or "unknown",
            "material": material or "unknown",
            "outcome": mapped,
            "print_time_seconds": int(print_time_seconds) if print_time_seconds else 0,
        }
        # Dedupe across the two monitors (await_print_completion +
        # watch_print_status) observing the same job: job_id is stable per
        # print; fall back to the file name / signature when monitoring the
        # printer directly with no queued job.
        dedupe_key = f"auto:{job_id or printer_file_name or signature}:{signature}"
        from kiln import community_outbox

        result = community_outbox.contribute(dedupe_key, record)
        return {"contributed": True, "signature": signature, **result}
    except Exception:
        logger.debug("auto community contribution skipped (best-effort)", exc_info=True)
        return {"contributed": False, "reason": "error"}
