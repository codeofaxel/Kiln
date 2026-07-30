"""Lightweight daily event counters for telemetry.

Tracks prints, generations, decorations, textures, slices, and
marketplace downloads completed today.  Supports detailed breakdowns
(e.g. texture name, decoration type, marketplace source).

Read by the heartbeat module for Supabase reporting.  Never blocks,
never errors visibly.

File: ``~/.kiln/daily_stats.json``
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_DEFAULT_STATS_PATH: Path = Path.home() / ".kiln" / "daily_stats.json"
_STATS_PATH: Path = _DEFAULT_STATS_PATH
_lock = threading.Lock()


def _recording_suppressed() -> bool:
    """True when a test/CI runner would otherwise write REAL telemetry.

    The heartbeat has always refused to SEND from CI (its ``_CI_ENV_VARS``
    guard), but recording had no such guard — so a test suite driving
    real adapters or the installed server wrote phantom counts into the
    developer's actual ``~/.kiln/daily_stats.json``, and the next real
    heartbeat shipped them (2026-07-26: one adapter-suite run left 47
    phantom prints queued for the dashboard).  Same env list, applied at
    the write side.

    A test that repoints ``_STATS_PATH`` at its own temp file is asking
    to exercise recording — a custom path is never suppressed.
    """
    if _STATS_PATH != _DEFAULT_STATS_PATH:
        return False
    # A local `pytest` run sets no CI variable, so delegating to the
    # heartbeat's CI check alone let developer test runs write real
    # telemetry — the exact failure this guard was added to stop.  It
    # was still happening on 2026-07-29, on 1.3.0, from a laptop.  Check
    # the test runner FIRST; the CI list is the remote half of the same
    # question, not the whole of it.
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return True
    try:
        from kiln.heartbeat import _is_ci_environment

        return _is_ci_environment()
    except Exception:
        return any(os.environ.get(v) for v in ("CI", "PYTEST_CURRENT_TEST"))

# A real MCP tool name: lowercase, starts alpha, 3-65 chars.  The
# per-tool counter only records names matching this so a weird
# callable ``__name__`` (or anything not a genuine tool) can't ride
# into the anonymous heartbeat as a "tool".
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,64}$")
# Cap distinct tool names tracked per day.  A real user touches well
# under this; the cap just stops the local file (and the heartbeat
# payload) from growing without bound.  Existing names keep counting
# once the cap is reached; only brand-new names past the cap are dropped.
_TOOL_CALLS_MAX_DISTINCT = 300

# A print is counted the moment it STARTS (see
# ``PrinterAdapter.start_print``), which is the only signal every adapter
# emits.  Its outcome may be recorded later — by the terminal-state hook
# or by an agent calling ``record_print_outcome`` — and that must not
# count the same physical print a second time.  So each start leaves a
# pending token here, and the first outcome for that printer/file
# consumes one instead of incrementing.
#
# Entries are ``{"printer": str, "file": <token>, "ts": float}``, where
# the file is a short hash of its basename rather than the name itself —
# matching only needs both sides to agree, and a telemetry file has no
# business holding what users called their models.  Bounded so a machine
# whose outcomes are never recorded can't grow the day file; the oldest
# tokens are dropped first.
_PENDING_STARTS_MAX = 32
# A print can legitimately run for days, so a token only expires when it
# is old enough that no print could still be attached to it.  Erring long
# is the safe direction: a stale token suppresses at most one count,
# while expiring early re-opens the double-count it exists to prevent.
_PENDING_START_TTL_S = 7 * 24 * 3600

# Outcome job ids already accounted for.  ``record_print_outcome`` is
# explicitly re-callable for the same job (an agent refining what the
# terminal-state hook auto-recorded), so the job id is remembered whether
# it counted or consumed a pending start.
_COUNTED_OUTCOMES_MAX = 500

# Valid event types (top-level counters).
_VALID_EVENTS = frozenset({
    "prints", "generations", "decorations", "textures",
    "slices", "downloads", "print_hours",
})

# Tool name → daily counter, applied at the tool-dispatch chokepoint
# (``server._record_local_tool_call``).  This is how a counter covers a
# whole tool FAMILY — kiln-pro's included, since pro tools dispatch
# through the same server — without each tool remembering to phone in:
# before this map, ``generations`` counted 2 of the ~25 tools that make
# models, ``textures`` counted zero, and the entire pro surface was
# invisible.  Names, not code paths, so nothing here imports kiln-pro.
#
# NOT in this map, deliberately:
# - Tools that self-record with a detail breakdown the dispatcher can't
#   see (``decorate_surface``, ``generate_texture``, ``generate_model``,
#   ``generate_model_from_image``, ``download_and_upload`` here;
#   ``apply_procedural_texture`` / ``apply_geometric_texture`` /
#   ``apply_image_texture`` in kiln-pro record ``textures`` in-body with
#   the texture name — which is why this map has no textures section).
# - Tools counted at a deeper engine chokepoint: prints at
#   ``PrinterAdapter.start_print``, slices at ``slicer.slice_file`` —
#   so ``slice_and_print`` needs no entry at all.
# - ``download_generated_model`` — fetching your own generated file is
#   not a marketplace download.
# - ``design_session`` — an orchestrator turn, not a produced artifact;
#   its compiles land here via ``compile_scad``.
TOOL_EVENT_MAP: dict[str, str] = {
    # -- generations: a model came into being --------------------------
    **dict.fromkeys(
        (
            "generate_ashtray", "generate_bookmark", "generate_coaster",
            "generate_fridge_magnet", "generate_frisbee",
            "generate_jewelry_tray", "generate_keychain",
            "generate_license_plate_frame", "generate_nameplate",
            "generate_ornament", "generate_pen_cup", "generate_pet_bowl",
            "generate_pet_tag", "generate_rolling_tray",
            "generate_soap_dish", "generate_wall_plaque",
            "generate_product_base", "generate_decorated_product",
            "batch_generate_products", "generate_from_template",
            "smart_generate_from_template", "generate_template_variations",
            "generate_and_print", "generate_model_with_provider",
            "compile_scad", "tweak_and_compile_scad",
            "compose_part_from_primitives", "build_organic_mesh",
        ),
        "generations",
    ),
    # -- decorations: an existing mesh gained a decoration -------------
    **dict.fromkeys(
        (
            "apply_decoration", "smart_decorate", "batch_decorate",
            "decorate_during_print", "decorate_during_print_fleet",
            "deboss_during_print", "add_qr_to_product",
            "generate_qr_decoration", "iterate_decoration",
            "auto_multicolor_from_texture",
        ),
        "decorations",
    ),
    # -- marketplace downloads -----------------------------------------
    "download_model": "downloads",
}


def _result_looks_failed(result: Any) -> bool:
    """Best-effort: did this tool result carry Kiln's failure shape?

    Tools report failure by RETURNING ``{"success": False, ...}``
    (``_error_dict``), not by raising — so the dispatch hook, which only
    knows the call returned, needs a peek inside.  The result arrives in
    whatever shape the MCP layer produced: the raw dict, an
    ``(unstructured, structured)`` tuple, or a list of content blocks
    whose text is the JSON-serialised dict.  Unknown shapes count as
    success — the status quo for a returned call — so a new wire format
    degrades to slight overcount, never to silent zero.
    """
    try:
        if isinstance(result, dict):
            return result.get("success") is False
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            return result[1].get("success") is False
        if isinstance(result, list):
            for block in result:
                text = getattr(block, "text", None)
                if isinstance(text, str) and text.lstrip().startswith("{"):
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed.get("success") is False
                break  # only the first block carries the payload
    except Exception:
        pass
    return False


def record_tool_event(tool_name: str, result: Any = None) -> None:
    """Count the outcome event for a mapped tool call.  Never raises.

    Called from the tool-dispatch hook after a tool returned.  Unmapped
    tools and failure-shaped results are no-ops.
    """
    event = TOOL_EVENT_MAP.get((tool_name or "").strip())
    if not event:
        return
    if _result_looks_failed(result):
        return
    record_event(event)


def _empty_day() -> dict[str, Any]:
    """Return a fresh day's stats."""
    return {
        "date": str(date.today()),
        "prints": 0,
        "generations": 0,
        "decorations": 0,
        "textures": 0,
        "slices": 0,
        "downloads": 0,
        "print_hours": 0.0,
        # Detailed breakdowns (name → count)
        "texture_names": {},       # {"tiger_stripe": 3, "custom": 1}
        "decoration_types": {},    # {"photo": 2, "qr": 1, "text": 5}
        "slicer_profiles": {},     # {"BambuLab A1 0.4": 2}
        "marketplace_sources": {}, # {"thingiverse": 3, "makerworld": 1}
        # Tier-denial counters: tool_name → number of TIER_REQUIRED
        # rejections today.  This is the key funnel-leak signal for
        # "user paid on the web but never synced their local agent" —
        # every denial here is a user reaching for a locked door they
        # should already have a key to.  Rolled up in the daily
        # heartbeat so we can see which tools are driving upgrades
        # vs. which are just hitting unsynced machines.
        "tier_denials": {},        # {"fleet_status": 2, "texture_apply": 1}
        # Not-signed-in refusals: tool_name -> times a caller reached
        # for a capability and was told to pair an account first.  The
        # call never leaves the machine, so no server counter can see
        # it — which made the product's most common refusal invisible.
        "account_wall": {},        # {"apply_image_texture": 3}
        # Per-tool call counts: tool_name → times called today.  Counts
        # EVERY local tool dispatch (not just the six outcome events),
        # so the anonymous heartbeat can finally show what unsigned
        # local users actually do — the "tools per month" signal that
        # was previously invisible for anyone not signed in.  Names +
        # counts only, never arguments or paths.
        "tool_calls": {},          # {"generate_coaster": 4, "slice_model": 2}
        # Print-counting bookkeeping — see _PENDING_STARTS_MAX above.
        # Local only: get_daily_stats() never returns these, so nothing
        # here reaches the heartbeat.
        "pending_starts": [],
        "counted_outcomes": [],
        "counted_hours": [],
    }


# Counter keys carried forward when a day rolls over, so the daily
# heartbeat can report a COMPLETE day instead of a partial one.
_ROLLOVER_COUNTERS = (
    "prints", "generations", "decorations",
    "textures", "slices", "downloads", "print_hours",
)

# The name->count maps carried through the day rollover alongside the
# scalar counters above.  Kept separate because the lockstep test pins
# _ROLLOVER_COUNTERS to _VALID_EVENTS (the scalar activity counters);
# these are a different shape answering a different question.
_ROLLOVER_MAPS = (
    "tier_denials", "account_wall", "tool_calls",
)


def _archive_completed_day(data: dict[str, Any]) -> dict[str, Any]:
    """Return a fresh day that remembers the day that just ended.

    The heartbeat fires ONCE, when the Kiln server starts, and reads
    whatever counters exist at that instant.  Since you must start the
    server to do anything with Kiln, today's counters are ~always zero
    at that moment — so the reported activity systematically undercounts
    (2026-07-25: 17 of 671 production heartbeats carried any print at
    all, which read as "nobody prints" rather than "we sample before the
    work happens").  Preserving the finished day lets the heartbeat send
    a whole day's real numbers, one day behind.
    """
    fresh = _empty_day()
    previous = {"date": data.get("date")}
    for key in _ROLLOVER_COUNTERS:
        previous[key] = data.get(key, 0)
    # The name→count maps need carrying for the same reason the scalars
    # do, and were missed when this function was written.  The heartbeat
    # samples at server start, so a denial recorded at 15:00 only ever
    # shipped if the server also restarted before midnight; otherwise the
    # day rolled over, the map reset to {}, and the evidence was gone.
    # That is the likeliest reason only 8 of 747 production heartbeats
    # carried any tier_denials at all.
    for key in _ROLLOVER_MAPS:
        carried = data.get(key)
        if isinstance(carried, dict) and carried:
            previous[key] = carried
    if previous["date"]:
        fresh["previous"] = previous
    # Print bookkeeping is not a counter — it spans the day boundary by
    # design.  A print started at 23:50 lands its outcome tomorrow, and
    # dropping its token at midnight would count that print twice.
    for key in ("pending_starts", "counted_outcomes", "counted_hours"):
        carried = data.get(key)
        if isinstance(carried, list):
            fresh[key] = carried
    return fresh


def _read() -> dict[str, Any]:
    """Read today's stats, archiving the prior day if it rolled over."""
    try:
        if _STATS_PATH.is_file():
            data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("date") == str(date.today()):
                    return data
                # A day ended: keep its totals so the next heartbeat can
                # report a complete day rather than an empty one.
                return _archive_completed_day(data)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return _empty_day()


def _write(data: dict[str, Any]) -> None:
    """Persist stats to disk.  Best-effort, never raises.

    No-ops under a CI/test runner still pointed at the real per-user
    file — see :func:`_recording_suppressed`.
    """
    if _recording_suppressed():
        return
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        _logger.debug("Could not write daily stats: %s", exc)


def record_event(event_type: str, *, detail: str | None = None) -> None:
    """Increment a daily counter.  Thread-safe, never raises.

    :param event_type: One of ``prints``, ``generations``,
        ``decorations``, ``textures``, ``slices``, ``downloads``.
    :param detail: Optional sub-category name for breakdowns.
        For textures: the texture name (e.g. ``"tiger_stripe"``).
        For decorations: the content type (e.g. ``"qr"``).
        For slices: the slicer profile name.
        For downloads: the marketplace name (e.g. ``"thingiverse"``).

    .. note::

        ``textures`` events also increment ``decorations`` and record the
        texture name in ``decoration_types`` (under the
        ``procedural_texture`` key).  The separate ``textures`` counter is
        kept for backward compatibility.
    """
    if event_type not in _VALID_EVENTS:
        return
    try:
        with _lock:
            data = _read()
            # Increment top-level counter
            data[event_type] = data.get(event_type, 0) + 1

            # Textures are a decoration subtype — also increment decorations
            if event_type == "textures":
                data["decorations"] = data.get("decorations", 0) + 1
                dec_types = data.get("decoration_types", {})
                if not isinstance(dec_types, dict):
                    dec_types = {}
                dec_types["procedural_texture"] = (
                    dec_types.get("procedural_texture", 0) + 1
                )
                data["decoration_types"] = dec_types

            # Increment breakdown if detail provided
            if detail:
                _DETAIL_KEYS = {
                    "textures": "texture_names",
                    "decorations": "decoration_types",
                    "slices": "slicer_profiles",
                    "downloads": "marketplace_sources",
                }
                breakdown_key = _DETAIL_KEYS.get(event_type)
                if breakdown_key:
                    breakdown = data.get(breakdown_key, {})
                    if not isinstance(breakdown, dict):
                        breakdown = {}
                    breakdown[detail] = breakdown.get(detail, 0) + 1
                    data[breakdown_key] = breakdown

            _write(data)
    except Exception as exc:
        _logger.debug("record_event(%s) failed: %s", event_type, exc)


def _file_token(file_name: str | None) -> str:
    """Return a short, stable, non-identifying token for a print file.

    Start and outcome see the same print under slightly different names
    (a local path one side, the printer's own listing the other), so we
    match on the basename — hashed, because the day file is telemetry
    bookkeeping and never needs to hold what a user called their model.
    """
    base = os.path.basename(str(file_name or "")).strip().lower()
    if not base:
        return ""
    import hashlib

    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:12]


def _take_pending_start(
    data: dict[str, Any], printer: str, file_token: str,
) -> bool:
    """Consume one pending start token, oldest match first.

    Returns True when this print was already counted at start (so the
    caller must NOT increment).  Matching is deliberately forgiving:
    ``printer`` is the adapter's own name at start but may arrive as a
    registry name from an agent-recorded outcome, and either side can be
    missing a file — so a match on EITHER identity claims the token, and
    a lone outstanding token is claimed on the strength of being alone.
    Being forgiving here trades a possible missed count for a guaranteed
    absence of double counting; the counter it feeds already errs low.
    """
    pending = data.get("pending_starts")
    if not isinstance(pending, list) or not pending:
        return False

    cutoff = time.time() - _PENDING_START_TTL_S
    live = [
        e for e in pending
        if isinstance(e, dict) and float(e.get("ts") or 0) >= cutoff
    ]

    index: int | None = None
    if file_token:
        index = next(
            (i for i, e in enumerate(live) if e.get("file") == file_token), None
        )
    if index is None and printer:
        index = next(
            (i for i, e in enumerate(live) if e.get("printer") == printer), None
        )
    if index is None and len(live) == 1:
        index = 0

    claimed = index is not None
    if claimed:
        live.pop(index)
    data["pending_starts"] = live
    return claimed


def record_print_start(printer_name: str, file_name: str | None = None) -> None:
    """Count a print at the moment it starts.  Never raises.

    This is the only print signal every adapter emits.  Counting outcomes
    instead undercounted by construction: ``record_print_outcome`` is an
    explicit "log how this went" action, and its auto-fire hook is wired
    into one adapter, so seven of the eight reported no prints at all
    regardless of how much their owners printed.

    The start leaves a pending token that the matching outcome consumes,
    so a print that IS followed by an outcome record still counts once.
    """
    try:
        with _lock:
            data = _read()
            data["prints"] = data.get("prints", 0) + 1

            pending = data.get("pending_starts")
            if not isinstance(pending, list):
                pending = []
            pending.append({
                "printer": (printer_name or "").strip()[:64],
                "file": _file_token(file_name),
                "ts": time.time(),
            })
            data["pending_starts"] = pending[-_PENDING_STARTS_MAX:]
            _write(data)
    except Exception as exc:
        _logger.debug("record_print_start failed: %s", exc)


def record_print_outcome_event(
    job_id: str,
    printer_name: str | None = None,
    file_name: str | None = None,
) -> None:
    """Count a print whose outcome was recorded but whose start was not.

    Kiln doesn't start every print it learns about — a user can print
    from the printer's own screen and then ask the agent to record how it
    went.  That outcome is the only signal for such a print, so it counts;
    a print Kiln started has already been counted and consumes its
    pending token instead.  Never raises.
    """
    try:
        with _lock:
            data = _read()

            key = str(job_id or "").strip()[:96]
            if key:
                seen = data.get("counted_outcomes")
                if not isinstance(seen, list):
                    seen = []
                if key in seen:
                    return  # re-recording the same job, refined or replayed
                seen.append(key)
                data["counted_outcomes"] = seen[-_COUNTED_OUTCOMES_MAX:]

            started = _take_pending_start(
                data,
                (printer_name or "").strip()[:64],
                _file_token(file_name),
            )
            if not started:
                data["prints"] = data.get("prints", 0) + 1
            _write(data)
    except Exception as exc:
        _logger.debug("record_print_outcome_event(%s) failed: %s", job_id, exc)


def record_print_hours(hours: float) -> None:
    """Add print hours to today's total.  Thread-safe, never raises."""
    try:
        with _lock:
            data = _read()
            data["print_hours"] = round(data.get("print_hours", 0.0) + hours, 2)
            _write(data)
    except Exception as exc:
        _logger.debug("record_print_hours failed: %s", exc)


def record_print_hours_for_job(job_id: str, hours: float) -> None:
    """Add print hours once per job.  Thread-safe, never raises.

    Two independent paths can learn one print's duration — the
    monitoring tool that watched it finish, and a later
    ``record_print_outcome`` reading the job record.  Whichever reports
    first wins; the ledger swallows the second.  A job id is required
    precisely because it is the dedupe key — durationless or idless
    reports should use plain :func:`record_print_hours` and accept the
    caller owns dedupe.
    """
    key = str(job_id or "").strip()[:96]
    if not key or hours <= 0:
        return
    try:
        with _lock:
            data = _read()
            seen = data.get("counted_hours")
            if not isinstance(seen, list):
                seen = []
            if key in seen:
                return
            seen.append(key)
            data["counted_hours"] = seen[-_COUNTED_OUTCOMES_MAX:]
            data["print_hours"] = round(
                data.get("print_hours", 0.0) + hours, 2
            )
            _write(data)
    except Exception as exc:
        _logger.debug("record_print_hours_for_job(%s) failed: %s", job_id, exc)


def _record_name_count(bucket: str, tool_name: str) -> None:
    """Increment ``data[bucket][tool_name]`` for today.  Never raises.

    The three name→count maps (tool calls, tier denials, account-wall
    hits) are the same structure with the same hygiene needs, so they
    share one recorder.  They did not always: ``record_tier_denial`` was
    written separately and validated nothing, which is how ``%s%s%s``
    and ``/tmp/pp-fuzz`` reached the production heartbeat table while
    the identically-shaped ``tool_calls`` map stayed clean.  Two
    functions doing one job is how one of them ends up wrong.

    Keys must look like a real tool name (``_TOOL_NAME_RE``) and each
    map is capped at ``_TOOL_CALLS_MAX_DISTINCT`` distinct names per day
    so neither the local file nor the heartbeat payload can grow without
    bound.  Names and counts only — never arguments or paths.
    """
    name = (tool_name or "").strip()
    if not _TOOL_NAME_RE.match(name):
        return  # not a real tool name — drop rather than pollute the map
    try:
        with _lock:
            data = _read()
            buckets = data.get(bucket, {})
            if not isinstance(buckets, dict):
                buckets = {}
            if name not in buckets and len(buckets) >= _TOOL_CALLS_MAX_DISTINCT:
                return  # cap distinct names; existing ones still counted below
            buckets[name] = int(buckets.get(name, 0)) + 1
            data[bucket] = buckets
            _write(data)
    except Exception as exc:
        _logger.debug("record %s[%s] failed: %s", bucket, tool_name, exc)


def record_tier_denial(tool_name: str) -> None:
    """Increment the TIER_REQUIRED denial counter for ``tool_name``.

    Called from every path that refuses a caller for their licence tier:
    :func:`requires_tier` here and in kiln-pro, and kiln-pro's inline
    ``check_pro`` / ``check_business`` / ``check_enterprise`` gates.
    Shows which locked doors people are actually pushing on.
    """
    _record_name_count("tier_denials", tool_name)


def record_account_wall(tool_name: str) -> None:
    """Increment the not-signed-in refusal counter for ``tool_name``.

    Distinct from a tier denial, and far more common: this is a user who
    reached for a capability and was told to pair an account first, so
    the call never left the machine.  It was invisible for exactly that
    reason — no server request means no server-side counter — which made
    the most-hit refusal in the product the one nobody could see.

    Kept as its own map rather than folded into ``tier_denials`` because
    the two demand different answers.  A tier denial means "wants it,
    won't pay yet"; an account-wall hit means "wants it, hasn't even
    told us who they are".
    """
    _record_name_count("account_wall", tool_name)


def record_tool_call(tool_name: str) -> None:
    """Increment the per-tool call counter for ``tool_name``.

    Called once per local tool dispatch (see ``server._record_local_tool_call``)
    so the anonymous daily heartbeat can report which tools are used, not
    just the six outcome events.  This is the only anonymous view of what
    a NOT-signed-in local user does — the per-user ledger only syncs when
    signed in.
    """
    _record_name_count("tool_calls", tool_name)


def get_daily_stats() -> dict[str, Any]:
    """Return today's counters and breakdowns."""
    data = _read()
    return {
        "prints": data.get("prints", 0),
        "generations": data.get("generations", 0),
        "decorations": data.get("decorations", 0),
        "textures": data.get("textures", 0),
        "slices": data.get("slices", 0),
        "downloads": data.get("downloads", 0),
        "print_hours": data.get("print_hours", 0.0),
        "texture_names": data.get("texture_names", {}),
        "decoration_types": data.get("decoration_types", {}),
        "slicer_profiles": data.get("slicer_profiles", {}),
        "marketplace_sources": data.get("marketplace_sources", {}),
        "tier_denials": data.get("tier_denials", {}),
        "account_wall": data.get("account_wall", {}),
        "tool_calls": data.get("tool_calls", {}),
        # The last COMPLETE day's counters (see _archive_completed_day).
        # The heartbeat reports these because the same-day counters it
        # can see at server startup are structurally near-empty.
        "previous_day": data.get("previous") or {},
    }
