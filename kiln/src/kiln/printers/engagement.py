"""Which printer Kiln is working with right now, and what that costs."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiln.printers.job_identity import JobIdentity, same_job
from kiln.printers.job_identity import resolve as resolve_job_identity

logger = logging.getLogger(__name__)

_STORE_NAME = "printer_engagement.json"
_SCHEMA_VERSION = 1

# Returns allowed per print, per machine
_RETURNS_PER_JOB = 1

_PEER_VERIFY_TTL_S = 30.0

# The rule covers every command an adapter has, by default
UNGATED_METHODS: dict[str, str] = {
    # Identity: read to register the printer.
    "get_printer_info": "identity, read to register the printer",
    "get_identity_channels": "identity, read to register the printer",
    "read_machine_identity": "identity, read to register the printer",
    "read_machine_limits": "setup: the limits a printer states for itself, for a person to confirm",
    # Setup: asked once by every door that saves a printer
    "camera_handshake": "setup: whether the camera answers, no frame read",
    # Facts about the model, from the catalogue.  No machine is asked.
    "declared_printer_model": "the model the person told Kiln, not a reading",
    "motion_facts": "catalogue facts about the model",
    "camera_catalogue_model": "catalogue facts about the model",
    "stream_capability": "what the camera can carry, from the model",
    "homed_axes_field": "a field name, not a reading",
    "purge_station": "where the model purges, from the catalogue",
    # Kiln's own plumbing: connections, credentials, local settings and files.
    "connect": "Kiln's own connection",
    "disconnect": "Kiln's own connection",
    "update_credentials": "a local setting",
    "set_safety_profile": "a local setting",
    "set_external_camera": "a local setting",
    "enable_push_monitoring": "Kiln's own connection",
    "disable_push_monitoring": "Kiln's own connection",
    "wrap_gcode_as_3mf": "a file on this computer; the colour it asks the machine for is gated",
    # Only through gated methods.
    "async_start_print": "calls start_print",
    "get_status": "calls get_state and get_job",
    "async_get_state": "calls get_state",
    "async_get_job_status": "calls get_job",
    "async_get_temperatures": "calls get_state",
    "async_cancel_print": "calls cancel_print",
}

GATED_ACTIONS = frozenset(
    {
        # Watches what the machine is doing right now.
        "get_state",
        "get_job",
        "get_snapshot",
        "get_stream_url",
        "frame_source",
        "camera_frame_source",
        "camera_probes",
        "get_tool_position",
        "get_speed_profile",
        # Reads what the machine holds and how it is set.
        "list_files",
        "read_print_file",
        "get_filament_status",
        "get_ams_status",
        "get_cfs_status",
        "get_multi_material_status",
        "active_filament_color",
        "read_active_slot",
        "read_nozzle_setting",
        "read_nozzle_clumping_detection",
        "get_printer_config",
        "get_bed_mesh",
        "homed_axes_now",
        "reported_firmware_version",
        "get_firmware_status",
        # Acts on the machine
        "start_print",
        "upload_file",
        "delete_file",
        "pause_print",
        "resume_print",
        "cancel_print",
        "emergency_stop",
        "set_tool_temp",
        "set_bed_temp",
        "send_gcode",
        "publish_print_command",
        "load_filament",
        "unload_filament",
        "purge_filament",
        "wipe_nozzle",
        "home_axes",
        "park_head",
        "set_fan",
        "set_light",
        "set_speed_profile",
        "skip_objects",
        "clear_error",
        "run_calibration",
        "firmware_resume_print",
        "update_firmware",
        "rollback_firmware",
        "set_laser_power",
        "set_spindle_speed",
    }
)


def is_gated(action: str) -> bool:
    """Whether the one-printer rule covers *action*: every public adapter method except the ones :data:`UNGATED_METHODS` names, with their reasons."""
    return bool(action) and not action.startswith("_") and action not in UNGATED_METHODS

_verify_cache: dict[str, tuple[float, bool]] = {}


def _kiln_dir() -> Path:
    """``~/.kiln`` (override with ``KILN_HOME``), created on demand."""
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_path() -> Path:
    return _kiln_dir() / _STORE_NAME


def _read_store() -> dict[str, Any]:
    """The record, or an empty one."""
    try:
        raw = _store_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("engagement store unreadable; treating as empty", exc_info=True)
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return {}
    return data


def _write_store(data: dict[str, Any]) -> None:
    """Replace the record atomically."""
    data["version"] = _SCHEMA_VERSION
    tmp: str | None = None
    try:
        path = _store_path()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)
        tmp = None
    except (OSError, ValueError, TypeError):
        logger.debug("engagement store could not be written", exc_info=True)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


@dataclass(frozen=True)
class Engagement:
    """The machine Kiln is driving, and what makes that true."""

    machine: str
    label: str
    job: JobIdentity | None
    since: float
    reason: str  # "started" | "watching"

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "label": self.label,
            "job": self.job.to_dict() if self.job else None,
            "since": self.since,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Engagement | None:
        if not isinstance(data, dict):
            return None
        machine = data.get("machine")
        if not isinstance(machine, str) or not machine:
            return None
        try:
            since = float(data.get("since") or 0.0)
        except (TypeError, ValueError):
            since = 0.0
        return cls(
            machine=machine,
            label=str(data.get("label") or "your printer"),
            job=JobIdentity.from_dict(data.get("job")),
            since=since,
            reason=str(data.get("reason") or "started"),
        )


# How each engagement reason reads to a person
_REASON_IN_ENGLISH = {
    "started": "Kiln started this print",
    "watching": "Kiln is watching this print for you",
    "commanded": "you asked Kiln to work with this printer",
    "returned": "Kiln came back to this print",
}


def reason_in_english(reason: str) -> str:
    """Plain-English form of an engagement reason, for a user-facing surface."""
    return _REASON_IN_ENGLISH.get(reason, "Kiln is working with this printer")


def machine_id(adapter: Any) -> str:
    """Durable identity for the machine behind *adapter*, or ``""``."""
    try:
        from kiln.registry import machine_fingerprint

        fingerprint = machine_fingerprint(adapter)
    except Exception:  # noqa: BLE001
        return ""
    return "" if ":object:" in fingerprint else fingerprint


def rename_label(old_name: str, new_name: str) -> int:
    """A printer's name changed: every record here that labels a machine *old_name* says *new_name* from now on."""
    changed = 0
    store = _read_store()
    for key in ("engaged", "queue"):
        entry = store.get(key)
        if isinstance(entry, dict) and str(entry.get("label") or "") == old_name:
            entry["label"] = new_name
            changed += 1
    if changed:
        _write_store(store)
    return changed


def current() -> Engagement | None:
    """The engagement on record, or ``None``."""
    return Engagement.from_dict(_read_store().get("engaged"))


def engage(
    adapter: Any, job: Any = None, *, reason: str, label: str = "", announce: bool = False,
) -> None:
    """Record that Kiln is now driving *adapter*."""
    try:
        machine = machine_id(adapter)
        if not machine:
            return  # unidentifiable machine: nothing that outlives this process
        identity = resolve_job_identity(job) if job is not None else None
        store = _read_store()
        previous = Engagement.from_dict(store.get("engaged"))
        if previous is not None and previous.machine != machine:
            _record_hand_back(store, previous)
        if reason == "started":
            _follow_with_the_queue(store, machine, label or _label_for(adapter))
        if announce:
            store["announce"] = {"machine": machine, "at": time.time()}
        else:
            store.pop("announce", None)
        store["engaged"] = Engagement(
            machine=machine,
            label=label or _label_for(adapter) or "your printer",
            job=identity,
            since=time.time(),
            reason=reason,
        ).to_dict()
        _write_store(store)
        _verify_cache.pop(machine, None)
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a print
        logger.debug("could not record engagement", exc_info=True)


def _record_hand_back(store: dict[str, Any], engagement: Engagement) -> None:
    """Remember that *engagement* was stepped off, so a return can be counted."""
    handbacks = store.setdefault("handbacks", {})
    if not isinstance(handbacks, dict):
        handbacks = {}
        store["handbacks"] = handbacks
    existing = handbacks.get(engagement.machine)
    used = 0
    if isinstance(existing, dict) and same_job(
        JobIdentity.from_dict(existing.get("job")), engagement.job
    ):
        # Stepping off the SAME print again keeps whatever it already spent.
        try:
            used = int(existing.get("returns_used") or 0)
        except (TypeError, ValueError):
            used = 0
    handbacks[engagement.machine] = {
        "job": engagement.job.to_dict() if engagement.job else None,
        "label": engagement.label,
        "at": time.time(),
        "returns_used": used,
    }


def hand_back(adapter: Any) -> dict[str, Any]:
    """Step off *adapter*, freeing the slot for another machine."""
    try:
        machine = machine_id(adapter)
        store = _read_store()
        engagement = Engagement.from_dict(store.get("engaged"))
        queue_released = _release_queue_in(store, machine)
        if engagement is None or (machine and engagement.machine != machine):
            if queue_released:
                _write_store(store)
                return {
                    "released": True, "printer": queue_released, "queue_released": True, "queue_only": True,
                    "returns_left": 0,
                }
            if engagement is None:
                return {"released": False, "reason": "Kiln is not working with a printer right now."}
            return {
                "released": False,
                "reason": f"Kiln is working with {engagement.label}, not this printer.",
                "engaged_with": engagement.label,
            }
        _record_hand_back(store, engagement)
        store["engaged"] = None
        _forget_unknown_machines(store)
        _write_store(store)
        _verify_cache.pop(engagement.machine, None)
        return {
            "released": True,
            "printer": engagement.label,
            "queue_released": bool(queue_released),
            "returns_left": max(0, _RETURNS_PER_JOB - _returns_used(store, engagement.machine)),
        }
    except Exception:  # noqa: BLE001
        logger.debug("hand-back failed", exc_info=True)
        return {"released": False, "reason": "Could not update the printer record."}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def queue_printer() -> dict[str, Any] | None:
    """``{"machine", "label", "since"}`` for the printer the queue is on, or ``None``."""
    entry = _read_store().get("queue")
    if not isinstance(entry, dict) or not isinstance(entry.get("machine"), str) or not entry["machine"]:
        return None
    return entry


def hold_queue(adapter: Any) -> None:
    """Put the queue on *adapter* if it is on no printer yet."""
    try:
        machine = machine_id(adapter)
        if not machine:
            return
        store = _read_store()
        if queue_printer_in(store) is None:
            store["queue"] = {"machine": machine, "label": _label_for(adapter) or "your printer", "since": time.time()}
            _write_store(store)
    except Exception:  # noqa: BLE001 -- bookkeeping never blocks the queue
        logger.debug("could not record the queue's printer", exc_info=True)


def release_queue() -> None:
    """The queue has nothing left: it is on no printer."""
    try:
        store = _read_store()
        if store.get("queue") is not None:
            store["queue"] = None
            _write_store(store)
    except Exception:  # noqa: BLE001
        logger.debug("could not release the queue's printer", exc_info=True)


def queue_printer_in(store: dict[str, Any]) -> dict[str, Any] | None:
    entry = store.get("queue")
    return entry if isinstance(entry, dict) and entry.get("machine") else None


def _follow_with_the_queue(store: dict[str, Any], machine: str, label: str) -> None:
    """A person started a print on *machine*: a queue on another printer follows."""
    entry = queue_printer_in(store)
    if entry is not None and entry["machine"] != machine:
        store["queue"] = {"machine": machine, "label": label or "your printer", "since": time.time()}


def _release_queue_in(store: dict[str, Any], machine: str) -> str:
    """Take the queue off *machine*; the label it was on, or ``""``."""
    entry = queue_printer_in(store)
    if entry is None or not machine or entry["machine"] != machine:
        return ""
    store["queue"] = None
    return str(entry.get("label") or "your printer")


def multi_machine_tier() -> bool:
    """Whether this caller's plan runs printers side by side (the fleet tier)."""
    return _multi_machine_tier()


def _forget_unknown_machines(store: dict[str, Any]) -> bool:
    """Drop hand-back records for printers that are no longer registered."""
    handbacks = store.get("handbacks")
    if not isinstance(handbacks, dict) or not handbacks:
        return False
    try:
        from kiln.registry import get_registry, machine_fingerprint

        registry = get_registry()
        live = set()
        for name in registry.list_machines():
            try:
                live.add(machine_fingerprint(registry.get(name)))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return False  # cannot tell who is registered: keep everything
    if not live:
        return False  # an empty registry proves nothing about what exists
    stale = [m for m in handbacks if m not in live]
    for machine in stale:
        handbacks.pop(machine, None)
    return bool(stale)


def _returns_used(store: dict[str, Any], machine: str) -> int:
    handbacks = store.get("handbacks")
    entry = handbacks.get(machine) if isinstance(handbacks, dict) else None
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get("returns_used") or 0)
    except (TypeError, ValueError):
        return 0


def _label_for(adapter: Any) -> str:
    """The name a user would recognise for *adapter*, best effort."""
    try:
        from kiln.registry import get_registry, machine_fingerprint

        registry = get_registry()
        target = machine_fingerprint(adapter)
        for name in registry.list_machines():
            try:
                if machine_fingerprint(registry.get(name)) == target:
                    return str(name)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return str(getattr(adapter, "name", "") or "")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402
import threading  # noqa: E402

_internal = threading.local()


def _in_internal_read() -> bool:
    return getattr(_internal, "depth", 0) > 0


@contextlib.contextmanager
def internal_read():
    """Mark a block as Kiln's own inspection, exempt from the engagement gate."""
    _internal.depth = getattr(_internal, "depth", 0) + 1
    try:
        yield
    finally:
        _internal.depth = max(0, getattr(_internal, "depth", 1) - 1)


def _multi_machine_tier() -> bool:
    """Whether this caller's tier runs more than one machine at a time."""
    try:
        from kiln.licensing import get_tier, max_printers_for_tier

        cap = max_printers_for_tier(get_tier())
    except Exception:  # noqa: BLE001 — kiln-pro absent is the common install
        return False
    return cap is None or cap > 1


def _peer_for(machine: str) -> Any | None:
    """The registered adapter whose fingerprint is *machine*, if still present."""
    try:
        from kiln.registry import get_registry, machine_fingerprint

        registry = get_registry()
        for name in registry.list_machines():
            try:
                peer = registry.get(name)
                if machine_fingerprint(peer) == machine:
                    return peer
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


_PEER_VERIFY_TIMEOUT_S = 5.0


def _ask_peer_bounded(peer: Any, engagement: Engagement) -> bool:
    """Is *peer* still running the print that justified the hold?"""
    answer: list[bool] = []

    def _ask() -> None:
        try:
            with internal_read():
                state = peer.get_state()
                if getattr(state, "is_occupied", False) is not True:
                    return
                if engagement.job is None:
                    answer.append(True)
                    return
                answer.append(
                    same_job(engagement.job, resolve_job_identity(peer.get_job()))
                )
        except Exception:  # noqa: BLE001
            return

    # A DAEMON thread joined with a timeout, deliberately, not a pool
    worker = threading.Thread(
        target=_ask, name="kiln-engagement-peer-check", daemon=True,
    )
    worker.start()
    worker.join(_PEER_VERIFY_TIMEOUT_S)
    if worker.is_alive():
        logger.debug("engaged peer did not answer in time; releasing the slot")
        return False
    return bool(answer and answer[0])


def _still_engaged(engagement: Engagement, *, now: float | None = None) -> bool:
    """Whether the engaged machine is still running the print that justified it."""
    stamp = time.monotonic() if now is None else now
    cached = _verify_cache.get(engagement.machine)
    if cached is not None and stamp - cached[0] < _PEER_VERIFY_TTL_S:
        return cached[1]

    verdict = False
    try:
        peer = _peer_for(engagement.machine)
        if peer is not None:
            verdict = _ask_peer_bounded(peer, engagement)
    except Exception:  # noqa: BLE001
        logger.debug("could not verify the engaged machine; releasing", exc_info=True)
        verdict = False

    _verify_cache[engagement.machine] = (stamp, verdict)
    return verdict


def _expire(machine: str) -> None:
    store = _read_store()
    engagement = Engagement.from_dict(store.get("engaged"))
    if engagement is not None and engagement.machine == machine:
        store["engaged"] = None
        handbacks = store.get("handbacks")
        if isinstance(handbacks, dict):
            handbacks.pop(machine, None)
        _write_store(store)
    _verify_cache.pop(machine, None)


def _return_open(adapter: Any, machine: str, store: dict[str, Any]) -> tuple[dict[str, Any], Any] | None:
    """``(hand-back entry, the print on it now)`` when this machine has its one return left for the print it handed back, else ``None``."""
    handbacks = store.get("handbacks")
    entry = handbacks.get(machine) if isinstance(handbacks, dict) else None
    if not isinstance(entry, dict):
        return None
    if _returns_used(store, machine) >= _RETURNS_PER_JOB:
        return None
    try:
        with internal_read():
            live = resolve_job_identity(adapter.get_job())
    except Exception:  # noqa: BLE001
        return None
    if not same_job(JobIdentity.from_dict(entry.get("job")), live):
        return None  # a different print: this is not a return, it is a new claim
    return entry, live


def _consume_return(adapter: Any, machine: str) -> bool:
    """Spend this machine's one return, if it has one for the print on it now."""
    store = _read_store()
    opened = _return_open(adapter, machine, store)
    if opened is None:
        return False
    entry, live = opened

    previous = Engagement.from_dict(store.get("engaged"))
    if previous is not None:
        _record_hand_back(store, previous)
        _verify_cache.pop(previous.machine, None)
    entry["returns_used"] = _returns_used(store, machine) + 1
    store["engaged"] = Engagement(
        machine=machine,
        label=str(entry.get("label") or _label_for(adapter) or "your printer"),
        job=live,
        since=time.time(),
        reason="returned",
    ).to_dict()
    _write_store(store)
    _verify_cache.pop(machine, None)
    return True


def observe(adapter: Any, action: str, result: Any) -> None:
    """Learn from a command that already ran, without asking anything twice."""
    try:
        if action not in ("get_state", "get_job") or _in_internal_read():
            return
        if _multi_machine_tier():
            return
        machine = machine_id(adapter)
        if not machine:
            return
        engagement = current()

        if engagement is None:
            if action != "get_state":
                return
            if getattr(result, "is_occupied", False) is not True:
                return
            engage(adapter, None, reason="commanded", announce=True)
            return

        if (
            action == "get_job"
            and engagement.machine == machine
            and engagement.job is None
        ):
            identity = resolve_job_identity(result)
            if identity is not None:
                store = _read_store()
                current_record = Engagement.from_dict(store.get("engaged"))
                if current_record is not None and current_record.machine == machine:
                    store["engaged"] = Engagement(
                        machine=current_record.machine,
                        label=current_record.label,
                        job=identity,
                        since=current_record.since,
                        reason=current_record.reason,
                    ).to_dict()
                    _write_store(store)
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a command
        logger.debug("engagement observation failed", exc_info=True)


def _held_elsewhere(adapter: Any, action: str) -> tuple[Engagement, str] | None:
    """``(engagement, machine)`` when Kiln is driving a DIFFERENT machine than the one *action* reaches, or ``None`` when nothing stands in its way."""
    if not is_gated(action) or _in_internal_read():
        return None
    if _multi_machine_tier():
        return None

    machine = machine_id(adapter)
    engagement = current()
    if engagement is None:
        # Nothing is engaged
        return None

    if not machine or machine == engagement.machine:
        return None

    if not _still_engaged(engagement):
        _expire(engagement.machine)
        return None
    return engagement, machine


def check_look(adapter: Any, action: str) -> dict[str, Any] | None:
    """Verdict for a command that only LOOKS at a machine, or ``None``."""
    try:
        held = _held_elsewhere(adapter, action)
        if held is None:
            return None
        engagement, machine = held
        return _refusal(engagement, action, machine, adapter)
    except Exception:  # noqa: BLE001 — a licensing rule never breaks a printer
        logger.debug("engagement look check soft-passed", exc_info=True)
        return None


def check_command(adapter: Any, action: str) -> dict[str, Any] | None:
    """Verdict for a printer-directed *action*, or ``None`` to allow it."""
    if action in _LOOKS:
        return check_look(adapter, action)
    try:
        held = _held_elsewhere(adapter, action)
        if held is None:
            return None
        engagement, machine = held
        if _consume_return(adapter, machine):
            return None
        return _refusal(engagement, action, machine, adapter)
    except Exception:  # noqa: BLE001 — a licensing rule never breaks a printer
        logger.debug("engagement check soft-passed", exc_info=True)
        return None


def refusal_ahead(adapter: Any, action: str) -> dict[str, Any] | None:
    """The refusal :func:`check_command` would give *action*, without spending anything to find out."""
    try:
        held = _held_elsewhere(adapter, action)
        if held is None:
            return None
        engagement, machine = held
        if action not in _LOOKS and _return_open(adapter, machine, _read_store()) is not None:
            return None
        return _refusal(engagement, action, machine, adapter)
    except Exception:  # noqa: BLE001 — a licensing rule never breaks a printer
        logger.debug("engagement look-ahead soft-passed", exc_info=True)
        return None


def is_look(action: str) -> bool:
    """Whether *action* only looks at a machine, so it is judged by :func:`check_look`."""
    return action in _LOOKS


# : How long a claim made by a look waits to be said
_ANNOUNCE_WITHIN_S = 120.0


def take_announcement() -> dict[str, Any] | None:
    """The block for the result of the call in which a look made Kiln start working with a printer, said once, or ``None``."""
    try:
        if not _store_path().exists():
            return None
        store = _read_store()
        pending = store.pop("announce", None)
        if pending is None:
            return None
        _write_store(store)
        engagement = Engagement.from_dict(store.get("engaged"))
        if (
            not isinstance(pending, dict)
            or engagement is None
            or pending.get("machine") != engagement.machine
        ):
            return None
        try:
            fresh = time.time() - float(pending.get("at") or 0.0) <= _ANNOUNCE_WITHIN_S
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            return None
        label = engagement.label
        return {
            "printer": label,
            "note": (
                f"Kiln is now working with {label}: it is mid-print, and Kiln works with "
                f"one printer at a time {_plan_phrase()}. Tell the person this. To work "
                f"with a different printer, hand {label} back with hand_back_printer."
            ),
        }
    except Exception:  # noqa: BLE001 — a note never breaks a result
        logger.debug("engagement announcement not taken", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_READ_ACTIONS = frozenset(
    {
        "list_files",
        "read_print_file",
        "get_filament_status",
        "get_ams_status",
        "get_cfs_status",
        "get_multi_material_status",
        "active_filament_color",
        "read_active_slot",
        "read_nozzle_setting",
        "read_nozzle_clumping_detection",
        "get_printer_config",
        "get_bed_mesh",
        "homed_axes_now",
        "reported_firmware_version",
        "get_firmware_status",
    }
)

_WATCH_ACTIONS = frozenset(
    {
        "get_state",
        "get_job",
        "get_snapshot",
        "get_stream_url",
        "frame_source",
        "camera_frame_source",
        "camera_probes",
        "get_tool_position",
        "get_speed_profile",
    }
)

# : Every gated command that only looks
_LOOKS = _READ_ACTIONS | _WATCH_ACTIONS


def _tier_name() -> str:
    try:
        from kiln.licensing import get_tier

        return str(getattr(get_tier(), "value", get_tier()) or "free").lower()
    except Exception:  # noqa: BLE001
        return "free"


_PLAN_NAMES = {"pro": "on Kiln Pro", "free": "on the free plan"}


def _plan_phrase() -> str:
    """Name the plan the caller actually holds."""
    return _PLAN_NAMES.get(_tier_name(), "on your plan")


def _starts_sentence(name: str) -> str:
    """*name* with its first letter raised, and NOTHING else touched."""
    return name[:1].upper() + name[1:] if name else name


def _waiting_at_a_stop(engagement: Engagement) -> dict[str, Any] | None:
    """The ``now`` note when the engaged machine is paused at a planned stop."""
    try:
        peer = _peer_for(engagement.machine)
        if peer is None:
            return None
        from kiln.hardware_stops import waiting_elsewhere

        return waiting_elsewhere(peer, timeout_s=_PEER_VERIFY_TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- the words never break the gate
        return None


def _refusal(
    engagement: Engagement, action: str, machine: str, adapter: Any = None,
) -> dict[str, Any]:
    from kiln.tiers_and_terms import upgrade_nudge_block

    other = engagement.label
    store = _read_store()
    spent = _returns_used(store, machine) >= _RETURNS_PER_JOB
    # Name the machine being refused
    handbacks = store.get("handbacks")
    entry = handbacks.get(machine) if isinstance(handbacks, dict) else None
    this_one = ""
    if isinstance(entry, dict) and entry.get("label"):
        this_one = str(entry["label"])
    if not this_one and adapter is not None:
        this_one = _label_for(adapter)
    this_one = this_one or "that printer"

    plan = _plan_phrase()
    if spent:
        headline = (
            f"Kiln has already come back to {this_one} once during this print, "
            f"and is working with {other} now."
        )
    else:
        headline = (
            f"Kiln is working with {other} right now, and with one printer at "
            f"a time {plan}."
        )

    not_watching = f"Kiln is not watching {this_one}. Nothing here is keeping an eye on it."

    waiting_line = way_on = ""
    note = _waiting_at_a_stop(engagement)
    if note is not None:
        from kiln.hardware_stops import waiting_elsewhere_words

        waiting_line, way_on = waiting_elsewhere_words(other, note)

    if action == "emergency_stop":
        first = (
            f"To stop {this_one} right now, use the printer's own controls or "
            f"its power switch."
        )
        free_included = (
            f"{first} Kiln keeps full emergency stop on {other}, the machine it is running."
        )
    elif action in _READ_ACTIONS:
        free_included = (
            f"{_starts_sentence(this_one)}'s own screen still shows its spools, files and "
            f"settings. To point Kiln at it instead, hand {other} back with hand_back_printer."
        )
    elif action not in _WATCH_ACTIONS:
        free_included = (
            f"{_starts_sentence(this_one)}'s own controls work as always, and you keep "
            f"full control of {other}. "
            f"To move Kiln over, hand {other} back with hand_back_printer."
        )
    else:  # status and camera reads
        free_included = (
            f"{_starts_sentence(this_one)} still shows its own status on the printer. "
            f"To point Kiln at it instead, hand {other} back with hand_back_printer."
        )

    verdict: dict[str, Any] = {
        "blocked": True,
        "code": "TIER_SINGLE_PRINTER_LIMIT",
        "reason": " ".join(part for part in (waiting_line, headline, not_watching) if part),
        "engaged_with": other,
        "engaged_since": engagement.since,
        "action": action,
        "returns_left": 0 if spent else max(0, _RETURNS_PER_JOB - _returns_used(store, machine)),
        "suggestions": [free_included, *([way_on] if way_on else [])],
    }
    verdict["upgrade_nudge"] = upgrade_nudge_block(
        variant="single_printer_engagement",
        tier="business",
        feature="Working several printers at once",
        headline=headline,
        outcome_preview=(
            "Kiln Business drives every printer at the same time, so status, "
            "pause and stop reach all of them without handing anything back."
        ),
        free_included=" ".join(part for part in (free_included, way_on) if part),
        moment="resource_threshold",
        context={"action": action, "plan": _plan_phrase()},
    )
    return verdict


def refusal_words(verdict: dict[str, Any]) -> str:
    """The whole refusal as one message, for a surface that prints text."""
    nudge = verdict.get("upgrade_nudge") or {}
    parts = [str(verdict.get("reason") or "").strip()]
    parts.extend(str(s).strip() for s in verdict.get("suggestions") or ())
    parts.append(str(nudge.get("display_text") or "").strip())
    return " ".join(part for part in parts if part)


def refusal_in_flight(exc: BaseException | None = None) -> dict[str, Any] | None:
    """The one-printer refusal behind a failure, or ``None``."""
    from kiln.printers.base import PrinterEngagementError

    if exc is None:
        exc = sys.exc_info()[1]
    seen: set[int] = set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, PrinterEngagementError) and isinstance(exc.verdict, dict):
            return exc.verdict
        exc = exc.__cause__ or getattr(exc, "cause", None)
    return None
