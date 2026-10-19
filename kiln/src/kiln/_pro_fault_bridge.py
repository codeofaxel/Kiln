"""Public-Kiln -> kiln-pro fault-reading bridge.

The one place public Kiln asks kiln-pro what a printer's fault code means.
Every door that shows a fault -- ``printer_status``'s ``fault_note`` and
``fault_remedy``, the load / unload / purge results, the fault event on the
bus, ``troubleshoot_printer``'s code lookup -- reaches the reading through
``kiln.printers.bambu.read_bambu_fault``, and that function asks here first.
So a reading lives in exactly one place and every door says the same thing.

Why the readings are not in this repo.  A fault code's cause and fix are
know-how Kiln has paid for (a vendor page read closely, an owner thread
where the real fix was found, a machine on the bench).  Where know-how
lives (the repo) and who pays for it (the tier) are two different axes:
kiln-pro holds the reading, and decides per row whether it is free -- a
safety or fix floor is never paywalled -- or paid depth.  Public Kiln keeps
the mechanism (this bridge, the door wiring) and a family line that says
what kind of fault it is and where the reading is.

With kiln-pro installed here the reading is read locally.  Otherwise this
asks Kiln's servers (https://kiln3d.com) for the one code, through the door
every served tool uses (``kiln.server._pro_api_call``), and keeps the answer
for the session: a code's reading does not change, and a printer that keeps
raising one code is asked about it once.  The servers apply the per-row rule
from the account asking, so a reading that comes back here is one the caller
is entitled to; nothing in this file decides a tier.

A code that gets no reading -- offline, signed out, the servers silent or
saying no, or a row the caller's plan does not reach -- falls back to public
Kiln's own family line, and :func:`note_for` holds the one sentence a door
says about why (in the shared voice, :mod:`kiln.served_answer`).  A fault is
never made to read as cleared or as fine.  A door that must not wait on the
network (the printer's own message thread) asks with ``wait=False``: it gets
what is already known, and the answer is fetched beside it for the next door.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: The hosted door, asked when kiln-pro is not installed here.
WIRE_TOOL = "fault_reading"
#: How long one served answer is kept.  A code's reading does not change;
#: a plan change shows within the hour.
READING_TTL_S: float = 3600.0
#: After the servers fail to answer, how long the bridge stops asking (the
#: same backoff the blade and nozzle bridges use), so a printer that keeps
#: reporting a fault on a dead link costs one timeout, not one per status read.
SERVICE_BACKOFF_S: float = 300.0
#: A status read must not wait long on the network for a fault's reading.
CONSULT_TIMEOUT_S: float = 4.0

_HEX = frozenset("0123456789ABCDEF")
_lock = threading.Lock()
#: ``(hex digits, kind) -> (expires at, the server's answer)``.
_answers: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
#: ``(hex digits, kind) -> why the last ask got no answer`` (a
#: :class:`kiln.served_answer.Miss`), cleared by an answer.
_misses: dict[tuple[str, str], Any] = {}
#: ``(hex digits, kind) -> set when the ask in flight for it has finished``:
#: a second door reading the same code joins that ask rather than sending
#: its own.
_inflight: dict[tuple[str, str], threading.Event] = {}
_service_down_until: float = 0.0
_service_down_miss: Any = None


def available() -> bool:
    """True when kiln-pro is installed and its fault catalog imports."""
    try:
        import kiln_pro.device_intelligence.hms_catalog  # noqa: F401

        return True
    except ImportError:
        return False


def decode_fault(code: str, *, kind: str = "print_error", wait: bool = True) -> dict[str, Any] | None:
    """kiln-pro's reading of a Bambu fault code, or ``None``.

    *kind* names which of Bambu's two fault fields the code came out of
    (``"print_error"`` or ``"hms"``), because the same eight digits mean
    different things in each and kiln-pro refuses to answer for the wrong
    one.  The dict, when there is one, carries at least ``cause`` and
    ``fix`` and never a URL -- the served shape is allowlisted on the
    kiln-pro side.  ``None`` means: no reading reached this install (see
    :func:`note_for` for why), no row for this code, or a row the caller's
    tier does not unlock.  All read the same to a door, which then falls
    back to public Kiln's own family line.
    """
    if not code or not isinstance(code, str):
        return None
    try:
        from kiln_pro.device_intelligence.hms_catalog import decode_for_caller
    except ImportError:
        answer = served_reading(code, kind=kind, wait=wait)
        reading = answer.get("reading") if answer else None
        return dict(reading) if isinstance(reading, dict) and reading.get("cause") else None
    try:
        decoded = decode_for_caller(code, namespace=kind)
    except Exception as exc:  # noqa: BLE001 -- a reading is never worth a crash
        logger.debug("kiln-pro fault decode failed for %s: %s", code, exc)
        return None
    if not isinstance(decoded, dict) or not decoded.get("cause"):
        return None
    return decoded


def safety_alert(code: str, *, kind: str = "hms", wait: bool = True) -> dict[str, Any] | None:
    """The immediate protective action a fault code carries, or ``None``.

    ``{"code", "means", "action", "severity"}`` -- the AMS dryer's thermal
    runaway code is the reason this exists: its answer is "turn off the AMS
    power supply", served at every tier.  Local with kiln-pro installed,
    else from the same served answer as the reading (no second request).
    """
    if not code or not isinstance(code, str):
        return None
    try:
        from kiln_pro.device_intelligence.ams_intelligence import ams_safety_alert
    except ImportError:
        answer = served_reading(code, kind=kind, wait=wait)
        alert = answer.get("safety_alert") if answer else None
        return dict(alert) if isinstance(alert, dict) and alert.get("means") else None
    try:
        alert = ams_safety_alert(code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("kiln-pro AMS safety alert failed for %s: %s", code, exc)
        return None
    return dict(alert) if isinstance(alert, dict) and alert.get("means") else None


def served_guide_for_code(code: str, *, kind: str | None = None) -> tuple[str, int] | None:
    """``(guide slug, start_at_step)`` from the served reading of *code*.

    The code -> guide pointer belongs to the code's row, so it rides the same
    served answer as the reading; the guide bridge asks here when kiln-pro is
    not installed.
    """
    answer = served_reading(code, kind=kind or _inferred_kind(code))
    guide = answer.get("guide") if answer else None
    if not isinstance(guide, dict) or not isinstance(guide.get("slug"), str) or not guide["slug"]:
        return None
    try:
        start = int(guide.get("start_at_step") or 1)
    except (TypeError, ValueError):
        start = 1
    return guide["slug"], max(1, start)


def note_for(code: str, *, kind: str = "print_error") -> str | None:
    """The one sentence a door says about *code*'s reading, or ``None``.

    Read from what this bridge already knows -- never a request.  ``None``
    when the reading arrived (or kiln-pro answered here, where nothing can
    miss).  Otherwise why it did not: the servers' own line when the code's
    row is beyond the caller's plan, else the miss in the shared voice
    (offline, signed out, unanswered, refused).  A door says it once.
    """
    key = _key(code, kind)
    if not key[0]:
        return None
    with _lock:
        held = _answers.get(key)
        miss = _misses.get(key)
    if held is not None:
        answer = held[1]
        if isinstance(answer.get("reading"), dict):
            return None
        upgrade = answer.get("upgrade")
        if isinstance(upgrade, str) and upgrade.strip():
            url = answer.get("upgrade_url")
            return f"{upgrade.strip()} {url}" if isinstance(url, str) and url else upgrade.strip()
        return None
    if miss is None:
        return None
    from kiln.served_answer import sentence

    shown = _shown(code)
    return sentence(
        miss,
        feature="servers",
        on_the_line="Kiln reads each fault code a printer raises: what it means and how to clear it",
        cannot=f"read {shown}",
        wont="shows only what kind of fault it is",
        safe_remedy="Clear it the way the printer's own screen says",
        then="ask again",
    )


def miss_for(code: str, *, kind: str = "print_error") -> dict[str, str]:
    """The machine-readable why beside :func:`note_for`: ``why`` /
    ``why_code`` / ``why_detail``, or ``{}`` when nothing was missed."""
    from kiln.served_answer import fields

    with _lock:
        miss = _misses.get(_key(code, kind))
    return fields(miss)


def served_reading(code: str, *, kind: str = "print_error", wait: bool = True) -> dict[str, Any] | None:
    """The servers' answer for one code, or ``None`` with why kept for :func:`note_for`.

    Kept for :data:`READING_TTL_S`.  With *wait* false a code not yet known
    is fetched on a short-lived thread and ``None`` comes back at once, for
    the doors that run on the printer's own message thread.
    """
    key = _key(code, kind)
    if not key[0]:
        return None
    with _lock:
        held = _answers.get(key)
        if held is not None and held[0] > time.monotonic():
            return held[1]
        pending = _inflight.get(key)
        mine = pending is None
        if mine:
            pending = _inflight[key] = threading.Event()
    if mine and wait:
        try:
            return _fetch(key, code, kind)
        finally:
            _finished(key, pending)
    if mine:
        threading.Thread(
            target=_fetch_beside, args=(key, code, kind, pending), name="kiln-fault-reading", daemon=True,
        ).start()
        return None
    if not wait:
        return None
    pending.wait(CONSULT_TIMEOUT_S + 1.0)
    with _lock:
        held = _answers.get(key)
    return held[1] if held is not None else None


def _fetch_beside(key: tuple[str, str], code: str, kind: str, pending: threading.Event) -> None:
    try:
        _fetch(key, code, kind)
    finally:
        _finished(key, pending)


def _finished(key: tuple[str, str], pending: threading.Event) -> None:
    with _lock:
        if _inflight.get(key) is pending:
            del _inflight[key]
    pending.set()


def _fetch(key: tuple[str, str], code: str, kind: str) -> dict[str, Any] | None:
    global _service_down_until, _service_down_miss
    from kiln.served_answer import Miss, classify_answer, classify_transport_error

    if time.monotonic() < _service_down_until:
        with _lock:
            _misses[key] = _service_down_miss or Miss("unanswered", detail="the servers did not answer a moment ago")
        return None
    try:
        import kiln.server as _srv

        answer = _srv._pro_api_call(
            WIRE_TOOL, _timeout=CONSULT_TIMEOUT_S, _asked_by_user=False, code=str(code), kind=str(kind or ""),
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("%s request failed", WIRE_TOOL, exc_info=True)
        import kiln.server as _srv

        miss = classify_transport_error(exc, host=getattr(_srv, "_HOSTED_KILN_API_URL", None))
        _service_down_until, _service_down_miss = time.monotonic() + SERVICE_BACKOFF_S, miss
        with _lock:
            _misses[key] = miss
        return None
    if isinstance(answer, dict) and answer.get("success") is True and "reading" in answer:
        with _lock:
            _answers[key] = (time.monotonic() + READING_TTL_S, answer)
            _misses.pop(key, None)
        return answer
    miss = classify_answer(answer) or Miss("unanswered", detail="no reading in the answer")
    if isinstance(answer, dict) and answer.get("code") == "SERVER_UNREACHABLE":
        _service_down_until, _service_down_miss = time.monotonic() + SERVICE_BACKOFF_S, miss
    with _lock:
        _misses[key] = miss
    return None


def forget() -> None:
    """Drop every kept answer and miss (a sign-in or sign-out changes what
    the servers would say)."""
    global _service_down_until, _service_down_miss
    with _lock:
        _answers.clear()
        _misses.clear()
        _inflight.clear()
    _service_down_until, _service_down_miss = 0.0, None


def _key(code: str, kind: str | None) -> tuple[str, str]:
    """``(the code's hex digits, its kind)``: the screen's trailing serial is
    a separate token and is dropped, the separators do not matter."""
    head = str(code or "").split()
    digits = "".join(c for c in (head[0].upper() if head else "") if c in _HEX)
    return digits, str(kind or "")


def _inferred_kind(code: str) -> str:
    return "hms" if len(_key(code, "")[0]) >= 16 else "print_error"


def _shown(code: str) -> str:
    """The code as the printer's screen spells it (``1200-8007``)."""
    digits = _key(code, "")[0]
    if len(digits) < 8:
        return str(code).strip() or "this code"
    return "-".join(digits[i : i + 4] for i in range(0, len(digits), 4))


__all__ = [
    "CONSULT_TIMEOUT_S",
    "READING_TTL_S",
    "SERVICE_BACKOFF_S",
    "WIRE_TOOL",
    "available",
    "decode_fault",
    "forget",
    "miss_for",
    "note_for",
    "safety_alert",
    "served_guide_for_code",
    "served_reading",
]
