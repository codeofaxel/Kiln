"""One verdict for "did the command take?", shared by every adapter write."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ACCEPTED",
    "CONFIRMED",
    "FAILED",
    "CommandVerdict",
]

#: The printer, in a report after the command, shows the effect.
CONFIRMED = "confirmed"
#: Sent and not refused; the printer has not shown the effect.
ACCEPTED = "accepted"
#: The transport refused the command.
FAILED = "failed"


@dataclass(frozen=True)
class CommandVerdict:
    """A single answer to "did the command take?", with its reasoning attached."""

    state: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """``False`` only when the transport refused the command."""
        return self.state != FAILED

    @property
    def confirmed(self) -> bool:
        """``True`` only when the printer was seen showing the effect."""
        return self.state == CONFIRMED

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        """The command half of a tool envelope."""
        return {
            "accepted": self.ok,
            "outcome": self.state,
            "confirmed": self.confirmed,
            "message": self.message,
            "evidence": dict(self.evidence),
        }

    # -- constructors ---------------------------------------------------

    @classmethod
    def confirmed_by(cls, message: str, **evidence: Any) -> CommandVerdict:
        return cls(state=CONFIRMED, message=message, evidence=evidence)

    @classmethod
    def accepted_only(cls, message: str, **evidence: Any) -> CommandVerdict:
        return cls(state=ACCEPTED, message=message, evidence=evidence)

    @classmethod
    def refused(cls, message: str, **evidence: Any) -> CommandVerdict:
        return cls(state=FAILED, message=message, evidence=evidence)

    @classmethod
    def coerce(cls, value: Any, *, what: str = "command") -> CommandVerdict:
        """Lift an adapter's answer into a verdict."""
        if isinstance(value, cls):
            return value
        if value:
            return cls.accepted_only(
                f"The {what} was accepted by the printer's interface; "
                "this adapter does not read the result back, so the effect "
                "is not confirmed. Read the printer to check.",
                corroboration="none",
                adapter_reported=bool(value),
            )
        return cls.refused(
            f"The adapter reported the {what} was not accepted.",
            corroboration="none",
            adapter_reported=bool(value),
        )
