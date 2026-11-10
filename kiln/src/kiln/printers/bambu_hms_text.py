"""The sentence a Bambu printer's own screen shows for a fault code."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# : The source class named beside every sentence this module hands back
BAMBU_HMS_TEXT_SOURCE = "bambu_hms_service"

_QUERY_URL = "https://e.bambulab.com/query.php?lang=en&d={device_type}"

_CACHE_TTL_SECONDS = 24 * 3600
_RETRY_AFTER_FAILURE_SECONDS = 60.0
_FETCH_TIMEOUT = 8.0

_SECTIONS: tuple[str, ...] = ("device_error", "device_hms")

_lock = threading.Lock()
_tables: dict[str, dict[str, Any]] = {}
_in_flight: set[str] = set()
_last_failure: dict[str, float] = {}


def text_lookup_enabled() -> bool:
    """Whether this install may ask the vendor for its fault sentences."""
    truthy = ("1", "true", "yes", "on")
    disabled = (
        os.environ.get("KILN_NO_BAMBU_HMS_TEXT", "").strip().lower() in truthy
        or os.environ.get("KILN_OFFLINE", "").strip().lower() in truthy
    )
    return not disabled


def device_type_from_serial(serial: str | None) -> str:
    """The device type the vendor keys its tables by: the serial's first three."""
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
        with _cache_path(device_type).open(encoding="utf-8") as f:
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
        with tmp.open("w", encoding="utf-8") as f:
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
    """The vendor's answer as ``{section: {ECODE: intro}}``, or ``None``."""
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
    """One request to the vendor -> a compact table, or ``None``."""
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
    """Start one background fetch for *device_type* unless one is running."""
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
    """The table on record for *device_type*: memory first, then disk."""
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
    """``(sentence, source)`` the vendor publishes for *code*, or ``None``."""
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
    """Forget every table and in-flight marker."""
    with _lock:
        _tables.clear()
        _in_flight.clear()
        _last_failure.clear()
