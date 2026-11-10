"""Which part a print file is a print of, kept from the slice to the print.

Contract
--------
* :func:`record_slice` is called once per successful slice, at the slice
  runners' shared tail (``kiln.slicer._record_slice``).  It writes one short
  comment line at the END of the sliced G-code (the stamp) and keeps one
  record on this computer: the stamp's id, the sliced file's name and content
  hash, the model's content hash and one-way shape signatures, and what the
  sliced file itself states (material, layer height, temperatures, printer
  profile, size).
* :func:`note_wrapped` is called where a G-code is wrapped into a printer
  package, so the package's name and content hash join the same record.
* :func:`resolve` is the ONE answer to "what is this print file a print of".
  It is called at the print-start chokepoint
  (``kiln.auto_record_hook.print_identity_at_start``, reached only from
  ``PrinterAdapter.start_print``) and by the contribution resolver
  (``kiln.community_autofire.geometric_signatures_for``).  It answers by the
  stamp in the file, else the file's content hash, else its name; a file
  Kiln did not slice that is on this disk is answered ``no_model`` with what
  the file states; a file Kiln cannot see is answered ``unknown``.

Safety invariants
-----------------
* The stamp is a G-code comment appended after the slicer's last line.  It
  changes no command line of the file (pinned by
  ``tests/test_print_lineage.py``, which compares every non-comment line).
* Nothing here raises into a slice or a print, and nothing here decides
  whether either happens.
* The record holds content hashes, signatures and figures: no path, so it
  cannot rot when a file moves.  File names are kept on this computer for
  the name join and are never part of anything sent.
* Unreadable, truncated or odd files yield ``None`` for the figure, never a
  guess and never a non-finite number.
* On Kiln's shared servers the store is neither read nor written.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The stamp line's key; the whole line is ``; kiln_lineage = <id>``.
STAMP_KEY = "kiln_lineage"
_STAMP = re.compile(r"^\s*;\s*kiln_lineage\s*=\s*([0-9a-f]{16})\s*$")
_ID = re.compile(r"^[0-9a-f]{16}$")

#: Records kept, oldest dropped first.
MAX_RECORDS = 2000
#: Print-start pins kept (printer-side name -> record id).
MAX_STARTS = 500

#: A print of a model Kiln sliced on this computer.
PART_SLICED_HERE = "sliced_here"
#: A print file on this disk that Kiln did not slice: no model behind it.
PART_NO_MODEL = "no_model"
#: A print file Kiln cannot see; nothing is known about the part.
PART_UNKNOWN = "unknown"

#: Whether a shape signature found through a slice record may be handed to
#: the anonymous print-result sharing.  Off: the published privacy wording
#: for that sharing does not yet name the shape signature, and wording is
#: signed off before data flows.  The local record is unaffected.
SHARE_SLICE_SIGNATURES = False

_lock = threading.Lock()
_workers: list[threading.Thread] = []


def store_path() -> Path:
    # Resolved at call time so a moved HOME (tests, sandboxes) is honoured.
    return Path.home() / ".kiln" / "print_lineage.json"


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- an unknown posture keeps no record
        return True


def _load() -> dict[str, Any]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    records = data.get("records")
    starts = data.get("starts")
    return {
        "records": [r for r in records if isinstance(r, dict)] if isinstance(records, list) else [],
        "starts": [s for s in starts if isinstance(s, dict)] if isinstance(starts, list) else [],
    }


def _save(data: dict[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"records": data["records"][-MAX_RECORDS:], "starts": data["starts"][-MAX_STARTS:]}
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _sha256(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _number(value: Any) -> float | None:
    """A finite number, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# What a print file states about itself
# ---------------------------------------------------------------------------


def stated_facts(path: str) -> dict[str, Any]:
    """What the print file at *path* says about itself.

    Read by the readers Kiln already has (``kiln.gcode_metadata`` for the
    slicer's own comments, ``kiln.printers.bed_fit`` for the size of the
    toolpath).  Every figure is a finite number or ``None``; a word is a
    short string or ``None``.  Never raises.
    """
    facts: dict[str, Any] = {
        "material": None, "layer_height_mm": None, "nozzle_temp_c": None, "bed_temp_c": None,
        "printer_model": None, "slicer": None, "size_mm": None,
    }
    try:
        from kiln.file_metadata import sliced_gcode_lines
        from kiln.gcode_metadata import _extract_from_lines

        lines = sliced_gcode_lines(path)
        if lines:
            meta = _extract_from_lines(lines)
            for key, value in (("material", meta.material), ("printer_model", meta.printer_model), ("slicer", meta.slicer)):
                if isinstance(value, str) and value.strip():
                    facts[key] = value.strip()[:80]
            facts["layer_height_mm"] = _positive(meta.layer_height)
            facts["nozzle_temp_c"] = _positive(meta.tool_temp)
            facts["bed_temp_c"] = _positive(meta.bed_temp)
    except Exception:  # noqa: BLE001 -- a file that cannot be read states nothing
        logger.debug("print file's own figures not read", exc_info=True)
    try:
        facts["size_mm"] = _toolpath_size(path)
    except Exception:  # noqa: BLE001
        logger.debug("print file's size not read", exc_info=True)
    return facts


def _positive(value: Any) -> float | None:
    value = _number(value)
    return round(value, 3) if value is not None and 0 < value < 1e5 else None


def _toolpath_size(path: str) -> dict[str, float] | None:
    """The print's extent as ``{"x", "y", "z"}`` in mm: the width and depth
    of its toolpath (``bed_fit``, only when the scan read the whole file)
    and its height (``plate_state.geometry_of``).  Each figure is present
    only when it was read and is finite."""
    from kiln.plate_state import geometry_of
    from kiln.printers import bed_fit

    out: dict[str, float] = {}
    box = bed_fit.compute_3mf_bbox(path) if path.lower().endswith(".3mf") else bed_fit.compute_gcode_bbox(path)
    if isinstance(box, dict) and not box.get("truncated"):
        for axis in ("x", "y"):
            low, high = _number(box.get(f"{axis}_min")), _number(box.get(f"{axis}_max"))
            if low is not None and high is not None and 0 < high - low < 1e5:
                out[axis] = round(high - low, 2)
    top = _number(geometry_of(path)[1])
    if top is not None and 0 < top < 1e5:
        out["z"] = round(top, 2)
    return out or None


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------


def _stamp(gcode_path: str, lineage_id: str) -> bool:
    """Append the stamp line to *gcode_path*.  Only ever adds a comment."""
    try:
        with open(gcode_path, "rb+") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size:
                fh.seek(size - 1)
                last = fh.read(1)
                fh.seek(0, os.SEEK_END)
            else:
                last = b"\n"
            line = f"; {STAMP_KEY} = {lineage_id}\n".encode("ascii")
            fh.write(line if last in (b"\n", b"\r") else b"\n" + line)
        return True
    except OSError:
        return False


def read_stamps(path: str) -> list[str]:
    """Every distinct stamp id in the print file at *path*, in file order.

    Reads the window every reader of a sliced file's own figures reads
    (``kiln.file_metadata.sliced_gcode_lines``), so a plain G-code and the
    plate inside a printer package are read alike.  Never raises.
    """
    return _stamps_of_print_file(path) or []


def _stamps_of_print_file(path: str) -> list[str] | None:
    """The stamps in *path*, or ``None`` when it is not a file a printer
    prints (a model, a missing or unreadable file).  One read of the file."""
    try:
        from kiln.file_metadata import sliced_gcode_lines

        lines = sliced_gcode_lines(path) if os.path.isfile(path) else None
        if lines is None:
            return None
        found: list[str] = []
        for line in lines:
            if STAMP_KEY not in line:
                continue
            match = _STAMP.match(line)
            if match and match.group(1) not in found:
                found.append(match.group(1))
        return found
    except Exception:  # noqa: BLE001 -- an unreadable file carries no stamp
        return None


# ---------------------------------------------------------------------------
# Written at the slice and the wrap
# ---------------------------------------------------------------------------


def record_slice(
    model_path: str | None,
    output_path: str | None,
    *,
    profile: str | None = None,
    source_path: str | None = None,
) -> str | None:
    """A slice just wrote *output_path* from *model_path*: stamp it, keep it.

    *source_path* is the file the slicer read when it is not the mesh the
    slice is drawn as (a CAD file); its shape signature is left empty,
    because a signature of one conversion of it is not a signature of the
    part.  Returns the record's id, or ``None`` when nothing was kept.
    Never raises.
    """
    if not model_path or not output_path or _hosted():
        return None
    try:
        if not os.path.isfile(output_path):
            return None
        lineage_id = uuid.uuid4().hex[:16]
        stamped = _stamp(output_path, lineage_id)
        sliced_from = source_path or model_path
        record: dict[str, Any] = {
            "id": lineage_id,
            "at": time.time(),
            "stamped": stamped,
            "files": [{"name": os.path.basename(output_path), "sha256": _sha256(output_path), "kind": "gcode"}],
            "model": {"sha256": _sha256(sliced_from), "signature": "", "signature_v2": ""},
            "profile": Path(profile).stem[:64] if profile else None,
            "stated": stated_facts(output_path),
        }
        with _lock:
            data = _load()
            data["records"].append(record)
            _save(data)
        if not source_path:
            # The signatures read every triangle; the slice does not wait.
            worker = threading.Thread(
                target=_fill_signatures, args=(lineage_id, model_path), name="kiln-print-lineage", daemon=False,
            )
            _workers.append(worker)
            worker.start()
        return lineage_id
    except Exception:  # noqa: BLE001 -- a record never costs a slice
        logger.debug("slice lineage not recorded", exc_info=True)
        return None


def _fill_signatures(lineage_id: str, model_path: str) -> None:
    try:
        from kiln.community_autofire import signatures_of_model

        signature, signature_v2 = signatures_of_model(model_path)
        if not signature:
            return
        with _lock:
            data = _load()
            for record in data["records"]:
                if record.get("id") == lineage_id and isinstance(record.get("model"), dict):
                    record["model"]["signature"] = signature
                    record["model"]["signature_v2"] = signature_v2
                    _save(data)
                    break
    except Exception:  # noqa: BLE001
        logger.debug("slice lineage signatures not filled", exc_info=True)


def flush(timeout: float = 60.0) -> None:
    """Wait for signatures still being read (a caller about to read them)."""
    deadline = time.time() + timeout
    for worker in list(_workers):
        worker.join(max(0.0, deadline - time.time()))
        if not worker.is_alive():
            with contextlib.suppress(ValueError):
                _workers.remove(worker)


def note_wrapped(gcode_path: str | None, wrapped_path: str | None) -> None:
    """*gcode_path* was wrapped into *wrapped_path*: the package joins the
    G-code's record under its own name and content hash.  Never raises."""
    if not gcode_path or not wrapped_path or _hosted():
        return
    try:
        stamps = read_stamps(gcode_path)
        gcode_sha = _sha256(gcode_path) if not stamps else None
        entry = {"name": os.path.basename(wrapped_path), "sha256": _sha256(wrapped_path), "kind": "wrap"}
        with _lock:
            data = _load()
            record = _find(data, stamps=stamps, sha=gcode_sha, name=None)
            if record is None:
                return
            files = [f for f in record.get("files", []) if isinstance(f, dict) and f.get("name") != entry["name"]]
            record["files"] = files + [entry]
            _save(data)
    except Exception:  # noqa: BLE001 -- a record never costs a wrap
        logger.debug("wrap lineage not recorded", exc_info=True)


# ---------------------------------------------------------------------------
# The one resolver
# ---------------------------------------------------------------------------


def _find(data: dict[str, Any], *, stamps: list[str], sha: str | None, name: str | None) -> dict[str, Any] | None:
    records = data["records"]
    if len(stamps) == 1:
        for record in reversed(records):
            if record.get("id") == stamps[0]:
                return record
    if sha:
        for record in reversed(records):
            if any(isinstance(f, dict) and f.get("sha256") == sha for f in record.get("files", [])):
                return record
    if name:
        for record in reversed(records):
            if any(isinstance(f, dict) and f.get("name") == name for f in record.get("files", [])):
                return record
    return None


def _answer(record: dict[str, Any], matched_by: str) -> dict[str, Any]:
    model = record.get("model") if isinstance(record.get("model"), dict) else {}
    kind = record.get("kind") or PART_SLICED_HERE
    return {
        "part": kind,
        "lineage_id": record.get("id"),
        "matched_by": matched_by,
        "model_sha256": model.get("sha256") or None,
        "signature": model.get("signature") or "",
        "signature_v2": model.get("signature_v2") or "",
        "profile": record.get("profile"),
        "stated": record.get("stated") if isinstance(record.get("stated"), dict) else {},
    }


_NOTHING: dict[str, Any] = {
    "part": PART_UNKNOWN, "lineage_id": None, "matched_by": None, "model_sha256": None,
    "signature": "", "signature_v2": "", "profile": None, "stated": {},
}


def resolve(file_name: str | None, *, local_paths: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """What *file_name* (as the printer knows it) is a print of.

    *local_paths* are copies of that file on this disk, most trusted first.
    The answer's ``part`` is one of :data:`PART_SLICED_HERE` (with the
    model's content hash and, when read, its shape signatures),
    :data:`PART_NO_MODEL` (a file on this disk Kiln did not slice, with what
    the file states) or :data:`PART_UNKNOWN`.  ``matched_by`` says how:
    ``stamp``, ``content``, ``name`` or ``start`` (the record pinned when
    this name last started).  A file carrying the stamps of several slices
    is not one part and is answered by content or name only.  Never raises.
    """
    if _hosted():
        return dict(_NOTHING)
    try:
        base = os.path.basename(str(file_name or ""))
        read = [(p, _stamps_of_print_file(p)) for p in local_paths if isinstance(p, str) and p]
        local = [p for p, stamps in read if stamps is not None]
        with _lock:
            data = _load()
        stamped_elsewhere = False
        for path, stamps in read:
            if stamps is None:
                continue
            if len(stamps) == 1:
                record = _find(data, stamps=stamps, sha=None, name=None)
                if record is not None:
                    return _answer(record, "stamp")
            record = _find(data, stamps=[], sha=_sha256(path), name=None)
            if record is not None:
                return _answer(record, "content")
            stamped_elsewhere = stamped_elsewhere or bool(stamps)
        if stamped_elsewhere:
            # A Kiln slice this computer keeps no record of (another
            # computer's, or one older than the records kept): it has a
            # model, and this computer cannot say which.
            return dict(_NOTHING)
        if not local and base:
            for start in reversed(data["starts"]):
                if start.get("name") == base:
                    record = next((r for r in reversed(data["records"]) if r.get("id") == start.get("id")), None)
                    if record is not None:
                        return _answer(record, "start")
                    break
            record = _find(data, stamps=[], sha=None, name=base)
            if record is not None:
                return _answer(record, "name")
        if local:
            return _keep_foreign(local[0], base or os.path.basename(local[0]))
    except Exception:  # noqa: BLE001 -- an unanswerable file is an unknown part
        logger.debug("print lineage not resolved", exc_info=True)
    return dict(_NOTHING)


def _keep_foreign(path: str, name: str) -> dict[str, Any]:
    """Keep and answer a print file Kiln did not slice: no model, and what
    the file states."""
    record: dict[str, Any] = {
        "id": uuid.uuid4().hex[:16],
        "at": time.time(),
        "kind": PART_NO_MODEL,
        "stamped": False,
        "files": [{"name": name, "sha256": _sha256(path), "kind": "foreign"}],
        "model": {"sha256": None, "signature": "", "signature_v2": ""},
        "profile": None,
        "stated": stated_facts(path),
    }
    with _lock:
        data = _load()
        data["records"].append(record)
        _save(data)
    return _answer(record, "content")


def note_print_started(file_name: str | None, lineage_id: str | None) -> None:
    """Pin the record a print of *file_name* just started with, so its
    ending is answered by this record whatever is sliced to that name
    later.  Never raises."""
    if not file_name or not isinstance(lineage_id, str) or not _ID.match(lineage_id) or _hosted():
        return
    try:
        base = os.path.basename(str(file_name))
        with _lock:
            data = _load()
            data["starts"] = [s for s in data["starts"] if s.get("name") != base]
            data["starts"].append({"name": base, "id": lineage_id, "at": time.time()})
            _save(data)
    except Exception:  # noqa: BLE001
        logger.debug("print-start lineage pin not written", exc_info=True)


def local_copies(file_name: str | None, kwargs: dict[str, Any] | None = None) -> list[str]:
    """The copies of a printer-side *file_name* on this disk: the one the
    door named, the name itself when it is a path, the file Kiln sliced or
    wrapped under that name, the copy this session uploaded.  Never raises."""
    out: list[str] = []
    try:
        named = next(
            (
                kwargs[k]
                for k in ("local_file_path", "source_path", "local_path", "gcode_path", "file_path", "threemf_path")
                if isinstance((kwargs or {}).get(k), str) and (kwargs or {})[k]
            ),
            None,
        )
        from kiln.plate_state import _local_candidates

        out.extend(_local_candidates(file_name, named))
    except Exception:  # noqa: BLE001
        logger.debug("local copies not listed", exc_info=True)
    try:
        from kiln.spool_usage import _uploaded_copy

        uploaded = _uploaded_copy(file_name)
        if uploaded and os.path.isfile(uploaded) and uploaded not in out:
            out.append(uploaded)
    except Exception:  # noqa: BLE001
        pass
    return out


def signatures_for(file_name: str | None) -> tuple[str, str]:
    """The ``(v1, v2)`` shape signatures of the model a print of *file_name*
    was sliced from on this computer, or ``("", "")``.  Never raises."""
    flush(timeout=30.0)
    answer = resolve(file_name)
    if answer["part"] != PART_SLICED_HERE:
        return ("", "")
    return (answer["signature"], answer["signature_v2"])


__all__ = [
    "PART_NO_MODEL",
    "PART_SLICED_HERE",
    "PART_UNKNOWN",
    "SHARE_SLICE_SIGNATURES",
    "STAMP_KEY",
    "flush",
    "local_copies",
    "note_print_started",
    "note_wrapped",
    "read_stamps",
    "record_slice",
    "resolve",
    "signatures_for",
    "stated_facts",
]
