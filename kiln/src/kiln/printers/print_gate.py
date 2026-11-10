"""Pre-print impossibility gate — the deterministic "can this physically print?" check."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from typing import Any

from kiln.nozzle_size_check import MISMATCH_CODE as NOZZLE_SIZE_MISMATCH
from kiln.printer_model_check import MISMATCH_CODE as PRINTER_MODEL_MISMATCH
from kiln.tiers_and_terms import upgrade_nudge_block
from kiln.untrusted_zip import UntrustedZipFile

_logger = logging.getLogger(__name__)


def _base_read_print_file() -> Any:
    from kiln.printers.base import PrinterAdapter

    return PrinterAdapter.read_print_file

# bed_fit error codes that mean "we couldn't be sure" — these SOFT-PASS.
_SOFT_FIT_CODES = frozenset({"BBOX_UNKNOWN", "VOLUME_UNKNOWN", "UNKNOWN_FILE"})

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
    job_path: str, printer_id: str | None, *, bed: Any = None,
) -> tuple[bool, str | None, str | None, dict[str, Any] | None]:
    """Geometry-fit half."""
    try:
        from kiln.printers import bed_fit
    except Exception:  # noqa: BLE001
        return (False, None, None, None)

    kind = _classify_file(job_path)
    try:
        if kind == "threemf":
            r = bed_fit.validate_3mf_for_printer(job_path, printer_id, bed=bed)
        elif kind == "gcode":
            r = bed_fit.validate_gcode_for_printer(job_path, printer_id, bed=bed)
        else:
            r = bed_fit.validate_mesh_for_printer(job_path, printer_id, bed=bed)
    except Exception:  # noqa: BLE001 — a gate that crashes must not block a print
        _logger.debug("print_gate: fit validator raised; soft-passing", exc_info=True)
        return (False, None, None, None)

    if not isinstance(r, dict) or r.get("ok"):
        return (False, None, None, r if isinstance(r, dict) else None)

    code = r.get("error_code")
    if code in _HARD_FIT_CODES:
        return (True, code, r.get("error_message"), r)
    if code and code not in _SOFT_FIT_CODES:
        return (True, code, r.get("error_message"), r)
    return (False, code, None, r)


def _temp_verdict(
    printer_id: str | None, material_id: str | None,
) -> tuple[bool, str | None, str | None]:
    """Temperature-ceiling half."""
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
        thermal = getattr(mat, "thermal", None)
        rng = thermal.get("print_temp_range_c") if isinstance(thermal, dict) else None
        mat_min = rng[0] if rng and len(rng) >= 1 else None  # don't drop a legit 0

        if printer_max is not None and mat_min is not None and mat_min > printer_max:
            if getattr(printer, "resolved_from", "") == "generic":
                return (
                    True,
                    "MATERIAL_EXCEEDS_HOTEND",
                    f"{material_id} needs at least {mat_min}°C at the nozzle, and "
                    f"Kiln is holding this printer to the generic {printer_max}°C "
                    f"floor because this install has no record for it. Sign in "
                    f"free and Kiln works to the printer's real limits.",
                )
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


def material_temp_blocks(printer_id: str | None, material_id: str | None) -> bool:
    """True when *printer_id* cannot melt *material_id* at all."""
    return _temp_verdict(printer_id, (material_id or "").strip().lower() or None)[0]


_SLICED_SUFFIXES = (".gcode", ".bgcode", ".gcode.3mf", ".gco", ".g")


def _too_big_fix(job_path: str | None) -> str:
    """The sentence that fixes an oversize part, for the file in hand."""
    if job_path and job_path.lower().endswith(_SLICED_SUFFIXES):
        return (
            "This file was sliced for a part bigger than this printer's bed: "
            "go back to the model it came from and turn it, scale it, or cut "
            "it into pieces that fit (split_mesh_to_fit), then slice again, "
            "or print on a printer with a larger bed."
        )
    return (
        "Pick a size that fits this printer, auto-orient/rotate it to fit, "
        "cut it into joinable pieces that fit (split_mesh_to_fit), or print "
        "on a larger-bed printer."
    )


def _words(name: str) -> set[str]:
    """``"PLA-ESD"`` -> ``{"pla", "esd"}``; ``"PLA+"`` -> ``{"pla"}``."""
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def _short_name(display_name: str) -> str:
    """``"PETG (Polyethylene ...)"`` -> ``"PETG"``."""
    return display_name.split(" (", 1)[0].strip()


def _filaments_that_run(printer_id: str | None, refused: str | None) -> dict[str, Any] | None:
    """The filaments *printer_id* runs on its stock hotend."""
    if not printer_id:
        return None
    held_to_floor: dict[str, str] | None = None
    try:
        from kiln.design_intelligence import (
            check_printer_material_compatibility,
            compat_floor_reason,
            get_material_profile,
        )

        report = check_printer_material_compatibility(printer_id)
        if report is None:
            return None
        if report.resolved_from == "default":
            held_to_floor = compat_floor_reason(printer_id)
            if held_to_floor is None:
                return None
        ids: list[str] = []
        families: list[str] = []
        for material_id, entry in (report.materials or {}).items():
            if not isinstance(entry, dict) or entry.get("status") != "compatible":
                continue
            if material_id == (refused or "").strip().lower():
                continue
            if _temp_verdict(printer_id, material_id)[0]:
                continue
            ids.append(material_id)
            profile = get_material_profile(material_id)
            name = _short_name(getattr(profile, "display_name", "") or material_id.upper())
            if not any(_words(f) <= _words(name) for f in families):
                families.append(name)
    except Exception:  # noqa: BLE001 -- the refusal stands without the list
        _logger.debug("print_gate: compatible filaments not read", exc_info=True)
        return None
    if not ids:
        return None
    if len(families) == 1:
        named = families[0]
    else:
        named = ", ".join(families[:-1]) + " and " + families[-1]
    variants = len(ids) - len(families)
    tail = f" (and {variants} variants of them)" if variants > 0 else ""
    if held_to_floor is not None:
        return {
            "materials": ids,
            "summary": (
                f"Until Kiln has this printer's record{_record_remedy(held_to_floor)}, it is "
                f"held to the generic floor, which runs {named}{tail}."
            ),
            "held_to_floor": True,
        }
    return {
        "materials": ids,
        "summary": f"On its stock hotend this printer runs {named}{tail}.",
    }


def _record_remedy(reason: dict[str, str]) -> str:
    """What gets this printer's record, in brackets, for a refusal that lists the floor's filaments: by the cause the record was not had."""
    why = reason.get("why_generic", "")
    if why == "signed_out":
        from kiln.served_answer import SESSION_EXPIRED_CODE

        return " (sign back in)" if reason.get("why_code") == SESSION_EXPIRED_CODE else " (sign in free)"
    if why == "offline":
        return " (reconnect to the internet)"
    return ""


def _material_help(
    block: dict[str, Any],
    *,
    printer_id: str | None,
    material_id: str | None,
    job_path: str | None = None,
) -> None:
    """Add the fix to a refusal over a filament the printer cannot melt, in place."""
    runs = _filaments_that_run(printer_id, material_id)
    if runs is not None:
        block["runs_here"] = runs
    enrichment = block.get("enrichment")
    if isinstance(enrichment, dict) and "material_swap" in (enrichment.get("fixes") or []):
        return
    try:
        from kiln._pro_print_fix_bridge import material_fix

        version_id = None
        if job_path:
            from kiln.auto_record_hook import version_of_print_file

            version_id = version_of_print_file(job_path)
        fix = material_fix(printer_id, material_id, version_id=version_id)
    except Exception:  # noqa: BLE001 -- the refusal stands without it
        _logger.debug("print_gate: filament fix not asked", exc_info=True)
        return
    paid = fix.get("enrichment")
    if isinstance(paid, dict):
        if isinstance(enrichment, dict):
            # An oversize part in an unmeltable filament: one block, both fixes.
            enrichment.setdefault("fixes", []).append("material_swap")
            enrichment["material_swap"] = paid.get("material_swap")
        else:
            block["enrichment"] = paid
        return
    for key in ("pro_adds", "pro_fix_missed"):
        if fix.get(key):
            block[key] = fix[key]


def _suggestions(
    fit_code: str | None, temp_code: str | None, job_path: str | None = None,
) -> list[str]:
    out: list[str] = []
    if fit_code == "EXCEEDS_BED":
        out.append(_too_big_fix(job_path))
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
    """Pro+ enrichment for a blocked verdict — "here's exactly how to fix it"."""
    try:
        from kiln_pro.print_fix.engine import enrich_block
    except Exception:  # noqa: BLE001
        # The enrichment is OPTIONAL
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
    """Deterministic 'is this print physically possible?' verdict."""
    if not job_path or not os.path.isfile(job_path):
        return {"ok": True, "blocked": False, "reason": "no local job to inspect — soft-pass"}

    # Fit and temperature only
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
        "suggestions": _suggestions(fit_code, temp_code, job_path),
        "override_hint": (
            "This is a hard physical limit, not a warning. To print anyway "
            "(e.g. you are sending this file to a different printer), a human "
            "must call force_print_oversize — an autonomous agent cannot "
            "self-approve it."
        ),
        "fit": fit_detail,
    }
    # Pro+ additive enrichment — attached as a NEW key only
    block["enrichment"] = _maybe_enrich_block(
        block, job_path=job_path, printer_id=printer_id, material_id=material_id,
    )
    if temp_blocked:
        _material_help(block, printer_id=printer_id, material_id=material_id, job_path=job_path)
    return block


def check_material_temp(
    printer_id: str | None, material_id: str | None, *, job_path: str | None = None,
) -> dict[str, Any] | None:
    """Slice-layer temperature-ceiling check (public, free)."""
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
    block["enrichment"] = _maybe_enrich_block(
        block, job_path=job_path, printer_id=printer_id, material_id=mid,
    )
    _material_help(block, printer_id=printer_id, material_id=mid, job_path=job_path)
    return block


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

import time  # noqa: E402  (kept local to the override section)

_DEFAULT_OVERRIDE_TTL_S = 300.0
_oversize_grants: dict[str, float] = {}


def grant_oversize_override(
    printer_id: str | None, *, ttl_seconds: float = _DEFAULT_OVERRIDE_TTL_S,
) -> None:
    """Allow the next oversize/over-temp print on *printer_id* for *ttl_seconds*."""
    _oversize_grants[(printer_id or "").lower()] = time.monotonic() + ttl_seconds


def _override_active(printer_id: str | None) -> bool:
    expiry = _oversize_grants.get((printer_id or "").lower())
    return expiry is not None and time.monotonic() < expiry


def _declared_model(adapter: Any) -> str:
    """The model this adapter's setup declared, or ``""`` -- never the global resolver, and never a non-string a stand-in adapter returns."""
    with contextlib.suppress(Exception):
        value = adapter.declared_printer_model()
        if isinstance(value, str):
            return value.strip()
    return ""


def _resolve_printer_model(adapter: Any) -> str | None:
    """The model of the printer this start is for."""
    if declared := _declared_model(adapter):
        return declared
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


def with_judged_plate(file_name: str, kwargs: dict[str, Any], kept: list[str]) -> dict[str, Any]:
    """The start's arguments."""
    if kwargs.get("local_file_path") or _is_resume_print(file_name, kwargs):
        return kwargs
    plate = _resolve_local_job(file_name, kwargs) or (kept[0] if kept else None)
    return {**kwargs, "local_file_path": plate} if plate else kwargs


class _ReadBackFailed(Exception):
    """A backend that CAN read a file back could not, this time."""


_READ_BACK_ATTEMPTS = 2


def _fetch_printer_copy(adapter: Any, file_name: str) -> str | None:
    """Read *file_name* back from the printer into a temp file."""
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
    for k in ("material", "material_id", "expected_material"):
        v = kwargs.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _is_resume_print(file_name: str, kwargs: dict[str, Any]) -> bool:
    """True for a resume-mode continuation (mid-print decoration / recovery)."""
    if kwargs.get("resume_from_paused"):
        return True
    name = (file_name or "").lower()
    return "_resume_" in name or name.startswith(
        ("transformed_resume", "original_resume")
    )


def _concurrent_fleet_verdict(adapter: Any) -> dict[str, Any] | None:
    """Block a print that would run a SECOND machine at once below Business."""
    try:
        from kiln.registry import get_registry

        registry = get_registry()
        if registry.count <= 1:
            return None

        try:
            from kiln.licensing import get_tier, max_printers_for_tier

            cap = max_printers_for_tier(get_tier())
        except Exception:
            cap = 1  # free-tier fallback: kiln-pro absent
        if cap is None or cap <= 0 or registry.count <= cap:
            return None

        # Which OTHER machines are busy right now?
        this_machine = _machine_id(adapter)
        busy: list[str] = []
        for name, state in _peer_states(registry, this_machine).items():
            if getattr(state, "is_occupied", False) is True:
                busy.append(name)

        if len(busy) < cap:
            return None

        others = ", ".join(sorted(busy)[:3])
        reason = (
            f"Kiln runs one printer at a time on this plan, and "
            f"{others} is already printing."
        )
        free_path = "Wait for it to finish, or start it after this one."
        headline = "Coordinate this job with the printer that is already running."
        free_included = "The job has not started; wait and run it next."
        waiting = _waiting_at_a_stop(registry, busy)
        if waiting is not None:
            label, note = waiting
            from kiln.hardware_stops import waiting_elsewhere_words

            waiting_line, way_on = waiting_elsewhere_words(label, note)
            reason = (
                f"{waiting_line} Kiln runs one printer at a time on this plan, so "
                f"this print can start once {label}'s print is finished or cancelled."
            )
            free_path = way_on
            headline = "Coordinate this job with the printer that is waiting for you."
            free_included = f"The job has not started. {waiting_line} {way_on}"
        verdict = {
            "blocked": True,
            "reason": reason,
            "override_hint": (
                f"{free_path} "
                "Kiln Business runs your printers in parallel — "
                "https://kiln3d.com/pricing"
            ),
            "code": "TIER_CONCURRENT_PRINT_LIMIT",
        }
        verdict.setdefault(
            "upgrade_nudge",
            upgrade_nudge_block(
                variant="concurrent_queue",
                tier="business",
                feature="Coordinated multi-printer queue",
                headline=headline,
                outcome_preview=(
                    "Kiln Business would route the queue across the available "
                    "machines and start eligible work in parallel."
                ),
                free_included=free_included,
                moment="resource_threshold",
            ),
        )
        return verdict
    except Exception:  # noqa: BLE001 — a licensing check never breaks a print
        _logger.debug("concurrent-fleet gate soft-passed", exc_info=True)
        return None


def _waiting_at_a_stop(registry: Any, busy: list[str]) -> tuple[str, dict[str, Any]] | None:
    """``(name, note)`` when the ONE busy machine is paused at a planned stop."""
    if len(busy) != 1:
        return None
    try:
        from kiln.hardware_stops import waiting_elsewhere

        note = waiting_elsewhere(registry.get(busy[0]))
    except Exception:  # noqa: BLE001 -- the words never break the gate
        return None
    return (busy[0], note) if note else None


def _machine_id(adapter: Any) -> str:
    """Fingerprint for *adapter*, or ``""`` when the registry can't say."""
    try:
        from kiln.registry import machine_fingerprint

        return machine_fingerprint(adapter)
    except Exception:
        return ""


def _peer_states(registry: Any, this_machine: str) -> dict[str, Any]:
    """State of every registered machine EXCEPT this one, aliases collapsed."""
    from kiln.printers.engagement import internal_read
    from kiln.registry import machine_fingerprint

    out: dict[str, Any] = {}
    for name in registry.list_machines():
        try:
            peer = registry.get(name)
            if this_machine and machine_fingerprint(peer) == this_machine:
                continue
            with internal_read():
                out[name] = peer.get_state()
        except Exception:
            continue
    return out


def _read_back_refusal(
    file_name: str, printer_id: str | None, cause: str, override: bool,
) -> dict[str, Any] | None:
    """The verdict for a file Kiln could not read back: refuse, or honour the single-use human override the way every other block does."""
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


def _plate_scan_verdict(
    adapter: Any, job_path: str, file_name: str, printer_id: str | None, plate_number: int,
) -> dict[str, Any] | None:
    """The verdict when the G-code safety scan refuses the plate inside a 3MF that is about to print."""
    from kiln.printers.print_file_scan import dialect_for, is_threemf, refusal, scan_print_file

    if not is_threemf(job_path):
        return None
    try:
        scan = scan_print_file(
            job_path,
            printer_id=printer_id,
            dialect=dialect_for(getattr(adapter, "name", None), path=job_path),
            plate_number=plate_number,
        )
    except Exception:  # noqa: BLE001 -- a scan that cannot run never blocks a print
        _logger.debug("print_gate: plate scan raised; soft-passing", exc_info=True)
        return None
    if scan is None or scan.valid:
        return None
    block = refusal(str(file_name), scan, verb="started", printer_id=printer_id)
    return {"ok": False, "blocked": True, **block}


def _nozzle_size_verdict(
    adapter: Any, job_path: str, file_name: str, printer_id: str | None, override: bool,
) -> dict[str, Any] | None:
    """The verdict when the file and the printer state different nozzle sizes (:func:`kiln.nozzle_size_check.check_nozzle_size`), or ``None``."""
    from kiln.nozzle_size_check import check_nozzle_size

    check = check_nozzle_size(adapter, job_path)
    if not check.refuses:
        return None
    reason = check.sentence(os.path.basename(str(file_name)))
    if override:
        _logger.warning("print_gate: OVERRIDE engaged for %s (%s)", printer_id, NOZZLE_SIZE_MISMATCH)
        return {
            "ok": True,
            "blocked": False,
            "overridden": True,
            "override_code": NOZZLE_SIZE_MISMATCH,
            "reason": f"OVERRIDE (human-confirmed): {reason}",
            "nozzle_size": check.to_dict(),
        }
    return {
        "ok": False,
        "blocked": True,
        "code": NOZZLE_SIZE_MISMATCH,
        "reason": reason,
        "nozzle_size": check.to_dict(),
        "suggestions": [
            f"Slice the part again for a {check.printer_mm:g} mm nozzle, if that is the one fitted.",
            f"Correct the nozzle setting on the printer, if a {check.file_mm:g} mm nozzle is the one fitted.",
        ],
        "override_hint": (
            "If you know the file suits the nozzle that is fitted, the single-use human "
            "override, force_print_oversize, covers this block too: a human calls it for "
            "this printer and re-issues the print once; an autonomous agent cannot "
            "self-approve it."
        ),
    }


def _bed_was_unknown(verdict: dict[str, Any]) -> bool:
    """Whether the fit half had no bed to judge the part against."""
    fit = verdict.get("fit")
    return isinstance(fit, dict) and fit.get("error_code") == "VOLUME_UNKNOWN"


def _bed_the_printer_states(adapter: Any, printer_id: str | None) -> tuple[str, str, Any] | None:
    """A bed the printer itself gives Kiln when the model it was set up as has none, as ``(the printer id."""
    try:
        from kiln.printer_model_check import _row

        info = adapter.get_printer_info()
        reported = _row(getattr(info, "model", None))
        if reported:
            return reported, "reported", None
    except Exception:  # noqa: BLE001 -- a printer that cannot say is asked the next way
        _logger.debug("print_gate: no model reported for the bed", exc_info=True)
    if not printer_id:
        return None
    try:
        from kiln.catalog_keys import GENERIC_ROWS, printer_key_candidates
        from kiln.printers.bed_fit import PrinterBed, catalogue_printer_id

        if catalogue_printer_id(printer_id) is not None:
            return None  # a catalogue printer's bed is the catalogue's to give
        limits = adapter.read_machine_limits()
        volume = ((limits or {}).get("profile") or {}).get("build_volume")
        if not volume:
            return None
        from kiln.safety_profiles import BED_FROM_PRINTER, set_local_printer_bed

        where = str((limits.get("read") or {}).get("build_volume") or "")
        if set(printer_key_candidates(printer_id)) & GENERIC_ROWS:
            bed = PrinterBed(tuple(float(v) for v in volume), printer_id, BED_FROM_PRINTER, where)
            return printer_id, "configuration", bed
        set_local_printer_bed(printer_id, volume, kind=BED_FROM_PRINTER, where=where)
        return printer_id, "configuration", None
    except Exception:  # noqa: BLE001 -- no bed read is a bed still unknown
        _logger.debug("print_gate: no bed read off the printer", exc_info=True)
        return None


def _unknown_bed_verdict(
    adapter: Any, job_path: str, file_name: str, printer_id: str | None, override: bool,
) -> dict[str, Any] | None:
    """The verdict for a start whose printer has no bed Kiln knows, or ``None`` to let the start through."""
    stated = _bed_the_printer_states(adapter, printer_id)
    if stated is not None:
        judged_as, how, bed = stated
        blocked, code, msg, detail = _fit_verdict(job_path, judged_as, bed=bed)
        if (detail or {}).get("error_code") != "VOLUME_UNKNOWN":
            if not blocked:
                return {"ok": True, "blocked": False, "reason": "pre-print gate passed", "fit": detail}
            where = "the model it reports itself as" if how == "reported" else "its own configuration"
            reason = f"{msg} (Kiln judged it against the bed of {where}.)"
            if override:
                _logger.warning("print_gate: OVERRIDE engaged (%s) for %s", code, printer_id)
                return {
                    "ok": True, "blocked": False, "overridden": True, "override_code": code,
                    "reason": f"OVERRIDE (human-confirmed): {reason}", "fit": detail,
                }
            return {
                "ok": False, "blocked": True, "code": code, "fit": detail, "reason": reason,
                "suggestions": _suggestions(code, None, job_path),
            }
    from kiln.print_consent import person_chose_unchecked_bed
    from kiln.printers.bed_fit import BED_UNKNOWN_CODE, bed_unknown_sentence

    name = getattr(adapter, "_kiln_registered_name", None)
    name = name if isinstance(name, str) and name else None
    reason = bed_unknown_sentence(
        printer_id, what=os.path.basename(str(file_name)) or "this file", printer_name=name,
    )
    if person_chose_unchecked_bed(name):
        _logger.warning("print_gate: %s started without the bed check, the person's choice", printer_id)
        return {
            "ok": True, "blocked": False, "bed_unchecked_by_person": True,
            "reason": "Started without the bed-size check, the person's choice on this print's approval.",
        }
    if override:
        _logger.warning("print_gate: OVERRIDE engaged (%s) for %s", BED_UNKNOWN_CODE, printer_id)
        return {
            "ok": True, "blocked": False, "overridden": True, "override_code": BED_UNKNOWN_CODE,
            "reason": f"OVERRIDE (human-confirmed): {reason}",
        }
    return {
        "ok": False,
        "blocked": True,
        "code": BED_UNKNOWN_CODE,
        "reason": reason,
        "suggestions": [
            "Record the bed size the printer's maker publishes, on the person's yes, with set_printer_model.",
        ],
        "override_hint": (
            "If the maker publishes no bed size, a human can let this one print start without the "
            "check with force_print_oversize, the single-use override; an autonomous agent cannot "
            "self-approve it."
        ),
    }


def _printer_model_verdict(
    adapter: Any, job_path: str, file_name: str, override: bool,
) -> dict[str, Any] | None:
    """The verdict when the printer reports itself as another model than it was set up as."""
    from kiln.printer_model_check import check_printer_model

    check = check_printer_model(adapter)
    if check.status != "differs":
        return None
    blocked, _code, fit_msg, fit_detail = _fit_verdict(job_path, check.reported_row)
    if not blocked:
        return None
    name = getattr(adapter, "_kiln_registered_name", None)
    measured = str(fit_msg or "").split(". ", 1)[0].rstrip(".")
    reason = (
        f"{os.path.basename(str(file_name))} was not started.  This printer was set up as "
        f"{check.declared_name}, but it reports itself as {check.reported_name}, and the part does not fit "
        f"{check.reported_name}'s bed: {measured}.  One of the two models is wrong.  If the printer is "
        f"{check.reported_name}, set it with {check.fix(name if isinstance(name, str) else '')}."
    )
    if override:
        _logger.warning("print_gate: OVERRIDE engaged (%s)", PRINTER_MODEL_MISMATCH)
        return {
            "ok": True,
            "blocked": False,
            "overridden": True,
            "override_code": PRINTER_MODEL_MISMATCH,
            "reason": f"OVERRIDE (human-confirmed): {reason}",
            "printer_model": check.to_dict(),
        }
    return {
        "ok": False,
        "blocked": True,
        "code": PRINTER_MODEL_MISMATCH,
        "reason": reason,
        "printer_model": check.to_dict(),
        "fit": fit_detail,
        "suggestions": [
            f"If the printer is {check.reported_name}, set its model to that and re-slice for its bed.",
            f"If it is {check.declared_name}, as set up, start this print once with the human override.",
        ],
        "override_hint": (
            f"If the printer is {check.declared_name}, a human can let this print start once with "
            "force_print_oversize, the single-use override: a human calls it for this printer and "
            "re-issues the print; an autonomous agent cannot self-approve it."
        ),
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

FILAMENT_SLOTS_CODE = "FILAMENTS_EXCEED_SLOTS"

CHUTE_MISMATCH_CODE = "COLOR_CHANGE_CHUTE_MISMATCH"

_COLOUR_CHANGE_SCAN_BYTES = 64 * 1024 * 1024
_COLOUR_CHANGE_LINE_BYTES = 4096

#: Two chute positions within this are the same chute.
_CHUTE_MATCH_MM = 0.5

_PAIRING_CODES = frozenset({NOZZLE_SIZE_MISMATCH, FILAMENT_SLOTS_CODE, CHUTE_MISMATCH_CODE, PRINTER_MODEL_MISMATCH})


def _plate_number(kwargs: dict[str, Any]) -> int:
    try:
        return max(1, int(kwargs.get("plate_number") or 1))
    except (TypeError, ValueError):
        return 1


def _plate_colours(job_path: str, plate_number: int) -> list[str]:
    """The colours a sliced plate declares for the filaments it uses, read from its own."""
    from kiln.printers.bambu_3mf import _declared_filament_colors

    try:
        with UntrustedZipFile(job_path) as zf:
            text = zf.read(f"Metadata/plate_{plate_number}.json").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- no plate record: nothing to judge
        return []
    return _declared_filament_colors(text) or []


def _kiln_colour_change_chute(job_path: str) -> tuple[float | None, float | None] | None:
    """Where the first colour change Kiln wrote into this plate sends the head to purge, as ``(x, y)``."""
    from kiln.gcode import axis_value
    from kiln.gcode_metadata import sliced_gcode_member, unpacks_in_chunks
    from kiln.printers.bambu_3mf import KILN_COLOUR_CHANGE_NOTE

    note = KILN_COLOUR_CHANGE_NOTE.encode("ascii")
    try:
        with UntrustedZipFile(job_path) as zf:
            member = sliced_gcode_member(zf)
            if member is None:
                return None
            info = zf.getinfo(member)
            if not unpacks_in_chunks(info):
                return None
            with zf.open(info) as fh:
                in_block = False
                read = 0
                while read < _COLOUR_CHANGE_SCAN_BYTES:
                    line = fh.readline(_COLOUR_CHANGE_LINE_BYTES)
                    if not line:
                        return None
                    read += len(line)
                    code = line.split(b";", 1)[0].strip()
                    if not in_block:
                        in_block = code.startswith(b"M620 S") and note in line
                        continue
                    if code.startswith(b"M621"):
                        return None
                    if code.startswith((b"G0 ", b"G1 ")):
                        text = code.decode("ascii", errors="replace")
                        x, y = axis_value(text, "X"), axis_value(text, "Y")
                        if x is not None or y is not None:
                            return (x, y)
    except Exception:  # noqa: BLE001 -- an archive Kiln cannot read is not judged here
        _logger.debug("print_gate: colour-change scan skipped for %s", job_path, exc_info=True)
    return None


def _same_chute(a: tuple[float | None, float | None], b: tuple[float | None, float | None]) -> bool:
    return all(
        (p is None and q is None) or (p is not None and q is not None and abs(p - q) <= _CHUTE_MATCH_MM)
        for p, q in zip(a, b, strict=True)
    )


def _chute_words(point: tuple[float | None, float | None]) -> str:
    return " ".join(f"{axis}{value:g}" for axis, value in zip("XY", point, strict=True) if value is not None)


def _chute_verdict(adapter: Any, job_path: str, name: str) -> dict[str, Any] | None:
    """Refuse a plate whose Kiln colour change purges at a chute this printer does not have there."""
    declared = _declared_model(adapter)
    if not declared:
        return None
    found = _kiln_colour_change_chute(job_path)
    if found is None:
        return None
    from kiln.printers.bambu_3mf import flush_station_for

    station = flush_station_for(declared)
    if station is not None and _same_chute(found, station):
        return None
    from kiln.printers.bed_fit import get_printer_display_name

    printer = get_printer_display_name(declared) or declared
    where = (
        f"its own chute is at {_chute_words(station)}"
        if station is not None
        else "Kiln has no waste-chute position on record for it"
    )
    return {
        "ok": False,
        "blocked": True,
        "code": CHUTE_MISMATCH_CODE,
        "reason": (
            f"{name} changes colour with a block Kiln wrote for another printer: before each "
            f"purge it sends the head to {_chute_words(found)}, where that printer's waste "
            f"chute is. This printer is set up as a {printer}, and {where}, so Kiln did not "
            f"start it here. Print it on the printer it was sliced for, or slice it again for "
            f"the {printer} in a single colour."
        ),
        "chute_in_file": list(found),
        "suggestions": [
            "Print it on the printer it was sliced for.",
            f"Or slice it again for the {printer} in a single colour.",
        ],
    }


def _slots_verdict(adapter: Any, name: str, colours: list[str]) -> dict[str, Any] | None:
    """Refuse a plate that uses more filaments than the filament unit Kiln drives on this printer has slots."""
    needed = len(colours)
    if needed < 2:
        return None
    try:
        from kiln.multi_material import multi_material_status

        unit = multi_material_status(adapter)
    except Exception:  # noqa: BLE001 -- a unit Kiln cannot read is not counted
        return None
    slots = unit.num_slots or 0
    if not unit.driven_by_kiln or slots < 1 or needed <= slots:
        return None
    return {
        "ok": False,
        "blocked": True,
        "code": FILAMENT_SLOTS_CODE,
        "reason": (
            f"{name} uses {needed} filaments, and this printer's {unit.label} has {slots} slots, "
            "so it cannot load them all at once. Kiln did not start it. Slice it again with "
            f"{slots} filaments or fewer (parts in the same colour can share one filament), or "
            "split the parts across plates by colour and print each plate with its filaments loaded."
        ),
        "filaments": needed,
        "slots": slots,
        "multi_material": unit.to_dict(),
        "suggestions": [
            f"Slice it again with {slots} filaments or fewer; parts in the same colour can share one.",
            "Or split the parts across plates by colour, and print each plate with its filaments loaded.",
        ],
    }


def _filament_path_verdict(
    adapter: Any, job_path: str, file_name: str, kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    """The plate against this printer's filament path, or ``None``."""
    if not str(job_path).lower().endswith(".3mf"):
        return None
    colours = _plate_colours(job_path, _plate_number(kwargs))
    if len(colours) < 2:
        return None
    name = os.path.basename(str(file_name)) or os.path.basename(str(job_path))
    return _chute_verdict(adapter, job_path, name) or _slots_verdict(adapter, name, colours)


# ---------------------------------------------------------------------------
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
_SAME_BED_HEADER_SCAN_LINES = 64
_SAME_BED_HEADER_MAX_LINES = 256

HOMED_UNKNOWN_SENTENCE = (
    "this printer cannot report whether it is homed; same-bed retry is not "
    "supported here — run the homed-state measurement for this backend, or "
    "reslice and print normally"
)

# ---------------------------------------------------------------------------
# The quiet start: a print started beside a part still on the plate
# ---------------------------------------------------------------------------
QUIET_START_HEADER_START = "; --- Kiln quiet start ---"
QUIET_START_HEADER_END = "; --- End Kiln quiet start ---"
QUIET_START_MOTION_CONTRACT = "absolute_z_first"
QUIET_START_POLICY_VERSION = "1"
QUIET_START_REQUIRED_KEYS = (
    "quiet_start_policy_version",
    "motion_contract",
    "planned_for_machine",
    "planned_for_plate",
    "clear_z_mm",
    "lift_floor_mm",
    "switched_off",
)


def same_bed_machine_id(adapter: Any) -> str:
    """The identity a same-bed retry is bound to, or ``""`` when there is none."""
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
    """The first *max_lines* of the job's gcode text, or ``None`` if unreadable."""
    low = path.lower()
    try:
        if low.endswith(".3mf"):

            from kiln.gcode import _MAX_SCAN_BYTES
            from kiln.gcode_metadata import read_member_text, sliced_gcode_member

            with UntrustedZipFile(path) as zf:
                member = sliced_gcode_member(zf)
                if member is None:
                    return None
                text = read_member_text(zf, member, _MAX_SCAN_BYTES)
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
    """The ``; key: value`` lines of the same-bed safe-startup header, or ``None`` when the file carries no such header at all."""
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


def read_quiet_start_contract(path: str) -> dict[str, str] | None:
    """The ``; key: value`` lines of Kiln's quiet-start header, or ``None`` when the file carries no such header."""
    return _read_contract(path, QUIET_START_HEADER_START, QUIET_START_HEADER_END)


def _read_contract(path: str, start_marker: str, end_marker: str) -> dict[str, str] | None:
    head = _gcode_head(path, _SAME_BED_HEADER_MAX_LINES)
    if not head:
        return None
    start = next(
        (i for i, line in enumerate(head[:_SAME_BED_HEADER_SCAN_LINES]) if line.strip() == start_marker),
        None,
    )
    if start is None:
        return None
    fields: dict[str, str] = {}
    for line in head[start + 1:]:
        stripped = line.strip()
        if stripped == end_marker:
            break
        if not stripped.startswith(";"):
            continue
        body = stripped[1:].strip()
        key, sep, value = body.partition(":")
        if sep and key.strip() and " " not in key.strip():
            fields.setdefault(key.strip(), value.strip())
    return fields


def evaluate_quiet_start(adapter: Any, job_path: str, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Judge a quiet-start file against the machine and the plate, live."""
    name = os.path.basename(str(job_path))
    contract = read_quiet_start_contract(job_path)
    machine = same_bed_machine_id(adapter)
    verdict: dict[str, Any] = {
        "ok": False, "blocked": True, "accepted": False, "code": None, "condition": None, "reason": "",
        "contract": contract, "machine": machine, "homed_evidence": None, "kind": "quiet_start",
    }

    def refuse(code: str, condition: str, reason: str) -> dict[str, Any]:
        verdict.update(code=code, condition=condition, reason=reason)
        _audit_quiet_start(verdict, name)
        return verdict

    remedy = " Slice it again beside what is on the plate, or clear the plate and say so, then print it the ordinary way."
    if contract is None:
        return refuse("QUIET_START_NO_CONTRACT", "contract", f"{name} carries no Kiln quiet-start header." + remedy)
    missing = [k for k in QUIET_START_REQUIRED_KEYS if not contract.get(k)]
    if missing:
        return refuse(
            "QUIET_START_NO_CONTRACT", "contract",
            f"{name} carries an incomplete Kiln quiet-start header: missing {', '.join(missing)}." + remedy,
        )
    if contract["motion_contract"] != QUIET_START_MOTION_CONTRACT:
        return refuse(
            "QUIET_START_NO_CONTRACT", "contract",
            f"{name}'s motion_contract is '{contract['motion_contract']}', not '{QUIET_START_MOTION_CONTRACT}'; only a "
            "prologue that lifts clear of the plate before it moves sideways may start beside a part." + remedy,
        )
    if contract["quiet_start_policy_version"] != QUIET_START_POLICY_VERSION:
        return refuse(
            "QUIET_START_NO_CONTRACT", "contract",
            f"{name} was planned under quiet-start policy {contract['quiet_start_policy_version']}; this Kiln "
            f"reads version {QUIET_START_POLICY_VERSION}." + remedy,
        )
    planned_for = contract["planned_for_machine"]
    if not machine:
        return refuse(
            "QUIET_START_WRONG_MACHINE", "machine",
            f"Kiln cannot identify this printer (it reports no serial, no address and no registered name), so it "
            f"cannot confirm {name} was planned for it (planned for {planned_for})." + remedy,
        )
    if planned_for != machine:
        return refuse(
            "QUIET_START_WRONG_MACHINE", "machine",
            f"{name} was planned for {planned_for} and is being started on {machine}; the plate it was planned "
            "around is not this one." + remedy,
        )
    from kiln.plate_state import fingerprint, read

    state = read(adapter)
    now = fingerprint(state) if state.occupied else ""
    if not state.occupied:
        return refuse(
            "QUIET_START_PLATE_NOT_OCCUPIED", "plate",
            f"{name} was planned beside a part on the plate, and the record now says the plate is "
            f"{state.status}; a quiet start skips the printer's own homing and levelling, which an empty plate "
            "should have. Slice it again and print it the ordinary way.",
        )
    if contract["planned_for_plate"] != now:
        return refuse(
            "QUIET_START_PLATE_CHANGED", "plate",
            f"{name} was planned for a plate holding "
            f"{', '.join(os.path.basename(j.file) for j in state.jobs) or 'another part'} as it stood then; the "
            "record has changed since." + remedy,
        )
    printer_name = _registered_name(adapter)
    status = _latch_status(printer_name)
    if status and bool(status.get("latched")):
        blockers = status.get("critical_interlocks_pending") or []
        pending = " Critical interlocks pending: " + ", ".join(str(x) for x in blockers) + "." if blockers else ""
        return refuse(
            "QUIET_START_EMERGENCY_LATCHED", "emergency_latch",
            f"Emergency latch is active for printer '{printer_name}'.{pending} Resolve hazards, acknowledge, then "
            "clear via clear_emergency_stop(), and start again.",
        )
    try:
        state_now = adapter.get_state()
    except Exception as exc:  # noqa: BLE001
        return refuse(
            "QUIET_START_STATE_READ_FAILED", "printer_state",
            f"could not read the printer's state before the quiet start ({exc}); try again in a moment.",
        )
    from kiln.printers.base import PrinterStatus, effective_state_of

    run_state = effective_state_of(state_now)
    if run_state in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
        return refuse(
            "QUIET_START_PRINTER_BUSY", "printer_state",
            f"the printer reports it is {run_state.value}; a quiet start begins only on an idle machine. Wait for "
            "that job to end, then start again.",
        )
    from datetime import datetime, timezone

    try:
        axes = adapter.homed_axes_now()
    except Exception as exc:  # noqa: BLE001
        return refuse(
            "QUIET_START_HOMED_READ_FAILED", "homed_axes",
            f"could not read which axes are homed from the printer ({exc}); try again in a moment.",
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
            "QUIET_START_HOMED_UNKNOWN", "homed_axes",
            f"{name} was not started: this printer cannot report whether it is homed, and a quiet start lifts to "
            "an absolute height before it moves; clear the plate and print it the ordinary way.",
        )
    reported = set(verdict["homed_evidence"]["axes"])
    not_homed = sorted({"x", "y", "z"} - reported)
    if not_homed:
        return refuse(
            "QUIET_START_NOT_HOMED", "homed_axes",
            f"the printer reports homed axes: {','.join(sorted(reported)) or 'none'} — {','.join(not_homed)} not "
            "homed (it has been switched off since the last print), and a quiet start never homes Z with a part "
            "on the plate. Clear the plate, home the printer (home_axes), then print it the ordinary way.",
        )
    wanted = [n.strip() for n in contract["switched_off"].split(",") if n.strip()]
    given = kwargs or {}
    still_on = [n for n in wanted if bool(given.get(n, _START_FLAG_DEFAULTS.get(n, True)))]
    if still_on:
        return refuse(
            "QUIET_START_SWITCH_ON", "switches",
            f"{name} must start with {', '.join(still_on)} switched off -- each one drives the head across the "
            "plate -- and the start command left them on.",
        )
    verdict.update(
        ok=True, blocked=False, accepted=True, code="QUIET_START_ACCEPTED", condition=None,
        reason=(
            f"quiet start accepted on {machine}: the file carries Kiln's absolute-lift-first contract for the plate "
            "as it stands, and the printer reports x,y,z homed and idle."
        ),
    )
    _audit_quiet_start(verdict, name)
    return verdict


_START_FLAG_DEFAULTS: dict[str, bool] = {
    "bed_leveling": True,
    "flow_cali": False,
    "vibration_cali": False,
    "timelapse": False,
    "layer_inspect": False,
    "nozzle_clog_detect": True,
}


def _audit_quiet_start(verdict: dict[str, Any], name: str) -> None:
    """One line per decision, with the evidence the decision rested on."""
    evidence = verdict.get("homed_evidence") or {}
    axes = evidence.get("axes")
    _logger.info(
        "print_gate: quiet start %s on %s (%s): plate=%s axes=%s field=%s read_at=%s code=%s",
        "accepted" if verdict.get("accepted") else "refused",
        verdict.get("machine") or "-",
        name,
        (verdict.get("contract") or {}).get("planned_for_plate") or "-",
        ",".join(axes) if axes else ("none" if axes is not None else "-"),
        evidence.get("field") or "-",
        evidence.get("read_at") or "-",
        verdict.get("code"),
    )


def _latch_status(printer_name: str) -> dict[str, Any] | None:
    """The emergency coordinator's latch status for *printer_name* -- the same read ``kiln.server`` makes before any control tool runs."""
    try:
        from kiln.emergency import get_emergency_coordinator

        return get_emergency_coordinator().get_latch_status(printer_name)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("emergency latch lookup failed for %s: %s", printer_name, exc)
        return None


def evaluate_same_bed_retry(adapter: Any, job_path: str) -> dict[str, Any]:
    """Judge a no-homing file as a same-bed retry, against the machine, live."""
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
    """A no-homing block, re-judged as a same-bed retry when the file claims to be one; every other file gets *original* back untouched."""
    if read_same_bed_contract(job_path) is None:
        return original
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
    adapter: Any, file_name: str, kwargs: dict[str, Any], *, keep: list[str] | None = None,
) -> dict[str, Any] | None:
    """Universal print-time backstop, called from ``PrinterAdapter.start_print``."""
    if _is_resume_print(file_name, kwargs):
        return None

    if fleet_blocked := _concurrent_fleet_verdict(adapter):
        return fleet_blocked

    printer_id = _resolve_printer_model(adapter)
    job = _resolve_local_job(file_name, kwargs)
    override = _override_active(printer_id)
    fetched: str | None = None
    if job is None:
        try:
            fetched = _fetch_printer_copy(adapter, file_name)
        except _ReadBackFailed as exc:
            return _read_back_refusal(file_name, printer_id, str(exc), override)
    inspected = job if job is not None else fetched
    from kiln.hardware_stops import stage as _stage_hardware_plan

    _stage_hardware_plan(adapter, file_name, inspected)
    material_id = _resolve_material(kwargs)
    if inspected and read_quiet_start_contract(inspected) is not None:
        quiet = evaluate_quiet_start(adapter, inspected, kwargs)
        if quiet.get("blocked"):
            return {
                "ok": False, "blocked": True, "code": quiet["code"], "reason": quiet["reason"],
                "condition": quiet["condition"], "homed_evidence": quiet["homed_evidence"], "quiet_start": quiet,
            }
    try:
        verdict = evaluate_pre_print_gate(
            inspected,
            printer_id,
            material_id=material_id,
            allow_oversize=override,
        )
        if inspected and verdict.get("blocked") and verdict.get("code") == "NO_HOMING_SEQUENCE":
            verdict = _same_bed_or_original(adapter, inspected, verdict, printer_id, material_id)
        if inspected and not verdict.get("blocked"):
            scanned = _plate_scan_verdict(adapter, inspected, file_name, printer_id, _plate_number(kwargs))
            if scanned is not None:
                verdict = scanned
        if fetched and not verdict.get("blocked") and str(printer_id or "").lower().startswith("bambu"):
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
            from kiln.printers.gcode_complete import (
                declared_model_for_adapter,
                family_for_adapter,
                gcode_problems,
            )

            problems = gcode_problems(
                fetched, family_for_adapter(adapter), name=str(file_name or ""),
                printer_model=declared_model_for_adapter(adapter),
            )
            if problems:
                verdict = {
                    "blocked": True,
                    "reason": (
                        f"{os.path.basename(str(file_name))} is not ready for the printer: "
                        + "; ".join(problems) + "."
                    ),
                }
        if inspected and not verdict.get("blocked") and _bed_was_unknown(verdict):
            judged = _unknown_bed_verdict(adapter, inspected, file_name, printer_id, override)
            if judged is not None and (judged.get("blocked") or not verdict.get("overridden")):
                verdict = judged
            elif judged is not None:
                verdict = {**verdict, "fit": judged.get("fit", verdict.get("fit"))}
        if inspected and not verdict.get("blocked"):
            paired = _filament_path_verdict(adapter, inspected, file_name, kwargs)
            if paired is not None:
                verdict = paired
        if inspected and not verdict.get("blocked"):
            sized = _nozzle_size_verdict(adapter, inspected, file_name, printer_id, override)
            if sized is not None:
                verdict = sized
        if inspected and not verdict.get("blocked"):
            disagrees = _printer_model_verdict(adapter, inspected, file_name, override)
            if disagrees is not None:
                verdict = disagrees
    finally:
        if fetched and keep is not None:
            keep.append(fetched)
        elif fetched:
            with contextlib.suppress(OSError):
                os.unlink(fetched)
    if fetched:
        verdict["inspected"] = "printer_copy"
        if verdict.get("blocked") and verdict.get("code") not in _PAIRING_CODES:
            verdict["reason"] = (
                f"{verdict.get('reason', '')} This is the printer's own copy of "
                f"{os.path.basename(str(file_name))}, read back from its storage: the file "
                "was not made by a current Kiln and is not safe to start. Delete it from "
                "the printer (delete_file) and re-slice through Kiln."
            ).strip()
    if override and verdict.get("overridden"):
        _oversize_grants.pop((printer_id or "").lower(), None)
    return verdict if verdict.get("blocked") else None
