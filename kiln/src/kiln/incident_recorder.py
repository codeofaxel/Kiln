"""Incident auto-recording: capture a full evidence envelope on dangerous print failures.

When a print fails in a way that matters for safety or learning — user cancels
the job before the first significant layer, a nozzle crash is suspected, a
thermal anomaly trips, or the firmware raises an HMS error — this module
snapshots every artifact we have about the event into a single, self-contained
directory under ``~/.kiln/incidents/``.  Each incident directory is a complete
evidence envelope: it can be zipped, emailed to support, diffed against a
second incident for pattern mining, or (once anonymized) contributed to the
community intelligence registry.

Design goals
------------
1. **Never crash the caller.**  Incident recording happens *after* a bad
   event — the caller is usually already in a degraded code path.  A failure
   inside the recorder (missing file, permission error on an artifact,
   unserializable status dict) must be logged and swallowed, never raised.
2. **Copy, never symlink.**  Referenced files (STL, gcode, 3mf, camera JPG)
   are ``shutil.copy2``'d into the incident directory.  Symlinks would rot
   as soon as temp files got cleaned up, and the whole point is preserving
   evidence that outlives the original session.
3. **Machine + human readable.**  Every incident has ``incident.json`` (full
   field dump for tooling) *and* ``report.md`` (scannable by a human without
   tools).  Human comes first when triaging — that's why the markdown is
   rendered eagerly, not lazily.
4. **Anonymizable.**  ``export_incident_for_sharing`` is the interface the
   future community upload will call.  It strips every path / IP / serial
   that could identify a user, while preserving the fields that matter for
   learning (printer model, material, bbox, failure type, gcode sample).

The structure mirrors the hand-built "incident #0" at
``~/.kiln/incidents/2026-04-15_disc_crash/`` which captured the Bambu A1
nozzle-into-purge-tool crash on 2026-04-15.  That directory is the ground
truth for what an incident should look like; this module automates it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Module-level lock serializing incident-id generation + dir creation.
# Fleet deployments can have many printers anomaly at once; perf_counter_ns
# + sha1 makes collisions astronomically unlikely, but a mutex around the
# ID-then-mkdir window makes it provably impossible.  Cost is microseconds
# per incident — irrelevant given incidents are rare.
_INCIDENT_ID_LOCK = threading.Lock()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants

#: Default root for incident directories.  Overridable via ``root_dir`` param
#: on every public entry point so tests can redirect to ``tmp_path``.
DEFAULT_INCIDENTS_ROOT = Path.home() / ".kiln" / "incidents"

#: File names written inside each incident directory.  Centralized so tests
#: and exporters don't hard-code strings.
REPORT_FILENAME = "report.md"
STATUS_FILENAME = "printer_status.json"
INCIDENT_JSON_FILENAME = "incident.json"

#: Fields in the incident record that, if present, point at a file we must
#: copy in.  Value is the stem we use for the copied file.
_FILE_FIELDS: dict[str, str] = {
    "stl_path": "model.stl",
    "gcode_path": "job.gcode",
    "threemf_path": "job.3mf",
    "camera_snapshot_path": "camera.jpg",
}


# ---------------------------------------------------------------------------
# Public API


def record_incident(
    incident_type: str,
    printer_status: dict[str, Any],
    *,
    printer_id: str | None = None,
    stl_path: str | Path | None = None,
    gcode_path: str | Path | None = None,
    threemf_path: str | Path | None = None,
    camera_snapshot_path: str | Path | None = None,
    bbox_info: dict[str, Any] | None = None,
    user_description: str | None = None,
    tool_call_trace: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    root_dir: str | Path | None = None,
) -> str:
    """Create an incident envelope and return its absolute path.

    Parameters
    ----------
    incident_type
        Short slug identifying the failure class.  Examples:
        ``"user_cancel_pre_layer_5"``, ``"nozzle_crash_suspected"``,
        ``"thermal_anomaly"``, ``"hms_error"``.  Used in the directory name
        and for grouping in analytics.
    printer_status
        Dict of printer state at the moment the incident fired.  Whatever
        the caller has (``printer_status()`` MCP output, firmware push_status
        payload, or a hand-rolled dict of temperatures + flags) is fine —
        we just serialize it.
    printer_id
        Logical printer ID (e.g. ``"default"`` or ``"bambu_a1_shop_02"``).
    stl_path, gcode_path, threemf_path, camera_snapshot_path
        Optional paths to evidence files.  Each is copied into the incident
        directory.  Missing files are logged at WARNING and skipped — they
        don't fail the recording.
    bbox_info
        Optional bounding-box dict (output of the bed_fit / mesh analysis
        modules).  Critical for crash diagnosis — the nozzle-into-purge-tool
        incident was explained entirely by a negative-x bbox.
    user_description
        Free-form text from the user describing what they saw.  Shown
        prominently in ``report.md``.
    tool_call_trace
        Ordered list of tool-call dicts leading up to the incident.  Each
        entry is expected to have at least ``{"tool": str, "args": dict}``
        but we don't enforce that — we just serialize whatever's passed.
    tags
        Free-form tags for filtering later (e.g. ``["bambu_a1", "A1-mini",
        "off_bed_geometry"]``).
    root_dir
        Override the incidents root.  Defaults to ``~/.kiln/incidents``.
        Tests pass a ``tmp_path`` so they never touch the real directory.

    Returns
    -------
    str
        Absolute path to the created incident directory.  Always populated
        even if some artifact copies failed — partial evidence is still
        evidence.
    """
    root = Path(root_dir) if root_dir is not None else DEFAULT_INCIDENTS_ROOT
    root.mkdir(parents=True, exist_ok=True)

    # Serialize ID generation + dir creation across threads so two
    # simultaneous anomalies (e.g. fleet-wide thermal event) can't race
    # and collide on the same incident_dir.
    with _INCIDENT_ID_LOCK:
        incident_id = _generate_incident_id(incident_type)
        incident_dir = root / incident_id
        # retry-until-unique — hash has ~2^32 space so one retry suffices
        if incident_dir.exists():
            incident_id = _generate_incident_id(incident_type + "_r")
            incident_dir = root / incident_id
        incident_dir.mkdir(parents=True, exist_ok=True)

    # Assemble the structured record first — paths may get rewritten below
    # when we copy files in, so we update the record after each copy.
    record: dict[str, Any] = {
        "incident_id": incident_id,
        "incident_type": incident_type,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "printer_status": printer_status,
    }
    if printer_id is not None:
        record["printer_id"] = printer_id
    if bbox_info is not None:
        record["bbox_info"] = bbox_info
    if user_description is not None:
        record["user_description"] = user_description
    if tool_call_trace is not None:
        record["tool_call_trace"] = tool_call_trace
    if tags is not None:
        record["tags"] = list(tags)

    # Copy evidence files.  We record both the original path (for forensics)
    # and the copied-in filename (for self-contained replay).
    file_inputs = {
        "stl_path": stl_path,
        "gcode_path": gcode_path,
        "threemf_path": threemf_path,
        "camera_snapshot_path": camera_snapshot_path,
    }
    artifacts: dict[str, dict[str, str]] = {}
    for field, src in file_inputs.items():
        if src is None:
            continue
        copied = _copy_artifact(src, incident_dir, _FILE_FIELDS[field])
        entry: dict[str, str] = {"original_path": str(src)}
        if copied is not None:
            entry["copied_as"] = copied
        else:
            entry["copy_error"] = "source missing or unreadable; see log"
        artifacts[field] = entry
    if artifacts:
        record["artifacts"] = artifacts

    # printer_status.json — separate file because it's the most commonly
    # diffed artifact when triaging.  Kept pretty-printed so humans can
    # grep it without jq.
    _write_json(incident_dir / STATUS_FILENAME, printer_status)

    # incident.json — full record in one place for machine readers.
    _write_json(incident_dir / INCIDENT_JSON_FILENAME, record)

    # report.md — human-readable summary.  Generated last so it can reference
    # everything we wrote above.
    (incident_dir / REPORT_FILENAME).write_text(
        _render_report(record, artifacts), encoding="utf-8"
    )

    logger.info("incident recorded: %s", incident_dir)
    return str(incident_dir)


def list_incidents(
    limit: int = 20, *, root_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """Return metadata for the most recent incidents, newest first.

    Each returned dict has at least ``incident_id``, ``incident_type``,
    ``path``, and ``recorded_at_utc`` (if the underlying ``incident.json``
    is intact).  Incidents with a corrupt or missing ``incident.json`` are
    still listed — we fall back to the directory name — because a silently-
    dropped incident is worse than a partially-parsed one.

    The sort key is directory name (which begins with the incident
    timestamp), falling back to mtime.  Directory names lead because
    ``incident_id`` is the canonical ordering and it survives moves; mtime
    is a safety net for manually created envelopes.
    """
    root = Path(root_dir) if root_dir is not None else DEFAULT_INCIDENTS_ROOT
    if not root.exists():
        return []

    entries: list[tuple[str, float, Path]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append((child.name, mtime, child))

    # Primary sort: name descending (newer timestamps sort later in ISO-ish
    # ``YYYY-MM-DD_HH-MM-SS`` format, so reverse for newest-first).
    # Secondary: mtime descending, same reason.
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)

    results: list[dict[str, Any]] = []
    for name, _mtime, path in entries[:limit]:
        meta = _load_incident_meta(path)
        meta.setdefault("incident_id", name)
        meta["path"] = str(path)
        results.append(meta)
    return results


def export_incident_for_sharing(
    incident_id: str,
    strip_user_paths: bool = True,
    *,
    root_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return an anonymized copy of an incident record.

    Intended for community-intelligence upload.  We keep the fields that
    help others learn (printer model, material, bbox, failure type, a
    gcode sample) and strip everything that could identify the user
    (home paths, hostnames, printer IPs and serials, filenames that might
    embed project names).

    When ``strip_user_paths`` is False, only the filesystem-level scrub is
    skipped — we still redact IPs, serials, and MAC-like strings inside
    serialized values, because the damage from a leaked printer serial is
    not mitigated by keeping paths verbatim.
    """
    root = Path(root_dir) if root_dir is not None else DEFAULT_INCIDENTS_ROOT
    incident_dir = root / incident_id
    incident_path = incident_dir / INCIDENT_JSON_FILENAME
    if not incident_path.exists():
        raise FileNotFoundError(f"incident not found: {incident_id}")

    raw = json.loads(incident_path.read_text(encoding="utf-8"))

    # Deep-copy via json round-trip — the record is already JSON so this is
    # safe and cheaper than a conditional deepcopy.
    record = json.loads(json.dumps(raw))

    # Remove fields that are inherently user-identifying even after scrubbing.
    record.pop("user_description", None)
    record.pop("tool_call_trace", None)

    if "artifacts" in record:
        slim: dict[str, dict[str, str]] = {}
        for field, entry in record["artifacts"].items():
            slim_entry: dict[str, str] = {}
            if "copied_as" in entry:
                slim_entry["copied_as"] = entry["copied_as"]
            if strip_user_paths:
                # Drop original_path entirely — it's ``/Users/<name>/...``.
                pass
            else:
                slim_entry["original_path"] = _scrub_string(
                    entry.get("original_path", "")
                )
            slim[field] = slim_entry
        record["artifacts"] = slim

    # Walk the record and scrub embedded PII from any string.  This covers
    # printer_status IPs, hostnames in tags, serials in bbox notes, etc.
    # ``artifacts`` is handled separately above — its nested keys
    # (``stl_path``, ``gcode_path``, etc.) would otherwise trip the
    # "pathy key" rule and clobber the already-sanitized entries.
    artifacts = record.pop("artifacts", None)
    record = _scrub_value(record, strip_user_paths)
    if artifacts is not None:
        record["artifacts"] = _scrub_value(
            artifacts, strip_user_paths=False
        )

    # Attach a gcode sample if available and small — first ~100 lines are
    # enough for diagnosis without leaking the whole toolpath.
    gcode_sample = _read_gcode_sample(incident_dir)
    if gcode_sample:
        record["gcode_sample"] = gcode_sample

    record["anonymized"] = True
    return record


# ---------------------------------------------------------------------------
# Internals


def _generate_incident_id(incident_type: str) -> str:
    """Build ``YYYY-MM-DD_HH-MM-SS_<type>_<hash>``.

    The hash breaks ties when two incidents land in the same second
    (auto-cancel + recorder in fast succession) and makes the id
    collision-resistant for test fixtures.
    """
    now = datetime.now()
    # Slug the type: letters, digits, underscore only.  Agents pass arbitrary
    # strings and a stray slash would break the directory.
    safe_type = re.sub(r"[^a-zA-Z0-9_]+", "_", incident_type).strip("_") or "unknown"
    short_hash = hashlib.sha1(
        f"{now.isoformat()}_{safe_type}_{time.perf_counter_ns()}".encode()
    ).hexdigest()[:8]
    return f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{safe_type}_{short_hash}"


def _copy_artifact(src: str | Path, dest_dir: Path, dest_name: str) -> str | None:
    """Copy ``src`` into ``dest_dir/dest_name``.  Return the filename or None.

    Failures (missing file, permission denied, cross-device oddities) are
    logged at WARNING and swallowed — a broken artifact must not take down
    the whole recording.
    """
    src_path = Path(src)
    if not src_path.exists():
        logger.warning("incident artifact missing, skipping: %s", src_path)
        return None
    try:
        # Preserve the original extension when possible — downstream tools
        # key off file extension (e.g. the slicer won't open a .gcode named
        # .stl) and the ``_FILE_FIELDS`` stems are best-guess defaults.
        suffix = src_path.suffix
        if suffix and not dest_name.endswith(suffix):
            dest_name = f"{Path(dest_name).stem}{suffix}"
        dest = dest_dir / dest_name
        shutil.copy2(src_path, dest)
        return dest.name
    except OSError as exc:
        logger.warning("incident artifact copy failed %s: %s", src_path, exc)
        return None


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` as pretty JSON.  Swallow-and-log unserializable values."""
    try:
        path.write_text(
            json.dumps(data, indent=2, default=_json_default, sort_keys=False),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("incident json write failed %s: %s", path, exc)


def _json_default(obj: Any) -> Any:
    """Fallback for non-JSON-native values (Path, datetime, sets, etc.)."""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


def _render_report(record: dict[str, Any], artifacts: dict[str, dict[str, str]]) -> str:
    """Render a minimal, scannable markdown report.

    Kept deliberately terse — the full structured record lives in
    ``incident.json``.  Humans reading ``report.md`` want the five-second
    answer: what failed, on what printer, what does the agent need to know?
    """
    lines: list[str] = []
    lines.append(f"# Incident {record['incident_id']}")
    lines.append("")
    lines.append(f"- **type**: `{record['incident_type']}`")
    lines.append(f"- **recorded_at_utc**: {record['recorded_at_utc']}")
    if "printer_id" in record:
        lines.append(f"- **printer_id**: `{record['printer_id']}`")
    if "tags" in record:
        lines.append("- **tags**: " + ", ".join(f"`{t}`" for t in record["tags"]))
    lines.append("")

    if "user_description" in record:
        lines.append("## User description")
        lines.append("")
        lines.append(record["user_description"].strip())
        lines.append("")

    if "bbox_info" in record:
        lines.append("## Bounding box")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(record["bbox_info"], indent=2))
        lines.append("```")
        lines.append("")

    lines.append("## Printer status (see `printer_status.json`)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(record.get("printer_status", {}), indent=2))
    lines.append("```")
    lines.append("")

    if artifacts:
        lines.append("## Artifacts")
        lines.append("")
        for field, entry in artifacts.items():
            copied = entry.get("copied_as", "(copy failed)")
            orig = entry.get("original_path", "")
            lines.append(f"- **{field}** → `{copied}`  (from `{orig}`)")
        lines.append("")

    if "tool_call_trace" in record:
        lines.append("## Tool call trace")
        lines.append("")
        for i, call in enumerate(record["tool_call_trace"], start=1):
            tool = call.get("tool", "?") if isinstance(call, dict) else str(call)
            lines.append(f"{i}. `{tool}`")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Full machine-readable record: `incident.json`.")
    lines.append("")
    return "\n".join(lines)


def _load_incident_meta(path: Path) -> dict[str, Any]:
    """Return a light metadata dict for ``list_incidents``.

    We only pull a handful of fields so the listing stays cheap even with
    hundreds of incidents.  The full record is a single JSON read away.
    """
    meta_path = path / INCIDENT_JSON_FILENAME
    if not meta_path.exists():
        return {"incident_id": path.name, "warning": "incident.json missing"}
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"incident_id": path.name, "warning": f"parse error: {exc}"}
    return {
        "incident_id": raw.get("incident_id", path.name),
        "incident_type": raw.get("incident_type"),
        "recorded_at_utc": raw.get("recorded_at_utc"),
        "printer_id": raw.get("printer_id"),
        "tags": raw.get("tags", []),
    }


# ---------------------------------------------------------------------------
# Anonymization

#: IPv4 like 10.0.1.23 — greedy enough for private-range addresses.
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

#: MAC address, colon- or dash-separated.
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")

#: User home paths on macOS and Linux.  We replace the whole home segment
#: with ``<HOME>`` so relative structure is preserved for debuggers.
_HOME_RE = re.compile(r"/(?:Users|home)/[^/\s\"']+")

#: Bambu-style printer serial (3-char prefix + digits).  Err on the side of
#: matching too much — false positives in a community dataset are fine.
_SERIAL_RE = re.compile(r"\b0[0-9A-Z]{2}[0-9A-Z]{11,}\b")


def _scrub_string(value: str) -> str:
    """Apply all PII regexes to a single string."""
    value = _HOME_RE.sub("<HOME>", value)
    value = _IPV4_RE.sub("<IP>", value)
    value = _MAC_RE.sub("<MAC>", value)
    value = _SERIAL_RE.sub("<SERIAL>", value)
    return value


def _scrub_value(value: Any, strip_user_paths: bool) -> Any:
    """Recursively walk a JSON-ish value, scrubbing strings in place.

    ``strip_user_paths`` currently only affects whether we drop keys that
    look like filesystem paths entirely — the regex scrub always runs,
    because IPs/serials are never safe to share.
    """
    if isinstance(value, str):
        scrubbed = _scrub_string(value)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_value(v, strip_user_paths) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if strip_user_paths and _is_pathy_key(k):
                # Keep the key but collapse the value to a marker so schema
                # consumers don't choke on a missing field.
                out[k] = "<REDACTED_PATH>"
                continue
            out[k] = _scrub_value(v, strip_user_paths)
        return out
    return value


def _is_pathy_key(key: str) -> bool:
    """Heuristic: does this key name a filesystem path?"""
    lk = key.lower()
    return lk.endswith("_path") or lk in {"path", "filename", "file_name", "file"}


def _read_gcode_sample(incident_dir: Path, max_lines: int = 100) -> str | None:
    """Return the first ``max_lines`` of any copied gcode file, scrubbed.

    The header of a gcode file is where slicer metadata, print bbox, and
    the first layer start live — exactly what a learning model wants.  The
    long toolpath tail is irrelevant and would bloat uploads.
    """
    for candidate in incident_dir.glob("job.gcode*"):
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as fh:
                head = list(_take(fh, max_lines))
            return _scrub_string("".join(head))
        except OSError:
            continue
    return None


def _take(it: Iterable[str], n: int) -> Iterable[str]:
    for i, item in enumerate(it):
        if i >= n:
            return
        yield item
