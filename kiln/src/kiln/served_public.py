"""Public tools a signed-in install runs on Kiln's servers.

Some of public Kiln's design and analysis tools give a fuller answer on
Kiln's servers than on this computer alone: there they run with Kiln's own
checks beside them, the same way they run for Kiln's hosted connector.  The
bundled manifest names those tools (the ``served_public`` block of
``pro_tool_manifest.json``, written by kiln-pro, https://kiln3d.com), what
each needs sent, and which calls stay on this computer.  Nothing here keeps
a list of its own.

On an install without kiln-pro, signed in, a call to a named tool is three
steps: the files it names are sent (:func:`kiln.served_makes.send_inputs`),
the same tool runs on Kiln's servers (:func:`kiln.server._pro_api_call`),
and the files it wrote come back, saved where the call asked for them or
under ``~/.kiln/served/files``, with their paths on this computer in the
answer.  The answer is otherwise the servers' own, word for word.  A tool
that answers for one of this computer's printers (the manifest names it
``reads_first``) first reads what its printers ARE -- name, model, the
nozzle size the printer reports -- and sends that with the call, never an
address or a credential (:func:`kiln.served_steps.read_first`).

Signed out, offline, or turned away, the tool runs on this computer
instead, and the first such answer for each of the four causes
(:mod:`kiln.served_answer`) says why, once per session.  With kiln-pro in
the process (Kiln's servers themselves) every tool runs here.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The version of the manifest block this build reads.
SCHEMA_VERSION = 1

#: Where the note rides on an answer made here instead.
NOTE_KEY = "answered_on_this_computer"

#: A 200 that is the servers' door, not the tool, saying no: a file the call
#: sent was gone by the time the tool ran.  Worth one more try, never the
#: tool's own answer.
_FILE_GONE_CODE = "SOURCE_NOT_AVAILABLE"

_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_FETCH_TIMEOUT_S = 300.0

_tools: dict[str, dict[str, Any]] = {}
_said: set[str] = set()
_lock = threading.Lock()


def load(block: Any) -> None:
    """Take the manifest's ``served_public`` block.  A block of a version
    this build cannot read leaves every tool on this computer."""
    with _lock:
        _tools.clear()
        if not isinstance(block, dict) or block.get("schema_version") != SCHEMA_VERSION:
            return
        tools = block.get("tools")
        if not isinstance(tools, dict):
            return
        for name, entry in tools.items():
            if isinstance(name, str) and isinstance(entry, dict):
                _tools[name] = entry


def _kiln_pro_here() -> bool:
    """Whether kiln-pro is loaded in this process, so the tool's whole
    answer is made here (Kiln's servers run every tool this way).  The same
    fact that decided, at start, whether the paid-tool stubs were needed."""
    return sys.modules.get("kiln_pro") is not None


def plan(name: str, arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """The manifest entry when this call is run on Kiln's servers, else
    ``None``: the tool is not named, kiln-pro is here, or the call's own
    arguments reach something kept on this computer."""
    entry = _tools.get(name)
    if entry is None or _kiln_pro_here():
        return None
    for param, values in (entry.get("stays_here_when") or {}).items():
        if isinstance(values, list) and (arguments or {}).get(param) in values:
            return None
    return entry


def ask(
    name: str, arguments: dict[str, Any], entry: dict[str, Any],
) -> tuple[dict[str, Any] | None, Any]:
    """Run *name* on Kiln's servers.  ``(answer, None)`` with every file it
    wrote saved here, or ``(None, miss)`` when it should run here instead:
    *miss* is the :class:`kiln.served_answer.Miss` to say, or ``None`` when
    there is nothing to say (the tool, run here, names a missing file
    itself).  Blocking; never raises."""
    import kiln.server as srv
    from kiln import served_makes
    from kiln.served_answer import SESSION_EXPIRED_CODE, Miss, classify_answer

    try:
        bearer, expired = srv._served_bearer()
        if not bearer:
            code = SESSION_EXPIRED_CODE if expired is not None else "KILN_ACCOUNT_NOT_PAIRED"
            return None, Miss("signed_out", code)
        facts, not_read = _read_first(name, arguments, entry)
        if not_read is not None:
            return None, classify_answer(not_read) or Miss("refused")
        trip: dict[str, Any] = {}
        kwargs, refusal = served_makes.send_inputs(
            name, dict(arguments), entry.get("inputs"), trip,
        )
        if refusal is not None:
            if refusal.get("code") == "FILE_NOT_FOUND":
                return None, None
            return None, classify_answer(refusal) or Miss("unanswered")
        if facts:
            kwargs["step_results"] = facts
        http: dict[str, Any] = {}
        answer = srv._pro_api_call(
            name, _timeout=srv._SERVED_MAKE_WAIT_S, _http=http, **kwargs,
        )
        if http.get("status") != 200:
            miss = classify_answer(answer) or Miss("unanswered", "KILN_API_HTTP_ERROR")
            return None, _in_their_words(miss, answer)
        if isinstance(answer, dict) and answer.get("code") == _FILE_GONE_CODE:
            return None, Miss("unanswered", _FILE_GONE_CODE, str(answer.get("error") or ""))
        answer, whole = _bring_home(answer, trip)
        if not whole:
            # The servers made a file this computer did not receive: an
            # answer pointing at it would open nothing.
            return None, Miss("unanswered", "FILE_NOT_BROUGHT")
        return answer, None
    except Exception as exc:  # noqa: BLE001 -- the tool still runs here
        logger.debug("served public: %s not run on Kiln's servers", name, exc_info=True)
        return None, Miss("unanswered", "SERVER_UNREACHABLE", str(exc)[:400])


def _read_first(
    name: str, arguments: dict[str, Any], entry: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """What this computer reads for the call before it goes (the manifest
    entry's ``reads_first``: which printer the call is about, its model,
    the nozzle it reports), as ``step_results``; ``({}, None)`` when the
    entry asks for nothing, ``({}, refusal)`` when a reading is refused."""
    reads = entry.get("reads_first")
    if not reads:
        return {}, None
    from kiln import served_steps

    param = entry.get("printer_param")
    asked = arguments.get(param) if isinstance(param, str) else None
    facts, refusal = served_steps.read_first(
        name, reads, printer_name=str(asked or ""), call=arguments,
    )
    listed = (facts.get("printer_facts") or {}).get("data") or {}
    if refusal is None and not listed.get("printers"):
        # No printer set up here: nothing to say about one.
        facts.pop("printer_facts", None)
    return facts, refusal


def _in_their_words(miss: Any, answer: Any) -> Any:
    """*miss* with the servers' own sentence as its detail when their answer
    has one: a refusal's ``error`` can be a bare code word ("upgrade_required"),
    with the sentence for a person elsewhere in the answer."""
    from kiln.served_answer import Miss

    if miss.cause != "refused" or " " in miss.detail or not isinstance(answer, dict):
        return miss
    upgrade = answer.get("upgrade")
    for said in (
        upgrade.get("headline") if isinstance(upgrade, dict) else None,
        answer.get("message"),
        answer.get("detail"),
    ):
        if isinstance(said, str) and " " in said.strip():
            return Miss(miss.cause, miss.code, said.strip()[:400])
    return Miss(miss.cause, miss.code, "")


def say_once(name: str, result: Any, miss: Any) -> Any:
    """*result*, made on this computer, with the first note this session
    for *miss*'s cause.  Later answers for the same cause say nothing."""
    if miss is None or not isinstance(result, dict):
        return result
    from kiln.served_answer import fields, sentence

    with _lock:
        if miss.cause in _said:
            return result
        _said.add(miss.cause)
    result[NOTE_KEY] = {
        "note": sentence(
            miss,
            feature="servers",
            on_the_line="This answer comes from this computer alone, without what Kiln's servers add to it",
            cannot=f"run {name} on its servers",
            wont="answered here instead",
            then="ask again",
        ),
        **fields(miss),
    }
    return result


# ---------------------------------------------------------------------------
# The files an answer names, brought to this computer
# ---------------------------------------------------------------------------


def _bring_home(answer: Any, trip: dict[str, Any]) -> tuple[Any, bool]:
    """``(answer, whole)``: *answer* with every file the servers handed over
    saved here and named by its path here, and whether every one arrived.
    A failed answer carries no files and is whole as it is."""
    from kiln import served_makes

    if not isinstance(answer, dict) or answer.get("success") is False or answer.get("status") == "error":
        return answer, True
    hand_over = answer.get("hand_over")
    if isinstance(hand_over, dict) and hand_over.get("available") is False:
        return answer, False
    before = _handed_over_paths(answer)
    served_makes._bring_documents(answer)
    served_makes._bring_files(answer, trip)
    moved = {
        theirs: item["path"]
        for (block, index), theirs in before.items()
        if isinstance(item := answer[block][index], dict)
        and item.get("on_this_computer") and isinstance(item.get("path"), str)
    }
    whole = all(
        item.get("on_this_computer") is not False
        for block in ("documents", "files")
        for item in (answer.get(block) or [])
        if isinstance(item, dict)
    )
    for key in ("artifact", "step_artifact"):
        whole = _bring_model(answer, answer, key, trip, moved) and whole
    if isinstance(answer.get("step"), dict):
        whole = _bring_model(answer, answer["step"], "artifact", trip, moved) and whole
    # A path the answer also quotes in its words ("Compiled to /tmp/...")
    # reads as the file here too.
    _replace_inside(answer, moved)
    served_makes._scrub_below_the_top(answer)
    return served_makes._without_server_files(answer), whole


def _handed_over_paths(answer: dict[str, Any]) -> dict[tuple[str, int], str]:
    """``{(block, index): servers' path}`` for each handed-over document or
    file whose place in the answer is named, read before it is saved."""
    from kiln import served_makes

    found: dict[tuple[str, int], str] = {}
    for block in ("documents", "files"):
        for index, item in enumerate(answer.get(block) or []):
            slot = served_makes._slot(answer, item.get("at")) if isinstance(item, dict) else None
            if slot is not None and isinstance(slot[0][slot[1]], str):
                found[(block, index)] = slot[0][slot[1]]
    return found


def _replace_inside(value: Any, moved: dict[str, str], depth: int = 0) -> Any:
    """Every string in *value* with each servers' path in *moved* replaced,
    wherever it appears in the string, by the path here."""
    if not moved or depth > 8:
        return value
    if isinstance(value, str):
        for theirs, ours in moved.items():
            if theirs and theirs in value:
                value = value.replace(theirs, ours)
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            value[key] = _replace_inside(item, moved, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _replace_inside(item, moved, depth + 1)
    return value


def _bring_model(
    answer: dict[str, Any], holder: dict[str, Any], key: str, trip: dict[str, Any],
    moved: dict[str, str],
) -> bool:
    """Save the model a handed-over token names (``holder[key]``) where the
    answer says it sits (its ``at``), note the move in *moved*, and drop the
    token block.  ``False`` when there was a model to save and it did not
    arrive.  A make that must be kept first (a metered make) is not one this
    module is handed."""
    from kiln import served_makes

    block = holder.get(key)
    if not isinstance(block, dict):
        return True
    keep = block.get("keep")
    if isinstance(keep, dict) and keep.get("required"):
        return True
    holder.pop(key, None)
    token = str(block.get("artifact_token") or "")
    kind = str(block.get("format") or "stl").lower()
    slot = served_makes._slot(answer, block.get("at"))
    suffix = served_makes._KIND_SUFFIX.get(kind)
    if not _TOKEN_SHAPE.match(token) or slot is None or suffix is None:
        return False
    container, at_key = slot
    theirs = container[at_key]
    if not isinstance(theirs, str) or not served_makes._is_elsewhere(theirs):
        return True  # already saved here by another hand-over
    data = _fetch(token)
    if data is None:
        return False
    path = _destination(block.get("at"), theirs, token, suffix, trip)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        logger.debug("served public: %s not saved", path, exc_info=True)
        return False
    container[at_key] = str(path)
    moved[theirs] = str(path)
    files = answer.get("files")
    if not isinstance(files, list):
        files = answer["files"] = []
    files.append({
        "filename": path.name,
        "format": kind,
        "path": str(path),
        "on_this_computer": True,
    })
    return True


def _destination(at: Any, theirs: str, token: str, suffix: str, trip: dict[str, Any]) -> Path:
    """Where a handed-over model is saved: the path the call asked for it
    (its parameter), else the folder the call named, else
    ``~/.kiln/served/files``."""
    from kiln import served_makes

    wanted = (trip or {}).get("output_files") or {}
    if isinstance(at, list) and len(at) == 1 and isinstance(wanted.get(at[0]), str):
        return Path(wanted[at[0]]).expanduser()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(theirs).stem).strip("-.") or "model"
    folder = next(iter(((trip or {}).get("output_folders") or {}).values()), None)
    if folder:
        return Path(folder).expanduser() / f"{stem[:80]}{suffix}"
    return served_makes.files_dir() / f"{stem[:80]}-{token[:6]}{suffix}"


def _fetch(token: str) -> bytes | None:
    """The bytes the servers hold under *token*, or ``None``."""
    import httpx

    from kiln import served_makes

    bearer = served_makes._bearer()
    if not bearer:
        return None
    try:
        resp = httpx.get(
            f"{served_makes._api_base()}/api/artifact/{token}",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_FETCH_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 -- the answer stands without the file
        logger.debug("served public: file not fetched", exc_info=True)
        return None
    if resp.status_code != 200 or "json" in (resp.headers.get("content-type") or "").lower():
        return None
    data = resp.content
    if not data or len(data) > served_makes.MAX_FILE_BYTES:
        return None
    return data


def _reset_for_tests() -> None:
    with _lock:
        _tools.clear()
        _said.clear()


__all__ = ["NOTE_KEY", "SCHEMA_VERSION", "ask", "load", "plan", "say_once"]
