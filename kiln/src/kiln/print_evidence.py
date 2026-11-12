"""Facts about a print that can only be read while it happens.

Each print Kiln starts is one row (``print_outcomes`` in
:mod:`kiln.persistence`).  Beside its ending the row keeps four facts, each
written from a reading Kiln can vouch for and left empty otherwise:

* ``settings_code``: a one-way code of the settings block the sent file was
  sliced with (:func:`settings_code`), read at the start from the file Kiln
  sent.  Never the settings themselves;
* ``progress_at_end`` and ``layer_at_end``: how far the print had got at an
  ending Kiln watched (:func:`note_progress_at_end`);
* ``spool_id``: the recorded spool the person linked to the feed that printed
  it, when exactly one fed the print (:func:`note_start_spool`);
* ``firmware_version``: the version the printer reports for itself
  (:func:`note_start_firmware`).

Nothing here changes a verdict or a count, and nothing here raises into a
print.  On Kiln's servers no print is the caller's, and nothing is written.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "note_progress_at_end",
    "note_start_firmware",
    "note_start_settings",
    "note_start_spool",
    "progress_reading",
    "settings_code",
    "wire_firmware",
]

#: The pair of lines that open and close a slicer's settings block: what
#: PrusaSlicer writes, and what OrcaSlicer and Bambu Studio write.
_PRUSA_BLOCK = ("; prusaslicer_config = begin", "; prusaslicer_config = end")
_ORCA_BLOCK = ("; CONFIG_BLOCK_START", "; CONFIG_BLOCK_END")

#: One line of a settings block: ``; key = value``.
_SETTING_LINE = re.compile(r"^; ([a-z0-9_]+) = (.*)$")

#: A key carrying one of these words names a person, a place, a file or a
#: saved preset, and is left out of the code.
_NAMING_WORDS = frozenset({
    "name", "names", "filename", "path", "paths", "file", "files", "dir", "directory",
    "folder", "user", "username", "host", "hostname", "author", "note", "notes",
    "thumbnail", "thumbnails", "inherits", "compatible", "profile", "profiles",
    "preset", "presets", "apikey", "password", "token", "login", "cafile", "url",
    "id", "ids", "printhost",
})
#: Keys left out by their whole name.
_NAMING_KEYS = frozenset({
    "post_process", "objects_info", "prusaslicer_config", "bed_custom_model", "bed_custom_texture",
})
#: Fewer settings than this is not a settings block.
_MIN_SETTINGS = 5
#: What the code is salted with, so a change of recipe is a change of code.
_RECIPE = "kiln-settings-code-v1"
#: Hex digits kept of the code.
_CODE_CHARS = 32

#: The longest firmware text kept on a row.
_FIRMWARE_KEPT_CHARS = 64
#: The longest a start waits for a printer to say its firmware version.
_FIRMWARE_WAIT_S = 1.0


def _names_something(key: str) -> bool:
    return key in _NAMING_KEYS or any(word in _NAMING_WORDS for word in key.split("_"))


def _block(lines: list[str], opens: str, closes: str) -> list[str] | None:
    """The lines strictly between the last *opens* line and the *closes*
    line after it, or ``None`` when the pair is not whole."""
    start = None
    for number, line in enumerate(lines):
        text = line.strip()
        if text == opens:
            start = number
        elif text == closes and start is not None:
            return lines[start + 1:number]
    return None


def settings_code(lines: Iterable[str] | None) -> str | None:
    """A one-way code of the settings a sliced file carries, or ``None``.

    *lines* are the file's own top and end
    (:func:`kiln.file_metadata.sliced_gcode_lines`).  The code is made from a
    settings block that is whole in them: PrusaSlicer's, else OrcaSlicer's or
    Bambu Studio's unless Kiln wrote that header itself
    (:data:`kiln.gcode.PACKAGED_BY_KILN`).  Each ``key = value`` line counts
    once, its last statement winning, in key order, so the same settings give
    the same code whatever order the slicer wrote them in.  Keys that name a
    person, a place, a file or a saved preset are left out.  ``None`` for a
    file with no whole block, or fewer than :data:`_MIN_SETTINGS` settings in
    it.  Never raises.
    """
    try:
        from kiln.gcode import PACKAGED_BY_KILN

        held = [str(line).rstrip("\r\n") for line in (lines or [])]
        block = _block(held, *_PRUSA_BLOCK)
        if block is None and not any(line.strip().startswith(PACKAGED_BY_KILN) for line in held):
            block = _block(held, *_ORCA_BLOCK)
        if block is None:
            return None
        settings: dict[str, str] = {}
        for line in block:
            m = _SETTING_LINE.match(line.strip())
            if m is not None and not _names_something(m.group(1)):
                settings[m.group(1)] = m.group(2).strip()
        if len(settings) < _MIN_SETTINGS:
            return None
        digest = hashlib.sha256(_RECIPE.encode())
        for key in sorted(settings):
            digest.update(f"\n{key}={settings[key]}".encode("utf-8", "replace"))
        return digest.hexdigest()[:_CODE_CHARS]
    except Exception:  # noqa: BLE001 -- a file that cannot be read has no code
        logger.debug("no settings code made", exc_info=True)
        return None


def wire_firmware(value: Any) -> str | None:
    """*value* as a firmware version that may go to the person's account, or
    ``None``: the outbound guard's own test of a version
    (:func:`kiln.outbound_guard.version_text`), so nothing but a bare version
    is ever offered to the courier."""
    from kiln.outbound_guard import version_text

    return version_text(value)


def progress_reading(job: Any) -> tuple[float | None, int | None]:
    """``(share complete 0..1, layer number)`` as *job* (a
    :class:`kiln.printers.base.JobProgress`) reports them, each ``None``
    when the printer did not say.  A percentage outside 0..100 is no
    reading, and so is one still at zero while the layer counter has moved.
    """
    share: float | None = None
    layer: int | None = None
    raw_layer = getattr(job, "current_layer", None)
    if isinstance(raw_layer, (int, float)) and not isinstance(raw_layer, bool) and raw_layer >= 0:
        layer = int(raw_layer)
    raw = getattr(job, "completion", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0.0 <= float(raw) <= 100.0:
        share = round(float(raw) / 100.0, 4)
        if share == 0.0 and layer is not None and layer >= 1:
            share = None
    return share, layer


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- unsure is treated as hosted: write nothing
        return True


def _db() -> Any:
    from kiln.persistence import get_db

    return get_db()


def note_start_settings(job_id: str | None, lines: Iterable[str] | None) -> None:
    """Write the settings code of the file a start sent onto the row that
    start opened.  Never raises."""
    if not job_id or _hosted():
        return
    try:
        code = settings_code(lines)
        if code:
            _db().set_outcome_evidence(job_id, "settings_code", code)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("settings code not written for %s", job_id, exc_info=True)


def note_start_firmware(job_id: str | None, adapter: Any) -> None:
    """Write the firmware version *adapter* reports onto the row a start
    opened.  The printer is asked on a thread of its own and the start waits
    at most :data:`_FIRMWARE_WAIT_S` for it; an answer that comes later is
    still written.  Never raises."""
    if not job_id or _hosted():
        return
    reader = getattr(adapter, "reported_firmware_version", None)
    if not callable(reader):
        return

    def _ask() -> None:
        try:
            said = reader()
            text = re.sub(r"[\x00-\x1f\x7f]", "", said).strip()[:_FIRMWARE_KEPT_CHARS] if isinstance(said, str) else ""
            if text:
                _db().set_outcome_evidence(job_id, "firmware_version", text)
        except Exception:  # noqa: BLE001 -- a printer that does not say has no version
            logger.debug("firmware version not written for %s", job_id, exc_info=True)

    try:
        worker = threading.Thread(target=_ask, name="kiln-print-firmware", daemon=True)
        worker.start()
        worker.join(_FIRMWARE_WAIT_S)
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("firmware version not asked for %s", job_id, exc_info=True)


def note_start_spool(job_id: str | None, charges: list[dict[str, Any]] | None) -> None:
    """Write the spool that feeds a print onto the row its start opened.

    *charges* is what :mod:`kiln.spool_usage` took for the print.  A spool
    is named only when exactly one was charged and the person linked it to
    the feed that prints (the charge carries that link's label); a spool
    paired to a tray by colour, and a print fed by several, name none.
    Never raises.
    """
    if not job_id or _hosted():
        return
    try:
        if not isinstance(charges, list) or len(charges) != 1:
            return
        charge = charges[0]
        spool_id = charge.get("spool_id")
        if charge.get("label") is None or not isinstance(spool_id, str) or not spool_id.strip():
            return
        _db().set_outcome_evidence(job_id, "spool_id", spool_id.strip())
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("spool not written for %s", job_id, exc_info=True)


def note_progress_at_end(
    printer_names: list[str], job_label: str | None, job: Any, *, settled: bool = False,
) -> bool:
    """Write how far the print ending under *job_label* had got, from *job*,
    the printer's reading at that ending.  The caller vouches that the
    ending was watched.  Returns whether a row took it; never raises."""
    if _hosted():
        return False
    try:
        share, layer = progress_reading(job)
        if share is None and layer is None:
            return False
        return bool(_db().record_progress_at_end(
            printer_names, job_label, fraction=share, layer=layer, settled=settled,
        ))
    except Exception:  # noqa: BLE001 -- bookkeeping never affects a print
        logger.debug("progress at the ending not written for %s", job_label, exc_info=True)
        return False
