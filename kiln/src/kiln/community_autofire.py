"""Silent, geometry-based community auto-contribution for monitored prints.

When a monitored print reaches a terminal outcome, its (anonymous) result is
contributed to the community pool automatically — no manual
``record_print_outcome`` call, and keyed on the printed file's GEOMETRIC
signature (:func:`kiln.print_dna.fingerprint_model`), not its file hash, so the
same model re-exported still aggregates with its siblings.

Best-effort + non-blocking throughout: a missing manifest entry, a non-STL
source, or an offline federation endpoint never affects the print path.  The
geometry lookup is fail-safe (returns ``""`` → the contribution is skipped
rather than sending a file-hash stand-in).

This module resolves the GEOMETRY and nothing else.  The outcome vocabulary
and the dedupe key belong to
:func:`kiln.community_outbox.contribute_print_outcome`, the single door both
contribution paths go through — when this module owned a private copy of
both, a print that was watched AND recorded shipped twice, under two
different words.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def geometric_signature_for(printer_file_name: str | None) -> str:
    """Resolve a printed file to its geometric signature.

    Pipeline: printer file name → upload manifest → local source path →
    :func:`fingerprint_model` (STL fast path).  A non-STL source (e.g. a
    ``.3mf`` from Bambu Studio / a multi-color project) is loaded via trimesh
    and round-tripped through the same STL path, so a 3MF and an STL of the
    same model hash IDENTICALLY (the signature is triangle/vertex/area/volume
    based — it survives the round-trip and is invariant under the build-plate
    transform).  Returns ``""`` on any failure (no manifest entry, missing
    source, unparseable/unsupported file, trimesh absent) so callers skip the
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

        try:
            # STL fast path — keep the exact native signature so existing
            # community data stays comparable.
            return fingerprint_model(source_path).geometric_signature or ""
        except Exception:
            # Not a parseable STL (e.g. .3mf): load the mesh and round-trip it
            # through the STL path so the signature matches the STL twin.
            return _signature_via_mesh_load(source_path)
    except Exception:
        logger.debug("geometric signature unavailable (best-effort)", exc_info=True)
        return ""


def _signature_via_mesh_load(source_path: str) -> str:
    """Fingerprint a non-STL mesh (3MF / OBJ / PLY / …) by loading it with
    trimesh and round-tripping through the STL path, so its
    ``geometric_signature`` matches the STL of the same model.

    Fail-safe: returns ``""`` on any failure — trimesh absent (optional dep),
    corrupt/unsupported file, or no geometry — so the caller skips rather than
    contributing a garbage signature.  trimesh applies the build-plate
    transform on load; the signature is rigid-invariant, so a plated 3MF still
    matches its as-designed STL (only a genuine scale difference diverges,
    which is correct — it prints differently).
    """
    import os as _os
    import tempfile

    try:
        import trimesh  # optional dep; absent → skip (no worse than before A5)
    except Exception:
        return ""
    try:
        from kiln.print_dna import fingerprint_model

        mesh = trimesh.load(source_path, force="mesh")
        faces = getattr(mesh, "faces", None)
        if faces is None or len(faces) == 0:
            return ""
        stl_bytes = mesh.export(file_type="stl")
        fd, tmp = tempfile.mkstemp(suffix=".stl")
        try:
            with _os.fdopen(fd, "wb") as fh:
                fh.write(stl_bytes)
            return fingerprint_model(tmp).geometric_signature or ""
        finally:
            _os.unlink(tmp)
    except Exception:
        logger.debug("mesh-load signature unavailable (best-effort)", exc_info=True)
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

    Dedupe spans BOTH contribution paths, not just the two monitors
    (``await_print_completion`` + ``watch_print_status``) that can watch the
    same job: ``contribute_print_outcome`` mints the same key
    ``record_print_outcome`` will mint for this print, so a print that is
    watched and then recorded lands ONE outbox row.
    """
    try:
        from kiln import community_outbox

        # Translate first: fingerprinting loads and hashes the mesh, and a
        # cancelled print never needed it.
        if community_outbox.translate_outcome(outcome) is None:
            return {"contributed": False, "reason": "non_quality_outcome"}
        signature = geometric_signature_for(printer_file_name)
        if not signature:
            return {"contributed": False, "reason": "no_geometry"}
        return community_outbox.contribute_print_outcome(
            outcome=outcome,
            geometric_signature=signature,
            job_id=job_id,
            printer_file_name=printer_file_name,
            printer_model=printer_model,
            material=material,
            print_time_seconds=print_time_seconds,
        )
    except Exception:
        logger.debug("auto community contribution skipped (best-effort)", exc_info=True)
        return {"contributed": False, "reason": "error"}


def resolve_adapter_model(adapter: Any) -> str | None:
    """The adapter's model string, via every spelling an adapter actually has.

    ``adapter.get_printer_info()`` spent months as a method NO
    production adapter implemented — every community call site that
    used it got an AttributeError into a bare except and contributed
    ``printer_model="unknown"`` (the heartbeat hit the identical bug on
    2026-07-25: 630 of 670 rows NULL — and was fixed with this fallback
    chain while the community sites were not).  The Bambu / PrusaLink /
    Elegoo / serial adapters grew real implementations in 2026-08, so
    the probe branch now resolves exact models; the attribute chain
    stays as the fallback.  One resolver — imported by the heartbeat,
    the registry fleet view, and every contribution path — so the
    telemetry, the fleet table, and the community rows can never drift
    apart about what a printer is.

    Only ``str`` values pass: a probe or attribute that yields any
    other type (a mock, a stray object) must not have its repr
    laundered into telemetry as a "model".
    """
    if adapter is None:
        return None

    def _clean(value: Any) -> str | None:
        # Cap mirrors the heartbeat's per-model limit: some of these
        # strings are device-controlled (M115 MACHINE_TYPE, SDCP names)
        # and a buggy firmware must not stuff a novel into telemetry.
        if isinstance(value, str):
            value = value.strip()[:60]
            if value:
                return value
        return None

    try:
        info = adapter.get_printer_info()
        model = _clean(getattr(info, "model", None)) or _clean(
            getattr(info, "printer_model", None)
        )
        if model:
            return model
    except Exception:  # noqa: BLE001 — probe is best-effort
        pass
    for attr in ("printer_model", "_printer_model", "model"):
        try:
            value = _clean(getattr(adapter, attr, None))
        except Exception:  # noqa: BLE001 — property raised; try the next
            continue
        if value:
            return value
    return None


def contribute_resolved_outcome(
    *,
    outcome: str,
    printer_file_name: str | None,
    job_id: str | None = None,
    printer_name: str | None = None,
    material: str | None = None,
    failure_mode: str | None = None,
) -> dict[str, Any]:
    """Contribute a reconciliation-resolved outcome to the community pool.

    The federation twin of :func:`auto_contribute_completion`, for prints
    resolved AFTER the fact.  ``reconcile_pending_outcomes`` settles rows
    a live process never watched end — start a print, close the session,
    reconnect tomorrow — and until 2026-08-05 those resolutions wrote the
    LOCAL outcome row and stopped: the community pool only ever learned
    from watched endings, so the long unattended prints (where the data
    matters most) were systematically missing from the corpus the local
    fix was built to save.  This is NOT the bulk-history door — the
    moonraker importer's non-contribution stance is unchanged; these are
    prints Kiln itself started, one at a time, resolved by the machine's
    own terminal testimony.

    Same guarantees as its twin: silent, never raises, non-verdict
    outcomes contribute nothing (``unknown`` stays a known unknown), and
    the shared dedupe key means a later user refinement of the same job
    collapses instead of double-shipping.  The canonical model comes from
    the adapter's self-reported model when the registry can answer,
    ``printer_name`` otherwise — the door normalizes either.
    """
    try:
        from kiln import community_outbox

        if community_outbox.translate_outcome(outcome) is None:
            return {"contributed": False, "reason": "non_quality_outcome"}
        signature = geometric_signature_for(printer_file_name)
        if not signature:
            return {"contributed": False, "reason": "no_geometry"}

        printer_model: str | None = None
        if printer_name:
            try:
                from kiln.server import _registry

                printer_model = resolve_adapter_model(_registry.get(printer_name))
            except Exception:
                logger.debug(
                    "resolved-outcome model lookup unavailable", exc_info=True
                )

        extra: dict[str, Any] = {}
        if failure_mode:
            extra["failure_mode"] = failure_mode
        return community_outbox.contribute_print_outcome(
            outcome=outcome,
            geometric_signature=signature,
            job_id=job_id,
            printer_file_name=printer_file_name,
            printer_model=printer_model or printer_name,
            material=material,
            extra=extra,
        )
    except Exception:
        logger.debug(
            "resolved-outcome contribution skipped (best-effort)", exc_info=True
        )
        return {"contributed": False, "reason": "error"}
