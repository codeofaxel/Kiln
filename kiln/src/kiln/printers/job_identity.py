"""Which print is this? — the question every printer-slot rule rests on.

A rule of the form "Kiln is busy with this machine until this print ends"
cannot be written without an answer to "is this still the *same* print?".
Get it wrong one way and a hold outlives the job that justified it; wrong
the other way and a pause looks like a brand-new print.

Two things make that harder than it sounds:

  * **No vendor agrees on job identity.**  Prusa Link issues a real
    server-assigned integer (``job.id``, the same handle its own
    pause/resume/cancel endpoints take).  Moonraker, OctoPrint, Duet and
    Elegoo issue nothing at all — they report a filename and a counter of
    seconds elapsed.  Bambu *appears* to have ids (``task_id`` /
    ``subtask_id``) and does not: Kiln itself publishes them as the literal
    ``"0"`` for every LAN print (``bambu._start_print_impl``), which is the
    convention for a job with no cloud project behind it.  An id that is
    ``"0"`` for every job on the machine is a field, not an identity.
  * **The answer has to survive a restart.**  Anything anchored to a
    process — a monotonic clock, an object id, an in-memory stamp — reads
    as "no job" the moment the server bounces, which turns a restart into
    a way to shrug off whatever the identity was holding up.

So identity is resolved off facts the *printer* reports, in one ladder that
is the same for every vendor:

  1. a **native id**, when the vendor issues one that is actually unique
     (sentinels like ``"0"`` are rejected — see ``_SENTINEL_IDS``);
  2. a **derived** pair of normalized job label plus the wall-clock instant
     the print appears to have started, recomputed as ``now - elapsed`` from
     the printer's own elapsed counter, so it lands on the same instant
     after a restart as before one;
  3. **nothing** — and nothing is an honest answer, not a failure.

**The direction of error is chosen, not accidental.**  When identity cannot
be resolved, or two identities cannot be compared, :func:`same_job` answers
``False`` — "treat it as a different print".  Every caller is expected to
read that as *release what you were holding*.  The alternative default,
"assume it's still the same print", makes uncertainty accumulate into a hold
the user cannot clear and cannot see the reason for, which is how a gate
stops being trusted.  Erring loose is bounded and visible; erring sticky is
neither.

Nothing here raises.  A printer that answers strangely produces ``None``,
never an exception into a caller that was only trying to ask a question.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from kiln.printers.progress_motion import normalize_job_label

logger = logging.getLogger(__name__)

# Values a backend reports in an id-shaped field that are not identities.
# Bambu publishes "0" for task_id/subtask_id on every LAN print; the others
# are the usual spellings of "unset" seen across firmware.
_SENTINEL_IDS = frozenset({"", "0", "-1", "none", "null", "nil", "n/a"})

# How far apart two derived start estimates may sit and still be read as the
# same print.  The estimate is ``now - elapsed``, so it drifts whenever the
# printer's elapsed counter and the wall clock advance at different rates —
# most obviously across a pause, where many firmwares stop counting.  The
# window has to be wider than that drift and narrower than the gap between
# two genuinely different prints of the same file.  Fifteen minutes clears
# a long pause without spanning a reprint.
_START_TOLERANCE_S = 900.0


@dataclass(frozen=True)
class JobIdentity:
    """A resolved answer to "which print is this?".

    Exactly one of the two shapes is populated: ``native`` for a vendor that
    issues real ids, or ``label`` + ``started_at`` for one that does not.
    """

    native: str | None = None
    label: str | None = None
    started_at: float | None = None  # wall-clock epoch seconds, estimated

    @property
    def is_usable(self) -> bool:
        """True when this identity can be compared to another one."""
        return bool(self.native) or bool(self.label)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form, for a store that must outlive the process."""
        return {"native": self.native, "label": self.label, "started_at": self.started_at}

    @classmethod
    def from_dict(cls, data: Any) -> JobIdentity | None:
        """Rebuild from :meth:`to_dict` output, or ``None`` if unreadable.

        A record written by a newer version, hand-edited, or truncated
        mid-write must read as "no identity" rather than raise into a caller
        that is only trying to decide whether a hold still applies.
        """
        if not isinstance(data, dict):
            return None
        native = data.get("native")
        label = data.get("label")
        started_at = data.get("started_at")
        try:
            started = float(started_at) if started_at is not None else None
        except (TypeError, ValueError):
            started = None
        ident = cls(
            native=str(native) if isinstance(native, (str, int)) and str(native) else None,
            label=str(label) if isinstance(label, str) and label else None,
            started_at=started,
        )
        return ident if ident.is_usable else None


def clean_native_id(value: Any) -> str | None:
    """A vendor id, or ``None`` when the field is really a placeholder.

    Public because adapters call it: "what counts as an id" is one decision,
    and a backend that re-implements it will eventually disagree with this.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in _SENTINEL_IDS:
        return None
    return text


def resolve(job: Any, *, native_id: Any = None, now: float | None = None) -> JobIdentity | None:
    """Identity for the print described by *job*, or ``None`` if unresolvable.

    :param job: A ``JobProgress`` (anything exposing ``file_name`` and
        ``print_time_seconds`` works — duck-typed so a caller holding a plain
        dict-like reading is not forced to build one).
    :param native_id: The backend's own job id, when it has one.  Passed in
        rather than read off the adapter so the one ladder stays in one
        place and each adapter keeps its own knowledge of where its id lives.
    :param now: Wall-clock override, for tests.
    """
    try:
        native = clean_native_id(native_id)
        if native is None:
            native = clean_native_id(getattr(job, "job_id", None))
        if native is not None:
            return JobIdentity(native=native)

        label = normalize_job_label(getattr(job, "file_name", None))
        if not label:
            # No id and no name: nothing to hold an identity on.  Prusa Link
            # takes this path today (its status payload carries progress but
            # no filename) and is rescued by its native id above.
            return None

        elapsed = getattr(job, "print_time_seconds", None)
        started_at: float | None = None
        if elapsed is not None:
            try:
                started_at = (time.time() if now is None else now) - float(elapsed)
            except (TypeError, ValueError):
                started_at = None
        return JobIdentity(label=label, started_at=started_at)
    except Exception:  # noqa: BLE001 — asking a question must never raise
        logger.debug("job identity could not be resolved", exc_info=True)
        return None


def same_job(a: JobIdentity | None, b: JobIdentity | None) -> bool:
    """Whether *a* and *b* are the same print.

    ``False`` whenever that cannot be established — see the module docstring
    on why uncertainty resolves loose rather than sticky.
    """
    try:
        if a is None or b is None or not a.is_usable or not b.is_usable:
            return False

        if a.native is not None or b.native is not None:
            # One side has a real id and the other does not: the backend
            # changed its mind about what it can tell us, which is not
            # evidence of sameness.
            return a.native is not None and a.native == b.native

        if a.label != b.label:
            return False
        if a.started_at is None or b.started_at is None:
            # Same file, and at least one side cannot say when it began.
            #
            # Bambu is why this reads as a match rather than a refusal.  Its
            # elapsed figure is a Kiln-side stopwatch, not a printer reading
            # (see ``progress_motion.job_elapsed_seconds``), so it is absent
            # for every print Kiln did not itself start and for every print
            # at all after a restart.  Refusing to match on a bare label
            # would therefore make any rule built on this a no-op on the
            # single most common printer brand -- silently, and only in the
            # cases that matter most.
            #
            # It is safe to be this loose only because the label is never the
            # sole expiry: a caller holding something against a job is
            # expected to release it when the machine reaches a terminal
            # state, which is what actually ends the hold.  The residue is
            # narrow and named -- a reprint of the SAME file, begun before
            # that terminal state was observed, reads as a continuation.
            return True
        return abs(a.started_at - b.started_at) <= _START_TOLERANCE_S
    except Exception:  # noqa: BLE001
        logger.debug("job identity comparison failed", exc_info=True)
        return False
