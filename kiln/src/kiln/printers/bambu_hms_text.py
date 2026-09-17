"""The sentence a Bambu printer's own screen shows for a fault code.

When a Bambu printer faults, its screen shows the code and an English
sentence -- ``[1200-8001 290420]`` over "Cutting the filament failed. Please
check to see if the cutter is stuck. Refer to the Assistant for solutions."
The sentence is not on the wire.  The MQTT ``print`` report carries the
code alone (``print_error``, one integer; the ``hms`` array's ``attr`` /
``code`` pairs), and Bambu's own desktop client looks the words up the
same way this module does: it asks Bambu's HMS text service for the table
for this device type and keeps a copy on disk (BambuStudio ``HMS.cpp``,
``HMSQuery::download_hms_related`` / ``_query_error_msg``; the table's rows
are ``{"ecode": "12008001", "intro": "..."}`` under ``device_error`` for
the 8-hex ``print_error`` namespace and ``device_hms`` for the 16-hex HMS
namespace).

Kiln does NOT ship the table.  The sentences are Bambu's, the vendor
revises them (the version stamp is a date-time and moved on the day this
was written), and the A1's table is not among the ones Bambu's own client
vendors -- it is served, per device type, from the vendor.  So Kiln asks
the same service, for the same device type, and names the source beside
every sentence it repeats.  Where the vendor has no sentence for a code the
field is simply absent: this module never composes one.

Design mirrors :mod:`kiln.version_check`, for the same reasons:

* stdlib :mod:`urllib` for the fetch -- nothing to install;
* a disk cache under ``~/.kiln/bambu_hms/`` with a 24h TTL, one file per
  device type, exactly as the vendor's client keeps ``hms_en_<type>.json``;
* the fetch runs on a daemon thread and **never blocks** a status read --
  a reader gets whatever is on disk and the thread warms it for next time;
* opt-out via ``KILN_NO_BAMBU_HMS_TEXT`` (or the generic ``KILN_OFFLINE``);
* every failure path is non-fatal: no network, the service down, a corrupt
  cache, an unknown device type -- the answer is "no sentence on record".

The request names the device TYPE (the first three characters of the
serial, e.g. ``039`` for an A1), not the unit, and carries no credentials.
The type matters: the A1's table holds 82 codes the default table does
not, ``12008001`` among them (measured 2026-09-17).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: The source class named beside every sentence this module hands back.
#: A class, not a URL: the sentence is the vendor's, and this says so.
BAMBU_HMS_TEXT_SOURCE = "bambu_hms_service"

# The vendor's HMS text service, as BambuStudio's production build resolves
# it (``AppConfig::get_hms_host`` -> ``e.bambulab.com``; ``HMS.cpp`` builds
# ``query.php?lang=<lang>&d=<device type>``).  English only: Kiln's own
# readings are English, and one language keeps the cache one file.
_QUERY_URL = "https://e.bambulab.com/query.php?lang=en&d={device_type}"

# The vendor's client refreshes its copy once a day and retries after a
# minute when it holds nothing; same here.
_CACHE_TTL_SECONDS = 24 * 3600
_RETRY_AFTER_FAILURE_SECONDS = 60.0
_FETCH_TIMEOUT = 8.0

#: The two namespaces the service publishes, keyed by the wire's own
#: section names.  ``device_error`` rows are 8 hex digits (``print_error``);
#: ``device_hms`` rows are 16 (the ``hms`` array's ``attr`` + ``code``).
_SECTIONS: tuple[str, ...] = ("device_error", "device_hms")

# Process-wide state: one table per device type, one in-flight fetch per
# device type, and the last failure time so a dead network is not re-asked
# on every poll.
_lock = threading.Lock()
_tables: dict[str, dict[str, Any]] = {}
_in_flight: set[str] = set()
_last_failure: dict[str, float] = {}


def text_lookup_enabled() -> bool:
    """Whether this install may ask the vendor for its fault sentences.

    On by default.  Off when ``KILN_NO_BAMBU_HMS_TEXT`` or the generic
    ``KILN_OFFLINE`` is set to a truthy value -- CI, air-gapped boxes, and
    anyone who does not want the call.  Off means the disk cache is still
    read; only the fetch is skipped.
    """
    truthy = ("1", "true", "yes", "on")
    disabled = (
        os.environ.get("KILN_NO_BAMBU_HMS_TEXT", "").strip().lower() in truthy
        or os.environ.get("KILN_OFFLINE", "").strip().lower() in truthy
    )
    return not disabled


def device_type_from_serial(serial: str | None) -> str:
    """The device type the vendor keys its tables by: the serial's first three.

    ``""`` for anything shorter, and the caller treats ``""`` as "no table
    to ask for".  Upper-cased because the vendor's own client compares the
    codes upper-cased and the type is part of the file name.
    """
    text = str(serial or "").strip()
    if len(text) < 3:
        return ""
    return text[:3].upper()


def _cache_dir() -> Path:
    # Resolved at call time so ``HOME`` overrides (tests, sandboxes) work.
    return Path.home() / ".kiln" / "bambu_hms"


def _cache_path(device_type: str) -> Path:
    return _cache_dir() / f"hms_en_{device_type}.json"


def _load_cache(device_type: str) -> dict[str, Any] | None:
    try:
        with _cache_path(device_type).open() as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    for section in _SECTIONS:
        if not isinstance(data.get(section), dict):
            return None
    return data


def _write_cache(device_type: str, table: dict[str, Any]) -> None:
    try:
        path = _cache_path(device_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(table, f)
        os.replace(tmp, path)
    except OSError as exc:
        _logger.debug("Bambu HMS text cache write failed: %s", exc)


def _is_stale(table: dict[str, Any]) -> bool:
    try:
        fetched_at = float(table.get("fetched_at", 0))
    except (TypeError, ValueError):
        return True
    return (time.time() - fetched_at) > _CACHE_TTL_SECONDS


def _compact(payload: Any, device_type: str) -> dict[str, Any] | None:
    """The vendor's answer as ``{section: {ECODE: intro}}``, or ``None``.

    The service answers ``{"result": 0, "ver": ..., "data": {"device_hms":
    {"ver": ..., "en": [{"ecode", "intro"}, ...]}, "device_error": {...}}}``
    for a known type, and ``{"result": 201, "ver": 0}`` for an unknown one.
    Anything that is not the first shape is "no table", never a partial
    one: a half-read table would answer some codes and silently not others.
    """
    if not isinstance(payload, dict) or payload.get("result") != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    table: dict[str, Any] = {
        "device_type": device_type,
        "ver": payload.get("ver"),
        "fetched_at": time.time(),
    }
    for section in _SECTIONS:
        rows = (data.get(section) or {}).get("en")
        if not isinstance(rows, list):
            return None
        mapping: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ecode = row.get("ecode")
            intro = row.get("intro")
            if isinstance(ecode, str) and isinstance(intro, str) and intro.strip():
                mapping[ecode.strip().upper()] = intro.strip()
        table[section] = mapping
    return table


def _fetch_table(device_type: str) -> dict[str, Any] | None:
    """One request to the vendor -> a compact table, or ``None``.

    Module-level so a test suite can stub it (the conftest does), the way
    ``version_check._fetch_latest_from_pypi`` is stubbed: the surrounding
    machinery stays exercised and no bytes leave the box.
    """
    try:
        import urllib.request

        from kiln.version_check import PACKAGE_NAME, _current_version

        req = urllib.request.Request(
            _QUERY_URL.format(device_type=device_type),
            headers={
                "Accept": "application/json",
                "User-Agent": f"{PACKAGE_NAME}/{_current_version()}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status >= 300:
                _logger.debug("Bambu HMS text fetch status: %s", resp.status)
                return None
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 -- network/JSON errors are non-fatal
        _logger.debug("Bambu HMS text fetch failed (non-fatal): %s", exc)
        return None
    return _compact(payload, device_type)


def _refresh_runner(device_type: str) -> None:
    try:
        table = _fetch_table(device_type)
        with _lock:
            if table is not None:
                _tables[device_type] = table
                _last_failure.pop(device_type, None)
            else:
                _last_failure[device_type] = time.monotonic()
        if table is not None:
            _write_cache(device_type, table)
    finally:
        with _lock:
            _in_flight.discard(device_type)


def kick_background_refresh(device_type: str) -> bool:
    """Start one background fetch for *device_type* unless one is running.

    Returns immediately, with whether a thread was started.  Safe to call on
    every status read: the in-flight guard, the TTL and the failure back-off
    collapse repeated calls into at most one request a day per device type
    on a working network, and one a minute on a dead one.
    """
    if not device_type or not text_lookup_enabled():
        return False
    with _lock:
        if device_type in _in_flight:
            return False
        table = _tables.get(device_type)
        if table is not None and not _is_stale(table):
            return False
        failed_at = _last_failure.get(device_type)
        if (
            failed_at is not None
            and (time.monotonic() - failed_at) < _RETRY_AFTER_FAILURE_SECONDS
        ):
            return False
        _in_flight.add(device_type)
    threading.Thread(
        target=_refresh_runner,
        args=(device_type,),
        name=f"kiln-bambu-hms-text-{device_type}",
        daemon=True,
    ).start()
    return True


def _table_for(device_type: str) -> dict[str, Any] | None:
    """The table on record for *device_type*: memory first, then disk.

    Reads only.  A missing or expired table is reported to the caller by
    :func:`kick_background_refresh`, never fetched here -- this is on the
    status path.
    """
    with _lock:
        table = _tables.get(device_type)
        if table is None:
            table = _load_cache(device_type)
            if table is not None:
                _tables[device_type] = table
    return table


def lookup_screen_text(
    code: str | None, *, device_type: str, kind: str
) -> tuple[str, str] | None:
    """``(sentence, source)`` the vendor publishes for *code*, or ``None``.

    *code* is matched exactly, in the vendor's own spelling -- hex digits
    only, upper-cased, 8 of them for ``kind="print_error"`` and 16 for
    ``kind="hms"`` -- so ``"1200-8001"``, ``"1200_8001"`` and ``"12008001"``
    all find the same row and a code the vendor does not list finds nothing.
    No family fallback, no nearest match: a sentence from the wrong row is
    worse than no sentence.

    Never blocks.  Answers from the table on record and, when there is none
    or it has expired, asks for a fresh one in the background so the NEXT
    reading can answer.
    """
    if not code or not device_type:
        return None
    section = {"print_error": "device_error", "hms": "device_hms"}.get(kind)
    if section is None:
        return None
    hex_only = "".join(c for c in str(code).upper() if c in "0123456789ABCDEF")
    want = {"device_error": 8, "device_hms": 16}[section]
    if len(hex_only) != want:
        return None
    table = _table_for(device_type)
    if table is None or _is_stale(table):
        kick_background_refresh(device_type)
    if table is None:
        return None
    sentence = table.get(section, {}).get(hex_only)
    if not isinstance(sentence, str) or not sentence:
        return None
    return sentence, BAMBU_HMS_TEXT_SOURCE


def _reset_for_tests() -> None:
    """Forget every table and in-flight marker.  Tests only."""
    with _lock:
        _tables.clear()
        _in_flight.clear()
        _last_failure.clear()
