"""Build the printer candidate list that fleet routing scores.

The job router answers "which printer should run this?" from a list of
printer-info dicts.  Assembling that list — probing each adapter's
state, reading the loaded filament, folding in queue depth and
historical success rate — is the same work no matter who is asking, so
it lives here and every door calls it.

Two doors do today: the ``kiln route`` CLI path, and the
``route_print_job`` MCP tool.  The tool used to reach into the CLI
module for this, which dragged the whole click stack into an MCP call
and made a plugin depend on a private CLI symbol; the alternative — a
second copy in the plugin — is how the two would have drifted apart.

Every lookup here is best-effort: a printer that cannot be probed still
appears as a candidate with unknown status rather than vanishing from
the fleet, because a router that silently drops machines gives a
confident answer over a partial fleet.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.materials import normalise_material_type

logger = logging.getLogger(__name__)

#: Wall-clock seconds assumed per already-queued job when estimating
#: how long a printer will be busy before it could start this one.
_ASSUMED_SECONDS_PER_QUEUED_JOB = 1800.0

#: Printer states that mean work is in progress, so the machine is not
#: free right now even when its queue is empty.
#:
#: "stale" is one of them.  An expired reading cannot show a machine is
#: free, and routing a job to one whose last known state was PRINTING is
#: how two prints end up on one bed.  Kept in step with
#: :func:`kiln.printers.base.status_is_occupied` by the test that pins the
#: two together.
_BUSY_STATES = frozenset(
    {"printing", "busy", "paused", "cancelling", "stale"}
)

#: Extensions that are plain machine code — every printer backend takes
#: them, so an adapter that advertises nothing still gets a chance.
_UNIVERSAL_EXTENSIONS = frozenset({".gcode", ".gco", ".g"})


def adapter_supports_extension(adapter: Any, extension: str) -> bool:
    """Return True if *adapter* advertises support for *extension*.

    Defaults to True: an adapter that publishes no capability list has
    not said no, and excluding it would shrink the fleet on missing
    metadata rather than on a real answer.
    """
    ext = extension.lower().strip()
    if not ext:
        return True
    try:
        capabilities = getattr(adapter, "capabilities", None)
        supported = getattr(capabilities, "supported_extensions", None)
        if not supported:
            return True
        return ext in {str(v).lower() for v in supported}
    except Exception:
        return True


def version_results_by_machine(db: Any, version_id: str | None) -> dict[str, dict[str, int]]:
    """Each machine's settled results printing *version_id*, keyed by machine
    fingerprint (:meth:`kiln.persistence.KilnDB.settled_version_results`);
    ``{}`` for no version, no store, or a version never printed here.  Never
    raises: a read that fails leaves every candidate without the block,
    which the router reads as no history, never as a bad one."""
    if not version_id or db is None:
        return {}
    try:
        return dict(db.settled_version_results(version_id))
    except Exception:  # noqa: BLE001 -- history is a refinement, never a blocker
        logger.debug("settled results for version %s not read", version_id, exc_info=True)
        return {}


def repair_block_by_machine(db: Any, fingerprint: str | None) -> dict[str, Any] | None:
    """The ``repair`` block for one machine (:func:`kiln.printer_repairs.routing_block`):
    ``{"status": "needed", "since"}`` for a machine marked out of service,
    ``{"status": "done", "at", "kind"}`` for a repaired one, ``None`` for a
    machine with nothing on record, no store, or no stable identity.  Never
    raises: a record that cannot be read leaves the candidate without the
    block, which the router reads as nothing said."""
    if not fingerprint or db is None:
        return None
    try:
        from kiln.printer_repairs import condition_summary, routing_block

        return routing_block(condition_summary(db.printer_condition(fingerprint)))
    except Exception:  # noqa: BLE001 -- a condition is a refinement, never a blocker
        logger.debug("repair record for a candidate not read", exc_info=True)
        return None


def collect_routing_candidates(
    *,
    adapters: dict[str, Any],
    material: str,
    pending_counts: dict[str, int] | None = None,
    file_extension: str | None = None,
    version_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build router candidate dicts from *adapters*.

    :param adapters: Printer name -> adapter, from the registry or from
        on-disk configs; this function does not care which.
    :param material: The job's material.  Accepted for call-site
        symmetry with the router's criteria; candidates report what each
        printer has LOADED, and the router does the matching.
    :param pending_counts: Printer name -> queued job count, for wait
        estimates.  Missing entries count as zero.
    :param file_extension: Restrict to adapters that accept this
        extension, e.g. ``".3mf"``.
    :param version_id: The kept design version being printed, when the
        door knows it.  Each candidate that has SETTLED prints of that
        version on this computer then carries ``version_results``:
        ``{"completed", "failed", "partial"}`` counts for that machine
        (:func:`version_results_by_machine`, the machine known by its
        fingerprint, never sent), and the router scores it by them.  A
        candidate with no settled print of the version carries no block,
        and the router reads that as no history.
    :returns: Candidate dicts shaped for ``JobRouter.route_job``.
    """
    from kiln.persistence import get_db
    from kiln.registry import stable_machine_fingerprint

    pending = pending_counts or {}
    file_ext = (file_extension or "").lower().strip()
    candidates: list[dict[str, Any]] = []

    try:
        tracker_db = get_db()
    except Exception:
        tracker_db = None

    by_machine = version_results_by_machine(tracker_db, version_id)

    tracker = None
    if tracker_db is not None:
        try:
            from kiln.materials import MaterialTracker

            tracker = MaterialTracker(db=tracker_db)
        except Exception:
            tracker = None

    for name, adapter in adapters.items():
        if file_ext and not adapter_supports_extension(adapter, file_ext):
            continue

        status = "unknown"
        try:
            from kiln.printers.engagement import internal_read

            # Kiln surveying its own candidates.  Without this a machine the
            # single-printer rule declines to command reads as "offline",
            # which is a wrong fact about the user's hardware, not a refusal.
            with internal_read():
                state = adapter.get_state()
            # effective_state: a candidate that raised a fault mid-print, or
            # whose reading expired mid-print, is still running a print.  The
            # headline word for either is absent from _BUSY_STATES, so the
            # bare read dropped the busy penalty on exactly the machines that
            # most deserve it.
            from kiln.printers.base import effective_state_of

            raw_state = effective_state_of(state)
            status = str(getattr(raw_state, "value", raw_state or "unknown")).lower()
        except Exception:
            status = "offline"

        supported_materials: list[str] = []
        if tracker is not None:
            try:
                loaded = tracker.get_material(name, tool_index=0)
                loaded_material = normalise_material_type(
                    getattr(loaded, "material_type", None)
                )
                if loaded_material:
                    supported_materials = [loaded_material]
            except Exception:
                supported_materials = []

        success_rate: float | None = None
        if tracker_db is not None:
            try:
                insights = tracker_db.get_printer_learning_insights(name)
                raw_rate = insights.get("success_rate")
                if raw_rate is not None:
                    success_rate = float(raw_rate)
            except Exception:
                success_rate = None

        queue_depth = max(0, int(pending.get(name, 0)))
        estimated_wait_s = float(queue_depth * _ASSUMED_SECONDS_PER_QUEUED_JOB)
        if status in _BUSY_STATES:
            estimated_wait_s = max(estimated_wait_s, _ASSUMED_SECONDS_PER_QUEUED_JOB)

        candidate: dict[str, Any] = {
            "printer_id": name,
            "printer_model": name,
            "status": status,
            "queue_depth": queue_depth,
            "supported_materials": supported_materials,
            "success_rate": success_rate,
            "estimated_wait_s": estimated_wait_s,
            "print_speed_factor": 1.0,
        }
        # This machine's own settled prints of the version: looked up by the
        # fingerprint the print row was keyed on, carried as counts under the
        # registry NAME.  The fingerprint stays on this computer.
        fingerprint = stable_machine_fingerprint(adapter)
        if by_machine:
            results = by_machine.get(fingerprint or "")
            if results:
                candidate["version_results"] = dict(results)
        # What the person said about the machine's condition, keyed the same
        # way: a machine marked needs repair is not a choice, and a repaired
        # one is judged by its prints since.  Status and time only.
        repair = repair_block_by_machine(tracker_db, fingerprint)
        if repair:
            candidate["repair"] = repair
        candidates.append(candidate)

    # Plain machine code runs anywhere, so an empty result here means
    # capability metadata excluded every printer rather than the fleet
    # genuinely being unable to print it.  Offer them all, unprobed.
    if not candidates and file_ext in _UNIVERSAL_EXTENSIONS:
        for name in adapters:
            queue_depth = max(0, int(pending.get(name, 0)))
            candidates.append(
                {
                    "printer_id": name,
                    "printer_model": name,
                    "status": "unknown",
                    "queue_depth": queue_depth,
                    "supported_materials": [],
                    "success_rate": None,
                    "estimated_wait_s": float(
                        queue_depth * _ASSUMED_SECONDS_PER_QUEUED_JOB
                    ),
                    "print_speed_factor": 1.0,
                }
            )

    return candidates
