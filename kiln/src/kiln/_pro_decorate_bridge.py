"""Public-Kiln -> kiln-pro bridge for what Kiln's servers add to a decoration.

``decorate_surface`` carves an image or a word into a face on this computer.
Kiln's servers (kiln-pro, https://kiln3d.com) add three things to the same
tool: a depth too shallow to show is raised, for everyone; a decoration on
the face that sits on the bed is put in a shallow recess (Kiln Pro); and a QR
code is put on the part as one print file in two colours (Kiln Pro).

With kiln-pro installed here (Kiln's servers) those run in this process's
care.  On an install without it, a call that needs one of them is sent to
Kiln's servers whole, the same tool runs there, and the files it wrote come
back (:mod:`kiln.served_public`).  A recess is asked for only when the
signed-in plan includes it, so nobody waits on an answer their plan would
refuse.

Signed out, offline, the servers silent or saying no: a carve still runs on
this computer as it always has, and says once why the servers' part did not
come.  A QR code has no version on this computer, so nothing is written and
the answer says which of the four it was (:mod:`kiln.served_answer`).
"""

from __future__ import annotations

from typing import Any

#: The tool Kiln's servers run for an install without kiln-pro.
WIRE_TOOL = "decorate_surface"
#: Where the same work is done when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.decoration.one_door"
#: The code on an answer that says the servers were not reached for a QR.
NO_QR_CODE = "QR_NOT_MADE"

__all__ = [
    "LOCAL_MODULE",
    "NO_QR_CODE",
    "WIRE_TOOL",
    "before_carve",
    "finish_carve",
    "kiln_pro_here",
    "on_servers",
    "plan_includes_recess",
    "qr_missed",
    "qr_on_part",
    "sketch_face",
]


def kiln_pro_here() -> bool:
    """Whether the servers' part runs in this process."""
    try:
        import kiln_pro.decoration.one_door  # noqa: F401
    except ImportError:
        return False
    return True


def before_carve(**facts: Any) -> dict[str, Any] | None:
    """The depth to carve at, a recess to end the carve in, and what to say;
    ``None`` on an install where that part is not here."""
    try:
        from kiln_pro.decoration.one_door import before_carve as prepare
    except ImportError:
        return None
    return prepare(**facts)


def finish_carve(carved_path: str, recess: dict[str, Any]) -> dict[str, Any]:
    """The carved part with its recess cut (``model_path``, ``done``), or
    ``error``.  Called only where :func:`before_carve` answered."""
    try:
        from kiln_pro.decoration.one_door import finish_carve as finish
    except ImportError:
        return {"error": "the recess is cut on Kiln's servers"}
    return finish(carved_path, recess)


def qr_on_part(**asked: Any) -> dict[str, Any] | None:
    """The QR code for a part, or ``None`` on an install where the QR maker
    is not here (the caller then asks the servers, :func:`on_servers`)."""
    try:
        from kiln_pro.decoration.one_door import qr_on_part as make
    except ImportError:
        return None
    return make(**asked)


def sketch_face(model_path: str, face: str) -> dict[str, Any] | None:
    """A sketch-built part's own wall name as the face that reaches it, or
    ``None`` when the name is not one or that reader is not here."""
    try:
        from kiln_pro.decoration.one_door import sketch_face as resolve
    except ImportError:
        return None
    return resolve(model_path, face)


def plan_includes_recess() -> bool:
    """Whether this install's signed-in plan includes the bottom recess."""
    try:
        from kiln.licensing import LicenseTier, get_tier

        return get_tier() >= LicenseTier.PRO
    except Exception:  # noqa: BLE001 -- an unread plan asks for nothing
        return False


def on_servers(arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    """Run this same call on Kiln's servers: ``(answer, None)`` with its
    files saved here, or ``(None, miss)`` saying why not."""
    from kiln import served_public

    return served_public.answer_or_missed(WIRE_TOOL, arguments)


def qr_missed(miss: Any, model_name: str) -> dict[str, Any]:
    """The answer for a QR code the servers did not make, in words."""
    from kiln.served_answer import fields, sentence

    out: dict[str, Any] = {
        "status": "error",
        "success": False,
        "error": sentence(
            miss,
            feature="servers",
            on_the_line="Kiln makes a QR code for a part on its servers, with Kiln Pro",
            cannot=f"make a QR code for {model_name}",
            wont=f"wrote nothing and left {model_name} as it was",
            safe_remedy="",
        ),
        "code": miss.code or NO_QR_CODE,
        "retryable": miss.cause != "refused",
        **fields(miss),
    }
    if miss.cause == "signed_out":
        from kiln.tiers_and_terms import paywall_fields, signin_hint_fields

        out.update(signin_hint_fields())
        out.update(paywall_fields("pro"))
    return out
