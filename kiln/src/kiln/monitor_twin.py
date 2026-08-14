"""The live print's local twin — what THIS machine last sliced and printed.

Every print, however it starts (web /create through the bridge, an agent over
MCP, the CLI), is sliced ON THIS MACHINE by the local engine and handed to
the printer from here.  So this machine is the one place the sliced toolpath
reliably exists — and the web Monitor's layer viewer needs exactly that file.

This module is the small ledger that remembers it, written at the engine
chokepoints every door already passes through:

  * :func:`note_sliced` — beside ``_record_slice`` in ``kiln.slicer`` (both
    dialect paths), pairing the slicer's OUTPUT with the mesh it came from.
  * :func:`note_wrapped` — in ``BambuAdapter.wrap_gcode_as_3mf``, because the
    file a Bambu printer receives is the WRAP, while the file the layer
    viewer wants is the raw G-code inside it.
  * :func:`note_print_started` — in ``PrinterAdapter.start_print``'s success
    block (the same single point that counts prints and stamps the job
    clock), which joins the printer-side file name back to the sliced pair
    and RETAINS copies under ``~/.kiln/monitor_twin/``.

The join is exact, never fuzzy: every name compared here was written by this
process during this flow (the slicer's output basename, the wrapper's output
basename, the adapter's uploaded name).  Nothing is ever matched against
cloud artifacts, other machines, or user libraries — that inference class is
explicitly rejected (see the web monitor spec).  When the ledger has no
exact match (a pre-sliced file, a file already on the printer, a print
started by the touchscreen), the active record honestly carries no toolpath
and :func:`publish` refuses with a reason.

Retention is one job per printer, overwritten at the next print start —
a viewing convenience, not an archive.  Everything here is best-effort and
never raises into a print path.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TWIN_DIR = Path("~/.kiln/monitor_twin").expanduser()
_SLICES_FILE = _TWIN_DIR / "slices.json"
_ACTIVE_FILE = _TWIN_DIR / "active.json"

#: Recent-slice ledger depth.  A machine slices a handful of jobs between
#: prints; eight covers every real flow without growing a history.
_MAX_SLICE_ENTRIES = 8

#: Retention ceilings.  Past these the twin quietly isn't retained — a
#: pathological file must not fill the user's disk for a preview.
_MAX_GCODE_BYTES = 512 * 1024 * 1024
_MAX_MESH_BYTES = 128 * 1024 * 1024

#: Upload ceiling for the compressed toolpath — mirrors the server's cap.
_MAX_GCODE_GZ_UPLOAD = 48 * 1024 * 1024

_MESH_EXTENSIONS = {".stl", ".3mf", ".obj"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt ledger is an empty ledger
        return default


def _write_json(path: Path, value: Any) -> None:
    _TWIN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
    tmp.replace(path)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name or "default")[:64] or "default"


# ---------------------------------------------------------------------------
# Chokepoint notes — called from slicer / wrapper / start_print
# ---------------------------------------------------------------------------


def note_sliced(input_path: str, output_path: str) -> None:
    """Remember that ``input_path`` was just sliced to ``output_path``.

    Paths only — nothing is copied until a print actually starts, so a
    slice that never prints costs a ledger line, not a file copy.
    """
    try:
        entries = _read_json(_SLICES_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(
            {
                "input": os.path.abspath(input_path),
                "output": os.path.abspath(output_path),
                "wrapped": None,
                "at": _now_iso(),
            }
        )
        _write_json(_SLICES_FILE, entries[-_MAX_SLICE_ENTRIES:])
    except Exception:  # noqa: BLE001 — bookkeeping never blocks a slice
        logger.debug("monitor_twin.note_sliced failed", exc_info=True)


def note_wrapped(gcode_path: str, wrapped_path: str) -> None:
    """Remember that ``gcode_path`` was wrapped into ``wrapped_path``.

    The printer-side name will be the WRAP's basename; the layer viewer
    wants the raw G-code inside — this line keeps the two joined.
    """
    try:
        gcode_abs = os.path.abspath(gcode_path)
        entries = _read_json(_SLICES_FILE, [])
        if not isinstance(entries, list):
            return
        changed = False
        for entry in entries:
            if isinstance(entry, dict) and entry.get("output") == gcode_abs:
                entry["wrapped"] = os.path.abspath(wrapped_path)
                changed = True
        if changed:
            _write_json(_SLICES_FILE, entries)
    except Exception:  # noqa: BLE001 — bookkeeping never blocks a wrap
        logger.debug("monitor_twin.note_wrapped failed", exc_info=True)


def note_print_started(printer_name: str, file_name: str) -> None:
    """A print just started: join the printer-side name to the sliced pair
    and retain copies of the toolpath (+ mesh) for the Monitor's twin.

    Called from the one success block every print passes through.  The
    active record is per printer and OVERWRITES the previous job's — and
    when the join finds nothing (a file Kiln didn't slice this session),
    the record still lands, carrying no toolpath, so a stale twin from an
    earlier job can never decorate a new one.
    """
    try:
        base = os.path.basename(str(file_name or ""))
        entries = _read_json(_SLICES_FILE, [])
        match: dict[str, Any] | None = None
        if base and isinstance(entries, list):
            for entry in reversed(entries):  # newest wins among exact twins
                if not isinstance(entry, dict):
                    continue
                out = os.path.basename(str(entry.get("output") or ""))
                wrapped = os.path.basename(str(entry.get("wrapped") or ""))
                if base in (out, wrapped) and out:
                    match = entry
                    break

        slug = _slug(printer_name)
        record: dict[str, Any] = {
            "file_name": base,
            "printer_name": printer_name or "default",
            "started_at": _now_iso(),
            "gcode": None,
            "mesh": None,
        }

        if match is not None:
            src_gcode = str(match.get("output") or "")
            if (
                src_gcode
                and os.path.isfile(src_gcode)
                and os.path.getsize(src_gcode) <= _MAX_GCODE_BYTES
            ):
                _TWIN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
                dst_gcode = _TWIN_DIR / f"{slug}-current.gcode"
                shutil.copy2(src_gcode, dst_gcode)
                record["gcode"] = str(dst_gcode)
            src_mesh = str(match.get("input") or "")
            ext = os.path.splitext(src_mesh)[1].lower()
            if (
                record["gcode"] is not None
                and src_mesh
                and ext in _MESH_EXTENSIONS
                and os.path.isfile(src_mesh)
                and os.path.getsize(src_mesh) <= _MAX_MESH_BYTES
            ):
                dst_mesh = _TWIN_DIR / f"{slug}-mesh{ext}"
                # A stale mesh from the previous job must not survive a
                # gcode-only retention under a different extension.
                for old in _TWIN_DIR.glob(f"{slug}-mesh.*"):
                    old.unlink(missing_ok=True)
                shutil.copy2(src_mesh, dst_mesh)
                record["mesh"] = str(dst_mesh)

        active = _read_json(_ACTIVE_FILE, {})
        if not isinstance(active, dict):
            active = {}
        active[slug] = record
        _write_json(_ACTIVE_FILE, active)
    except Exception:  # noqa: BLE001 — retention never blocks a print
        logger.debug("monitor_twin.note_print_started failed", exc_info=True)


# ---------------------------------------------------------------------------
# Reads + publish
# ---------------------------------------------------------------------------


def active_twin(printer_name: str | None = None) -> dict[str, Any] | None:
    """The retained record for ``printer_name``, or — when the ledger holds
    exactly one printer — that one.  ``None`` when nothing is retained."""
    active = _read_json(_ACTIVE_FILE, {})
    if not isinstance(active, dict) or not active:
        return None
    if printer_name:
        rec = active.get(_slug(printer_name))
        return rec if isinstance(rec, dict) else None
    if len(active) == 1:
        rec = next(iter(active.values()))
        return rec if isinstance(rec, dict) else None
    return None


def _default_api_url() -> str:
    return os.environ.get("KILN_API_URL", "https://api.kiln3d.com").rstrip("/")


def _multipart(fields: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """Encode ``(name, filename, payload, content_type)`` parts; filename
    ``""`` means a plain form field."""
    boundary = f"kiln-twin-{uuid.uuid4().hex}"
    out = io.BytesIO()
    for name, filename, payload, content_type in fields:
        out.write(f"--{boundary}\r\n".encode())
        if filename:
            out.write(
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode()
            )
        else:
            out.write(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def publish(printer_name: str | None = None) -> dict[str, Any]:
    """Upload the active print's retained toolpath (+ mesh) to the caller's
    own Kiln account and return the artifact token the browser fetches.

    The web Monitor calls this through the bridge relay when it is watching
    a live print and has no toolpath yet.  Refusals are structured and
    honest — every ``success: False`` carries a ``code`` and a sentence a
    person can act on.  Network cost is paid only when someone is watching;
    nothing here runs ambiently.
    """
    rec = active_twin(printer_name)
    if rec is None:
        return {
            "success": False,
            "code": "NO_ACTIVE_TWIN",
            "message": (
                "No retained print on this machine yet — the twin is "
                "recorded when Kiln starts a print."
            ),
        }
    gcode_path = rec.get("gcode")
    if not gcode_path or not os.path.isfile(str(gcode_path)):
        return {
            "success": False,
            "code": "TWIN_NOT_RETAINED",
            "file_name": rec.get("file_name"),
            "message": (
                "This print's sliced file wasn't retained — it was not "
                "sliced by Kiln on this machine (a pre-sliced upload, or a "
                "job started at the printer)."
            ),
        }

    try:
        from kiln.auth_session import resolve_api_bearer

        bearer = resolve_api_bearer()
        if not bearer.token:
            return {
                "success": False,
                "code": "SIGN_IN_REQUIRED",
                "message": (
                    "Publishing the print twin needs your Kiln sign-in — "
                    "run `kiln signin` on this machine."
                ),
            }

        raw = Path(str(gcode_path)).read_bytes()
        gz = gzip.compress(raw, compresslevel=6)
        if len(gz) > _MAX_GCODE_GZ_UPLOAD:
            return {
                "success": False,
                "code": "TWIN_TOO_LARGE",
                "message": "This print's toolpath is too large to publish.",
            }

        fields: list[tuple[str, str, bytes, str]] = [
            ("gcode", "toolpath.gcode.gz", gz, "application/gzip"),
            (
                "file_name",
                "",
                str(rec.get("file_name") or "").encode(),
                "",
            ),
        ]
        mesh_path = rec.get("mesh")
        if mesh_path and os.path.isfile(str(mesh_path)):
            mesh_name = os.path.basename(str(mesh_path))
            fields.insert(
                1,
                ("mesh", mesh_name, Path(str(mesh_path)).read_bytes(),
                 "application/octet-stream"),
            )

        body, content_type = _multipart(fields)
        import urllib.request

        request = urllib.request.Request(
            f"{_default_api_url()}/api/print-twin",
            data=body,
            headers={
                "Authorization": f"Bearer {bearer.token}",
                "Content-Type": content_type,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as resp:  # noqa: S310
            answer = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — a publish failure is an answer, not a crash
        logger.info("monitor_twin.publish failed: %s", exc)
        return {
            "success": False,
            "code": "PUBLISH_FAILED",
            "message": f"Couldn't publish the print twin: {exc}",
        }

    if answer.get("status") != "success":
        return {
            "success": False,
            "code": str(answer.get("code") or "PUBLISH_FAILED"),
            "message": str(answer.get("error") or "The server refused the twin."),
        }
    return {
        "success": True,
        "artifact_token": answer.get("artifact_token"),
        "stl_url": answer.get("stl_url"),
        "gcode_url": answer.get("gcode_url"),
        "format": answer.get("format"),
        "expires_in": answer.get("expires_in"),
        "file_name": rec.get("file_name"),
    }
