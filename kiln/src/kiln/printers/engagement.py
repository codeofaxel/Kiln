"""Which printer Kiln is working with right now, and what that costs.

Kiln's fleet tier sells running printers in PARALLEL.  The print-start gate
(``print_gate._concurrent_fleet_verdict``) has always refused to START a
second simultaneous print below the fleet tier.  It could not refuse
anything else, because it lives on ``start_print`` and every other
printer-directed command -- status, pause, resume, cancel, temperatures --
is a sibling method that never passes through it.  So one machine's worth
of parallelism was sold while the commands that actually operate a second
machine stayed open.

This module is the missing half.  It records ONE engagement -- the machine
Kiln is currently driving -- and every printer-directed command consults it.

**"Driving" is a specific state, not an association.**  Kiln is driving a
machine when Kiln STARTED the print, or when Kiln is WATCHING one the user
started by hand somewhere else.  It is emphatically not "the machine is
busy": a print sent from vendor software that Kiln is not watching is
invisible here, and Kiln works with a different printer normally.  That
distinction is the whole reason this is a recorded state rather than a
guess made from printer status.

**The engagement is releasable, and the release is one-way for that job.**
Handing a machine back ("I'll watch the rest myself, go work on my other
printer") moves the single slot immediately.  Coming BACK to a machine
while its same print is still running is the motion that, repeated, IS a
fleet -- someone supervising two live jobs by alternating.  So a return is
allowed once per print and then refused.  Note what a return is not: it
does not grant a second machine.  It MOVES the one slot, stepping off
whatever Kiln was driving.  At no instant does a below-fleet caller operate
two machines, which is why one return is generous rather than a loophole.

**It is written to disk on purpose.**  Anything held in memory is released
by restarting the server, and ``restart_server`` is a tool an agent can
call, which would make the whole rule a one-call bypass.  The record
therefore lives in ``~/.kiln`` and is re-read, not remembered.

**Where it refuses to guess.**  Unknown tier, an unreadable store, a peer
that cannot be reached, an identity that cannot be resolved -- every one of
those ALLOWS the command.  A licensing rule that blocks on uncertainty
spends user trust it cannot earn back, and a gate users learn to distrust
gets worked around on everything, including the cases that matter.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiln.printers.job_identity import JobIdentity
from kiln.printers.job_identity import resolve as resolve_job_identity
from kiln.printers.job_identity import same_job

logger = logging.getLogger(__name__)

_STORE_NAME = "printer_engagement.json"
_SCHEMA_VERSION = 1

# Returns allowed per print, per machine.  One: enough to go back and deal
# with a print that started misbehaving after you handed it over, not enough
# to alternate between two live machines.
_RETURNS_PER_JOB = 1

# How long a "is the engaged machine still running that job?" answer is
# reused.  Without this, every status poll in a monitoring loop would cost a
# network round trip to a DIFFERENT printer.
_PEER_VERIFY_TTL_S = 30.0

# Commands this module gates.  Registration, listing and discovery are NOT
# here: owning printers is free at every tier, and always has been.
GATED_ACTIONS = frozenset(
    {
        "get_state",
        "get_job",
        "pause_print",
        "resume_print",
        "cancel_print",
        "emergency_stop",
        "set_tool_temp",
        "set_bed_temp",
        "send_gcode",
    }
)

_verify_cache: dict[str, tuple[float, bool]] = {}


def _kiln_dir() -> Path:
    """``~/.kiln`` (override with ``KILN_HOME``), created on demand."""
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_path() -> Path:
    return _kiln_dir() / _STORE_NAME


def _read_store() -> dict[str, Any]:
    """The record, or an empty one.  Never raises, never blocks a command.

    A truncated, hand-edited or future-version file reads as "no engagement",
    which allows commands.  The alternative -- refusing everything because a
    JSON file is malformed -- would turn a bookkeeping fault into a user
    locked out of their own printer.
    """
    try:
        raw = _store_path().read_text()
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
    """Replace the record atomically.  Never raises into a caller."""
    data["version"] = _SCHEMA_VERSION
    try:
        path = _store_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        logger.debug("engagement store could not be written", exc_info=True)


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


def machine_id(adapter: Any) -> str:
    """Durable identity for the machine behind *adapter*, or ``""``.

    ``machine_fingerprint`` falls back to the adapter's OBJECT id when a
    printer reports neither serial nor address.  That is process-local, so
    it cannot mean anything to a record that outlives the process -- it is
    reported as "unidentifiable" here rather than written down.
    """
    try:
        from kiln.registry import machine_fingerprint

        fingerprint = machine_fingerprint(adapter)
    except Exception:  # noqa: BLE001
        return ""
    return "" if ":object:" in fingerprint else fingerprint


def current() -> Engagement | None:
    """The engagement on record, or ``None``."""
    return Engagement.from_dict(_read_store().get("engaged"))


def engage(adapter: Any, job: Any = None, *, reason: str, label: str = "") -> None:
    """Record that Kiln is now driving *adapter*.

    Called where Kiln takes responsibility for a machine: a print Kiln
    started, or a watch Kiln was asked to keep.  Replaces any previous
    engagement -- there is only ever one.  Never raises.
    """
    try:
        machine = machine_id(adapter)
        if not machine:
            return  # unidentifiable machine: nothing that outlives this process
        identity = resolve_job_identity(job) if job is not None else None
        store = _read_store()
        previous = Engagement.from_dict(store.get("engaged"))
        if previous is not None and previous.machine != machine:
            _record_hand_back(store, previous)
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
    """Step off *adapter*, freeing the slot for another machine.

    Returns a small report describing what happened, for the tool that
    exposes this to a user.  Never raises.
    """
    try:
        machine = machine_id(adapter)
        store = _read_store()
        engagement = Engagement.from_dict(store.get("engaged"))
        if engagement is None:
            return {"released": False, "reason": "Kiln is not working with a printer right now."}
        if machine and engagement.machine != machine:
            return {
                "released": False,
                "reason": f"Kiln is working with {engagement.label}, not this printer.",
                "engaged_with": engagement.label,
            }
        _record_hand_back(store, engagement)
        store["engaged"] = None
        _write_store(store)
        _verify_cache.pop(engagement.machine, None)
        return {
            "released": True,
            "printer": engagement.label,
            "returns_left": max(0, _RETURNS_PER_JOB - _returns_used(store, engagement.machine)),
        }
    except Exception:  # noqa: BLE001
        logger.debug("hand-back failed", exc_info=True)
        return {"released": False, "reason": "Could not update the printer record."}


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
# Internal reads
#
# Kiln inspects OTHER machines for its own reasons: the print-start gate asks
# every peer whether it is busy, and the check below asks whether the engaged
# machine is still running its job.  Both call ``get_state`` on a printer that
# is deliberately not the engaged one, so without an exemption the gate would
# refuse Kiln's own bookkeeping and then read that refusal as evidence.  This
# marks the call as Kiln asking itself, never a user command.
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
    """Whether this caller's tier runs more than one machine at a time.

    Derived from the SAME authority the print-start gate uses rather than a
    tier name written down again here: a cap above one machine IS the fleet
    tier, so a future change to the ladder moves both gates together.
    Anything unknown reads as multi-machine, which allows the command.
    """
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



# A command aimed at one printer must never wait on a DIFFERENT printer.
# The verification below reaches across to the engaged machine, so a peer
# that has gone quiet -- unplugged, asleep, a dead DHCP lease -- would
# otherwise stall the command in front of the user for as long as that
# adapter's own transport takes to give up, which for some is minutes.
_PEER_VERIFY_TIMEOUT_S = 5.0


def _ask_peer_bounded(peer: Any, engagement: Engagement) -> bool:
    """Is *peer* still running the print that justified the hold?

    Bounded, and a timeout answers ``False`` -- the same as every other
    thing this module cannot prove, so a silent printer releases the slot
    instead of freezing a command meant for another machine.
    """
    answer: list[bool] = []

    def _ask() -> None:
        from kiln.printers.base import PrinterStatus

        try:
            with internal_read():
                state = peer.get_state()
                if state.state not in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
                    return
                if engagement.job is None:
                    answer.append(True)
                    return
                answer.append(
                    same_job(engagement.job, resolve_job_identity(peer.get_job()))
                )
        except Exception:  # noqa: BLE001
            return

    # A DAEMON thread joined with a timeout, deliberately, not a pool.
    # ThreadPoolExecutor's context manager shuts down with wait=True, so
    # leaving the block joins the worker anyway and the timeout buys
    # nothing -- measured at the full 30 s on a stalled peer before this
    # was written this way.  A daemon thread that outlives the answer is
    # harmless: it holds no lock, writes nothing, and cannot keep the
    # process alive at exit.
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
    """Whether the engaged machine is still running the print that justified it.

    Answers ``False`` -- releasing the slot -- on anything it cannot prove:
    the printer is gone from the registry, unreachable, no longer printing,
    or running something else.  An engagement that outlives its job is a hold
    the user cannot see the reason for and cannot clear.
    """
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
        # A job that ENDED is not a hand-back: nothing was given up, so it
        # must not consume the return that a real hand-back would.
        store["engaged"] = None
        handbacks = store.get("handbacks")
        if isinstance(handbacks, dict):
            handbacks.pop(machine, None)
        _write_store(store)
    _verify_cache.pop(machine, None)


def _consume_return(adapter: Any, machine: str) -> bool:
    """Spend this machine's one return, if it has one for the print on it now."""
    store = _read_store()
    handbacks = store.get("handbacks")
    entry = handbacks.get(machine) if isinstance(handbacks, dict) else None
    if not isinstance(entry, dict):
        return False
    if _returns_used(store, machine) >= _RETURNS_PER_JOB:
        return False
    try:
        with internal_read():
            live = resolve_job_identity(adapter.get_job())
    except Exception:  # noqa: BLE001
        return False
    if not same_job(JobIdentity.from_dict(entry.get("job")), live):
        return False  # a different print: this is not a return, it is a new claim

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
    """Learn from a command that already ran, without asking anything twice.

    Two jobs, both paid for by results the caller already has:

    * **Claim the free slot.**  If Kiln is driving nothing and this machine
      turns out to be printing, it becomes the machine Kiln works with.
      Without this, handing a machine back would empty the slot and leave
      EVERY machine commandable -- the rule paying for its own escape hatch:
      hand back, then drive the whole bench.  An idle machine claims
      nothing; there is no job to be busy with, so asking about one costs
      nothing and holds nothing.
    * **Fill in the job identity.**  A claim made from a status reply knows
      the machine is printing but not WHICH print.  The next ``get_job`` on
      that machine supplies it for free, so the engagement sharpens itself
      rather than spending a round trip to start out sharp.

    Measured before it was written this way: claiming inside the gate cost a
    second ``get_state`` and a ``get_job`` on the first status call of every
    engagement.  Never raises.
    """
    try:
        if action not in ("get_state", "get_job") or _in_internal_read():
            return
        # Asked before anything is read from disk, deliberately: a caller
        # whose tier runs several machines is never gated, so it must not
        # pay a file read on every status poll to find that out.  The same
        # ordering as ``check_command``.
        if _multi_machine_tier():
            return
        machine = machine_id(adapter)
        if not machine:
            return
        engagement = current()

        if engagement is None:
            if action != "get_state":
                return
            from kiln.printers.base import PrinterStatus

            if getattr(result, "state", None) not in (
                PrinterStatus.PRINTING,
                PrinterStatus.PAUSED,
            ):
                return
            engage(adapter, None, reason="commanded")
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



def check_command(adapter: Any, action: str) -> dict[str, Any] | None:
    """Verdict for a printer-directed *action*, or ``None`` to allow it.

    Allows on anything unproven -- see the module docstring.  The refusal it
    does return names what Kiln is doing, what the user can do about it right
    now without paying anything, and the tier that lifts the rule, once.
    """
    try:
        if action not in GATED_ACTIONS or _in_internal_read():
            return None
        if _multi_machine_tier():
            return None

        machine = machine_id(adapter)
        engagement = current()
        if engagement is None:
            # Nothing is engaged.  The machine being asked about becomes the
            # one Kiln works with, but that is decided in ``observe`` AFTER
            # the command runs, from its own answer -- asking the printer here
            # would make every first status call pay for a second round trip.
            return None

        if not machine or machine == engagement.machine:
            return None

        if not _still_engaged(engagement):
            _expire(engagement.machine)
            return None

        if _consume_return(adapter, machine):
            return None

        return _refusal(engagement, action, machine, adapter)
    except Exception:  # noqa: BLE001 — a licensing rule never breaks a printer
        logger.debug("engagement check soft-passed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# What a person reads when the rule fires
#
# Three things have to be true of this copy, and each was a separate finding.
# It has to TEACH the rule in one line, because the first time anyone meets
# this they have two printers and no prior context, and a refusal they cannot
# explain reads as a fault.  It has to say plainly that Kiln is NOT watching
# the other machine, because the most expensive mistake here is leaving
# someone with the impression that something is keeping half an eye on a hot
# printer.  And it has to name the tier exactly once, as a fact, after the
# free path -- ``upgrade_nudge_block`` owns that sentence, which is why none
# of the strings below mention a tier by name.
# ---------------------------------------------------------------------------

_CONTROL_ACTIONS = frozenset(
    {"pause_print", "resume_print", "cancel_print", "set_tool_temp", "set_bed_temp", "send_gcode"}
)


def _tier_name() -> str:
    try:
        from kiln.licensing import get_tier

        return str(getattr(get_tier(), "value", get_tier()) or "free").lower()
    except Exception:  # noqa: BLE001
        return "free"


_PLAN_NAMES = {"pro": "on Kiln Pro", "free": "on the free plan"}


def _plan_phrase() -> str:
    """Name the plan the caller actually holds.

    A Pro subscriber reading "on the free plan" is being told they did not
    pay, by the product they pay for.  Naming their own plan keeps the
    sentence a fact rather than a sales pitch aimed at the wrong person;
    the tier that LIFTS the rule is named once, separately, by the nudge.
    """
    return _PLAN_NAMES.get(_tier_name(), "on your plan")



def _starts_sentence(name: str) -> str:
    """*name* with its first letter raised, and NOTHING else touched.

    ``str.capitalize`` lowercases the remainder, which would render a
    printer the user called "MK4S" as "Mk4s" and "X1C" as "X1c".  A rule
    that quietly rewrites the name someone gave their own machine has no
    business being in a refusal message.
    """
    return name[:1].upper() + name[1:] if name else name


def _refusal(
    engagement: Engagement, action: str, machine: str, adapter: Any = None,
) -> dict[str, Any]:
    from kiln.tiers_and_terms import upgrade_nudge_block

    other = engagement.label
    store = _read_store()
    spent = _returns_used(store, machine) >= _RETURNS_PER_JOB
    # Name the machine being refused.  "this printer" is what a user reads
    # when nobody bothered to look up what they call it, and it lands badly
    # in a sentence that names the OTHER machine three words later.
    handbacks = store.get("handbacks")
    entry = handbacks.get(machine) if isinstance(handbacks, dict) else None
    this_one = ""
    if isinstance(entry, dict) and entry.get("label"):
        this_one = str(entry["label"])
    if not this_one and adapter is not None:
        this_one = _label_for(adapter)
    this_one = this_one or "that printer"

    # A subscriber must not be handed copy written for someone who has not
    # paid.  Same rule, named against the plan they actually hold.
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

    if action == "emergency_stop":
        # The one moment where self-help comes first: a person who wants a
        # machine stopped needs the fastest real answer in the first sentence,
        # not after an explanation of a licensing rule.
        first = (
            f"To stop {this_one} right now, use the printer's own controls or "
            f"its power switch."
        )
        free_included = (
            f"{first} Kiln keeps full emergency stop on {other}, the machine it is running."
        )
    elif action in _CONTROL_ACTIONS:
        free_included = (
            f"{_starts_sentence(this_one)}'s own controls work as always, and you keep "
            f"full control of {other}. "
            f"To move Kiln over, hand {other} back with hand_back_printer."
        )
    else:  # status reads
        free_included = (
            f"{_starts_sentence(this_one)} still shows its own status on the printer. "
            f"To point Kiln at it instead, hand {other} back with hand_back_printer."
        )

    verdict: dict[str, Any] = {
        "blocked": True,
        "code": "TIER_SINGLE_PRINTER_LIMIT",
        "reason": f"{headline} {not_watching}",
        "engaged_with": other,
        "engaged_since": engagement.since,
        "action": action,
        "returns_left": 0 if spent else max(0, _RETURNS_PER_JOB - _returns_used(store, machine)),
        "suggestions": [free_included],
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
        free_included=free_included,
        moment="resource_threshold",
        context={"action": action, "plan": _plan_phrase()},
    )
    return verdict
