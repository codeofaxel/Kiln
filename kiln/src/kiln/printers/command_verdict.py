"""One verdict for "did the command take?", shared by every adapter write.

Why this module exists
----------------------
Every adapter write — set a heater, a fan, a light, a speed profile, send
raw G-code — answered with a boolean, and on the fire-and-forget transports
that boolean was hardcoded ``True``.  On the Bambu adapter the MQTT publish
result was discarded, so a client that already knew it was disconnected still
reported a successful send; and nothing ever asked the printer whether the
command had taken.  Measured on an A1 on 2026-09-06: a hotend commanded to
250°C four times over twenty minutes, every call answering ``accepted: True``,
the nozzle never heating.

A boolean has no room for the state a command is genuinely in a moment after
it goes out.  So this mirrors :mod:`kiln.print_start_verdict` — one field,
three values — for every write that is not a print start.

Three states, and every answer is exactly one of them
-----------------------------------------------------
``confirmed``
    The printer, in a report that postdates the command, shows the effect
    (the heater target changed, the speed level moved, the light is on).
``accepted``
    The command was sent and not refused, and the printer has not shown the
    effect — either nothing has been heard back yet, or the command has no
    observable effect on this transport (raw G-code over MQTT).  It is a real
    state: the caller's next move is to read the printer, not to build on it.
``failed``
    The transport refused the command.  Adapters raise ``PrinterError`` for
    this, so a caller usually meets it as an exception; the state exists so a
    caller that coerces a legacy ``False`` still has one vocabulary.

The softening is ONE-DIRECTIONAL: a command is never promoted to
``confirmed`` on the strength of a reading that predates it, and a bare
``True`` from a request/response adapter coerces to ``accepted``, never
``confirmed``.  A false "confirmed" is exactly the bug this replaces.
"""

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
    """A single answer to "did the command take?", with its reasoning attached.

    Truthy exactly when the command was not refused, so a caller written
    against the old boolean (``if adapter.set_tool_temp(t): ...``) keeps its
    meaning — "sent", which is all the boolean ever honestly meant.
    """

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
        """The command half of a tool envelope.

        ``accepted`` is the pre-existing key every door already published and
        keeps its old meaning (sent, not refused).  ``outcome`` is the field
        to branch on.
        """
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
        """Lift an adapter's answer into a verdict.

        A :class:`CommandVerdict` passes through.  A boolean — from an
        adapter not yet migrated, or a third-party plugin — becomes
        ``accepted`` or ``failed``: a bare ``True`` only ever meant "the
        transport took it", so it is never read as ``confirmed``.
        """
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
