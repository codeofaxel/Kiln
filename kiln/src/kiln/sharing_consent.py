"""The person's own answer about sharing, and the switches that defer to it.

Two things an install can send on its own: the anonymous daily usage report
(:mod:`kiln.heartbeat`, ``KILN_TELEMETRY``) and anonymous print outcomes for
the shared learning loop (:mod:`kiln.community_sync`, ``KILN_COMMUNITY_OPT_IN``).
Each switch reads one of three settings:

* off (``false``, ``0``, ``no``, ``off``): nothing is sent, whatever the
  person said, because whoever set the environment decided.
* ``ask``: nothing is sent until the person turns sharing on
  (``kiln sharing on``).  A program that installs Kiln under rules requiring
  opt-in sets this, and :mod:`kiln.sharing_nudge` asks the person once.
* anything else, or unset: on, unless the person turned it off.

The answer is one choice covering both, kept in ``~/.kiln/sharing.json``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

TELEMETRY_ENV = "KILN_TELEMETRY"
COMMUNITY_ENV = "KILN_COMMUNITY_OPT_IN"
ASK = "ask"
_OFF = ("false", "0", "no", "off")


def store_path() -> Path:
    # Resolved at call time so HOME overrides (tests, sandboxes) work.
    return Path.home() / ".kiln" / "sharing.json"


def _read() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def stored_answer() -> bool | None:
    """``True`` / ``False`` once the person has answered, else ``None``."""
    share = _read().get("share")
    return share if isinstance(share, bool) else None


def record_answer(share: bool, *, via: str) -> None:
    data = _read()
    data.update({"share": bool(share), "answered_at": time.time(), "via": via})
    _write(data)


def setting(env: str) -> str:
    return os.environ.get(env, "").strip().lower()


def resolve(env: str) -> bool:
    """Whether the sender behind ``env`` may send, by the rules above."""
    value = setting(env)
    if value in _OFF:
        return False
    answer = stored_answer()
    if value == ASK:
        return answer is True
    return answer is not False


def waiting_for_an_answer() -> bool:
    """An ``ask`` install the person has not answered yet."""
    asks = any(setting(env) == ASK for env in (TELEMETRY_ENV, COMMUNITY_ENV))
    return asks and stored_answer() is None


def was_asked() -> bool:
    return bool(_read().get("asked_at"))


def mark_asked() -> None:
    data = _read()
    data["asked_at"] = time.time()
    _write(data)
