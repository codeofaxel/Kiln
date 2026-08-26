"""The ``kiln.monitor.v1`` wire — the inline print monitor's snapshot format.

This is the ONE home of the monitor payload contract, the way
:mod:`kiln.mesh_payload` is the one home of the mesh wire.  It began life
beside the hosted connector (kiln-pro's MCP Apps layer), which now
re-exports it from here — promoted when the local door learned to speak it,
exactly as the promotion note beside its original definition said it would
be.  A second copy of a wire format is a second wire format, so there is
exactly one.

It is serialization, not curated knowledge: which facts ride under which
keys, and nothing about what they mean.  The meaning lives in the six-state
resolver (the web Monitor's model, ported into the panel), which consumes
these axes and is the only place allowed to resolve a view.

The axes, mirroring the web resolver's inputs exactly:

* ``bridge`` / ``bridge_error`` — the pairing/reachability answer and the
  separate fact that the QUESTION failed.  A local instance answering for
  its own printer reports ``transport: "direct"`` — no bridge exists and
  none is needed, and the reader must be able to tell that from "bridge up".
* ``status`` / ``status_failure`` — the status tool's own answer, and its
  own structured refusal, which is a different fact from the transport
  failing.
* ``camera`` / ``camera_note`` — optional context, never an error.
* ``account`` — who is watching, where a door cares.  The hosted door never
  composes this axis (its callers are signed in structurally); the local
  door reports ``signed_in`` so the panel can offer the sign-in invitation
  instead of the live view.  Absent means "the door does not gate".
"""

from __future__ import annotations

from typing import Any

#: structuredContent key the composed snapshot rides under.
MONITOR_STRUCTURED_CONTENT_KEY = "kiln_monitor"

#: The payload's self-identifying kind marker.
MONITOR_PAYLOAD_KIND = "kiln.monitor.v1"

#: Machine states that mean "a print is on this machine" — the server-side
#: twin of the panel resolver's isPrintingish().  The camera gate reads this
#: SERVER-SIDE: a printer camera is a room camera, and the active-print rule
#: must not be enforceable only by panel JS.
ACTIVE_PRINT_STATES = frozenset(
    {"printing", "paused", "busy", "cancelling", "stopping", "pausing"}
)


def is_active_print_state(state: Any) -> bool:
    """True when the machine's state word means a print is on it."""
    return isinstance(state, str) and state in ACTIVE_PRINT_STATES


def compose_monitor_payload(
    bridge: dict[str, Any] | None,
    bridge_error: str | None,
    status: dict[str, Any] | None,
    status_failure: dict[str, Any] | None,
    camera_base64: str | None,
    camera_note: str | None,
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``kiln.monitor.v1`` snapshot — pure composition, no I/O.

    Mirrors the web resolver's input axes exactly (monitorView.ts
    ``MonitorInputs``): the bridge answer and its failure are separate facts,
    the status tool's own refusal (``status_failure``) is a different fact
    from the transport failing, and the camera is optional context, never an
    error.  The panel's ported resolver consumes these axes; nothing here
    pre-resolves a view, because the resolver is the one place allowed to.
    """
    payload: dict[str, Any] = {"kind": MONITOR_PAYLOAD_KIND}
    if bridge is not None:
        payload["bridge"] = {
            "online": bool(bridge.get("online")),
            "paired": bool(bridge.get("paired")),
            "lastSeenAt": bridge.get("lastSeenAt"),
        }
        # Direct mode (a local instance on the printer's own machine) marks
        # its transport so a reader can tell "bridge up" from "no bridge
        # needed" — the panel ignores unknown fields, the agent should not.
        if bridge.get("transport"):
            payload["bridge"]["transport"] = bridge["transport"]
    if bridge_error:
        payload["bridge_error"] = bridge_error
    if status is not None:
        payload["status"] = status
    if status_failure is not None:
        payload["status_failure"] = {
            "code": status_failure.get("code"),
            "message": status_failure.get("message"),
        }
    if camera_base64:
        payload["camera"] = {"image_base64": camera_base64}
    if camera_note:
        payload["camera_note"] = camera_note
    if account is not None:
        payload["account"] = {"signed_in": bool(account.get("signed_in"))}
    return payload
