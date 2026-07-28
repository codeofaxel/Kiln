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
