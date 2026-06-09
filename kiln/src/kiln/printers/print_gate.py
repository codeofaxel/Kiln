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

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

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
    for k in ("source_path", "local_path", "gcode_path", "file_path", "threemf_path"):
        v = kwargs.get(k)
        if isinstance(v, str) and os.path.isfile(v):
            return v
    if isinstance(file_name, str) and os.path.isfile(file_name):
        return file_name
    return None


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

    printer_id = _resolve_printer_model(adapter)
    job = _resolve_local_job(file_name, kwargs)
    override = _override_active(printer_id)
    verdict = evaluate_pre_print_gate(
        job,
        printer_id,
        material_id=_resolve_material(kwargs),
        allow_oversize=override,
    )
    # Single-use override: a human confirmation authorises ONE otherwise-blocked
    # print, not a time window of them.  Consume the grant the instant it's used
    # (verdict.overridden is set only when the override actually rescued a block).
    if override and verdict.get("overridden"):
        _oversize_grants.pop((printer_id or "").lower(), None)
    return verdict if verdict.get("blocked") else None
