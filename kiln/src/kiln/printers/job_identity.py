"""Which print is this?"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from kiln.printers.progress_motion import normalize_job_label

logger = logging.getLogger(__name__)

# Values a backend reports in an id-shaped field that are not identities
_SENTINEL_IDS = frozenset({"", "0", "-1", "none", "null", "nil", "n/a"})

_START_TOLERANCE_S = 900.0

#: The three answers :func:`compare` gives.
SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobIdentity:
    """A resolved answer to "which print is this?"."""

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
        """Rebuild from :meth:`to_dict` output, or ``None`` if unreadable."""
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
    """A vendor id, or ``None`` when the field is really a placeholder."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.lower() in _SENTINEL_IDS:
        return None
    return text


def resolve(job: Any, *, native_id: Any = None, now: float | None = None) -> JobIdentity | None:
    """Identity for the print described by *job*, or ``None`` if unresolvable."""
    try:
        native = clean_native_id(native_id)
        if native is None:
            native = clean_native_id(getattr(job, "job_id", None))
        if native is not None:
            return JobIdentity(native=native)
        # No id: the derived rung
        return resolve_label(job, now=now)
    except Exception:  # noqa: BLE001 — asking a question must never raise
        logger.debug("job identity could not be resolved", exc_info=True)
        return None


def resolve_label(job: Any, *, now: float | None = None) -> JobIdentity | None:
    """The derived rung on its own: label plus start estimate, ignoring any id."""
    try:
        label = normalize_job_label(getattr(job, "file_name", None))
        if not label:
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
        logger.debug("job label could not be resolved", exc_info=True)
        return None


def compare(a: JobIdentity | None, b: JobIdentity | None) -> str:
    """Whether *a* and *b* are the same print: ``SAME``, ``DIFFERENT`` or ``UNKNOWN``."""
    try:
        if a is None or b is None or not a.is_usable or not b.is_usable:
            return UNKNOWN

        if a.native is not None or b.native is not None:
            if a.native is None or b.native is None:
                return UNKNOWN
            return SAME if a.native == b.native else DIFFERENT

        if a.label != b.label:
            return DIFFERENT
        if a.started_at is None or b.started_at is None:
            return SAME
        gap = abs(a.started_at - b.started_at)
        if gap <= _START_TOLERANCE_S:
            return SAME
        # A NaN or infinite estimate fails both tests; it is not evidence.
        return DIFFERENT if math.isfinite(gap) else UNKNOWN
    except Exception:  # noqa: BLE001
        logger.debug("job identity comparison failed", exc_info=True)
        return UNKNOWN


def same_job(a: JobIdentity | None, b: JobIdentity | None) -> bool:
    """Whether *a* and *b* are the same print."""
    return compare(a, b) == SAME
