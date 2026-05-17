"""Map printer-side file names back to their local source paths.

The printer reports the currently-printing file by its *uploaded*
name (e.g. ``"MyCoaster.gcode.3mf"`` on a Bambu A1).  Several downstream
surfaces want the *source* mesh path — most prominently the D3 sidecar
derivation on ``monitor_print`` / ``await_print_completion`` (so the
brief tail can auto-attach without the user re-typing brief_id) and
future "open in slicer" / "re-export" actions.

Without a mapping table we can't recover the source path from the
printer's file_name alone — the uploader strips it down to a leaf name.
This module is that table:

- :func:`record_upload` writes ``(printer_file_name → source_path)`` to
  ``~/.kiln/upload_manifest.json`` after a successful upload.
- :func:`resolve_source_path` reads back the most recent source path
  recorded for a given printer file name.

The manifest is a bounded ring (default 500 entries) so it doesn't
grow without limit; oldest entries are dropped first.  Storage layout
is plain JSON — easy to grep, easy to diff, easy to delete.

Failure modes are best-effort throughout: a missing / unwritable /
corrupt manifest never breaks the upload or monitor path.  The
manifest is an enrichment, never a correctness gate.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MANIFEST_PATH = Path.home() / ".kiln" / "upload_manifest.json"
_MAX_ENTRIES = 500


def _manifest_path(override: Path | str | None = None) -> Path:
    """Resolve the manifest file path (override for tests)."""
    if override is not None:
        return Path(override)
    return _DEFAULT_MANIFEST_PATH


def _load_entries(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read the manifest from disk; return [] on any failure."""
    p = _manifest_path(path)
    if not p.is_file():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except (OSError, ValueError) as exc:
        logger.debug("upload_manifest: read failed (%s)", exc)
    return []


def _atomic_write(path: Path, payload: str) -> None:
    """Write *payload* to *path* atomically via mkstemp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_upload(
    source_path: str,
    printer_file_name: str,
    *,
    manifest_path: Path | str | None = None,
    max_entries: int = _MAX_ENTRIES,
) -> bool:
    """Append a (source_path → printer_file_name) mapping to the manifest.

    Returns True on a clean write, False on any failure (write errors
    are debug-logged so the upload path never crashes on a manifest IO
    problem).

    The newest entry wins for resolve_source_path lookups; older entries
    for the same printer_file_name remain in the manifest but are
    ignored by readers — useful as an audit trail when debugging which
    source produced which print.
    """
    if not source_path or not printer_file_name:
        return False
    try:
        path = _manifest_path(manifest_path)
        entries = _load_entries(path)
        entries.append({
            "printer_file_name": printer_file_name,
            "source_path": source_path,
            "ts": time.time(),
        })
        # Bounded ring — drop oldest when over cap.
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
        _atomic_write(path, json.dumps(entries, separators=(",", ":")))
        return True
    except Exception:
        logger.debug(
            "upload_manifest: record_upload failed", exc_info=True,
        )
        return False


def resolve_source_path(
    printer_file_name: str,
    *,
    manifest_path: Path | str | None = None,
) -> str | None:
    """Return the most recent source path for *printer_file_name*, or None.

    Best-effort: missing / corrupt / unreadable manifest returns None.
    The lookup is O(n) over the manifest; n is bounded at
    :data:`_MAX_ENTRIES` (default 500) so this stays cheap.
    """
    if not printer_file_name:
        return None
    entries = _load_entries(manifest_path)
    # Newest-first scan
    for entry in reversed(entries):
        if entry.get("printer_file_name") == printer_file_name:
            src = entry.get("source_path")
            if isinstance(src, str) and src:
                return src
    return None


__all__ = ["record_upload", "resolve_source_path"]
