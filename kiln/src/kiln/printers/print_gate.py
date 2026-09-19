"""Pre-print impossibility gate — the deterministic "can this physically print?" check.

This gate blocks ONLY cases that are *certain* to fail or damage hardware:

  * the job's geometry exceeds the printer's build volume (nozzle crash /
    clipped print), in the job's own coordinates, and
  * the material's *minimum* required nozzle temperature exceeds the
    printer's rated hotend ceiling (it physically cannot melt the filament).

Everything *probabilistic* — overhangs, thin walls, bed-adhesion/warp risk,
abrasive nozzle wear — is the advisory layer's job and is deliberately NOT
gated here.  (Membership test, from the safety review: "is failure or damage
certain and deterministic?"  Yes -> gate.  No -> advise.)

Design rules:

  * **Never false-block.**  If fit or temperature cannot be determined
    (unknown bbox, unknown build volume, unknown material, unparseable
    file), the gate SOFT-PASSES.  A wrong block on a valid print destroys
    user trust in the gate — and a distrusted gate gets force-overridden on
    everything, including the real nozzle-crash.
  * **Reuse, don't reinvent.**  Fit reuses the trusted ``bed_fit``
    validators (which already soft-pass on uncertainty and fold in the
    incident-#0 homing check); temperature reuses ``design_intelligence``
    data.  No new geometry or thermal math lives here.
  * **Agent-resistant override.**  When blocked, the only escape is
    ``force_print_oversize`` — a ``confirm``-level tool, so an autonomous
    agent at default autonomy cannot self-approve it (``check_autonomy``
    denies it).  ``allow_oversize=True`` is only ever passed by code paths
    downstream of that human-gated confirmation.

Consumed by:

  * the universal print-time backstop — ``PrinterAdapter.start_print``
    (template method) calls this so NO entry point (scheduler, CLI,
    smart-reprint, recovery) can bypass it; and
  * the pre-slice helpful layer — ``slice_and_print`` calls this on the
    mesh, where auto-orient can still rescue an "exceeds bed" verdict.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from kiln.tiers_and_terms import upgrade_nudge_block

_logger = logging.getLogger(__name__)


def _base_read_print_file() -> Any:
    from kiln.printers.base import PrinterAdapter

    return PrinterAdapter.read_print_file

# bed_fit error codes that mean "we couldn't be sure" — these SOFT-PASS.
_SOFT_FIT_CODES = frozenset({"BBOX_UNKNOWN", "VOLUME_UNKNOWN", "UNKNOWN_FILE"})

# bed_fit error codes that are deterministic, will-crash-or-not-print blocks.
_HARD_FIT_CODES = frozenset({"EXCEEDS_BED", "OFF_BED_GEOMETRY"})


def _classify_file(job_path: str) -> str:
    """Return 'threemf' | 'gcode' | 'mesh' from a path's extension."""
    low = job_path.lower()
    if low.endswith(".3mf"):  # covers .gcode.3mf and plain .3mf
        return "threemf"
    if low.endswith((".gcode", ".gco", ".g")):
        return "gcode"
    return "mesh"


def _fit_verdict(
    job_path: str, printer_id: str | None,
) -> tuple[bool, str | None, str | None, dict[str, Any] | None]:
    """Geometry-fit half. Returns (blocked, code, message, detail).

    Soft-passes (blocked=False, code possibly set) on any uncertainty or
    error, per the never-false-block rule.
    """
    try:
        from kiln.printers import bed_fit
    except Exception:  # noqa: BLE001
        return (False, None, None, None)

    kind = _classify_file(job_path)
    try:
        if kind == "threemf":
            r = bed_fit.validate_3mf_for_printer(job_path, printer_id)
        elif kind == "gcode":
            r = bed_fit.validate_gcode_for_printer(job_path, printer_id)
        else:
            r = bed_fit.validate_mesh_for_printer(job_path, printer_id)
    except Exception:  # noqa: BLE001 — a gate that crashes must not block a print
        _logger.debug("print_gate: fit validator raised; soft-passing", exc_info=True)
        return (False, None, None, None)

    if not isinstance(r, dict) or r.get("ok"):
        return (False, None, None, r if isinstance(r, dict) else None)

    code = r.get("error_code")
    if code in _HARD_FIT_CODES:
        return (True, code, r.get("error_message"), r)
    # Homing-missing (incident #0) is also a deterministic crash — block it,
    # but only when it's a *real* parsed failure, not an unparseable file.
    if code and code not in _SOFT_FIT_CODES:
        return (True, code, r.get("error_message"), r)
    return (False, code, None, r)  # soft code -> pass


def _temp_verdict(
    printer_id: str | None, material_id: str | None,
) -> tuple[bool, str | None, str | None]:
    """Temperature-ceiling half. Blocks only when the material's MINIMUM
    required nozzle temp exceeds the printer's rated hotend ceiling — i.e.
    the printer physically cannot reach the temperature to extrude it.

    Reaching *between* min and optimal is a quality concern (advisory layer),
    NOT an impossibility, so it is not gated here.  Soft-passes on any
    missing data.
    """
    if not material_id or not printer_id:
        return (False, None, None)
    try:
        from kiln.design_intelligence import (
            get_material_profile,
            get_printer_design_profile,
        )

        printer = get_printer_design_profile(printer_id)
        mat = get_material_profile(material_id)
        if printer is None or mat is None:
            return (False, None, None)

        printer_max = getattr(printer, "max_hotend_temp_c", None)
        # Material min print temp lives in the .thermal dict:
        #   thermal["print_temp_range_c"] == [min, max]  (e.g. PC = [270, 310])
        thermal = getattr(mat, "thermal", None)
        rng = thermal.get("print_temp_range_c") if isinstance(thermal, dict) else None
        mat_min = rng[0] if rng and len(rng) >= 1 else None  # don't drop a legit 0

        # Strict ">" is deliberate: min == max means the printer can JUST reach
        # the material's floor — printable (zero headroom is a quality concern,
        # not an impossibility), so blocking it would be a false-block.  Only a
        # min that strictly exceeds the ceiling is physically impossible.
        # Explicit "is not None" (not truthiness) so a legitimate 0 isn't lost.
        if printer_max is not None and mat_min is not None and mat_min > printer_max:
            return (
                True,
                "MATERIAL_EXCEEDS_HOTEND",
                f"{material_id} needs at least {mat_min}°C at the nozzle, but "
                f"this printer's hotend is rated to {printer_max}°C — it "
                f"physically cannot reach the temperature to extrude this "
                f"material.",
            )
    except Exception:  # noqa: BLE001 — never let the gate crash a print
        _logger.debug("print_gate: temp check raised; soft-passing", exc_info=True)
    return (False, None, None)


def _suggestions(fit_code: str | None, temp_code: str | None) -> list[str]:
    out: list[str] = []
    if fit_code == "EXCEEDS_BED":
        out.append(
            "Pick a size that fits this printer, auto-orient/rotate it to fit, "
            "split it into joinable pieces, or print on a larger-bed printer."
        )
    elif fit_code == "OFF_BED_GEOMETRY":
        out.append(
            "Re-center the model on the bed (center_model_on_bed) before printing."
        )
    if temp_code == "MATERIAL_EXCEEDS_HOTEND":
        out.append(
            "Use a printer with a higher-temp hotend, or a material this "
            "printer can reach."
        )
    return out


def _maybe_enrich_block(
    verdict: dict[str, Any],
    *,
    job_path: str | None = None,
    printer_id: str | None = None,
    material_id: str | None = None,
) -> dict[str, Any] | None:
    """Pro+ enrichment for a blocked verdict — "here's exactly how to fix it".

    Lazy-imports kiln-pro's pre-print-fix engine, mirroring the
    degrade-to-None bridge pattern the slicer calibration overlay uses
    (``_maybe_overlay_calibration``).  Strictly ADDITIVE: it returns a NEW
    enrichment object (the printer's real usable envelope + an optimal split
    plan + a material swap) or ``None``; it never reads or changes the
    verdict's ``blocked`` / ``code`` / ``suggestions``.

    Free tier (kiln-pro absent, or its Pro+ check denies) -> ``None``.  Never
    raises -> a kiln-pro hiccup can never make the gate block more, so the
    never-false-block guarantee is untouched.  Pro tier lives entirely in
    kiln-pro; this public hook only knows "call it, attach what comes back."
    """
    try:
        from kiln_pro.print_fix.engine import enrich_block
    except Exception:  # noqa: BLE001
        # The enrichment is OPTIONAL.  Catch EVERYTHING, not just ImportError:
        # kiln-pro may be absent (free tier — the common case), OR its engine
        # module may fail to import for some other reason (broken module-level
        # init, a failed transitive dependency, a partially-initialized
        # circular import surfacing as AttributeError).  A non-ImportError that
        # escaped here would propagate out of the gate and, via start_print's
        # broad ``except Exception``, fail the never-false-block gate OPEN —
        # letting a genuinely-impossible print through.  So a broken kiln-pro
        # must degrade to "no enrichment", never to "no gate".
        return None
    try:
        return enrich_block(
            verdict,
            job_path=job_path,
            printer_id=printer_id,
            material_id=material_id,
        )
    except Exception:  # noqa: BLE001 — enrichment must NEVER break the gate
        _logger.debug("print_gate: enrichment hook failed; degrading to None", exc_info=True)
        return None


def evaluate_pre_print_gate(
    job_path: str | None,
    printer_id: str | None,
    *,
    material_id: str | None = None,
    allow_oversize: bool = False,
) -> dict[str, Any]:
    """Deterministic 'is this print physically possible?' verdict.

    :param job_path: Path to the mesh, gcode, or .3mf about to print.
    :param printer_id: Target printer model id (e.g. ``"bambu_a1"``).
    :param material_id: Material the job will run, if known.
    :param allow_oversize: Set only by code downstream of a human-gated
        ``force_print_oversize`` confirmation.  When True, a blocking
        verdict is converted to an allowed-but-flagged verdict.
    :returns: ``{"ok": bool, "blocked": bool, ...}``.  ``blocked=False``
        whenever the print may proceed (including all soft-pass / undetermined
        cases and honored overrides).
    """
    if not job_path or not os.path.isfile(job_path):
        # Nothing local to inspect — soft-pass (the slice/upload layer is the
        # primary enforcement point; the backstop catches what it can see).
        return {"ok": True, "blocked": False, "reason": "no local job to inspect — soft-pass"}

    fit_blocked, fit_code, fit_msg, fit_detail = _fit_verdict(job_path, printer_id)
    temp_blocked, temp_code, temp_msg = _temp_verdict(printer_id, material_id)

    if not (fit_blocked or temp_blocked):
        return {
            "ok": True,
            "blocked": False,
            "reason": "pre-print gate passed",
            "fit": fit_detail,
        }

    reason = " ".join(m for m in (fit_msg, temp_msg) if m)
    code = fit_code or temp_code

    if allow_oversize:
        _logger.warning("print_gate: OVERRIDE engaged for %s (%s)", printer_id, code)
        return {
            "ok": True,
            "blocked": False,
            "overridden": True,
            "override_code": code,
            "reason": f"OVERRIDE (human-confirmed): {reason}",
            "fit": fit_detail,
        }

    block = {
        "ok": False,
        "blocked": True,
        "code": code,
        "reason": reason,
        "suggestions": _suggestions(fit_code, temp_code),
        "override_hint": (
            "This is a hard physical limit, not a warning. To print anyway "
            "(e.g. you are sending this file to a different printer), a human "
            "must call force_print_oversize — an autonomous agent cannot "
            "self-approve it."
        ),
        "fit": fit_detail,
    }
    # Pro+ additive enrichment — attached as a NEW key only.  None on free
    # tier; the block above is already complete and unchanged either way.
    block["enrichment"] = _maybe_enrich_block(
        block, job_path=job_path, printer_id=printer_id, material_id=material_id,
    )
    return block


def check_material_temp(
    printer_id: str | None, material_id: str | None,
) -> dict[str, Any] | None:
    """Slice-layer temperature-ceiling check (public, free).

    Returns a block verdict when the material's *minimum* nozzle temperature
    exceeds the printer's rated hotend ceiling — i.e. the printer physically
    cannot melt it — or ``None`` to allow (including every soft-pass /
    undetermined case).  Used by the slice path, where the material is known.

    Reuses the exact ``_temp_verdict`` the print-time backstop uses (one source
    of truth) and only ever reads PUBLIC datasheet / safety-floor data (printer
    rated max-temp + material safety-floor temp range) — never the curated
    device-intelligence SME, which stays in the kiln-pro overlay.
    """
    mid = (material_id or "").strip().lower() or None
    blocked, code, msg = _temp_verdict(printer_id, mid)
    if not blocked:
        return None
    block = {
        "ok": False,
        "blocked": True,
        "code": code,
        "reason": msg,
        "suggestions": _suggestions(None, code),
        "override_hint": (
            "This is a hard physical limit. To slice/print anyway (e.g. you are "
            "targeting a different printer), a human can call "
            "force_print_oversize — an autonomous agent cannot self-approve it."
        ),
    }
    # Pro+ additive enrichment — the material swap that lets this printer
    # print the part.  None on free tier; verdict unchanged either way.
    block["enrichment"] = _maybe_enrich_block(
        block, printer_id=printer_id, material_id=mid,
    )
    return block


# ---------------------------------------------------------------------------
# Human-gated override state
#
# force_print_oversize (a 'confirm'-level tool — see tool_safety.json) calls
# grant_oversize_override() AFTER the autonomy/confirmation layer has proven a
# human is in the loop.  The backstop reads _override_active().  Grants are
# short-lived and per-printer so a single override can't silently whitelist
# every future print.
# ---------------------------------------------------------------------------

import time  # noqa: E402  (kept local to the override section)

_DEFAULT_OVERRIDE_TTL_S = 300.0
_oversize_grants: dict[str, float] = {}


def grant_oversize_override(
    printer_id: str | None, *, ttl_seconds: float = _DEFAULT_OVERRIDE_TTL_S,
) -> None:
    """Allow the next oversize/over-temp print on *printer_id* for *ttl_seconds*.

    Called only from ``force_print_oversize`` after the confirm-level gate
    has verified a human authorised it.
    """
    _oversize_grants[(printer_id or "").lower()] = time.monotonic() + ttl_seconds


def _override_active(printer_id: str | None) -> bool:
    expiry = _oversize_grants.get((printer_id or "").lower())
    return expiry is not None and time.monotonic() < expiry


def _resolve_printer_model(adapter: Any) -> str | None:
    for attr in ("printer_id", "model", "printer_model", "_model"):
        v = getattr(adapter, attr, None)
        if isinstance(v, str) and v:
            return v
    try:
        from kiln.printer_model_resolver import resolve_printer_model

        return resolve_printer_model()
    except Exception:  # noqa: BLE001
        return None


def _resolve_local_job(file_name: str, kwargs: dict[str, Any]) -> str | None:
    """Find a LOCAL gcode/3mf/mesh to inspect, or None (then we soft-pass)."""
    for k in ("source_path", "local_path", "local_file_path", "gcode_path", "file_path", "threemf_path"):
        v = kwargs.get(k)
        if isinstance(v, str) and os.path.isfile(v):
            return v
    if isinstance(file_name, str) and os.path.isfile(file_name):
        return file_name
    return None


class _ReadBackFailed(Exception):
    """A backend that CAN read a file back could not, this time.

    Carries the cause in plain words.  Distinct from a backend that has no
    read-back at all (``read_print_file`` returns ``None`` by design), which
    is not a failure and soft-passes.
    """


#: How many times the printer's copy is asked for before the read counts as
#: failed.  One retry: a Bambu FTPS server answers 550 to a first LIST often
#: enough that a single attempt would refuse honest starts for nothing, and
#: a second attempt a moment later is cheap.  More than that is the gate
#: hiding a printer that is really unreachable.
_READ_BACK_ATTEMPTS = 2


def _fetch_printer_copy(adapter: Any, file_name: str) -> str | None:
    """Read *file_name* back from the printer into a temp file.

    The start-by-name door.  2026-09-16, Bambu A1: a ``.gcode.3mf`` that had
    sat on the SD card since April -- raw PrusaSlicer output wrapped with no
    start block -- was started by name.  The upload door refuses that file
    class (incident #0); this door had nothing local to inspect and
    soft-passed.  The printer heated, never homed, never purged, and rolled
    its first extrusion into a log under the nozzle.  Now the gate asks the
    adapter for the printer's own copy and runs the SAME validators on it
    (:func:`evaluate_pre_print_gate`), so the two doors judge one file the
    same way.

    Three outcomes, and they are not the same:

    * ``None`` -- this backend has no read-back (``read_print_file`` is the
      base default).  Not a failure; the gate soft-passes as it always did.
    * a temp path -- the printer's bytes, for the caller to judge and delete.
    * :class:`_ReadBackFailed` -- the backend CAN read back and could not,
      after :data:`_READ_BACK_ATTEMPTS` tries (transfer error, no such file,
      empty file, over the size cap, or nowhere to stage it).  A file Kiln
      could not look at is a file Kiln cannot vouch for, so the caller
      refuses -- with the human override named, because the person may know
      more than the gate (ruled 2026-09-16: refuse beats a quiet pass; a
      refusal costs one retry, a pass costs incident #0).
    """
    reader = getattr(adapter, "read_print_file", None)
    if not callable(reader):
        return None
    if getattr(reader, "__func__", None) is _base_read_print_file():
        return None  # the base default: this backend has no read-back at all
    last: str = ""
    data: bytes | None = None
    for attempt in range(_READ_BACK_ATTEMPTS):
        try:
            data = reader(file_name)
        except Exception as exc:  # noqa: BLE001 -- the cause goes in the refusal
            last = str(exc)
            _logger.debug(
                "print_gate: read-back of %s failed (attempt %d/%d): %s",
                file_name, attempt + 1, _READ_BACK_ATTEMPTS, exc,
            )
            continue
        if data is None:
            return None  # the backend says it cannot read this one back
        if data:
            break
        last = "the printer returned an empty file"
    else:
        raise _ReadBackFailed(last or "the printer returned an empty file")
    if not data:
        raise _ReadBackFailed(last or "the printer returned an empty file")
    import tempfile

    low = str(file_name).lower()
    suffix = ".gcode.3mf" if low.endswith(".gcode.3mf") else os.path.splitext(low)[1] or ".gcode"
    try:
        with tempfile.NamedTemporaryFile(prefix="kiln-printer-copy-", suffix=suffix, delete=False) as fh:
            fh.write(data)
            return fh.name
    except OSError as exc:
        raise _ReadBackFailed(f"could not stage the printer's copy locally: {exc}") from exc


def _resolve_material(kwargs: dict[str, Any]) -> str | None:
    # kwargs-only on purpose: the backstop must stay cheap + side-effect-free
    # (no network call to the printer).  The slice layer does the authoritative
    # material-aware temp check where the material is already known.
    for k in ("material", "material_id", "expected_material"):
        v = kwargs.get(k)
        if isinstance(v, str) and v.strip():
            # Normalize to the catalog key shape (lowercased, trimmed) so AMS
            # tray strings like "PETG " / "ABS" resolve to 'petg' / 'abs'.
            # Unknown ids still soft-pass (get_material_profile -> None) — safe.
            return v.strip().lower()
    return None


def _is_resume_print(file_name: str, kwargs: dict[str, Any]) -> bool:
    """True for a resume-mode continuation (mid-print decoration / recovery).

    A resume print's geometry was already validated when the original job was
    started, it is already homed, and its resume 3MF strips the start gcode (no
    G28) and cannot be re-oriented.  Re-gating it could ONLY false-block a valid
    continuation, so the backstop skips it.  Detected via the explicit flag or
    the conventional resume filenames produced by decorate_during_print /
    revert_mid_print.
    """
    if kwargs.get("resume_from_paused"):
        return True
    name = (file_name or "").lower()
    return "_resume_" in name or name.startswith(
        ("transformed_resume", "original_resume")
    )


def _concurrent_fleet_verdict(adapter: Any) -> dict[str, Any] | None:
    """Block a print that would run a SECOND machine at once below Business.

    Kiln's fleet tier sells *parallelism*, not possession.  Owning two
    printers and using them one at a time is honest single-machine use and
    stays free forever; driving two at once is a fleet, and a fleet is
    Business.  Gating here — the one chokepoint every entry point reaches
    (tool, CLI, scheduler, pipelines, recovery) — is what makes that true
    at every door, instead of only at ``register_printer`` where the cap
    used to live while the CLI and config.yaml loaded printers uncapped
    (2026-07-27).

    This function is the START half.  The commands that operate a machine
    ALREADY running — status, pause, resume, cancel, temperatures,
    emergency stop — are sibling adapter methods that never pass through
    here, so gating only this one sold a machine's worth of parallelism
    while leaving every other way to drive a second machine open.  The
    other half lives in ``kiln.printers.engagement``, which records the one
    machine Kiln is driving and is consulted by all of them.

    Deliberately NOT gated anywhere: registering, listing and discovering
    printers.  Owning machines is free at every tier and always has been;
    what the fleet tier sells is running them at the same time.

    The engaged machine keeps everything, emergency stop included, and the
    engagement is releasable, so a user can always hand a machine back and
    move Kiln to another one.  Kiln is not the safety system it once
    claimed to be here: thermal runaway protection lives in the printer's
    own firmware, the machine has its own controls, and a refusal on a
    machine Kiln is not driving says so and points at them.

    Soft-passes on anything it cannot prove: unknown tier, unreachable
    peers, any error.  A network hiccup must never block a print.
    """
    try:
        from kiln.registry import get_registry

        registry = get_registry()
        # Fast path — one machine can never be a fleet.  Costs no network,
        # which is what nearly every install pays here.
        if registry.count <= 1:
            return None

        try:
            from kiln.licensing import get_tier, max_printers_for_tier

            cap = max_printers_for_tier(get_tier())
        except Exception:
            cap = 1  # free-tier fallback: kiln-pro absent
        if cap is None or cap <= 0 or registry.count <= cap:
            return None

        # Which OTHER machines are busy right now?  Queried in parallel with
        # the registry's own bounded-timeout helper; unreachable peers read
        # as "not busy" so an offline printer can't wedge a valid print.
        this_machine = _machine_id(adapter)
        busy: list[str] = []
        for name, state in _peer_states(registry, this_machine).items():
            # `is_occupied` rather than a list of states, so a peer whose
            # reading has gone STALE is counted from what it was last seen
            # doing instead of dropping out of the count entirely.
            if getattr(state, "is_occupied", False) is True:
                busy.append(name)

        if len(busy) < cap:
            return None

        others = ", ".join(sorted(busy)[:3])
        verdict = {
            "blocked": True,
            "reason": (
                f"Kiln runs one printer at a time on this plan, and "
                f"{others} is already printing."
            ),
            "override_hint": (
                "Wait for it to finish, or start it after this one. "
                "Kiln Business runs your printers in parallel — "
                "https://kiln3d.com/pricing"
            ),
            "code": "TIER_CONCURRENT_PRINT_LIMIT",
        }
        # The structured twin of the two sentences above, for a surface that
        # renders rather than prints.  Additive: the verdict, the waiting path
        # and every safety behaviour are untouched, and this fires ONLY on the
        # tier refusal — never on a physical block, and never on the control
        # paths, which do not reach this function at all.
        verdict.setdefault(
            "upgrade_nudge",
            upgrade_nudge_block(
                variant="concurrent_queue",
                tier="business",
                feature="Coordinated multi-printer queue",
                headline=(
                    "Coordinate this job with the printer that is already "
                    "running."
                ),
                outcome_preview=(
                    "Kiln Business would route the queue across the available "
                    "machines and start eligible work in parallel."
                ),
                free_included=(
                    "The job has not started; wait and run it next."
                ),
                moment="resource_threshold",
            ),
        )
        return verdict
    except Exception:  # noqa: BLE001 — a licensing check never breaks a print
        _logger.debug("concurrent-fleet gate soft-passed", exc_info=True)
        return None


def _machine_id(adapter: Any) -> str:
    """Fingerprint for *adapter*, or ``""`` when the registry can't say."""
    try:
        from kiln.registry import machine_fingerprint

        return machine_fingerprint(adapter)
    except Exception:
        return ""


def _peer_states(registry: Any, this_machine: str) -> dict[str, Any]:
    """State of every registered machine EXCEPT this one, aliases collapsed.

    Returns the whole :class:`~kiln.printers.base.PrinterState`, not just its
    status word: a stale reading needs ``last_known_state`` to be judged, and
    a caller handed only ``STALE`` would have to guess.
    """
    from kiln.printers.engagement import internal_read
    from kiln.registry import machine_fingerprint

    out: dict[str, Any] = {}
    for name in registry.list_machines():
        try:
            peer = registry.get(name)
            if this_machine and machine_fingerprint(peer) == this_machine:
                continue
            # Kiln asking about its own peers, not a user commanding one --
            # without this the engagement gate would refuse the very reads
            # this gate is made of, then read the refusal as an answer.
            with internal_read():
                out[name] = peer.get_state()
        except Exception:
            continue  # unreachable peer reads as not-busy, never blocks
    return out


def _read_back_refusal(
    file_name: str, printer_id: str | None, cause: str, override: bool,
) -> dict[str, Any] | None:
    """The verdict for a file Kiln could not read back: refuse, or honour the
    single-use human override the way every other block does."""
    name = os.path.basename(str(file_name))
    reason = (
        f"Could not verify {name} before starting it: Kiln reads a file back from "
        f"the printer to check it has a homing sequence, and the read failed "
        f"({cause}). A file Kiln cannot look at is one it cannot vouch for, so it "
        "is not starting it. Retry in a moment, or start the file from the "
        "printer's own screen."
    )
    if override:
        _logger.warning("print_gate: OVERRIDE engaged for %s (READ_BACK_FAILED)", printer_id)
        _oversize_grants.pop((printer_id or "").lower(), None)
        return None
    return {
        "ok": False,
        "blocked": True,
        "code": "READ_BACK_FAILED",
        "reason": reason,
        "inspected": "none",
        "suggestions": [
            "Retry the print in a moment (the printer's file server may have been busy).",
            "Start the file from the printer's own screen if you know it is a complete Kiln-sliced job.",
            "Upload the file again through Kiln so it is checked on the way in.",
        ],
        "override_hint": (
            "If you know this file is safe, the single-use human override, "
            "force_print_oversize, covers this block too: a human calls it for this "
            "printer and re-issues the print once; an autonomous agent cannot "
            "self-approve it."
        ),
    }


# ---------------------------------------------------------------------------
# The one file that must NOT home: kiln-pro's same-bed retry
#
# A same-bed retry prints the failed part again, shifted into a clear zone of
# the same bed, with the failed part still sitting beside it.  Its startup
# deliberately emits no G28, no bed scan and no purge line -- any of those
# would drive the head or the probe into the remnant.  The no-homing rule
# above would refuse it every time, so the gate takes that one file class on
# a contract instead, and checks the contract against the MACHINE, live, in
# this order:
#
#   (a) the file carries Kiln's same-bed safe-startup header, whole;
#   (b) the machine being started is the machine it was baked for;
#   (c) no emergency latch on that machine;
#   (d) the machine is not printing or paused;
#   (e) the firmware, asked now, reports X, Y and Z homed.
#
# Every other file keeps the rule unchanged.  A cached "homed" is a guess,
# and a backend that cannot answer (e) at all refuses with the measurement
# sentence rather than starting next to a part on the bed.
# ---------------------------------------------------------------------------

SAME_BED_HEADER_START = "; --- Kiln same-bed retry safe startup ---"
SAME_BED_HEADER_END = "; --- End Kiln same-bed retry safe startup ---"
SAME_BED_MOTION_CONTRACT = "no_xyz_motion"
SAME_BED_MACHINE_KEY = "baked_for_machine"
SAME_BED_REQUIRED_KEYS = (
    "safe_startup_policy_id",
    "safe_startup_policy_version",
    "safe_startup_motion_contract",
    SAME_BED_MACHINE_KEY,
)
#: The header is the file's first thing; a start marker further down than
#: this is not Kiln's prologue.
_SAME_BED_HEADER_SCAN_LINES = 64
#: And the prologue itself is short; a header that never ends is not one.
_SAME_BED_HEADER_MAX_LINES = 256

HOMED_UNKNOWN_SENTENCE = (
    "this printer cannot report whether it is homed; same-bed retry is not "
    "supported here — run the homed-state measurement for this backend, or "
    "reslice and print normally"
)


def same_bed_machine_id(adapter: Any) -> str:
    """The identity a same-bed retry is bound to, or ``""`` when there is none.

    The hardware fingerprint first (``family:serial:…`` / ``family:host:…``,
    from :func:`kiln.printers.engagement.machine_id`): it survives the
    registry aliasing one machine under two names (``"default"`` and its
    config name) and a rename between the bake and the start.  A machine
    that reports neither serial nor address falls back to the name its
    owner registered it under, which both the baking door and the starting
    door can read off the same adapter.  Neither -> ``""``, and a retry
    cannot be bound to a machine Kiln cannot tell apart from another.

    kiln-pro writes this value into the artifact (``baked_for_machine``) and
    the gate recomputes it from the adapter being started; the two must
    agree, so there is exactly one way to compute it.
    """
    try:
        from kiln.printers.engagement import machine_id

        fingerprint = machine_id(adapter)
    except Exception:  # noqa: BLE001
        fingerprint = ""
    if fingerprint:
        return fingerprint
    registered = getattr(adapter, "_kiln_registered_name", None)
    return registered if isinstance(registered, str) and registered else ""


def _gcode_head(path: str, max_lines: int) -> list[str] | None:
    """The first *max_lines* of the job's gcode text, or ``None`` if unreadable.

    Reads the plate gcode of a ``.3mf`` the way ``bed_fit`` does, so the two
    look at the same bytes.
    """
    low = path.lower()
    try:
        if low.endswith(".3mf"):
            import zipfile

            with zipfile.ZipFile(path) as zf:
                names = [
                    n for n in zf.namelist()
                    if n.startswith("Metadata/plate_") and n.endswith(".gcode")
                ]
                if not names:
                    return None
                text = zf.read(names[0]).decode("utf-8", errors="replace")
            return text.splitlines()[:max_lines]
        out: list[str] = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                out.append(line.rstrip("\n"))
                if len(out) >= max_lines:
                    break
        return out
    except Exception:  # noqa: BLE001
        return None


def read_same_bed_contract(path: str) -> dict[str, str] | None:
    """The ``; key: value`` lines of the same-bed safe-startup header, or
    ``None`` when the file carries no such header at all.

    A header that is present but incomplete still comes back (with its keys
    missing), so the refusal can name the missing line rather than say
    "no header".
    """
    head = _gcode_head(path, _SAME_BED_HEADER_MAX_LINES)
    if not head:
        return None
    start = next(
        (i for i, line in enumerate(head[:_SAME_BED_HEADER_SCAN_LINES])
         if line.strip() == SAME_BED_HEADER_START),
        None,
    )
    if start is None:
        return None
    fields: dict[str, str] = {}
    for line in head[start + 1:]:
        stripped = line.strip()
        if stripped == SAME_BED_HEADER_END:
            break
        if not stripped.startswith(";"):
            continue
        body = stripped[1:].strip()
        key, sep, value = body.partition(":")
        if sep and key.strip() and " " not in key.strip():
            fields.setdefault(key.strip(), value.strip())
    return fields


def _latch_status(printer_name: str) -> dict[str, Any] | None:
    """The emergency coordinator's latch status for *printer_name* -- the
    same read ``kiln.server`` makes before any control tool runs."""
    try:
        from kiln.emergency import get_emergency_coordinator

        return get_emergency_coordinator().get_latch_status(printer_name)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("emergency latch lookup failed for %s: %s", printer_name, exc)
        return None


def evaluate_same_bed_retry(adapter: Any, job_path: str) -> dict[str, Any]:
    """Judge a no-homing file as a same-bed retry, against the machine, live.

    Conditions (a)-(e) in the module comment above, checked in that order;
    the first one that fails is the verdict, named with its remedy, and the
    later ones are not read (no machine round-trip for a file that is not
    even a retry).  ``blocked=False`` with ``accepted=True`` when all hold.

    ``homed_evidence`` is filled the moment (e) is consulted -- the axes the
    firmware reported, the field they came from, and when -- on the accepted
    verdict and on the (e) refusals alike.  The audit line repeats it.
    """
    name = os.path.basename(str(job_path))
    contract = read_same_bed_contract(job_path)
    machine = same_bed_machine_id(adapter)
    verdict: dict[str, Any] = {
        "ok": False,
        "blocked": True,
        "accepted": False,
        "code": None,
        "condition": None,
        "reason": "",
        "contract": contract,
        "machine": machine,
        "homed_evidence": None,
    }

    def refuse(code: str, condition: str, reason: str) -> dict[str, Any]:
        verdict.update(code=code, condition=condition, reason=reason)
        _audit_same_bed(verdict, name)
        return verdict

    # (a) the contract, whole.
    remedy_a = (
        " Re-bake the retry through auto_recover on this printer, or reslice "
        "the part and print it normally (a normal print homes first)."
    )
    if contract is None:
        return refuse(
            "SAME_BED_RETRY_NO_CONTRACT", "contract",
            f"{name} has no homing before its first move and carries no Kiln "
            f"same-bed retry safe-startup header, so it is not a same-bed retry."
            + remedy_a,
        )
    missing = [k for k in SAME_BED_REQUIRED_KEYS if not contract.get(k)]
    if missing:
        return refuse(
            "SAME_BED_RETRY_NO_CONTRACT", "contract",
            f"{name} carries an incomplete Kiln same-bed retry safe-startup "
            f"header: missing {', '.join(missing)}." + remedy_a,
        )
    if contract["safe_startup_motion_contract"] != SAME_BED_MOTION_CONTRACT:
        return refuse(
            "SAME_BED_RETRY_NO_CONTRACT", "contract",
            f"{name}'s safe_startup_motion_contract is "
            f"'{contract['safe_startup_motion_contract']}', not "
            f"'{SAME_BED_MOTION_CONTRACT}'; only a prologue that moves nothing "
            f"may skip homing." + remedy_a,
        )

    # (b) this machine is the one it was baked for.
    baked_for = contract[SAME_BED_MACHINE_KEY]
    if not machine:
        return refuse(
            "SAME_BED_RETRY_WRONG_MACHINE", "machine",
            f"Kiln cannot identify this printer (it reports no serial, no "
            f"address and no registered name), so it cannot confirm {name} "
            f"was baked for it (baked for {baked_for})." + remedy_a,
        )
    if baked_for != machine:
        return refuse(
            "SAME_BED_RETRY_WRONG_MACHINE", "machine",
            f"{name} was baked for {baked_for} and is being started on "
            f"{machine}; the bed it was planned around is not this one."
            + remedy_a,
        )

    # (c) no emergency latch.
    printer_name = _registered_name(adapter)
    status = _latch_status(printer_name)
    if status and bool(status.get("latched")):
        blockers = status.get("critical_interlocks_pending") or []
        pending = (
            " Critical interlocks pending: " + ", ".join(str(x) for x in blockers) + "."
            if blockers else ""
        )
        return refuse(
            "SAME_BED_RETRY_EMERGENCY_LATCHED", "emergency_latch",
            f"Emergency latch is active for printer '{printer_name}'.{pending} "
            f"Resolve hazards, acknowledge, then clear via "
            f"clear_emergency_stop(), and start the retry again.",
        )

    # (d) not printing or paused.
    try:
        state = adapter.get_state()
    except Exception as exc:  # noqa: BLE001
        return refuse(
            "SAME_BED_RETRY_STATE_READ_FAILED", "printer_state",
            f"could not read the printer's state before the retry ({exc}); "
            f"retry in a moment, or reslice and print normally.",
        )
    from kiln.printers.base import PrinterStatus, effective_state_of

    run_state = effective_state_of(state)
    if run_state in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
        return refuse(
            "SAME_BED_RETRY_PRINTER_BUSY", "printer_state",
            f"the printer reports it is {run_state.value}; a same-bed retry "
            f"starts only on an idle machine. Wait for that job to end, or "
            f"cancel it, then start the retry again.",
        )

    # (e) the firmware, asked now, reports X, Y and Z homed.
    from datetime import datetime, timezone

    try:
        axes = adapter.homed_axes_now()
    except Exception as exc:  # noqa: BLE001
        return refuse(
            "SAME_BED_RETRY_HOMED_READ_FAILED", "homed_axes",
            f"could not read which axes are homed from the printer ({exc}); "
            f"retry in a moment, or reslice and print normally.",
        )
    try:
        field = adapter.homed_axes_field()
    except Exception:  # noqa: BLE001
        field = None
    verdict["homed_evidence"] = {
        "axes": None if axes is None else sorted(str(a).lower() for a in axes),
        "field": field,
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": getattr(adapter, "name", None) or type(adapter).__name__,
    }
    if axes is None:
        return refuse(
            "SAME_BED_RETRY_HOMED_UNKNOWN", "homed_axes",
            f"{name} was not started: {HOMED_UNKNOWN_SENTENCE}.",
        )
    reported = set(verdict["homed_evidence"]["axes"])
    not_homed = sorted({"x", "y", "z"} - reported)
    if not_homed:
        return refuse(
            "SAME_BED_RETRY_NOT_HOMED", "homed_axes",
            f"the printer reports homed axes: {','.join(sorted(reported)) or 'none'} "
            f"— {','.join(not_homed)} not homed, and a same-bed retry cannot home "
            f"with the failed part on the bed. Clear the bed, home the printer "
            f"(home_axes), then reslice and print normally.",
        )

    verdict.update(
        ok=True, blocked=False, accepted=True, code="SAME_BED_RETRY_ACCEPTED",
        condition=None,
        reason=(
            f"same-bed retry accepted on {machine}: the file carries Kiln's "
            f"no-motion startup contract and the printer reports x,y,z homed."
        ),
    )
    _audit_same_bed(verdict, name)
    return verdict


def _registered_name(adapter: Any) -> str:
    registered = getattr(adapter, "_kiln_registered_name", None)
    if isinstance(registered, str) and registered:
        return registered
    return getattr(adapter, "name", "") or "printer"


def _audit_same_bed(verdict: dict[str, Any], name: str) -> None:
    """One line per decision, with the evidence the decision rested on."""
    evidence = verdict.get("homed_evidence") or {}
    axes = evidence.get("axes")
    _logger.info(
        "print_gate: same-bed retry %s on %s (%s): axes=%s field=%s read_at=%s code=%s",
        "accepted" if verdict.get("accepted") else "refused",
        verdict.get("machine") or "-",
        name,
        ",".join(axes) if axes else ("none" if axes is not None else "-"),
        evidence.get("field") or "-",
        evidence.get("read_at") or "-",
        verdict.get("code"),
    )


def _same_bed_or_original(
    adapter: Any,
    job_path: str,
    original: dict[str, Any],
    printer_id: str | None,
    material_id: str | None,
) -> dict[str, Any]:
    """A no-homing block, re-judged as a same-bed retry when the file claims
    to be one; every other file gets *original* back untouched."""
    if read_same_bed_contract(job_path) is None:
        return original
    # The contract excuses the missing homing, nothing else: a material the
    # hotend cannot reach is still impossible, and is answered before the
    # machine is asked anything.
    temp_blocked, temp_code, temp_msg = _temp_verdict(printer_id, material_id)
    if temp_blocked:
        block = dict(original)
        block.update(code=temp_code, reason=temp_msg, suggestions=_suggestions(None, temp_code))
        return block
    verdict = evaluate_same_bed_retry(adapter, job_path)
    if verdict.get("accepted"):
        return {
            "ok": True,
            "blocked": False,
            "reason": verdict["reason"],
            "same_bed_retry": verdict,
            "homed_evidence": verdict["homed_evidence"],
            "fit": original.get("fit"),
        }
    return {
        "ok": False,
        "blocked": True,
        "code": verdict["code"],
        "reason": verdict["reason"],
        "condition": verdict["condition"],
        "homed_evidence": verdict["homed_evidence"],
        "same_bed_retry": verdict,
        "fit": original.get("fit"),
    }


def run_adapter_gate(
    adapter: Any, file_name: str, kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """Universal print-time backstop, called from ``PrinterAdapter.start_print``.

    Returns a *blocked* verdict dict (caller refuses the print) or ``None`` to
    allow.  Soft-passes (``None``) whenever the model, local file, or material
    can't be determined, and for resume-mode continuations — the backstop only
    refuses what it can prove impossible.
    """
    # Resume-mode prints are committed continuations — never re-gate them.
    if _is_resume_print(file_name, kwargs):
        return None

    # Fleet concurrency (tier), before the physical checks: it needs no file
    # parsing and answers instantly for the single-machine case.
    if fleet_blocked := _concurrent_fleet_verdict(adapter):
        return fleet_blocked

    printer_id = _resolve_printer_model(adapter)
    job = _resolve_local_job(file_name, kwargs)
    override = _override_active(printer_id)
    # No local copy: the file is being started by name off the printer's own
    # storage.  Read it back and inspect THAT -- see _fetch_printer_copy.
    fetched: str | None = None
    if job is None:
        try:
            fetched = _fetch_printer_copy(adapter, file_name)
        except _ReadBackFailed as exc:
            return _read_back_refusal(file_name, printer_id, str(exc), override)
    inspected = job if job is not None else fetched
    material_id = _resolve_material(kwargs)
    try:
        verdict = evaluate_pre_print_gate(
            inspected,
            printer_id,
            material_id=material_id,
            allow_oversize=override,
        )
        # A file refused for moving before it homes may be kiln-pro's
        # same-bed retry, the one file that must not home; that claim is
        # judged against the machine, live -- see evaluate_same_bed_retry.
        # Any other file keeps this verdict exactly as it was.
        if inspected and verdict.get("blocked") and verdict.get("code") == "NO_HOMING_SEQUENCE":
            verdict = _same_bed_or_original(adapter, inspected, verdict, printer_id, material_id)
        if fetched and not verdict.get("blocked") and str(printer_id or "").lower().startswith("bambu"):
            # Same rule the upload door applies, on the printer's own copy: a
            # plate its screen cannot draw is not started by name either.
            from kiln.printers.bambu_3mf import bambu_archive_problems

            problems = bambu_archive_problems(fetched, name=str(file_name or ""))
            if problems:
                verdict = {
                    "blocked": True,
                    "reason": (
                        f"{os.path.basename(str(file_name))} is not ready for the printer: "
                        + "; ".join(problems) + "."
                    ),
                }
        elif fetched and not verdict.get("blocked"):
            # The same rule for every other family, on the printer's own
            # copy: a file whose surface would show no preview and no weight
            # is not started by name either.  Soft-passes for a backend with
            # no established surface -- see kiln.printers.gcode_complete.
            from kiln.printers.gcode_complete import family_for_adapter, gcode_problems

            problems = gcode_problems(
                fetched, family_for_adapter(adapter), name=str(file_name or ""),
            )
            if problems:
                verdict = {
                    "blocked": True,
                    "reason": (
                        f"{os.path.basename(str(file_name))} is not ready for the printer: "
                        + "; ".join(problems) + "."
                    ),
                }
    finally:
        if fetched:
            with contextlib.suppress(OSError):
                os.unlink(fetched)
    if fetched:
        verdict["inspected"] = "printer_copy"
        if verdict.get("blocked"):
            verdict["reason"] = (
                f"{verdict.get('reason', '')} This is the printer's own copy of "
                f"{os.path.basename(str(file_name))}, read back from its storage: the file "
                "was not made by a current Kiln and is not safe to start. Delete it from "
                "the printer (delete_file) and re-slice through Kiln."
            ).strip()
    # Single-use override: a human confirmation authorises ONE otherwise-blocked
    # print, not a time window of them.  Consume the grant the instant it's used
    # (verdict.overridden is set only when the override actually rescued a block).
    if override and verdict.get("overridden"):
        _oversize_grants.pop((printer_id or "").lower(), None)
    return verdict if verdict.get("blocked") else None
