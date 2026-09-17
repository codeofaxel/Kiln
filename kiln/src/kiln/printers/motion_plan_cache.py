"""On-disk cache of served motion plans, so a paired printer keeps homing offline.

A plan the hosted service handed this install for one of its own machines
is kept under ``~/.kiln/motion_plans/``, one file per (machine, verb,
axes, consent), encrypted at rest with a key derived from the sign-in
and this device's fingerprint -- the same shape as the served-overlay
cache, and the same honest limit: the machine that runs the plan has to
be able to read it, so the cache protects the file on disk, not the plan
in use.  What it never holds is a plan for a machine this install was not
served for: the service answers only for a paired printer, and only what
it answered is written here.

A plan is served fresh whenever the network allows (:func:`store` runs on
every successful serve); the cache is read only when the service does not
ANSWER -- a refusal from the service drops the cached copy for that
request (:func:`forget`), because a machine the service no longer serves
must not keep homing from a stale copy -- and a cached plan older than
:data:`MAX_AGE_S` is not used either, so a record the service has since
corrected cannot outlive the correction by more than that.  Every failure
here reads as "no cached plan", never as a plan.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A cached plan is trusted for this long after the last successful serve.
MAX_AGE_S: float = 30 * 24 * 3600.0
_DIR_NAME = "motion_plans"
_VERSION = 1


def _kiln_dir() -> Path:
    return Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))


def _identity() -> str | None:
    """A stable identity for the key: who is signed in, on which device.

    Returns ``None`` when nothing is signed in -- then nothing is cached and
    nothing cached can be read, which is the point: a plan is served to a
    signed-in account, and it is kept for that account only.
    """
    try:
        from kiln.api_device import device_fingerprint
        from kiln.auth_session import _read_tokens

        stored = _read_tokens() or {}
        who = str(stored.get("email") or stored.get("sub") or stored.get("user_id") or "").strip()
        if not who:
            return None
        return f"{who}|{device_fingerprint()}"
    except Exception:  # noqa: BLE001 -- no identity, no cache
        return None


def _fernet(identity: str) -> Any | None:
    try:
        from cryptography.fernet import Fernet
    except Exception:  # noqa: BLE001 -- without the library nothing is written or read
        return None
    digest = hashlib.sha256(f"kiln-motion-plan-cache/{_VERSION}/{identity}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _path(request: dict[str, Any]) -> Path:
    key = "|".join(str(request.get(k, "")) for k in ("printer_id", "serial", "verb", "axes", "on_plate_ok"))
    name = hashlib.sha256(key.encode()).hexdigest()[:32]
    return _kiln_dir() / _DIR_NAME / f"{name}.plan"


def store(request: dict[str, Any], doc: dict[str, Any]) -> bool:
    """Keep *doc* for *request*; ``True`` when written.  Never raises."""
    identity = _identity()
    if not identity:
        return False
    fernet = _fernet(identity)
    if fernet is None:
        return False
    try:
        path = _path(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": _VERSION, "stored_at": time.time(), "request": request, "plan": doc}).encode()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(fernet.encrypt(payload))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("motion plan cache write failed", exc_info=True)
        return False


def load(request: dict[str, Any]) -> dict[str, Any] | None:
    """The cached plan for *request*, or ``None`` (missing, stale, unreadable, foreign)."""
    identity = _identity()
    if not identity:
        return None
    fernet = _fernet(identity)
    if fernet is None:
        return None
    try:
        path = _path(request)
        if not path.is_file():
            return None
        payload = json.loads(fernet.decrypt(path.read_bytes()).decode())
        if not isinstance(payload, dict) or payload.get("version") != _VERSION:
            return None
        if time.time() - float(payload.get("stored_at") or 0) > MAX_AGE_S:
            return None
        same = {k: str(v) for k, v in (payload.get("request") or {}).items()}
        wanted = {k: str(v) for k, v in request.items()}
        if same != wanted:
            return None
        plan = payload.get("plan")
        return plan if isinstance(plan, dict) else None
    except Exception:  # noqa: BLE001 -- a torn or foreign file is not a plan
        logger.debug("motion plan cache read failed", exc_info=True)
        return None


def forget(request: dict[str, Any]) -> bool:
    """Drop the cached plan for *request* (the service refused it).  Never raises."""
    try:
        path = _path(request)
        if path.is_file():
            path.unlink()
            return True
    except Exception:  # noqa: BLE001
        logger.debug("motion plan cache forget failed", exc_info=True)
    return False


def forget_all() -> int:
    """Remove every cached plan (a sign-out, or a person's request).  Returns the count."""
    removed = 0
    try:
        for path in (_kiln_dir() / _DIR_NAME).glob("*.plan"):
            path.unlink(missing_ok=True)
            removed += 1
    except Exception:  # noqa: BLE001
        logger.debug("motion plan cache clear failed", exc_info=True)
    return removed
