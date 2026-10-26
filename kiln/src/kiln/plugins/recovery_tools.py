"""Failure recovery, job splitting, and generation feedback tools plugin.

Provides MCP tools for intelligent failure analysis, multi-printer job
splitting, generation feedback loops, and AI-driven print failure
recovery with automatic resume.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


# Keyword sets used by ``_classify_drift_kind_from_failure`` to map a
# failure context to a ``drift_kind`` value for the kiln-pro wear
# hypothesis.  Kept at module scope so the hooks below share one
# canonical vocabulary — adding a synonym in either set immediately
# benefits both ``analyze_print_failure_smart`` and
# ``plan_failure_recovery``.
_DIMENSIONAL_DRIFT_KEYWORDS = (
    "first layer thickness",
    "first-layer thickness",
    "dimension",
    "dimensional",
    "geometry",
    "geometric",
    "small text",
    "fine detail",
    "fine-detail",
)
_FLOW_DRIFT_KEYWORDS = (
    "under-extrusion",
    "under extrusion",
    "underextrusion",
    "layer adhesion",
    "layer-adhesion",
    "clog",
    "clogged",
    "starved",
    "no extrusion",
)


def _classify_drift_kind_from_failure(
    failure_message: str | None,
    analysis: Any | None = None,
) -> str:
    """Map a free-form failure context to a ``drift_kind`` value.

    Returns ``"dimensional"`` (geometric symptoms — first-layer
    thickness drift, fine-detail loss), ``"flow"`` (extrusion-rate
    symptoms — under-extrusion, layer-adhesion failures), or
    ``"unknown"``.  The value is consumed by the kiln-pro overlay.

    Looks at both the failure message and the classified analysis text
    for known keyword cues.  Flow keywords take precedence over
    dimensional keywords because flow symptoms are typically more
    specific (under-extrusion implicates the extrusion path directly),
    while dimensional symptoms can also stem from belt tension or frame
    settling.
    """
    msg = ((failure_message or "") + " " + str(analysis or "")).lower()
    if any(k in msg for k in _FLOW_DRIFT_KEYWORDS):
        return "flow"
    if any(k in msg for k in _DIMENSIONAL_DRIFT_KEYWORDS):
        return "dimensional"
    return "unknown"


_MESH_EXTENSIONS = (".stl", ".3mf", ".obj")


def _plan_fill(
    file_path: str, copies: int, material: str, *, quality: str | None, priority: str | None, share: str = "soonest",
) -> dict:
    """Ask for the copies plan over every registered printer, keep it, draw it."""
    import os

    import kiln.server as _srv
    from kiln import _pro_fleet_fill_bridge as fleet_fill
    from kiln import _pro_placement_bridge as placement
    from kiln.auto_record_hook import version_of_print_file
    from kiln.job_splitter import save_fill_plan
    from kiln.queue import JobStatus
    from kiln.routing_candidates import collect_routing_candidates

    if os.path.splitext(file_path)[1].lower() not in _MESH_EXTENSIONS:
        return _srv._error_dict(
            "Copies are laid out from the part's mesh: give the STL, 3MF or OBJ, not a sliced file.",
            code="VALIDATION_ERROR",
        )
    if not material or not str(material).strip():
        return _srv._error_dict("material is required: the plan only uses printers that can run it.", code="VALIDATION_ERROR")
    registry = _srv._get_registry()
    names = registry.list_names()
    if not names:
        return _srv._error_dict("No printers registered. Register printers before planning copies across them.", code="NO_PRINTERS")
    pending: dict[str, int] = {}
    for job in _srv._get_queue().list_jobs(status=JobStatus.QUEUED):
        if job.printer_name:
            pending[job.printer_name] = pending.get(job.printer_name, 0) + 1
    adapters = {name: registry.get(name) for name in names}
    version = version_of_print_file(file_path)
    # No file type to match: each printer's plate is sliced for that printer
    # when its turn comes.
    candidates = collect_routing_candidates(
        adapters=adapters, material=str(material).strip(), pending_counts=pending, version_id=version,
    )
    if not candidates:
        return _srv._error_dict("No registered printer can accept this file type.", code="NO_ELIGIBLE_PRINTERS")
    times, per_model, untimed_models = _printer_times(
        file_path, [str(c["printer_id"]) for c in candidates], adapters, str(material).strip(),
    )
    answer = fleet_fill.ask(
        file_path, copies, material=str(material).strip(), candidates=candidates, adapters=adapters,
        quality_priority={"draft": 1, "standard": 3, "fine": 5}.get((quality or "standard").lower(), 3),
        speed_priority={"low": 1, "normal": 3, "high": 5}.get((priority or "normal").lower(), 3),
        version_id=version, times=times, share=share,
    )
    if answer.get("success") is not True:
        return answer
    plan = answer["fill"]
    finish = _finish_estimate(plan, per_model, untimed_models)
    from kiln import plate_state

    seen: dict[str, str] = {}
    for name, adapter in adapters.items():
        state = plate_state.read(adapter)
        if state.occupied:
            seen[name] = plate_state.fingerprint(state)
    plan_id = save_fill_plan(file_path, copies, str(material).strip(), plan, plates=seen)
    picture = None
    try:
        picture = _draw_fill(plan, file_path, adapters, placement, finish)
    except Exception:  # noqa: BLE001 -- a plan without its picture still says itself
        _logger.warning("copies plan picture failed", exc_info=True)
    go = (
        f"Show the person the plan; when they say go, call plan_multi_copy_split again with plan_id={plan_id!r} "
        "and submit=True."
        if plan.get("now") or plan.get("later") else "Nothing can be queued as the plates stand."
    )
    return {
        "success": True,
        "plan_id": plan_id,
        "plan": plan,
        "finish": finish,
        "picture": picture,
        "message": f"{plan.get('summary')} {finish['sentence'] if finish else ''} {go}".replace("  ", " "),
    }


def _printer_times(
    file_path: str, names: list[str], adapters: dict, material: str | None,
) -> tuple[dict[str, dict[str, float]], dict[str, tuple[float, float]], list[str]]:
    """Each printer's time for the part, from the slicer, for the plan.

    One copy and two copies of the part are sliced once per printer MODEL,
    with that model's own profile (:func:`_model_times`), which splits the
    slicer's time into what each copy adds and what each plate costs once;
    each printer adds the print it is still finishing (its own countdown).
    Returns ``(times per printer, times per model, models with no time)``;
    a printer whose model could not be sliced here has no time.
    """
    from kiln import plate_state

    by_model: dict[str, list[str]] = {}
    for name in names:
        try:
            model = plate_state.declared_model_of(adapters[name]) or ""
        except Exception:  # noqa: BLE001 -- a printer whose model is unknown gets no time
            model = ""
        by_model.setdefault(model, []).append(name)
    per_model = _model_times(file_path, {m: group[0] for m, group in by_model.items() if m}, material)
    times: dict[str, dict[str, float]] = {}
    for model, group in by_model.items():
        if model not in per_model:
            continue
        per_copy, per_plate = per_model[model]
        for name in group:
            try:
                left = adapters[name].get_job().print_time_left_seconds
                busy = float(left) if left else 0.0
            except Exception:  # noqa: BLE001 -- a printer that says nothing adds nothing
                busy = 0.0
            times[name] = {
                "per_copy_seconds": round(per_copy, 1), "per_plate_seconds": round(per_plate, 1), "busy_seconds": busy,
            }
    untimed = sorted(m or "a printer with no model set" for m in by_model if m not in per_model)
    return times, per_model, untimed


def _finish_estimate(plan: dict, per_model: dict[str, tuple[float, float]], untimed_models: list[str]) -> dict | None:
    """When the run ends, in words, from the plan's own finish: the busiest
    printer's printing time, then the other share's, so the person sees the
    trade.  The time to clear a plate between is the person's and is not
    guessed.  ``None`` when no printer could be timed."""
    from kiln.generation.validation import _human_duration

    finish = plan.get("finish")
    if not isinstance(finish, dict) or not finish.get("busiest"):
        return None
    busiest, seconds = str(finish["busiest"]), float(finish.get("seconds") or 0.0)
    row = next((r for r in plan.get("printers") or [] if r.get("printer") == busiest), {})
    clears = int(row.get("later_plates") or 0)
    between = (
        f", plus clearing its plate {'once' if clears == 1 else 'twice' if clears == 2 else f'{clears} times'} in between"
        if clears else ""
    )
    which = "busiest printer" if not plan.get("untimed") else "busiest printer Kiln could time"
    sentence = (
        f"About {_human_duration(seconds)} of printing on the {which} ({busiest}){between}, by the slicer's own "
        f"estimate for each printer model.{_share_trade(plan, finish)}"
    )
    if untimed_models:
        sentence += (
            f" No time for {', '.join(untimed_models)}: Kiln could not slice the part for it here, so the copies "
            "are shared evenly."
        )
    return {
        "per_model": {m: {"per_copy_seconds": round(t[0], 1), "per_plate_seconds": round(t[1], 1)} for m, t in per_model.items()},
        "printers_seconds": dict(finish.get("printers_seconds") or {}),
        "busiest": busiest,
        "seconds": seconds,
        "share": plan.get("share"),
        "other": finish.get("other"),
        "untimed_models": untimed_models,
        "sentence": sentence,
    }


def _share_trade(plan: dict, finish: dict) -> str:
    """The other share in one sentence: when it would end, and the plates
    it would leave to clear, more or fewer."""
    from kiln.generation.validation import _human_duration as duration

    other = finish.get("other")
    if not isinstance(other, dict):
        return ""
    mine, theirs = float(finish.get("seconds") or 0.0), float(other.get("seconds") or 0.0)
    extra = int(finish.get("plates") or 0) - int(other.get("plates") or 0)
    if plan.get("share") == "soonest":
        if theirs <= mine:
            return " Sharing them evenly would end no sooner."
        fewer = f", with {extra} fewer {'plate' if extra == 1 else 'plates'} to clear" if extra > 0 else ""
        return f" That is the soonest finish; shared evenly it would take about {duration(theirs)}{fewer}."
    if theirs >= mine:
        return " Shared evenly, as asked, which is also the soonest finish here."
    more = f", with {-extra} more {'plate' if extra == -1 else 'plates'} to clear" if extra < 0 else ""
    return f" Shared evenly, as asked; giving the faster printers more would finish in about {duration(theirs)}{more}."


#: How many printer models are sliced at once while a plan is made.
_SLICE_WORKERS = 4


def _model_times(file_path: str, model_printer: dict[str, str], material: str | None) -> dict[str, tuple[float, float]]:
    """``{model: (seconds each copy adds, seconds each plate costs once)}``
    from the slicer: one copy, then two side by side, sliced with the
    model's own profile for one printer of that model.  A model that could
    not be sliced is left out."""
    from concurrent.futures import ThreadPoolExecutor

    def one(item: tuple[str, str]) -> tuple[str, tuple[float, float] | None]:
        model, printer = item
        single = _sliced_seconds(file_path, model, printer, material)
        if single is None:
            return model, None
        pair = None
        try:
            from kiln.auto_orient import duplicate_stl_on_plate
            from kiln.printers.bed_fit import get_build_volume

            bed = get_build_volume(model)
            if bed:
                pair_path = duplicate_stl_on_plate(file_path, 2, bed_width_mm=float(bed[0]), bed_depth_mm=float(bed[1]))
                pair = _sliced_seconds(pair_path, model, printer, material)
        except Exception:  # noqa: BLE001 -- two copies that will not lay out leave the one-copy time
            pair = None
        if pair is not None and pair > single:
            per_copy = pair - single
            return model, (per_copy, max(0.0, single - per_copy))
        return model, (single, 0.0)

    with ThreadPoolExecutor(max_workers=_SLICE_WORKERS) as pool:
        return {model: t for model, t in pool.map(one, model_printer.items()) if t is not None}


def _sliced_seconds(file_path: str, model: str, printer: str, material: str | None) -> float | None:
    """The slicer's own time for *file_path* on *model*, or ``None``."""
    import shutil
    import tempfile

    import kiln.server as _srv
    from kiln.gcode_metadata import extract_metadata
    from kiln.plugins.estimate_tools import _estimate_slice

    # Its own folder: the slicer names its file after the part, so models
    # sliced side by side would otherwise write over each other's answer.
    out_dir = tempfile.mkdtemp(prefix="kiln_fill_time_")
    try:
        printer_id, profile = _srv._resolve_slice_profile_context(profile=None, printer_id=model, printer_name=printer)
        result, err, _info = _estimate_slice(
            file_path, effective_printer_id=printer_id, printer_name=printer, placement=None,
            profile_path=profile, auto_center=True, material=material or None, output_dir=out_dir,
        )
        if err is not None or result is None or not getattr(result, "output_path", None):
            return None
        seconds = extract_metadata(result.output_path).estimated_time_seconds
        return float(seconds) if seconds else None
    except Exception:  # noqa: BLE001 -- a model the slicer cannot do gets no time, never a guess
        _logger.debug("could not slice %s for %s", file_path, model, exc_info=True)
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _draw_fill(plan: dict, file_path: str, adapters: dict, placement: Any, finish: dict | None = None) -> str:
    """The plan's picture, from what this install knows of each plate."""
    from kiln import plate_state
    from kiln.fleet_fill_picture import draw
    from kiln.printers.bed_fit import get_build_volume

    beds, on_plate, models = {}, {}, {}
    for name, adapter in adapters.items():
        model = plate_state.declared_model_of(adapter) or ""
        models[name] = model
        volume = get_build_volume(model) if model else None
        beds[name] = [float(volume[0]), float(volume[1])] if volume else None
        state = plate_state.read(adapter)
        on_plate[name] = [list(j.footprint_mm) for j in state.jobs if j.footprint_mm] if state.occupied else []
    part = (placement.job_envelope(file_path).get("part") or {}).get("size_mm")
    import os

    title = f"{plan.get('wanted')} copies of {os.path.splitext(os.path.basename(file_path))[0]}"
    return draw(
        plan, part_size_mm=part, beds=beds, on_plate=on_plate, models=models, title=title,
        printing_seconds=(finish or {}).get("printers_seconds"), finish=(finish or {}).get("sentence", ""),
    )


def _same_file(a: str, b: str) -> bool:
    """Whether two paths hold the same bytes."""
    import hashlib

    def digest(path: str) -> str | None:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    first = digest(a)
    return first is not None and first == digest(b)


def _submit_fill(file_path: str, plan_id: str) -> dict:
    """Queue a kept copies plan.  The door took the person's yes for the part
    (the preview gate) before calling this."""
    import os

    import kiln.server as _srv
    from kiln import print_signoff
    from kiln.job_splitter import _fill_record, submit_fill_plan

    found = _fill_record(plan_id)
    if found is None:
        return _srv._error_dict(f"No copies plan {plan_id!r} on this computer; plan the copies first.", code="NOT_FOUND")
    kept, record, _status = found
    # The yes is asked for the part given here, and the plan prints the part
    # it kept: they must be the same part, byte for byte.
    if not _same_file(file_path, kept):
        return _srv._error_dict(
            f"{os.path.basename(file_path)} is not the part plan {plan_id} was made for; plan this part first.",
            code="PLAN_PART_MISMATCH",
        )
    signoff = print_signoff.record_for(print_signoff.current())
    names = [str(r.get("printer")) for r in (record.get("fill") or {}).get("printers") or []
             if r.get("now") or r.get("later")]
    # Every printer the plan queues to must be one the yes covers: refused
    # here, whole, rather than job by job at the printer.
    refusals = [why for name in names if (why := print_signoff.record_refusal(signoff, name))]
    if refusals:
        return _srv._error_dict("Nothing was queued: " + " ".join(refusals), code="CONSENT_SCOPE")
    if isinstance(signoff, dict) and not signoff.get("scope") and not signoff.get("printer_name"):
        # A yes that named no printer (the run's own question) is written onto
        # each job as a yes for exactly this run's printers, so the queue
        # refuses any other at the start.
        signoff = {**signoff, "scope": names}
    out = submit_fill_plan(plan_id, signoff)
    if out.get("already"):
        out["message"] = f"This plan was already {out['already']}; nothing was queued again."
        return out
    if not out.get("success"):
        return {**_srv._error_dict("Kiln could not queue this plan: " + "; ".join(out.get("errors") or []), code="QUEUE_ERROR"), **out}
    total = sum(int(q["copies"]) for q in out["queued"])
    printers = len(out["queued"])
    out["message"] = (
        f"Queued {total} copies on {printers} printer{'s' if printers != 1 else ''}. Each starts on its own once its "
        "plate is clear, and later plates follow on the same printer as each plate is cleared. "
        f"split_plan_status({plan_id!r}) follows the run; cancel_split_plan stops it."
    )
    return out


def _fill_progress_sentence(run: dict) -> str:
    """Where a copies run stands, in a sentence."""
    plates = [p["plates"] for p in run.get("printers") or []]
    done = sum(p["done"] for p in plates)
    printing = sum(p["printing"] for p in plates)
    waiting = sum(p["waiting"] for p in plates)
    failed = sum(p["failed"] for p in plates)
    if run.get("status") == "planned":
        return "This plan has not been queued yet."
    good = run.get("copies_done") or 0
    parts = [f"{done} plate{'s' if done != 1 else ''} printed", f"{printing} printing", f"{waiting} waiting"]
    if failed:
        parts.append(f"{failed} that did not come out (a failed plate's copies are queued again)")
    more = run.get("copies_to_come") or 0
    tail = f"; {more} copies still to come" if more else ""
    return f"{good} of {run.get('wanted')} copies printed: " + ", ".join(parts) + tail + "."


class _RecoveryToolsPlugin:
    """Failure recovery, job splitting, generation feedback, and AI-driven recovery tools.

    Tools:
        - analyze_print_failure_smart
        - get_recovery_plan
        - failure_history
        - plan_multi_copy_split (Kiln Business: copies across printers, wherever there is safe room)
        - plan_assembly_split
        - split_plan_status
        - cancel_split_plan
        - analyze_generation_feedback
        - improve_generation_prompt
        - generation_feedback_loop_status
        - detect_print_failure
        - plan_failure_recovery
        - start_print_recovery
        - confirm_print_recovery
        - cancel_print_recovery
        - get_recovery_session
        - get_recovery_steps
        - record_recovery_check
        - complete_print_recovery
        - get_recovery_statistics
    """

    @property
    def name(self) -> str:
        return "recovery_tools"

    @property
    def description(self) -> str:
        return "Failure recovery, job splitting, and generation feedback tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register recovery, splitting, and feedback tools with the MCP server."""

        import kiln.server as _srv

        # ---------------------------------------------------------------
        # Failure Recovery Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def analyze_print_failure_smart(
            progress: float = 0.0,
            error_message: str | None = None,
            printer_name: str | None = None,
            job_id: str | None = None,
        ) -> dict:
            """Classify a print failure and suggest recovery steps.

            Uses heuristics based on error messages, print progress, and
            failure history to classify the failure type and generate an
            actionable recovery plan.

            With kiln-pro installed the answer can also carry
            ``nozzle_wear_hypothesis``: whether the printer's nozzle wear
            likely contributed.  That check is Kiln Pro
            (https://kiln3d.com/pricing); below it, a failure wear could
            explain carries ``nozzle_wear_hypothesis_gated`` saying so once.

            Args:
                progress: Print progress at failure (0.0 - 1.0).
                error_message: Error message from the printer or system.
                printer_name: Name of the printer that failed.
                job_id: Job ID of the failed print.
            """
            import kiln.server as _srv
            from kiln.failure_recovery import analyze_failure, record_failure

            if err := _srv._check_auth("read"):
                return err
            try:
                analysis = analyze_failure(
                    job_id=job_id,
                    printer_name=printer_name,
                    progress=progress,
                    error_message=error_message,
                )

                # Record the failure for future learning
                try:
                    record_failure(
                        analysis.classification,
                        analysis.recovery_plan,
                        printer_name=printer_name,
                        job_id=job_id,
                    )
                except Exception:
                    _logger.debug("Failed to record failure (non-fatal)")

                response: dict[str, Any] = {
                    "success": True,
                    "analysis": analysis.to_dict(),
                    "message": (
                        f"Failure classified as {analysis.classification.failure_type.value} "
                        f"(confidence: {analysis.classification.confidence:.0%}). "
                        f"Recommended action: {analysis.recovery_plan.action.value}."
                    ),
                }

                # Nozzle-wear contributing factor.  kiln-pro decides, by
                # the caller's tier, whether the block or a line saying
                # what it would add rides on this answer; this door only
                # merges what comes back.  A plain install has no kiln-pro
                # and the answer is unchanged.  Best-effort: a fault there
                # never costs the classification above.
                try:
                    from kiln_pro.recovery.nozzle_wear_hypothesis import (
                        wear_hypothesis_fields,
                    )

                    response.update(
                        wear_hypothesis_fields(
                            printer_id=printer_name,
                            drift_kind=_classify_drift_kind_from_failure(
                                failure_message=error_message,
                                analysis=analysis,
                            ),
                        )
                    )
                except ImportError:
                    pass  # a plain install: no kiln-pro, nothing to merge
                except Exception as exc:
                    _logger.debug(
                        "nozzle_wear_hypothesis failed (non-fatal): %s", exc,
                    )

                return response
            except Exception as exc:
                _logger.exception("Unexpected error in analyze_print_failure_smart")
                return _srv._error_dict(
                    f"Unexpected error analyzing failure: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def get_recovery_plan(
            failure_type: str,
            printer_name: str | None = None,
            has_power_loss_recovery: bool = False,
            has_filament_sensor: bool = False,
        ) -> dict:
            """Get a recovery plan for a specific failure type.

            Args:
                failure_type: One of: spaghetti, layer_shift, adhesion_loss,
                    nozzle_clog, stringing, thermal_runaway, power_loss,
                    filament_runout, warping, unknown.
                printer_name: Name of the affected printer.
                has_power_loss_recovery: Whether the printer supports
                    power-loss recovery.
                has_filament_sensor: Whether the printer has a filament
                    runout sensor.
            """
            import kiln.server as _srv
            from kiln.failure_recovery import (
                FailureClassification,
                FailureType,
                plan_recovery,
            )

            if err := _srv._check_auth("read"):
                return err
            try:
                ftype = FailureType(failure_type)
            except ValueError:
                return _srv._error_dict(
                    f"Unknown failure type: {failure_type!r}. "
                    f"Valid types: {[ft.value for ft in FailureType]}",
                    code="VALIDATION_ERROR",
                )

            classification = FailureClassification(
                failure_type=ftype,
                confidence=1.0,
                evidence=["Manual classification"],
                progress_at_failure=0.0,
                time_printing_seconds=0,
                material_wasted_grams=0.0,
            )

            capabilities = {
                "power_loss_recovery": has_power_loss_recovery,
                "filament_sensor": has_filament_sensor,
            }

            plan = plan_recovery(
                classification,
                printer_name=printer_name,
                printer_capabilities=capabilities,
            )

            return {
                "success": True,
                "recovery_plan": plan.to_dict(),
                "message": f"Recovery plan for {failure_type}: {plan.action.value}.",
            }

        @mcp.tool()
        def failure_history(
            printer_name: str | None = None,
            failure_type: str | None = None,
            limit: int = 20,
        ) -> dict:
            """View failure history for a printer or failure type.

            Args:
                printer_name: Filter by printer name.
                failure_type: Filter by failure type.
                limit: Maximum records to return (default 20).

            The response declares its own SCOPE: these are the failures
            THIS machine recorded, and ``scope.local.total_records``
            gives the store's full matching count when ``limit``
            truncated the page.
            """
            import kiln.server as _srv
            from kiln.failure_recovery import (
                count_failure_records,
                get_failure_history,
            )
            from kiln.store_scope import FAILURE_HISTORY, scoped_store_response

            if err := _srv._check_auth("read"):
                return err
            try:
                records = get_failure_history(
                    printer_name=printer_name,
                    failure_type=failure_type,
                    limit=limit,
                )
                return scoped_store_response(
                    {
                        "success": True,
                        "records": records,
                        "count": len(records),
                    },
                    store=FAILURE_HISTORY,
                    items_key="records",
                    filters={
                        "printer_name": printer_name or None,
                        "failure_type": failure_type or None,
                        "limit": limit,
                    },
                    local_total=count_failure_records(
                        printer_name=printer_name, failure_type=failure_type
                    ),
                )
            except Exception as exc:
                _logger.exception("Unexpected error in failure_history")
                return _srv._error_dict(
                    f"Unexpected error fetching failure history: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ---------------------------------------------------------------
        # Job Splitting Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        @_srv.requires_tier(_srv.LicenseTier.BUSINESS)
        def plan_multi_copy_split(
            file_path: str,
            copies: int,
            material: str = "pla",
            plan_id: str | None = None,
            submit: bool = False,
            preview_token: str | None = None,
            quality: str | None = None,
            priority: str | None = None,
            share: str = "soonest",
        ) -> dict:
            """Plan many copies of one part across your printers, wherever there is safe room.

            Requires Kiln Business.  Kiln reads every registered printer's
            plate as it stands and works out how many copies each printer
            takes now -- an empty plate filled, an occupied one filled
            beside what is already there -- and how many follow as later
            plates once a plate is cleared.  A printer marked needs repair,
            a plate nobody has looked at, or a plate with no room takes
            none, and says why.  By default the faster printers take more,
            so the run ends as soon as it can; ``share="even"`` gives every
            printer the same share.  The plan says when the run ends both
            ways, and how many more plates finishing sooner leaves to clear.
            Below Kiln Business, copies print on one printer with
            ``multi_copy_print(fill=True)``, plate after plate.

            Two steps.  Call it with the part (STL, 3MF or OBJ) and how many
            copies: it answers ``plan_id``, the plan per printer, and
            ``picture``, a top-down map of every plate.  AGENT DISPLAY
            CONTRACT: show ``picture`` to the person before anything is
            queued, and say ``message``.  When they say go, call it again
            with the same ``file_path`` and ``copies``, ``plan_id`` and
            ``submit=True`` (and ``preview_token``): one job per printer is
            queued, printers run side by side, each waits for its plate to
            be clear, and later plates follow on the same printer.  Follow
            the run with ``split_plan_status(plan_id)``; stop it with
            ``cancel_split_plan(plan_id)``.

            Args:
                file_path: The part, as a mesh (STL, 3MF or OBJ).
                copies: How many copies in all.
                material: The filament (default ``"pla"``).
                plan_id: The plan to queue, from the first call.
                submit: Queue the plan named by ``plan_id``.
                preview_token: The person's yes for the part, when submitting.
                quality: "draft", "standard" or "fine".
                priority: "low", "normal" or "high".
                share: "soonest" (the default) or "even".
            """
            import kiln.server as _srv

            if err := _srv._check_auth("queue"):
                return err
            if isinstance(copies, bool) or not isinstance(copies, int) or copies < 1:
                return _srv._error_dict("copies must be a whole number of at least 1", code="VALIDATION_ERROR")
            if share not in ("soonest", "even"):
                return _srv._error_dict('share must be "soonest" or "even"', code="VALIDATION_ERROR")
            if submit:
                if not plan_id:
                    return _srv._error_dict("submit needs the plan_id the plan answered.", code="VALIDATION_ERROR")
                if block := _srv._preview_gate_error("plan_multi_copy_split", file_path, preview_token):
                    return block
                return _submit_fill(file_path, plan_id)
            try:
                return _plan_fill(file_path, copies, material, quality=quality, priority=priority, share=share)
            except Exception as exc:
                _logger.exception("Unexpected error in plan_multi_copy_split")
                return _srv._error_dict(f"Unexpected error planning copies: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def plan_assembly_split(
            file_paths: list[str],
            material: str = "pla",
        ) -> dict:
            """Split a multi-file assembly across printers.

            Assigns each file in a multi-part assembly to a different
            printer for parallel printing.

            Args:
                file_paths: List of file paths in the assembly.
                material: Material type (default ``"pla"``).
            """
            import kiln.server as _srv
            from kiln.job_splitter import plan_assembly_split as _plan

            if err := _srv._check_auth("queue"):
                return err
            if not file_paths:
                return _srv._error_dict(
                    "file_paths must not be empty",
                    code="VALIDATION_ERROR",
                )
            try:
                plan = _plan(file_paths, material=material)
                return {
                    "success": True,
                    "plan": plan.to_dict(),
                    "message": (
                        f"Split {len(file_paths)} parts across {plan.total_printers} printers. "
                        f"Time savings: {plan.time_savings_percentage:.0f}%."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in plan_assembly_split")
                return _srv._error_dict(
                    f"Unexpected error planning assembly split: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def split_plan_status(plan_id: str) -> dict:
            """Check the progress of a split plan.

            Args:
                plan_id: The plan ID returned by submitting a split plan.
            """
            import kiln.server as _srv
            from kiln.job_splitter import fill_progress, get_split_progress

            if err := _srv._check_auth("read"):
                return err
            try:
                run = fill_progress(plan_id)
                if run is not None:
                    return {"success": True, "progress": run, "message": _fill_progress_sentence(run)}
                progress = get_split_progress(plan_id)
                return {
                    "success": True,
                    "progress": progress.to_dict(),
                    "message": (
                        f"Plan {plan_id}: {progress.completed_parts}/{progress.total_parts} "
                        f"completed ({progress.overall_progress:.0%})."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in split_plan_status")
                return _srv._error_dict(
                    f"Unexpected error checking split progress: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def cancel_split_plan(plan_id: str) -> dict:
            """Cancel all pending/in-progress parts of a split plan.

            Args:
                plan_id: The plan ID to cancel.
            """
            import kiln.server as _srv
            from kiln.job_splitter import cancel_split_plan as _cancel

            if err := _srv._check_auth("queue"):
                return err
            try:
                result = _cancel(plan_id)
                return result
            except Exception as exc:
                _logger.exception("Unexpected error in cancel_split_plan")
                return _srv._error_dict(
                    f"Unexpected error cancelling split plan: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ---------------------------------------------------------------
        # Generation Feedback Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def analyze_generation_feedback(
            file_path: str,
            original_prompt: str,
            failure_mode: str | None = None,
            max_overhang_angle: float | None = None,
            min_wall_thickness: float | None = None,
            has_bridges: bool = False,
            has_floating_parts: bool = False,
            non_manifold: bool = False,
        ) -> dict:
            """Analyze a generated model and get feedback for improvement.

            Returns feedback with specific constraints to add to the
            generation prompt to fix identified issues.

            Args:
                file_path: Path to the generated model file.
                original_prompt: The original generation prompt.
                failure_mode: Optional failure mode if the model was printed
                    and failed (e.g. ``"adhesion"``, ``"spaghetti"``).
                max_overhang_angle: Maximum overhang angle in degrees.
                min_wall_thickness: Minimum wall thickness in mm.
                has_bridges: Whether the model has bridge features.
                has_floating_parts: Whether the model has disconnected parts.
                non_manifold: Whether the mesh is non-manifold.
            """
            import kiln.server as _srv
            from kiln.generation_feedback import analyze_for_feedback

            if err := _srv._check_auth("read"):
                return err
            try:
                printability_report: dict[str, Any] = {}
                if max_overhang_angle is not None:
                    printability_report["max_overhang_angle"] = max_overhang_angle
                if min_wall_thickness is not None:
                    printability_report["min_wall_thickness"] = min_wall_thickness
                if has_bridges:
                    printability_report["has_bridges"] = True
                if has_floating_parts:
                    printability_report["has_floating_parts"] = True
                if non_manifold:
                    printability_report["non_manifold"] = True

                feedback = analyze_for_feedback(
                    file_path,
                    original_prompt=original_prompt,
                    failure_mode=failure_mode,
                    printability_report=printability_report if printability_report else None,
                )

                # Structural analysis — identify geometry that will
                # snap, crack, or topple and feed it back as constraints.
                # A file whose size puts its units in question has none
                # judged, and the reply opens with that question instead
                # of calling the model clean.
                units_question = ""
                try:
                    from kiln.design_reasoning import analyze_structural_risks as _analyze_risks
                    from kiln.design_reasoning import assess_load_bearing as _assess_load
                    from kiln.generation_feedback import structural_risks_to_feedback

                    risks = _analyze_risks(file_path)
                    load = _assess_load(file_path)
                    units_question = load.units_question
                    structural_fb = structural_risks_to_feedback(
                        risks,
                        original_prompt=original_prompt,
                        load_analysis=load,
                    )
                    feedback.extend(structural_fb)
                except (ValueError, ImportError):
                    pass  # File not STL or design_reasoning unavailable.

                found = f"Found {len(feedback)} feedback items." if feedback else "No issues detected."
                return {
                    "success": True,
                    "feedback": [f.to_dict() for f in feedback],
                    "feedback_count": len(feedback),
                    "message": (
                        f"{units_question}  {found}" if units_question and feedback
                        else units_question or found
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in analyze_generation_feedback")
                return _srv._error_dict(
                    f"Unexpected error analyzing feedback: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def improve_generation_prompt(
            original_prompt: str,
            failure_mode: str | None = None,
            max_overhang_angle: float | None = None,
            min_wall_thickness: float | None = None,
            has_bridges: bool = False,
            iteration: int = 1,
            file_path: str | None = None,
            enforce_sanity: bool = True,
        ) -> dict:
            """Generate an improved prompt from feedback.

            Adds physical constraints to the original prompt to address
            printability and structural issues, without modifying the
            creative intent.

            The improved prompt is run through a three-check sanity gate
            (no contradictions, fits the provider budget, ≥70% original-token overlap so intent
            is preserved).  When ``enforce_sanity=True`` (default) and
            the gate fails, this tool refuses with
            ``code="SANITY_GATE_FAILED"`` and returns the failure list
            instead of a contradictory prompt.  Callers that genuinely
            want the failed prompt — for inspection, repair, or model
            self-correction — set ``enforce_sanity=False`` and read
            ``improved_prompt.sanity`` from the response.

            Args:
                original_prompt: The original generation prompt.
                failure_mode: Optional failure mode string.
                max_overhang_angle: Maximum overhang angle detected.
                min_wall_thickness: Minimum wall thickness detected.
                has_bridges: Whether bridges were detected.
                iteration: Which retry iteration this is.
                file_path: Optional path to the STL file for structural
                    analysis.  When provided, the tool also analyzes
                    structural risks and folds them into the improved
                    prompt.
                enforce_sanity: When True (default), refuse the
                    response if the prompt sanity gate fails.  Pass
                    False to receive a contradictory prompt anyway —
                    useful when the agent intends to repair it before
                    sending to the generator.
            """
            import kiln.server as _srv
            from kiln.generation_feedback import (
                analyze_for_feedback,
                generate_improved_prompt,
            )

            if err := _srv._check_auth("read"):
                return err
            try:
                printability_report: dict[str, Any] = {}
                if max_overhang_angle is not None:
                    printability_report["max_overhang_angle"] = max_overhang_angle
                if min_wall_thickness is not None:
                    printability_report["min_wall_thickness"] = min_wall_thickness
                if has_bridges:
                    printability_report["has_bridges"] = True

                feedback = analyze_for_feedback(
                    file_path or "",
                    original_prompt=original_prompt,
                    failure_mode=failure_mode,
                    printability_report=printability_report if printability_report else None,
                )

                # Structural feedback from mesh geometry: none for a file
                # whose size puts its units in question, which the reply
                # says first.
                units_question = ""
                if file_path:
                    try:
                        from kiln.design_reasoning import analyze_structural_risks as _sr_risks
                        from kiln.design_reasoning import assess_load_bearing as _sr_load
                        from kiln.generation_feedback import structural_risks_to_feedback

                        risks = _sr_risks(file_path)
                        load = _sr_load(file_path)
                        units_question = load.units_question
                        feedback.extend(
                            structural_risks_to_feedback(
                                risks,
                                original_prompt=original_prompt,
                                load_analysis=load,
                            )
                        )
                    except (ValueError, ImportError):
                        pass

                improved = generate_improved_prompt(
                    original_prompt,
                    feedback,
                    iteration=iteration,
                )

                # Sanity gate: refuse contradictory / over-budget /
                # intent-drifted prompts before they reach the generator.
                # The agent gets the failure list and the would-be
                # prompt so it can pick a repair strategy (drop a
                # constraint, raise the budget, regenerate the original
                # request).
                if (
                    enforce_sanity
                    and improved.sanity is not None
                    and not improved.sanity.passed
                ):
                    err = _srv._error_dict(
                        f"Prompt sanity gate failed with "
                        f"{len(improved.sanity.failures)} issue(s); "
                        "refusing to forward a contradictory prompt to "
                        "the generator.  Pass enforce_sanity=False to "
                        "receive the prompt anyway.",
                        code="SANITY_GATE_FAILED",
                    )
                    err["sanity"] = improved.sanity.to_dict()
                    err["improved_prompt"] = improved.to_dict()
                    return err

                return {
                    "success": True,
                    "improved_prompt": improved.to_dict(),
                    "message": (
                        (f"{units_question}  " if units_question else "")
                        + f"Improved prompt with {len(improved.constraints_added)} constraints "
                        f"(iteration {iteration})."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in improve_generation_prompt")
                return _srv._error_dict(
                    f"Unexpected error improving prompt: {exc}",
                    code="INTERNAL_ERROR",
                )

        @mcp.tool()
        def generation_feedback_loop_status(model_id: str) -> dict:
            """Check the status of a generation feedback loop.

            Args:
                model_id: The model ID of the feedback loop.
            """
            import kiln.server as _srv
            from kiln.generation_feedback import get_feedback_loop

            if err := _srv._check_auth("read"):
                return err
            try:
                loop = get_feedback_loop(model_id)
                if loop is None:
                    return _srv._error_dict(
                        f"No feedback loop found for model {model_id!r}",
                        code="NOT_FOUND",
                    )
                return {
                    "success": True,
                    "feedback_loop": loop.to_dict(),
                    "message": (
                        f"Feedback loop for {model_id}: "
                        f"iteration {loop.current_iteration}, "
                        f"{'resolved' if loop.resolved else 'in progress'}."
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in generation_feedback_loop_status")
                return _srv._error_dict(
                    f"Unexpected error checking feedback loop: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ---------------------------------------------------------------
        # AI-Driven Failure Recovery Tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def detect_print_failure(
            printer_name: str,
            telemetry: dict[str, Any],
            telemetry_history: list[dict[str, Any]] | None = None,
            job_info: dict[str, Any] | None = None,
        ) -> dict:
            """Analyze printer telemetry to detect and classify a print failure.

            Examines current telemetry data and optional historical snapshots
            to identify failure conditions such as thermal runaway, layer shift,
            filament runout, adhesion loss, nozzle clogs, and more.

            Args:
                printer_name: Identifier of the printer to analyze.
                telemetry: Current telemetry snapshot with keys like
                    ``hotend_temp``, ``bed_temp``, ``connected``,
                    ``filament_detected``, etc.
                telemetry_history: Optional list of recent telemetry snapshots
                    for trend analysis (newest last).
                job_info: Optional current job metadata with keys like
                    ``file_name``, ``layer``, ``total_layers``, ``z_mm``.

            Returns a failure report dict if a failure is detected, or a
            success dict with ``failure_detected: False`` if no failure found.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("read"):
                return err
            try:
                engine = get_recovery_engine()
                report = engine.detect_failure(
                    printer_name=printer_name,
                    telemetry=telemetry,
                    telemetry_history=telemetry_history,
                    job_info=job_info,
                )
                if report is None:
                    return {
                        "success": True,
                        "failure_detected": False,
                        "message": "No failure detected in telemetry data.",
                    }
                return {
                    "success": True,
                    "failure_detected": True,
                    "failure": report.to_dict(),
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in detect_print_failure")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def plan_failure_recovery(
            failure_id: str,
            printer_capabilities: dict[str, Any] | None = None,
            safety_profile: dict[str, Any] | None = None,
            part_attached: bool | None = None,
            try_untested_resume: bool = False,
        ) -> dict:
            """Generate a recovery plan for a previously detected failure.

            Uses the failure ID from a prior ``detect_print_failure`` call
            to look up the failure report and generate an appropriate recovery
            strategy with preparation steps, parameter adjustments, and risk
            assessment.

            **Which recovery tool to use:**

            - Have a printer_name + job_id from a failed print? → ``plan_print_recovery``
            - Have a failure_id from ``detect_print_failure``? → ``plan_failure_recovery`` (this tool)

            With kiln-pro installed the answer can also carry
            ``nozzle_wear_hypothesis`` (Kiln Pro), or below Pro
            ``nozzle_wear_hypothesis_gated``, as for
            ``analyze_print_failure_smart``.

            Picking the print back up where it stopped is offered only on a
            printer model a Kiln-built resume has been shown to work on, and
            only when the half-printed part is still on the plate; otherwise
            the plan is the next safe move and ``plan.not_offered`` says why
            and what to answer.

            Args:
                failure_id: The failure_id from a detect_print_failure result.
                printer_capabilities: Optional printer capabilities dict.
                safety_profile: Optional safety profile dict.
                part_attached: Your word on whether the half-printed part is
                    still stuck to the plate (true / false).  Left out, the
                    plan asks once before it offers a resume.
                try_untested_resume: Opt this printer in to a resume Kiln has
                    not yet seen work on its model; Kiln learns from how it goes.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("read"):
                return err
            try:
                engine = get_recovery_engine()
                failure = None
                for report in engine.get_failure_history(limit=500):
                    if report.failure_id == failure_id:
                        failure = report
                        break
                if failure is None:
                    return _srv._error_dict(
                        f"Failure report not found: {failure_id!r}",
                        code="NOT_FOUND",
                    )
                plan = engine.plan_recovery(
                    failure,
                    part_attached=part_attached,
                    try_untested_resume=try_untested_resume,
                    printer_capabilities=printer_capabilities,
                    safety_profile=safety_profile,
                )
                response: dict[str, Any] = {"success": True, "plan": plan.to_dict()}

                # Nozzle-wear contributing factor, decided and worded by
                # kiln-pro exactly as in analyze_print_failure_smart; this
                # door merges what comes back.  Best-effort: a fault there
                # never costs the plan above.
                try:
                    from kiln_pro.recovery.nozzle_wear_hypothesis import (
                        wear_hypothesis_fields,
                    )

                    # ``FailureReport`` carries the failure context as
                    # ``probable_cause`` plus an ``evidence`` list.
                    # Stitch both into one string the classifier can
                    # scan for keyword cues.
                    failure_text = " ".join(
                        [
                            getattr(failure, "probable_cause", "") or "",
                            *(getattr(failure, "evidence", []) or []),
                        ]
                    )
                    response.update(
                        wear_hypothesis_fields(
                            printer_id=failure.printer_name,
                            drift_kind=_classify_drift_kind_from_failure(
                                failure_message=failure_text,
                                analysis=failure,
                            ),
                        )
                    )
                except ImportError:
                    pass  # a plain install: no kiln-pro, nothing to merge
                except Exception as exc:
                    _logger.debug(
                        "nozzle_wear_hypothesis failed (non-fatal): %s", exc,
                    )

                return response
            except Exception as exc:
                _logger.exception("Unexpected error in plan_failure_recovery")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def start_print_recovery(
            plan_id: str,
            failure_id: str,
            part_attached: bool | None = None,
            try_untested_resume: bool = False,
        ) -> dict:
            """Begin executing a recovery plan.

            Creates a recovery session that tracks the recovery lifecycle.

            Args:
                plan_id: The plan_id from a plan_failure_recovery result.
                failure_id: The failure_id this recovery addresses.
                part_attached: The same answer given to plan_failure_recovery,
                    so the session runs the plan that was approved.
                try_untested_resume: The same answer given to
                    plan_failure_recovery.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("queue"):
                return err
            try:
                engine = get_recovery_engine()
                failure = None
                for report in engine.get_failure_history(limit=500):
                    if report.failure_id == failure_id:
                        failure = report
                        break
                if failure is None:
                    return _srv._error_dict(
                        f"Failure report not found: {failure_id!r}",
                        code="NOT_FOUND",
                    )
                plan = engine.plan_recovery(
                    failure, part_attached=part_attached, try_untested_resume=try_untested_resume,
                )
                plan.plan_id = plan_id
                # Best-effort PROVEN park route for aborts.  Every fact is
                # asked, none assumed: home position from the machine's own
                # config, occupancy from the job's own gcode.  Any missing
                # fact -> no park (the abort just stays at the homing
                # position, the pre-park behaviour) — never a guessed move.
                park_route = None
                try:
                    from kiln.print_recovery import RecoveryStrategy
                    from kiln.printer_model_resolver import (
                        resolve_printer_model_for,
                    )
                    from kiln.printers.safe_motion import (
                        bed_rect_from_config,
                        home_xy_from_config,
                        occupied_regions_for_job,
                        plan_live_park,
                    )

                    if (
                        plan.strategy == RecoveryStrategy.SAFE_ABORT
                        and failure.gcode_path
                    ):
                        adapter = _srv._get_registry().get(failure.printer_name)
                        config = (
                            adapter.get_printer_config()
                            if hasattr(adapter, "get_printer_config")
                            else None
                        )
                        park_route = plan_live_park(
                            resolve_printer_model_for(failure.printer_name),
                            home_xy_from_config(config),
                            occupied_regions_for_job(failure.gcode_path),
                            bed_rect=bed_rect_from_config(config),
                        )
                except Exception:
                    _logger.debug(
                        "park-route planning skipped (non-fatal)", exc_info=True
                    )
                    park_route = None
                session = engine.start_recovery(
                    plan, failure, park_route=park_route
                )
                response = {
                    "success": True,
                    "session": session.to_dict(),
                    "message": f"Recovery session {session.session_id} started.",
                }
                # Attach a quick inspection bundle so the user sees the
                # mesh being recovered.  Recovery is a stressful moment;
                # visual reassurance that we're recovering the right
                # design matters.  Helper is failure-closed: render
                # failure decorates the response but cannot disrupt the
                # recovery state-machine setup.  Free-tier installs
                # (no kiln-pro) fall through unchanged.
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
            except Exception as exc:
                _logger.exception("Unexpected error in start_print_recovery")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def confirm_print_recovery(session_id: str) -> dict:
            """Confirm that a recovery plan should proceed.

            For plans requiring human confirmation, transitions from
            ``awaiting_confirmation`` to ``executing``.

            Args:
                session_id: The session_id to confirm.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("queue"):
                return err
            try:
                engine = get_recovery_engine()
                session = engine.confirm_recovery(session_id)
                return {
                    "success": True,
                    "session": session.to_dict(),
                    "message": f"Recovery session {session_id} confirmed.",
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_STATE")
            except Exception as exc:
                _logger.exception("Unexpected error in confirm_print_recovery")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def cancel_print_recovery(session_id: str, reason: str = "") -> dict:
            """Cancel an active recovery session.

            Args:
                session_id: The session_id to cancel.
                reason: Optional reason for the cancellation.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("queue"):
                return err
            try:
                engine = get_recovery_engine()
                session = engine.cancel_recovery(session_id, reason=reason)
                return {
                    "success": True,
                    "session": session.to_dict(),
                    "message": f"Recovery session {session_id} cancelled.",
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_STATE")
            except Exception as exc:
                _logger.exception("Unexpected error in cancel_print_recovery")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def get_recovery_session_status(session_id: str) -> dict:
            """Get the current status of a recovery session.

            Args:
                session_id: The session_id to query.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("read"):
                return err
            try:
                engine = get_recovery_engine()
                session = engine.get_session(session_id)
                if session is None:
                    return _srv._error_dict(
                        f"Recovery session not found: {session_id!r}",
                        code="NOT_FOUND",
                    )
                return {"success": True, "session": session.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in get_recovery_session_status")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def get_recovery_gcode_steps(session_id: str) -> dict:
            """Get the G-code/commands for executing a recovery.

            Args:
                session_id: The session_id to get steps for.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("read"):
                return err
            try:
                engine = get_recovery_engine()
                steps = engine.get_recovery_steps(session_id)
                return {"success": True, "steps": steps, "count": len(steps)}
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in get_recovery_gcode_steps")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def record_recovery_check(
            session_id: str,
            passed: bool,
            notes: str = "",
        ) -> dict:
            """Record a post-recovery monitoring check result.

            After recovery execution, monitoring checks verify the print
            is proceeding correctly.

            Args:
                session_id: The session_id being monitored.
                passed: Whether this monitoring check passed.
                notes: Optional notes about the check result.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("queue"):
                return err
            try:
                engine = get_recovery_engine()
                session = engine.record_monitoring_check(
                    session_id, passed=passed, notes=notes
                )
                return {
                    "success": True,
                    "session": session.to_dict(),
                    "monitoring_checks": session.monitoring_checks,
                    "monitoring_passed": session.monitoring_passed,
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_STATE")
            except Exception as exc:
                _logger.exception("Unexpected error in record_recovery_check")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def complete_print_recovery(
            session_id: str,
            success: bool,
            notes: str = "",
            alternative_printers: list[dict] | None = None,
            completion_pct_at_failure: float = 0.0,
        ) -> dict:
            """Mark a recovery session as completed.

            On failure (success=False) with ``alternative_printers`` and
            kiln-pro installed, the answer can carry a
            ``reroute_recommendation``: whether the failed job should
            move to one of those printers, and which.  Choosing between
            printers is Kiln Business (https://kiln3d.com/pricing); below
            it the answer says so once in ``reroute_recommendation_gated``.
            A failure that needs hands-on inspection of the printer is
            held where it is, in ``reroute_recommendation``, at every
            tier.  Recommendation only; the agent runs any move itself
            via ``submit_job`` / ``start_print``.

            Args:
                session_id: The session_id to complete.
                success: Whether the recovery was ultimately successful.
                notes: Final notes about the recovery outcome.
                alternative_printers: Optional other printers the job
                    could move to (each with ``printer_id`` plus optional
                    ``is_idle``, ``supported_materials``,
                    ``build_volume_mm``, ``success_rate``).  Read only
                    when ``success=False``.  Single-printer setups can
                    omit this.
                completion_pct_at_failure: How far the failed print got
                    (0.0–1.0); the recommendation weighs it.  Defaults
                    to 0.
            """
            import kiln.server as _srv
            from kiln.print_recovery import (
                MonitoringThresholdNotMet,
                get_recovery_engine,
            )

            if err := _srv._check_auth("queue"):
                return err
            try:
                engine = get_recovery_engine()
                session = engine.complete_recovery(
                    session_id, success=success, notes=notes
                )
                outcome = "completed successfully" if success else "failed"

                # Pro-tier outcome learning — record to kiln-pro's store so
                # get_recovery_recommendations can rank strategies by
                # historical success AND replay the winning fix on
                # next attempt.  Passing the plan's parameter_adjustments
                # as fix_applied closes the learning loop -- without
                # it, get_winning_fix (the replay surfacer) only sees
                # fixes from the auto_recover flow, not from manual
                # complete_print_recovery flow.  Best-effort — failures
                # here must not break the public recovery flow.
                pro_outcome: dict | None = None
                try:
                    from kiln_pro.bridge import pro_features

                    if pro_features is not None and pro_features.recovery is not None:
                        from kiln_pro.recovery.outcome_learning import record_outcome

                        plan = session.plan
                        failure = session.failure
                        # Compose fix_applied from the plan's
                        # parameter_adjustments.  The kiln-pro side
                        # stores this for replay-ranked recommendations.
                        fix_applied: dict | None = None
                        if plan is not None and getattr(
                            plan, "parameter_adjustments", None,
                        ):
                            fix_applied = dict(plan.parameter_adjustments)
                        pro_outcome = record_outcome(
                            failure_type=failure.failure_type.value if failure else "unknown",
                            strategy=plan.strategy.value if plan else "unknown",
                            success=success,
                            printer_name=failure.printer_name if failure else None,
                            material_type=failure.material_type if failure else None,
                            notes=notes,
                            session_id=session_id,
                            fix_applied=fix_applied,
                        )
                except ImportError:
                    pass  # kiln-pro not installed — free tier
                except Exception as exc:
                    _logger.debug(
                        "kiln-pro outcome recording failed (non-fatal): %s", exc,
                    )

                # Whether the failed job should move to another printer —
                # only when the recovery FAILED and the caller listed some.
                # kiln-pro decides, by the caller's tier, what rides on this
                # answer: the recommendation, a hold on a safety-critical
                # failure, or one line saying what it would add; this door
                # only merges what comes back.  Recommendation only; never
                # moves anything.  Best-effort: a fault there never costs
                # the completion.
                reroute_fields: dict = {}
                failure = session.failure
                if not success and alternative_printers and failure is not None:
                    try:
                        from kiln_pro.recovery.failure_rerouter import (
                            reroute_fields_for_failure,
                        )

                        reroute_fields = reroute_fields_for_failure(
                            original_printer_id=failure.printer_name,
                            original_job_id=failure.failure_id,
                            failure_type=failure.failure_type.value,
                            completion_pct=completion_pct_at_failure,
                            alternative_printers=alternative_printers,
                            material=failure.material_type,
                        )
                    except ImportError:
                        pass  # a plain install: no kiln-pro, nothing to merge
                    except Exception as exc:
                        _logger.debug(
                            "kiln-pro reroute evaluation failed (non-fatal): %s",
                            exc,
                        )

                response: dict = {
                    "success": True,
                    "session": session.to_dict(),
                    "message": f"Recovery session {session_id} {outcome}.",
                }
                if pro_outcome is not None:
                    response["pro_outcome_recorded"] = True
                response.update(reroute_fields)
                # Attach a quick inspection bundle so the user sees the
                # mesh that just finished its recovery cycle.  Closes the
                # recovery experience with visual confirmation.  Helper
                # is failure-closed: render failure decorates the
                # response but cannot disrupt the recovery completion.
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
            except MonitoringThresholdNotMet as exc:
                # The structured error includes the deficit
                # so the orchestrator/UI can prompt for additional checks.
                err = _srv._error_dict(str(exc), code=exc.to_dict()["code"])
                err.update(exc.to_dict())
                return err
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="INVALID_STATE")
            except Exception as exc:
                _logger.exception("Unexpected error in complete_print_recovery")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def get_recovery_statistics() -> dict:
            """Get historical recovery success rates and failure distribution.

            Returns aggregate statistics about all recovery attempts including
            success rates per strategy, failure type distribution, and active
            session counts.
            """
            import kiln.server as _srv
            from kiln.print_recovery import get_recovery_engine

            if err := _srv._check_auth("read"):
                return err
            try:
                engine = get_recovery_engine()
                stats = engine.get_recovery_statistics()
                return {"success": True, "statistics": stats}
            except Exception as exc:
                _logger.exception("Unexpected error in get_recovery_statistics")
                return _srv._error_dict(
                    f"Unexpected error: {exc}", code="INTERNAL_ERROR"
                )

        _logger.debug("Registered recovery, splitting, feedback, and AI recovery tools")


plugin = _RecoveryToolsPlugin()
