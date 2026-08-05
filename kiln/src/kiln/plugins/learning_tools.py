"""Learning and agent memory tools plugin.

Extracts cross-printer learning and persistent agent memory tools from
server.py into a focused plugin module.  These tools enable agents to
record print outcomes, query learning insights, get printer suggestions,
recommend settings, and persist notes across sessions.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any

from kiln.failure_vocabulary import (
    AUTO_CLASSIFY_MIN_CONFIDENCE as _AUTO_CLASSIFY_MIN_CONFIDENCE,
)
from kiln.failure_vocabulary import (
    VALID_FAILURE_MODES as _VALID_FAILURE_MODES,
)
from kiln.failure_vocabulary import (
    to_canonical as _to_canonical_failure_mode,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — validation sets and safety limits
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = frozenset({"success", "failed", "partial", "cancelled"})
_VALID_QUALITY_GRADES = frozenset({"excellent", "good", "acceptable", "poor"})
_VALID_DETERMINED_BY = frozenset({"observed", "inferred", "user_reported"})

# Hard safety limits — recorded settings cannot exceed these.
# Prevents malicious agents from poisoning the learning database
# with dangerous temperature data that could damage printers.
_MAX_SAFE_TOOL_TEMP: float = 320.0  # Above this, even high-temp materials are dangerous
_MAX_SAFE_BED_TEMP: float = 140.0
_MAX_SAFE_SPEED: float = 500.0  # mm/s — beyond any consumer printer

_LEARNING_SAFETY_NOTICE = (
    "These insights are advisory only. They do NOT override safety limits. "
    "Always run preflight checks before starting a print. Temperature and "
    "G-code safety enforcement applies regardless of learning data."
)


# ---------------------------------------------------------------------------
# Standalone functions — importable for direct calls and testing.
#
# Each function reads _check_auth, _error_dict, etc. from kiln.server at
# call time (via module attribute access) so that monkeypatching works in
# tests.
# ---------------------------------------------------------------------------


def _auto_classify_failure(
    job_id: str | None,
    printer_name: str | None,
) -> dict[str, Any] | None:
    """Run the failure classifier and translate its output for storage.

    Returns a dict describing the classification (always safe to echo to
    the caller), or ``None`` when the classifier cannot be reached.  The
    ``stored`` flag tells the caller whether the classification was
    confident enough to become the row's ``failure_mode``.

    Separated from ``record_print_outcome`` so the logic has a single
    testable entry point and so the caller still writes the outcome row
    when classification fails — never lets auto-classify break the
    primitive.
    """
    try:
        from kiln.failure_recovery import analyze_failure
    except Exception:
        _logger.debug("Failure classifier unavailable", exc_info=True)
        return None

    try:
        analysis = analyze_failure(job_id=job_id, printer_name=printer_name)
    except Exception:
        _logger.debug("Failure classifier raised", exc_info=True)
        return None

    classification = analysis.classification
    raw_type = classification.failure_type.value
    canonical = _to_canonical_failure_mode(raw_type)
    stored = (
        canonical is not None
        and classification.confidence >= _AUTO_CLASSIFY_MIN_CONFIDENCE
    )
    return {
        "failure_type": raw_type,
        "db_mode": canonical or raw_type,  # echo something even on untranslatable
        "confidence": round(classification.confidence, 3),
        "evidence": list(classification.evidence),
        "stored": stored,
        "min_confidence": _AUTO_CLASSIFY_MIN_CONFIDENCE,
    }


def _material_from_job(job_record: dict[str, Any] | None, job_id: str) -> str | None:
    """Material this job DECLARED, from the job's own records.

    Two stores, both recorded when the print was set up, both about this
    exact job: the print-history row (its ``material_type`` column, then its
    ``metadata`` payload) and the queue job's metadata, which is where a
    submitted job carries the material the scheduler routed it on.

    Returns ``None`` when no store names one — never a placeholder.
    """
    def named(value: Any) -> str | None:
        text = str(value or "").strip()
        # "unknown" is a placeholder some producers write instead of leaving
        # the field empty; it names nothing, so it is an absence here too.
        return text if text and text.lower() != "unknown" else None

    if job_record:
        metadata = job_record.get("metadata")
        for candidate in (
            job_record.get("material_type"),
            metadata.get("material_type") if isinstance(metadata, dict) else None,
        ):
            if found := named(candidate):
                return found
    try:
        import kiln.server as _srv

        job = _srv._get_queue().get_job(job_id)
        return named((job.metadata or {}).get("material_type"))
    except Exception:
        _logger.debug("No queue job material for %s", job_id, exc_info=True)
    return None


def _material_from_printer(printer_name: str | None) -> str | None:
    """Material physically loaded in the printer RIGHT NOW, or ``None``.

    Reads the same live AMS state ``get_active_material`` reads, through the
    same tray helpers, so there is one parse of "which tray is active" rather
    than a second one that drifts.  Honest-or-nothing at every step: an
    external spool (no RFID), an unparseable tray index, a tray with no type,
    or a printer with no AMS all return ``None``.  The one inference allowed
    is the one that cannot be wrong — when the active slot is unreported but
    every loaded tray holds the SAME material, that material ran the print
    whichever slot fed it.
    """
    if not printer_name:
        return None
    try:
        import kiln.server as _srv
        from kiln.plugins.material_tools import (
            _coerce_ams_slot,
            _find_tray,
            _iter_ams_trays,
            _loaded_ams_trays,
        )

        adapter = _srv._registry.get(printer_name)
        if adapter is None or not hasattr(adapter, "get_ams_status"):
            return None
        ams = adapter.get_ams_status()
        if not isinstance(ams, dict):
            return None

        loaded = _loaded_ams_trays(ams)
        slot = _coerce_ams_slot(ams.get("tray_now"))
        if slot is None:
            for field in ("active_tray", "tray_pre", "tray_tar"):
                candidate = _coerce_ams_slot(ams.get(field))
                if candidate is not None and _find_tray(loaded, candidate) is not None:
                    slot = candidate
                    break
        if slot is not None:
            tray = _find_tray(_iter_ams_trays(ams), slot)
            material = str((tray or {}).get("tray_type", "") or "").strip()
            return material or None

        materials = {
            str(tray.get("tray_type", "") or "").strip() for tray in loaded
        }
        materials.discard("")
        if len(materials) == 1:
            return materials.pop()
    except Exception:
        _logger.debug(
            "Live material unavailable for %s (best-effort)", printer_name, exc_info=True
        )
    return None


def _resolve_material_type(
    job_record: dict[str, Any] | None,
    job_id: str,
    printer_name: str | None,
    determined_by: str,
) -> str | None:
    """Backfill ``material_type`` for an outcome that arrived without one.

    Mirrors the printer/file backfill: the primary capture path (the
    terminal-state hook) knows the printer, the job, and the file, and
    nothing else — so every auto-recorded row saved ``material_type=None``
    and the material dimension of the learning loop never saw the prints it
    was built to learn from.

    Source order, strongest first: what the JOB declared (print history, then
    the queue job), then what the PRINTER currently holds.  The live reading
    is only consulted for an ``observed`` outcome — a live process watched
    this print end, so the spool in the machine is the spool that just ran.
    For a record arriving after the fact (``inferred`` on reconnect, or a
    user settling last week's unknown row) today's spool is not evidence
    about that print, and stamping it on would be a guess wearing a fact's
    clothes.  ``None`` is the honest answer there, and an honest absence is
    exactly what the per-material reads filter out.
    """
    from_job = _material_from_job(job_record, job_id)
    if from_job:
        return from_job
    if determined_by == "observed":
        return _material_from_printer(printer_name)
    return None


def record_print_outcome(
    job_id: str,
    outcome: str,
    quality_grade: str | None = None,
    failure_mode: str | None = None,
    settings: dict | None = None,
    environment: dict | None = None,
    notes: str | None = None,
    printer_name: str | None = None,
    file_name: str | None = None,
    file_hash: str | None = None,
    material_type: str | None = None,
    decoration_slug: str | None = None,
    decoration_settings: dict | None = None,
    auto_classify: bool = False,
    auto_recorded: bool = False,
    determined_by: str | None = None,
) -> dict:
    """Record the outcome of a print for cross-printer learning.

    The learning database helps agents make better decisions about which
    printer to use for a given job and material.  Outcomes are agent-curated
    quality data — separate from the auto-populated print history.

    **Safety**: Settings are validated against hard safety limits.  Outcomes
    with temperatures exceeding safe maximums are rejected to prevent
    poisoning the learning database with dangerous data.

    **Decoration feedback**: When ``decoration_slug`` is provided, the
    corresponding decoration's proven-settings counter is auto-updated
    (``success_count`` or ``failure_count``) so the library's tracked
    reliability reflects real field outcomes without manual curation.

    **Auto-classification** (opt-in): When ``auto_classify=True`` and the
    outcome is ``"failed"`` with no explicit ``failure_mode``, the failure
    classifier runs (:func:`kiln.failure_recovery.analyze_failure`) and
    its result is mapped into the canonical DB vocabulary.  The
    classification is always echoed back in the ``auto_classification``
    key of the response; it is only STORED as ``failure_mode`` when the
    classifier's confidence meets or exceeds
    ``_AUTO_CLASSIFY_MIN_CONFIDENCE`` (0.75).  Lower-confidence guesses
    are surfaced to the caller without poisoning the learning database.

    Args:
        job_id: The job ID from the print queue.
        outcome: One of ``"success"``, ``"failed"``, ``"partial"``, or
            ``"cancelled"``.
        quality_grade: Optional — ``"excellent"``, ``"good"``, ``"acceptable"``, ``"poor"``.
        failure_mode: Optional — e.g. ``"spaghetti"``, ``"layer_shift"``, ``"warping"``.
        settings: Optional dict of print settings used (temp_tool, temp_bed, speed, etc.).
        environment: Optional dict of environment conditions (ambient_temp, humidity).
        notes: Optional free-text notes about the print.
        printer_name: Printer used.  Auto-resolved from job if omitted.
        file_name: File printed.  Auto-resolved from job if omitted.
        file_hash: Optional hash of the file for cross-printer comparison.
        material_type: Material used (e.g. ``"PLA"``, ``"PETG"``).  Omit and
            Kiln backfills it from what the job declared (print history, then
            the queue job) or — for an outcome watched live — from the
            filament the printer currently holds.  Stays unset when no honest
            source knows it; per-material learning skips unset rows rather
            than learning from a guess.
        decoration_slug: Optional decoration slug that was applied to this
            print.  When set, the matching decoration's success/failure
            counters are auto-updated.
        decoration_settings: Optional dict of decoration settings used
            (``depth_mm``, ``mode``, ``image_style``).  Falls back to the
            decoration's current defaults when omitted.
        auto_classify: When True and this outcome is a failure with no
            explicit ``failure_mode``, run the failure classifier and
            store its best-guess mode if confidence >= 0.75.  Default
            False — callers opt in.
        auto_recorded: When True, tags the outcome as auto-fired by
            the terminal-state hook (see
            :mod:`kiln.auto_record_hook`).  Agents can later refine
            the outcome by calling record_print_outcome again with the
            same ``job_id`` — the most recent call wins at the
            ``proven_settings`` level.  Default False.
        determined_by: Who settled this outcome — ``"observed"`` (a
            live process watched the print end), ``"inferred"``
            (reconstructed from printer state after the fact), or
            ``"user_reported"`` (the human said so).  Defaults to
            ``"observed"`` for auto-recorded outcomes and
            ``"user_reported"`` otherwise — a manual record normally
            relays what the user reported about the part in hand.
            Recording an outcome for a print that started while Kiln
            wasn't watching RESOLVES its pending row in place rather
            than duplicating it.
    """
    import kiln.server as _srv
    from kiln.persistence import get_db

    if err := _srv._check_auth("learning"):
        return err

    # --- Validate enums ---
    if outcome not in _VALID_OUTCOMES:
        return _srv._error_dict(
            f"Invalid outcome {outcome!r}. Must be one of: {', '.join(sorted(_VALID_OUTCOMES))}",
            code="VALIDATION_ERROR",
        )
    if quality_grade and quality_grade not in _VALID_QUALITY_GRADES:
        return _srv._error_dict(
            f"Invalid quality_grade {quality_grade!r}. Must be one of: {', '.join(sorted(_VALID_QUALITY_GRADES))}",
            code="VALIDATION_ERROR",
        )
    if determined_by and determined_by not in _VALID_DETERMINED_BY:
        return _srv._error_dict(
            f"Invalid determined_by {determined_by!r}. Must be one of: {', '.join(sorted(_VALID_DETERMINED_BY))}",
            code="VALIDATION_ERROR",
        )
    if determined_by is None:
        determined_by = "observed" if auto_recorded else "user_reported"

    # --- Auto-classification (opt-in) ---
    # Runs only for failed outcomes with no explicit failure_mode, and
    # only when the caller opts in.  High-confidence classifications
    # promote to ``failure_mode``; lower-confidence results are echoed
    # back so the caller can decide what to do.  Never raises.
    auto_classification: dict[str, Any] | None = None
    if auto_classify and outcome == "failed" and not failure_mode:
        auto_classification = _auto_classify_failure(job_id, printer_name)
        if auto_classification and auto_classification.get("stored"):
            failure_mode = auto_classification["db_mode"]

    if failure_mode and failure_mode not in _VALID_FAILURE_MODES:
        return _srv._error_dict(
            f"Invalid failure_mode {failure_mode!r}. Must be one of: {', '.join(sorted(_VALID_FAILURE_MODES))}",
            code="VALIDATION_ERROR",
        )

    # --- Safety: validate settings against hard limits ---
    if settings:
        _SETTING_LIMITS = {
            "temp_tool": (0.0, _MAX_SAFE_TOOL_TEMP, "\u00b0C"),
            "temp_bed": (0.0, _MAX_SAFE_BED_TEMP, "\u00b0C"),
            "speed": (0.0, _MAX_SAFE_SPEED, "mm/s"),
        }
        for key, (lo, hi, unit) in _SETTING_LIMITS.items():
            raw = settings.get(key)
            if raw is None:
                continue
            try:
                val = float(raw)
            except (ValueError, TypeError):
                return _srv._error_dict(
                    f"Setting {key!r} value {raw!r} is not a valid number.",
                    code="VALIDATION_ERROR",
                )
            if val < lo or val > hi:
                return _srv._error_dict(
                    f"Recorded {key} {val}{unit} is outside safe range "
                    f"({lo}\u2013{hi}{unit}). Outcome rejected to protect hardware.",
                    code="SAFETY_VIOLATION",
                )

    # --- Resolve printer/file from job if not provided ---
    job_record: dict[str, Any] | None = None
    try:
        job_record = get_db().get_print_record(job_id)
        if job_record and not printer_name:
            printer_name = job_record.get("printer_name", "unknown")
        if job_record and not file_name:
            file_name = job_record.get("file_name")
    except Exception as exc:
        _logger.debug("Failed to resolve printer/file from job %s: %s", job_id, exc)

    if not printer_name:
        printer_name = "unknown"

    # --- Backfill the material the same way, from honest sources only ---
    # Callers that know the material pass it; the terminal-state hook cannot,
    # so without this every auto-recorded row was material-blind.  Resolved
    # BEFORE the row is written so the pending row opened at print start
    # gains it too (``_resolve_row_locked`` COALESCEs material_type).
    if not material_type:
        material_type = _resolve_material_type(
            job_record, job_id, printer_name, determined_by
        )

    # Bug #10: auto-recorded outcomes get a tag in the notes so agents
    # and analytics can distinguish them from agent-curated outcomes.
    # This is the minimal tagging that avoids a schema change; a full
    # dedicated column lives on the follow-up roadmap.
    if auto_recorded:
        tag = "[auto-recorded]"
        if notes:
            if tag not in notes:
                notes = f"{tag} {notes}"
        else:
            notes = tag

    try:
        row_id = get_db().save_print_outcome(
            {
                "job_id": job_id,
                "printer_name": printer_name,
                "file_name": file_name,
                "file_hash": file_hash,
                "material_type": material_type,
                "outcome": outcome,
                "quality_grade": quality_grade,
                "failure_mode": failure_mode,
                "settings": settings,
                "environment": environment,
                "notes": notes,
                "agent_id": "auto" if auto_recorded else "mcp",
                "determined_by": determined_by,
                "created_at": time.time(),
            }
        )
        # Telemetry: count completed print + print hours.
        #
        # Prints Kiln started are already counted at start (see
        # PrinterAdapter.start_print) — this call consumes that pending
        # token rather than counting again.  It only adds a print when
        # there's no start to pair with, which is the print a user ran
        # from the printer's own screen and then asked us to record.
        try:
            from kiln.daily_stats import (
                record_print_hours_for_job,
                record_print_outcome_event,
            )
            record_print_outcome_event(
                job_id, printer_name=printer_name, file_name=file_name,
            )
            # Print time from the job record, deduped by job id —
            # record_print_outcome is re-callable for the same job (an
            # agent refining an auto-recorded outcome) and the hours
            # must not add up again on each refinement.
            if job_record and job_record.get("print_time_seconds"):
                record_print_hours_for_job(
                    job_id, job_record["print_time_seconds"] / 3600.0,
                )
        except Exception:
            pass

        # Auto-contribute to community (anonymous, opt-in).
        #
        # The ``printer_model`` field is what lets the pull side
        # ``fetch_community_insights`` aggregate across users with the
        # same hardware — so we need a canonical model identifier, not
        # the per-user ``printer_name`` (which might be "my-bambu-01").
        # Resolution order:
        #   1. adapter.get_printer_info().model — the printer's own
        #      self-reported model string (what
        #      ``resolve_printer_generation_context`` uses on the pull
        #      side — guarantees push/pull match)
        #   2. printer_name — backward compat with existing rows
        #      pre-dating this fix
        try:
            import hashlib as _hl
            import json as _js

            from kiln import community_outbox
            from kiln.community_sync import community_opt_in_enabled

            # Which outcomes are worth aggregating is NOT decided here — a
            # second opinion about the vocabulary is what let one print ship
            # twice under two different words.  ``translate_outcome`` is the
            # one authority; a cancelled print translates to nothing.
            # The geometry key: the caller's file_hash when supplied, else
            # resolved from the printed file the same way the monitors do.
            # Requiring the CALLER to pass file_hash made the auto-record
            # hook a silent no-op here — ``fire_terminal_state_hook`` never
            # passes one, so every hook-observed ending (the most common
            # automatic path) contributed nothing while looking wired.
            _signature = file_hash
            if not _signature:
                from kiln import community_autofire as _caf

                _signature = _caf.geometric_signature_for(file_name)
            if (
                community_opt_in_enabled()
                and _signature
                and community_outbox.translate_outcome(outcome) is not None
            ):
                resolved_model: str | None = None
                try:
                    from kiln.community_autofire import resolve_adapter_model

                    resolved_model = resolve_adapter_model(
                        _srv._registry.get(printer_name)
                    )
                except Exception:
                    _logger.debug("Could not resolve printer_model for community push", exc_info=True)

                _grade_map = {
                    "excellent": "A", "good": "B",
                    "acceptable": "C", "poor": "D",
                }
                # Route through the durable outbox: persist locally first,
                # then flush in the background.  A failed send (offline / crash
                # / lock) is retried by a later drain instead of silently
                # dropped — the old fire-and-forget thread lost the
                # contribution on any hiccup.  Idempotent per PRINT, not per
                # call path: the key is minted by the shared helper, so a
                # print the monitors already contributed collapses into that
                # one row instead of shipping a second copy of itself.
                community_outbox.contribute_print_outcome(
                    outcome=outcome,
                    geometric_signature=_signature,
                    job_id=job_id,
                    printer_file_name=file_name,
                    printer_model=resolved_model or printer_name,
                    material=material_type,
                    extra={
                        "settings_hash": _hl.sha256(
                            _js.dumps(settings or {}, sort_keys=True).encode(),
                        ).hexdigest()[:16],
                        "settings": settings,
                        # An ungraded print carries no grade.  The old
                        # default ("B") minted a quality verdict nobody
                        # gave — the corpus's job is to hold what was
                        # SAID, and grade absence is data too.
                        "quality_grade": _grade_map.get(quality_grade or "") or None,
                        "failure_mode": failure_mode,
                    },
                )
        except Exception:
            pass  # Never let community sync block outcome recording

        # Auto-update decoration proven-settings counters when this print
        # carried a decoration.  Mirrors the community-sync pattern — the
        # dispatch is silent and never blocks the outcome return.
        #
        # We only dispatch when we have real settings to record: either
        # the caller supplied them, or the library already has a proven
        # setting for this material that we can re-stamp.  If neither is
        # true we skip silently rather than invent defaults — proven
        # settings must reflect real prints, not our guesses.
        if decoration_slug and material_type and outcome in ("success", "failed"):
            try:
                from kiln.decoration_library import (
                    get_decoration,
                    record_decoration_failure,
                    record_decoration_success,
                )

                dec = get_decoration(decoration_slug)
                if dec is not None:
                    ds = decoration_settings or {}
                    existing = dec.proven_settings.get(material_type)
                    have_explicit = ds.get("depth_mm") is not None
                    if have_explicit or existing is not None:
                        depth_mm = float(
                            ds["depth_mm"] if have_explicit else existing.depth_mm
                        )
                        mode = str(ds.get("mode") or (existing.mode if existing else "emboss"))
                        image_style = str(
                            ds.get("image_style")
                            or (existing.image_style if existing else "auto")
                        )

                        # ``source_job_id`` makes the count idempotent: this
                        # tool is re-callable for the same job (an agent
                        # refining an auto-recorded outcome) and the pro
                        # decoration tool can record the same print too — the
                        # counters have no natural key of their own, so the
                        # job id is what stops one print counting twice.
                        if outcome == "success":
                            record_decoration_success(
                                decoration_slug,
                                material=material_type,
                                depth_mm=depth_mm,
                                mode=mode,
                                image_style=image_style,
                                source_job_id=job_id,
                            )
                        else:
                            record_decoration_failure(
                                decoration_slug,
                                material=material_type,
                                depth_mm=depth_mm,
                                mode=mode,
                                image_style=image_style,
                                failure_mode=failure_mode,
                                source_job_id=job_id,
                            )
            except Exception:
                _logger.debug(
                    "Decoration outcome update failed (non-fatal)",
                    exc_info=True,
                )

        result: dict[str, Any] = {
            "success": True,
            "outcome_id": row_id,
            "job_id": job_id,
            "printer_name": printer_name,
            "outcome": outcome,
            "quality_grade": quality_grade,
            "determined_by": determined_by,
        }
        if failure_mode is not None:
            result["failure_mode"] = failure_mode
        # Echo the material only when one is actually known — its ABSENCE is
        # the signal that per-material learning will skip this row, and a
        # caller that expected material data should see that, not a filler.
        if material_type:
            result["material_type"] = material_type
        if auto_classification is not None:
            result["auto_classification"] = auto_classification
        return result
    except Exception as exc:
        _logger.exception("Unexpected error in record_print_outcome")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def recommend_settings(
    printer_name: str | None = None,
    material_type: str | None = None,
    file_hash: str | None = None,
) -> dict:
    """Recommend print settings based on historical successful outcomes.

    Queries the learning database for settings that produced successful
    prints, filtered by printer, material, and/or file hash.  Returns
    aggregated recommendations (most common temps, speeds, slicer profiles)
    plus the raw successful settings for agent review.

    When kiln-pro is installed AND the user has a calibrated slicer
    profile for ``(printer_name, material_type)``, the calibration coach
    overlays personally-tuned values (flow rate, max volumetric speed,
    pressure advance, retraction distance) on top of the historical
    medians.  The response gains a ``calibration_used`` block and an
    extra ``rationale`` line per overridden value naming the slicer +
    staleness — so the user sees "Using your calibrated flow rate of
    0.95 from OrcaSlicer (updated 12 days ago)" instead of the generic
    aggregate.  Behavior is unchanged when kiln-pro is not installed
    or no calibration is available.

    **Note**: Recommendations are advisory.  They do NOT override safety
    limits or preflight checks.  Always validate settings against printer
    safety profiles before use.

    Args:
        printer_name: Filter by printer (e.g. ``"voron-350"``).
        material_type: Filter by material (e.g. ``"PLA"``, ``"PETG"``).
        file_hash: Filter by file hash for exact file matching.
    """
    import kiln.server as _srv
    from kiln.persistence import get_db

    if err := _srv._check_auth("learning"):
        return err

    if not printer_name and not material_type and not file_hash:
        return _srv._error_dict(
            "At least one filter required: printer_name, material_type, or file_hash",
            code="VALIDATION_ERROR",
        )

    try:
        outcomes = get_db().get_successful_settings(
            printer_name=printer_name,
            material_type=material_type,
            file_hash=file_hash,
            limit=20,
        )

        # Calibration overlay — when kiln-pro is installed AND the user
        # has a calibrated profile for (printer_name, material_type),
        # the coach attaches a calibration_used block and overrides
        # specific slicer values (flow_rate, max_volumetric_speed,
        # pressure_advance, retraction_distance) on top of the historical
        # aggregates.  Lazy-imported so public Kiln keeps working without
        # the pro package.  Returns (None, None, []) when calibration
        # is not available — behavior is then identical to today.
        cal_used, cal_overrides, cal_rationale = _recommend_calibration_overlay(
            printer_name=printer_name,
            material_type=material_type,
        )

        if not outcomes:
            empty: dict[str, Any] = {
                "success": True,
                "has_data": False,
                "message": "No successful outcomes found for the given criteria.",
                "query": {
                    "printer_name": printer_name,
                    "material_type": material_type,
                    "file_hash": file_hash,
                },
                "safety_notice": _LEARNING_SAFETY_NOTICE,
            }
            # Even with zero historical outcomes, surface calibrated
            # overrides so the user's tuning shows up in the response.
            if cal_overrides:
                empty["recommended_settings"] = dict(cal_overrides)
                empty["rationale"] = list(cal_rationale)
            if cal_used is not None:
                empty["calibration_used"] = cal_used
            return empty

        # Aggregate settings across successful outcomes
        temp_tools: list[float] = []
        temp_beds: list[float] = []
        speeds: list[float] = []
        slicer_profiles: list[str] = []
        quality_grades: list[str] = []

        for o in outcomes:
            _settings = o.get("settings") or {}
            if isinstance(_settings, dict):
                if "temp_tool" in _settings:
                    with contextlib.suppress(ValueError, TypeError):
                        temp_tools.append(float(_settings["temp_tool"]))
                if "temp_bed" in _settings:
                    with contextlib.suppress(ValueError, TypeError):
                        temp_beds.append(float(_settings["temp_bed"]))
                if "speed" in _settings:
                    with contextlib.suppress(ValueError, TypeError):
                        speeds.append(float(_settings["speed"]))
                if "slicer_profile" in _settings:
                    slicer_profiles.append(str(_settings["slicer_profile"]))
            if o.get("quality_grade"):
                quality_grades.append(o["quality_grade"])

        def _median(vals: list[float]) -> float | None:
            if not vals:
                return None
            s = sorted(vals)
            n = len(s)
            if n % 2 == 1:
                return round(s[n // 2], 1)
            return round((s[n // 2 - 1] + s[n // 2]) / 2, 1)

        def _mode_str(vals: list[str]) -> str | None:
            if not vals:
                return None
            from collections import Counter

            return Counter(vals).most_common(1)[0][0]

        recommended = {}
        if temp_tools:
            recommended["temp_tool"] = _median(temp_tools)
        if temp_beds:
            recommended["temp_bed"] = _median(temp_beds)
        if speeds:
            recommended["speed"] = _median(speeds)
        if slicer_profiles:
            recommended["slicer_profile"] = _mode_str(slicer_profiles)

        # Confidence based on sample size
        n = len(outcomes)
        confidence = "low" if n < 3 else ("medium" if n < 10 else "high")

        # Calibration overrides win over historical medians — when the
        # user has personally tuned the machine, that work supersedes
        # the generic learning aggregate.  Only flow physics keys are
        # touched; temps and speeds keep their historical values.
        if cal_overrides:
            recommended.update(cal_overrides)

        result: dict[str, Any] = {
            "success": True,
            "has_data": True,
            "recommended_settings": recommended,
            "sample_size": n,
            "confidence": confidence,
            "quality_distribution": {
                grade: quality_grades.count(grade)
                for grade in ["excellent", "good", "acceptable", "poor"]
                if quality_grades.count(grade) > 0
            },
            "query": {
                "printer_name": printer_name,
                "material_type": material_type,
                "file_hash": file_hash,
            },
            "recent_successful_settings": [
                {
                    "settings": o.get("settings"),
                    "quality_grade": o.get("quality_grade"),
                    "printer_name": o.get("printer_name"),
                    "material_type": o.get("material_type"),
                    "notes": o.get("notes"),
                }
                for o in outcomes[:5]  # Only show top 5
            ],
            "safety_notice": _LEARNING_SAFETY_NOTICE,
        }
        if cal_rationale:
            result["rationale"] = list(cal_rationale)
        if cal_used is not None:
            result["calibration_used"] = cal_used

        # Nozzle context overlay — when kiln-pro is installed AND
        # the printer has a known/factory nozzle state, attach a
        # nozzle block so downstream agents see "this printer's
        # nozzle is brass" and tailor warnings (e.g. don't suggest
        # CF filaments on brass).  Free-tier installs silently skip.
        if printer_name:
            try:
                from kiln import _pro_nozzle_bridge
                _nozzle = _pro_nozzle_bridge.consult_nozzle_summary(
                    printer_name,
                )
                if _nozzle is not None:
                    result["nozzle"] = _nozzle
            except Exception as exc:
                _logger.debug("Nozzle context overlay skipped: %s", exc)

        return result
    except Exception as exc:
        _logger.exception("Unexpected error in recommend_settings")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# Tiers where the user's calibrated values win over historical medians.
# HIGH: user verified values on this machine.
# MEDIUM: imported profile diverges from defaults — treated as personally
#   tuned (or vendor-baseline fallback, which is what we'd inject anyway).
# LOW / UNKNOWN: skip — values look like defaults, no point overriding
#   with the same numbers.
_RECOMMEND_OVERRIDE_TIERS: frozenset[str] = frozenset({"high", "medium"})


def _recommend_calibration_overlay(
    *,
    printer_name: str | None,
    material_type: str | None,
) -> tuple[dict | None, dict, list[str]]:
    """Lazy-import calibration_coach and resolve overrides for a recommend.

    Returns ``(calibration_used_block, overrides, rationale_lines)``.
    The block is the standard payload from
    :func:`kiln_pro.engineering.calibration_coach.calibration_used_block`
    so the response shape matches the 6 other wire-up sites
    (compute_iso_fit, design_for_load, tolerance_stack_analysis,
    compute_hole, get_clearance_recommendation, slice_and_estimate's
    slicer-args injection).

    Returns ``(None, {}, [])`` when:

    - kiln-pro is not installed (the public Kiln free-tier path)
    - ``printer_name`` is None (calibration is meaningless without a printer)
    - calibration tier is LOW or UNKNOWN (no override warranted)
    - the coach raises (defensive — never break the recommend path)

    Behavior in those cases is identical to the historic recommend
    output, so the wire-up is a strict superset of today's surface.
    """
    if printer_name is None:
        return None, {}, []
    try:
        from kiln_pro.engineering.calibration_coach import (  # type: ignore[import-not-found]
            calibration_for,
            calibration_used_block,
        )
    except ImportError:
        return None, {}, []

    try:
        verdict = calibration_for(printer_name, material_type)
        cal_used = calibration_used_block(verdict, printer_id=printer_name)
    except Exception:
        return None, {}, []

    tier = getattr(verdict.tier, "value", None) or str(verdict.tier)
    if tier not in _RECOMMEND_OVERRIDE_TIERS:
        # LOW / UNKNOWN: still surface the calibration_used block so
        # the user knows the coach looked, but no override.
        return cal_used, {}, []

    profile = verdict.profile
    if profile is None:
        return cal_used, {}, []

    overrides: dict[str, Any] = {}
    rationale: list[str] = []
    candidates: tuple[tuple[str, Any], ...] = (
        ("flow_rate", profile.flow_rate),
        ("max_volumetric_speed", profile.max_volumetric_speed_mm3s),
        ("pressure_advance", profile.pressure_advance),
        ("retraction_distance", profile.retraction_distance_mm),
    )

    slicer_pretty = {
        "orcaslicer": "OrcaSlicer",
        "bambustudio": "Bambu Studio",
        "prusaslicer": "PrusaSlicer",
        "vendor_baseline": "the manufacturer baseline",
    }.get(profile.slicer_name, profile.slicer_name)

    age_days = profile.age_days() if hasattr(profile, "age_days") else None
    if age_days is None or profile.slicer_name == "vendor_baseline":
        age_phrase = ""
    else:
        days = int(round(age_days))
        if days <= 0:
            age_phrase = " (updated today)"
        elif days == 1:
            age_phrase = " (updated 1 day ago)"
        else:
            age_phrase = f" (updated {days} days ago)"

    for key, value in candidates:
        if value is None:
            continue
        overrides[key] = value
        formatted = f"{value:g}"
        rationale.append(
            f"Using your calibrated {key} of {formatted} from "
            f"{slicer_pretty}{age_phrase}"
        )

    staleness_days = getattr(verdict, "staleness_days", None)
    if (
        staleness_days is not None
        and isinstance(staleness_days, (int, float))
        and staleness_days > 180.0
    ):
        rationale.append(
            f"Calibration is {int(round(staleness_days))} days old — "
            "consider re-running the wizard for a fresher baseline."
        )

    return cal_used, overrides, rationale


def save_agent_note(
    key: str,
    value: str,
    scope: str = "global",
    printer_name: str | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """Save a persistent note or preference that survives across sessions.

    Use this to remember printer quirks, calibration findings, material
    preferences, or any operational knowledge worth preserving.

    Args:
        key: Name for this memory (e.g., ``"z_offset_adjustment"``, ``"pla_temp_notes"``).
        value: The information to store.
        scope: Namespace — ``"global"``, ``"fleet"``, or use *printer_name* for printer-specific.
        printer_name: If provided, scope is automatically set to ``"printer:<name>"``.
        ttl_seconds: Optional time-to-live in seconds.  The note will be
            automatically excluded from queries after this duration.  Pass
            ``None`` (default) for notes that should never expire.
    """
    import kiln.server as _srv
    from kiln.persistence import get_db

    if err := _srv._check_auth("memory"):
        return err
    try:
        agent_id = os.environ.get("KILN_AGENT_ID", "default")
        effective_scope = f"printer:{printer_name}" if printer_name else scope
        get_db().save_memory(agent_id, effective_scope, key, value, ttl_seconds=ttl_seconds)
        return {
            "success": True,
            "agent_id": agent_id,
            "scope": effective_scope,
            "key": key,
            "ttl_seconds": ttl_seconds,
        }
    except Exception as exc:
        _logger.exception("Unexpected error in save_agent_note")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def get_agent_context(
    printer_name: str | None = None,
    scope: str | None = None,
) -> dict:
    """Retrieve all stored agent memory for context.

    Call this at the start of a session to recall what you've learned
    about printers, materials, and past print outcomes.  Expired entries
    are automatically filtered out.  Each entry includes a ``version``
    field showing how many times it has been updated.

    Args:
        printer_name: If provided, retrieves printer-specific memory.
        scope: Filter by scope (e.g., ``"global"``, ``"fleet"``).
    """
    import kiln.server as _srv
    from kiln.persistence import get_db

    if err := _srv._check_auth("memory"):
        return err
    try:
        agent_id = os.environ.get("KILN_AGENT_ID", "default")
        effective_scope = f"printer:{printer_name}" if printer_name else scope
        entries = get_db().list_memory(agent_id, scope=effective_scope)
        return {"success": True, "agent_id": agent_id, "entries": entries, "count": len(entries)}
    except Exception as exc:
        _logger.exception("Unexpected error in get_agent_context")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


def clean_agent_memory() -> dict:
    """Remove all expired agent memory entries.

    Entries with a TTL that has elapsed are permanently deleted.
    Returns the count of entries removed.
    """
    import kiln.server as _srv
    from kiln.persistence import get_db

    if err := _srv._check_auth("memory"):
        return err
    try:
        deleted = get_db().clean_expired_notes()
        return {
            "success": True,
            "deleted_count": deleted,
            "message": f"Cleaned {deleted} expired memory entries.",
        }
    except Exception as exc:
        _logger.exception("Unexpected error in clean_agent_memory")
        return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Plugin class — registers standalone functions as MCP tools.
# ---------------------------------------------------------------------------


class _LearningToolsPlugin:
    """Cross-printer learning and persistent agent memory tools.

    Provides tools for recording print outcomes, querying learning
    insights, suggesting printers and settings, and managing agent
    memory that persists across sessions.
    """

    @property
    def name(self) -> str:
        return "learning_tools"

    @property
    def description(self) -> str:
        return "Cross-printer learning and agent memory tools"

    def register(self, mcp: Any) -> None:
        """Register learning and memory tools with the MCP server."""

        # Lazy imports — by the time register() runs, server.py is fully
        # initialized so these resolve without circular import issues.
        from kiln.persistence import get_db
        from kiln.server import _check_auth, _error_dict, _registry

        mcp.tool()(record_print_outcome)
        mcp.tool()(recommend_settings)
        mcp.tool()(save_agent_note)
        mcp.tool()(get_agent_context)
        mcp.tool()(clean_agent_memory)

        @mcp.tool()
        def get_printer_insights(
            printer_name: str,
            limit: int = 20,
        ) -> dict:
            """Query cross-printer learning insights for a specific printer.

            Returns success rates, failure mode breakdown, and per-material
            statistics based on previously recorded outcomes — plus any
            UNRESOLVED prints: jobs that started (or ended) while no Kiln
            process was watching, whose outcome nobody has settled yet.

            **Agent contract for ``unresolved_prints``**: these entries are
            waiting on the one witness the machine can't replace — the
            user, who has the part.  When the moment is natural (not
            mid-task), ask casually ("Your ashtray finished while Kiln
            wasn't watching — did it come out OK?") and settle the answer
            via ``record_print_outcome(job_id=..., outcome=...,
            determined_by="user_reported")``.  Never guess an outcome on
            the user's behalf; an unresolved print stays out of all
            success-rate math until someone who knows answers.

            **Note**: Insights are advisory.  They do NOT override safety limits
            or preflight checks.

            Args:
                printer_name: The printer to get insights for.
                limit: Maximum recent outcomes to include (default 20).
            """
            if err := _check_auth("learning"):
                return err
            try:
                insights = get_db().get_printer_learning_insights(printer_name)
                recent = get_db().list_print_outcomes(printer_name=printer_name, limit=limit)
                unresolved = get_db().list_unresolved_outcomes(
                    printer_name=printer_name, limit=limit,
                )

                # Confidence level based on sample size
                total = insights.get("total_outcomes", 0)
                if total < 5:
                    confidence = "low"
                elif total < 20:
                    confidence = "medium"
                else:
                    confidence = "high"

                return {
                    "success": True,
                    "printer_name": printer_name,
                    "insights": insights,
                    "recent_outcomes": recent,
                    "unresolved_prints": unresolved,
                    "confidence": confidence,
                    "safety_notice": _LEARNING_SAFETY_NOTICE,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_printer_insights")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def suggest_printer_for_job(
            file_hash: str | None = None,
            material_type: str | None = None,
            file_name: str | None = None,
        ) -> dict:
            """Suggest the best printer for a job based on historical outcomes.

            Rankings are based on success rates from previously recorded outcomes,
            optionally filtered by file hash or material type.  Cross-references
            the printer registry for current availability.

            **Note**: Suggestions are advisory.  They do NOT override safety limits
            or preflight checks.  Always run preflight validation before starting
            a print regardless of learning data.

            Args:
                file_hash: Optional hash of the file to match previous prints.
                material_type: Optional material type to filter by (e.g. ``"PLA"``).
                file_name: Optional file name (informational, not used for matching).
            """
            if err := _check_auth("learning"):
                return err
            try:
                ranked = get_db().suggest_printer_for_outcome(
                    file_hash=file_hash,
                    material_type=material_type,
                )

                # Cross-reference availability from registry
                try:
                    idle = set(_registry.get_idle_printers())
                except Exception as exc:
                    _logger.debug("Failed to get idle printers for ranking: %s", exc)
                    idle = set()

                suggestions = []
                for entry in ranked:
                    pname = entry["printer_name"]
                    rate = entry["success_rate"]
                    total = entry["total_prints"]
                    suggestions.append(
                        {
                            "printer_name": pname,
                            "success_rate": rate,
                            "total_prints": total,
                            "score": round(rate * (1 - 1 / (1 + total)), 2),
                            "reason": f"{int(rate * 100)}% success rate ({total} prints)",
                            "currently_available": pname in idle,
                        }
                    )

                # Sort by score descending
                suggestions.sort(key=lambda s: s["score"], reverse=True)

                total_outcomes = sum(e["total_prints"] for e in ranked)
                confidence = "low" if total_outcomes < 5 else ("medium" if total_outcomes < 20 else "high")

                return {
                    "success": True,
                    "suggestions": suggestions,
                    "query": {
                        "file_hash": file_hash,
                        "material_type": material_type,
                        "file_name": file_name,
                    },
                    "data_quality": {
                        "total_outcomes": total_outcomes,
                        "printers_with_data": len(ranked),
                        "confidence": confidence,
                    },
                    "safety_notice": _LEARNING_SAFETY_NOTICE,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in suggest_printer_for_job")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def delete_agent_note(
            key: str,
            scope: str = "global",
            printer_name: str | None = None,
        ) -> dict:
            """Remove a stored note or preference.

            Args:
                key: The key of the note to delete.
                scope: The scope namespace (default ``"global"``).
                printer_name: If provided, targets ``"printer:<name>"`` scope.
            """
            if err := _check_auth("memory"):
                return err
            try:
                agent_id = os.environ.get("KILN_AGENT_ID", "default")
                effective_scope = f"printer:{printer_name}" if printer_name else scope
                deleted = get_db().delete_memory(agent_id, effective_scope, key)
                if not deleted:
                    return _error_dict(
                        f"No memory entry found for key '{key}' in scope '{effective_scope}'.",
                        code="NOT_FOUND",
                    )
                return {"success": True, "key": key, "scope": effective_scope}
            except Exception as exc:
                _logger.exception("Unexpected error in delete_agent_note")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered learning and agent memory tools")


plugin = _LearningToolsPlugin()
