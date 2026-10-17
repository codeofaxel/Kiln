"""Models that travel between this computer and Kiln's servers.

An install without kiln-pro reaches Kiln's served tools through stubs that
forward the call (``kiln.server._pro_api_call``).  A forwarded call carries
text.  A model is a file, and files need three things a forwarded call does
not do on its own:

**Arrival.**  A served make is built on Kiln's servers, and its answer named
paths there (``/tmp/kiln_generate_coaster_x/coaster.stl``) that do not exist
on this computer.  :func:`arrive` takes those paths out of the answer, says
plainly where the make is, and fetches the make's look into a private folder
so the stage on this computer can show it (:func:`arrival_path`).  The look
is for looking: its path never rides the answer.

**Keep.**  The file lands on this computer when it is kept (:func:`keep`).
A keep is where a free monthly allowance is spent; the server decides and
charges, and answers with the full-fidelity file, which is written here and
named in the answer.  Iterating stays free.

**Send.**  A served tool that works on the caller's own files cannot read
this computer's disk.  :func:`send_inputs` uploads each file the call names
and hands the tool a token for it; a make that is still on the servers is
named by its ``artifact_token`` and nothing is uploaded at all.  One model
and one image go by their own doors; every other file (a G-code, a second
model, a list of parts, a PDF) goes by one door for all of them, a G-code
compressed on the way.

**Bring.**  A served tool's answer names the files it wrote (a resume
file, a jointed part, a page picture) on the servers' disk.  The servers
hand each one over under a token; :func:`arrive` saves it here -- where the
call asked for its output, else under ``~/.kiln/served/files`` -- and puts
the real path where the servers' path was.

Which parameters take which files, and which say where a tool writes, come
from the manifest entry (``inputs``), written by the side that fills them.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Refuse to move a model larger than this in either direction (the same
#: ceiling the servers apply to an upload).
MAX_MODEL_BYTES = 64 * 1024 * 1024
#: The image door's ceiling.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_SENDABLE_MODEL_TYPES = frozenset({".stl", ".3mf", ".obj"})
_SENDABLE_IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".svg"})
_KIND_SUFFIX = {"stl": ".stl", "3mf": ".3mf", "obj": ".obj", "step": ".step"}

#: Every kind of file the servers take for a tool to read.  They decide
#: what a file is by its content; this only says which files on this
#: computer are worth sending at all.
_SENDABLE_FILE_TYPES = (
    _SENDABLE_MODEL_TYPES
    | _SENDABLE_IMAGE_TYPES
    | frozenset({".gcode", ".gco", ".g", ".step", ".stp", ".pdf", ".dxf", ".mtl", ".json"})
)
_GCODE_TYPES = frozenset({".gcode", ".gco", ".g"})
#: The largest G-code sent, before compressing, and the most of anything
#: that goes over the wire in one upload (the servers' own ceilings).
MAX_GCODE_BYTES = 256 * 1024 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
#: The most files one call brings.
MAX_FILES_PER_CALL = 32
#: Where a text parameter's file goes once it has been sent.
_INLINE_FILE_MARK = "{file}"
_INLINE_PREFIX = re.compile(r"^\s*[A-Za-z][A-Za-z0-9_]{0,31}:(?!//)")

_FETCH_TIMEOUT_S = 30.0
_UPLOAD_TIMEOUT_S = 60.0
#: A sliced file on a home uplink takes longer than a picture does.
_FILE_UPLOAD_TIMEOUT_S = 300.0
_FILE_FETCH_TIMEOUT_S = 300.0

#: How many makes the record remembers.
_RECORD_MAX = 256

#: An artifact token as the servers mint it: URL-safe, no path separators.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Where things are kept
# ---------------------------------------------------------------------------


def _served_home() -> Path:
    home = os.environ.get("KILN_HOME") or str(Path.home() / ".kiln")
    return Path(home) / "served"


def _arrivals_dir() -> Path:
    return _served_home() / "arrivals"


def kept_dir() -> Path:
    """Where kept makes are written."""
    return _served_home() / "kept"


def documents_dir() -> Path:
    """Where documents a served tool made (a drawing, a manual) are saved."""
    return _served_home() / "documents"


def files_dir() -> Path:
    """Where the other files a served tool wrote (a resume file, a jointed
    part) are saved when the call named no place for them."""
    return _served_home() / "files"


def _record_path() -> Path:
    return _served_home() / "makes.json"


def _read_record() -> dict[str, dict]:
    try:
        data = json.loads(_record_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — no record is an empty record
        return {}


def _remember(token: str, **facts: Any) -> None:
    """Merge *facts* into what this computer knows about the make *token*."""
    with _lock:
        record = _read_record()
        entry = dict(record.get(token) or {})
        entry.update({k: v for k, v in facts.items() if v is not None})
        entry.setdefault("made_at", int(time.time()))
        record.pop(token, None)
        record[token] = entry
        while len(record) > _RECORD_MAX:
            record.pop(next(iter(record)))
        path = _record_path()
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001 — the record is a convenience
            logger.debug("served makes: record not written", exc_info=True)


def _known(token: str) -> dict | None:
    entry = _read_record().get(token)
    return entry if isinstance(entry, dict) else None


def token_for_path(path: str) -> str | None:
    """The make a file on this computer is the kept copy (or the look) of."""
    try:
        wanted = str(Path(path).resolve())
    except Exception:  # noqa: BLE001
        return None
    for token, entry in _read_record().items():
        for key in ("kept", "arrival"):
            held = entry.get(key)
            if held and str(Path(held).resolve()) == wanted:
                return token
    return None


def arrival_path(token: str) -> str | None:
    """The file the stage shows for a make that is on Kiln's servers, or
    ``None`` when this computer holds no look for it.  A kept copy wins."""
    entry = _known(str(token or ""))
    if not entry:
        return None
    for key in ("kept", "arrival"):
        held = entry.get(key)
        if held and Path(held).is_file():
            return str(held)
    return None


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def _api_base() -> str:
    from kiln.auth_session import _api_base as base

    return base()


def _bearer() -> str:
    from kiln.auth_session import resolve_api_bearer

    return resolve_api_bearer().token


def _signed_out(tool: str) -> dict[str, Any]:
    from kiln.tiers_and_terms import signed_out_message, signin_hint_fields

    return {
        "success": False,
        "status": "error",
        "error": signed_out_message(),
        "code": "KILN_ACCOUNT_NOT_PAIRED",
        "tool": tool,
        "why": "signed_out",
        **signin_hint_fields(),
    }


def _fetch_look(token: str, kind: str) -> str | None:
    """Fetch what the servers serve for an un-kept make and write it to the
    private arrivals folder.  ``None`` when there is nothing to show."""
    import httpx

    bearer = _bearer()
    if not bearer:
        return None
    try:
        resp = httpx.get(
            f"{_api_base()}/api/artifact/{token}",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_FETCH_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — a look is furniture, never a failed make
        logger.debug("served makes: look not fetched", exc_info=True)
        return None
    if resp.status_code != 200:
        return None
    if "json" in (resp.headers.get("content-type") or "").lower():
        # The servers answered with words (a make that shows only once
        # kept), not a model.
        return None
    data = resp.content
    if not data or len(data) > MAX_MODEL_BYTES:
        return None
    folder = _arrivals_dir()
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = hashlib.sha256(token.encode()).hexdigest()[:24]
        path = folder / f"{name}{_KIND_SUFFIX.get(kind, '.stl')}"
        path.write_bytes(data)
    except Exception:  # noqa: BLE001
        logger.debug("served makes: look not written", exc_info=True)
        return None
    return str(path)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

#: The largest document saved (the servers' own ceiling for one).
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
_DOCUMENT_SUFFIX = {"pdf": ".pdf", "svg": ".svg", "dxf": ".dxf", "png": ".png"}


def _safe_filename(name: Any, kind: str, token: str) -> str:
    """A file name for a fetched document: the servers' name for it with
    anything that is not a plain character removed, the right suffix, and
    part of its token so two drawings never overwrite each other."""
    suffix = _DOCUMENT_SUFFIX[kind]
    stem = Path(str(name or "document")).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "document"
    return f"{stem[:60]}-{token[:6]}{suffix}"


def _replace_everywhere(value: Any, theirs: str, ours: str, depth: int = 0) -> None:
    """Swap every string equal to ``theirs`` for ``ours``, in place."""
    if depth > 6:
        return
    items: Any = ()
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = list(enumerate(value))
    for key, item in items:
        if item == theirs and isinstance(item, str):
            value[key] = ours
        elif isinstance(item, (dict, list)):
            _replace_everywhere(item, theirs, ours, depth + 1)


def _bring_documents(answer: dict) -> None:
    """Save each document the answer carries to this computer and put its
    path where the servers' path was.

    The servers name each one (``documents``: where it sits in the answer,
    its format, a token).  A drawing is the tool's product and was already
    paid for by the plan check that let the tool run, so there is no keep:
    it is fetched now.  One that cannot be fetched keeps its entry and says
    so; the rest of the answer stands.  Never raises.
    """
    documents = answer.get("documents")
    if not isinstance(documents, list) or not documents:
        return
    bearer = _bearer()
    import httpx

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        token = str(doc.get("artifact_token") or "")
        kind = str(doc.get("format") or "").lower()
        where = doc.get("at")
        doc["on_this_computer"] = False
        if (
            not bearer
            or not _TOKEN_SHAPE.match(token)
            or kind not in _DOCUMENT_SUFFIX
            or not isinstance(where, list)
            or not where
            or not all(isinstance(key, str) for key in where)
        ):
            continue
        try:
            resp = httpx.get(
                f"{_api_base()}/api/artifact/{token}",
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=_FETCH_TIMEOUT_S,
            )
            if resp.status_code != 200 or not resp.content:
                continue
            if len(resp.content) > MAX_DOCUMENT_BYTES:
                continue
            folder = documents_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / _safe_filename(doc.get("filename"), kind, token)
            path.write_bytes(resp.content)
        except Exception:  # noqa: BLE001 — one document must not cost the answer
            logger.debug("served makes: document not fetched", exc_info=True)
            continue
        # Put the path where the servers' own path was, and wherever else
        # the answer repeats that same path (a drawing names its sheet
        # under its outputs and again under its preview).
        block: Any = answer
        for key in where[:-1]:
            block = block.get(key) if isinstance(block, dict) else None
        if isinstance(block, dict) and where[-1] in block:
            theirs = block[where[-1]]
            block[where[-1]] = str(path)
            if _is_elsewhere(theirs):
                _replace_everywhere(answer, theirs, str(path))
        doc["path"] = str(path)
        doc["on_this_computer"] = True
        # The token and its link have done their work.
        doc.pop("artifact_token", None)
        doc.pop("url", None)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

#: The largest file saved (the servers' own ceiling for one).
MAX_FILE_BYTES = 256 * 1024 * 1024
_FILE_SUFFIX = {
    "gcode": ".gcode", "3mf": ".3mf", "stl": ".stl", "obj": ".obj",
    "png": ".png", "json": ".json",
}
_GZIP_MAGIC = b"\x1f\x8b"


def _inflated(data: bytes, limit: int) -> bytes | None:
    """*data* with its gzip undone, or ``None`` past *limit*."""
    import zlib

    inflater = zlib.decompressobj(wbits=31)
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit or inflater.unconsumed_tail:
        return None
    return out


def _plain_name(name: Any, kind: str) -> str:
    """The servers' name for a file, with nothing in it but plain
    characters and the suffix its kind has."""
    suffix = _FILE_SUFFIX[kind]
    stem = Path(str(name or "file")).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "file"
    return f"{stem[:80]}{suffix}"


def _slot(answer: dict, where: Any) -> tuple[Any, Any] | None:
    """The container and key *where* leads to in *answer*, or ``None``."""
    if not isinstance(where, list) or not where:
        return None
    block: Any = answer
    for key in where[:-1]:
        if isinstance(block, dict) and isinstance(key, str):
            block = block.get(key)
        elif isinstance(block, list) and isinstance(key, int) and 0 <= key < len(block):
            block = block[key]
        else:
            return None
    last = where[-1]
    if isinstance(block, dict) and isinstance(last, str) and last in block:
        return block, last
    if isinstance(block, list) and isinstance(last, int) and 0 <= last < len(block):
        return block, last
    return None


def _bring_files(answer: dict, trip: dict | None) -> None:
    """Save each file the answer hands over to this computer and put its
    path where the servers' path was.

    The servers name each one (``files``: where it sits in the answer, its
    format, a token, and the output parameter it was written for when there
    was one).  It is the product of a tool the plan already let run, so
    there is no keep: it is fetched now.  A file the call named a place for
    is saved exactly there; one written while the call named a folder goes
    into that folder; the rest go under :func:`files_dir`.  One that cannot
    be fetched keeps its entry and says so.  Never raises.
    """
    files = answer.get("files")
    if not isinstance(files, list) or not files:
        return
    bearer = _bearer()
    wanted_files = dict((trip or {}).get("output_files") or {})
    wanted_folder = next(iter(((trip or {}).get("output_folders") or {}).values()), None)
    import httpx

    for item in files:
        if not isinstance(item, dict):
            continue
        token = str(item.get("artifact_token") or "")
        kind = str(item.get("format") or "").lower()
        param = item.get("param")
        item["on_this_computer"] = False
        if not bearer or not _TOKEN_SHAPE.match(token) or kind not in _FILE_SUFFIX:
            continue
        try:
            resp = httpx.get(
                f"{_api_base()}/api/artifact/{token}",
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=_FILE_FETCH_TIMEOUT_S,
            )
            if resp.status_code != 200 or not resp.content:
                continue
            data = resp.content
            if kind == "gcode" and data.startswith(_GZIP_MAGIC):
                data = _inflated(data, MAX_FILE_BYTES)
            if data is None or len(data) > MAX_FILE_BYTES:
                continue
            name = _plain_name(item.get("filename"), kind)
            if isinstance(param, str) and wanted_files.get(param):
                path = Path(wanted_files[param]).expanduser()
            elif wanted_folder:
                path = Path(wanted_folder).expanduser() / name
            else:
                path = files_dir() / f"{Path(name).stem}-{token[:6]}{_FILE_SUFFIX[kind]}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception:  # noqa: BLE001 — one file must not cost the answer
            logger.debug("served makes: file not fetched", exc_info=True)
            continue
        slot = _slot(answer, item.get("at"))
        if slot is not None:
            block, key = slot
            theirs = block[key]
            block[key] = str(path)
            # The servers name a file once, however often the answer
            # repeats its path (the file to upload is also "the resume
            # file"); every repeat is this same file.
            if isinstance(theirs, str) and theirs:
                _replace_everywhere(answer, theirs, str(path))
        elif isinstance(param, str) and param not in answer:
            # The tool wrote it and did not say where: the answer does now.
            answer[param] = str(path)
        item["path"] = str(path)
        item["on_this_computer"] = True
        item.pop("artifact_token", None)


# ---------------------------------------------------------------------------
# Arrival
# ---------------------------------------------------------------------------


def _is_elsewhere(value: Any) -> bool:
    """A string that names a file by absolute path, where no such file is on
    this computer: a path on the server that made it."""
    if not isinstance(value, str) or len(value) > 4096:
        return False
    if not value.startswith("/") or "\n" in value:
        return False
    path = Path(value)
    # A file by its suffix, or anything in a scratch folder (a tool's
    # working directory has none).
    scratch = value.startswith(("/tmp/", "/var/tmp/", "/private/tmp/"))
    return (bool(path.suffix) or scratch) and not path.exists()


#: Blocks this module writes or reads by its own rules.
_OWN_BLOCKS = frozenset({
    "artifact", "documents", "files", "made_on_kiln_servers", "files_on_kiln_servers",
})


def _scrub_nested(value: Any, depth: int = 0) -> None:
    """Below the top level of an answer, take out every path that is not on
    this computer: a key holding one goes, and one in a list is left as its
    file name.  The top level is the caller's to word (what was removed is
    said there); deeper, a server's path is only ever a dead end."""
    if depth > 6:
        return
    if isinstance(value, dict):
        for key in [k for k, item in value.items() if _is_elsewhere(item)]:
            del value[key]
        for item in value.values():
            _scrub_nested(item, depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if _is_elsewhere(item):
                value[index] = Path(item).name
            else:
                _scrub_nested(item, depth + 1)


def _scrub_below_the_top(answer: dict) -> None:
    for key, item in answer.items():
        if key not in _OWN_BLOCKS and isinstance(item, (dict, list)):
            _scrub_nested(item)


def _made_a_model(answer: dict) -> bool:
    """Whether the answer names a model the tool MADE, at a path that is not
    on this computer.  A tool that only read a model echoes the one it was
    handed (``source_path``); that is its input, and no make went missing.
    Told apart by the rule the stage uses to pick what to show."""
    from kiln.stage_link import _key_rank, _looks_like_mesh_key

    return any(
        _is_elsewhere(value)
        and Path(value).suffix.lower() in _SENDABLE_MODEL_TYPES
        and _looks_like_mesh_key(key)
        and _key_rank(key) is not None
        for key, value in answer.items()
    )


def _without_server_files(answer: dict) -> dict:
    """An answer that made no model but names files (a drawing, a manual):
    they are on Kiln's servers, and nothing brings them to this computer
    yet.  The paths go, and the answer says so, rather than handing an
    agent a path that opens nothing."""
    from kiln.stage_link import _key_rank

    removed = sorted(key for key, value in answer.items() if _is_elsewhere(value))
    if not removed:
        return answer
    names = {key: Path(answer.pop(key)).name for key in removed}
    # The servers' copy of what the caller sent, and a tool's working
    # folder, are nobody's files to want: they go without comment.
    names = {
        key: name
        for key, name in names.items()
        if _key_rank(key) is not None and Path(name).suffix
    }
    if not names:
        return answer
    answer["files_on_kiln_servers"] = {
        "on_this_computer": False,
        "files": names,
        "note": (
            "These files were made on Kiln's servers and cannot be saved to "
            "this computer from here yet. Everything else in this answer is "
            "complete. Tell the user plainly; do not offer a path to them."
        ),
    }
    return answer


def arrive(
    tool: str, answer: Any, *, allowance: dict | None = None, trip: dict | None = None,
) -> Any:
    """*answer* from a served tool, made true for this computer.

    *trip* is what :func:`send_inputs` noted on the way out: where the call
    asked for its outputs.

    An answer that made nothing (no ``artifact`` block, or an error) comes
    back untouched.  One that did has its server-side paths removed, gains a
    ``made_on_kiln_servers`` block saying where the make is and what to do
    next, and has its look fetched for the stage.  Never raises.
    """
    try:
        if not isinstance(answer, dict):
            return answer
        if answer.get("success") is False or answer.get("status") == "error":
            return answer
        # Documents first: once saved here their paths are real, and the
        # sweep for server-only paths below leaves them alone.
        _bring_documents(answer)
        _bring_files(answer, trip)
        _scrub_below_the_top(answer)
        artifact = answer.get("artifact")
        token = (
            str(artifact.get("artifact_token") or "").strip()
            if isinstance(artifact, dict)
            else ""
        )
        if not _TOKEN_SHAPE.match(token):
            hand_over = answer.get("hand_over")
            said_not_handed_over = (
                isinstance(hand_over, dict) and hand_over.get("available") is False
            )
            if said_not_handed_over or _made_a_model(answer):
                # The servers built a model and handed over no token for
                # it, so there is nothing to show and nothing to keep.
                # Seen live 2026-10-03, now and then.  A "success" nobody
                # can use is a failure the caller should hear as one.
                return {
                    "success": False,
                    "status": "error",
                    "code": "MAKE_NOT_HANDED_OVER",
                    "tool": tool,
                    "retryable": True,
                    "error": (
                        f"Kiln's servers built this, but did not hand it over, "
                        f"so there is nothing to show or keep. Nothing was "
                        f"charged. Call {tool} again with the same settings."
                    ),
                }
            return _without_server_files(answer)
        kind = str(artifact.get("format") or "stl").lower()

        removed = sorted(key for key, value in answer.items() if _is_elsewhere(value))
        for key in removed:
            del answer[key]

        look = _fetch_look(token, kind)
        _remember(token, tool=tool, kind=kind, arrival=look)

        from kiln.tiers_and_terms import free_allowance_phrase

        phrase = free_allowance_phrase(allowance) if allowance else ""
        cost = (
            f"On the Free plan a keep counts toward the monthly allowance ({phrase}); "
            "looking and changing it are free."
            if phrase
            else "Keeping it costs nothing extra."
        )
        answer["made_on_kiln_servers"] = {
            "artifact_token": token,
            "on_this_computer": False,
            "what_to_do": (
                "This make is on Kiln's servers, not on this computer. Show it "
                "to the user on the stage. To slice, print or export it, call "
                f'keep_design(artifact_token="{token}") first: that saves the '
                "file here and returns its path. To change it with another "
                "Kiln tool (a texture, a decoration), pass this artifact_token "
                "where that tool takes the model's path."
            ),
            "keep": cost,
            "held_for_seconds": artifact.get("expires_in"),
            "server_paths_removed": removed,
        }
    except Exception:  # noqa: BLE001 — never turn a make into an error
        logger.debug("served makes: arrival failed", exc_info=True)
    return answer


# ---------------------------------------------------------------------------
# Keep
# ---------------------------------------------------------------------------


def _kept_name(tool: str, token: str, kind: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", (tool or "make").lower()).strip("-")
    stem = re.sub(r"^(generate|apply|add)-", "", stem) or "make"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{stem}-{stamp}-{token[:6]}{_KIND_SUFFIX.get(kind, '.stl')}"


def keep(artifact_token: str) -> dict[str, Any]:
    """Keep a served make: the servers spend the keep (where one is owed) and
    send the full file, which is saved on this computer.  Never raises."""
    tool = "keep_design"
    token = str(artifact_token or "").strip()
    if not _TOKEN_SHAPE.match(token):
        return {
            "success": False,
            "status": "error",
            "code": "INVALID_INPUT",
            "tool": tool,
            "error": (
                "keep_design takes the artifact_token of a make from Kiln's "
                "servers (it is in that make's answer, under "
                "made_on_kiln_servers)."
            ),
        }
    known = _known(token) or {}
    held = known.get("kept")
    if held and Path(held).is_file():
        return _kept_answer(token, str(held), known, headers={}, again=True)

    bearer = _bearer()
    if not bearer:
        return _signed_out(tool)
    import httpx

    from kiln.served_answer import envelope_for_http, envelope_for_transport

    try:
        resp = httpx.post(
            f"{_api_base()}/api/artifact/{token}/keep",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_UPLOAD_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        return envelope_for_transport(tool, exc, host=_api_base(), kind="made")
    if resp.status_code == 404:
        return {
            "success": False,
            "status": "error",
            "code": "MAKE_NO_LONGER_HELD",
            "tool": tool,
            "error": (
                "That make is no longer on Kiln's servers. A make that is not "
                "kept is held for about half an hour. Make it again, then keep it."
            ),
        }
    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None
        return envelope_for_http(tool, resp.status_code, body, kind="made")

    data = resp.content
    if not data:
        return envelope_for_http(tool, 502, None, kind="made")
    disposition = resp.headers.get("content-disposition") or ""
    match = re.search(r'filename="model\.([a-z0-9]+)"', disposition)
    kind = (match.group(1) if match else known.get("kind") or "stl").lower()
    folder = kept_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _kept_name(str(known.get("tool") or ""), token, kind)
        path.write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "status": "error",
            "code": "KEEP_NOT_SAVED",
            "tool": tool,
            "error": (
                f"The make was kept, but the file could not be saved to {folder}: "
                f"{exc}. Fix that and call keep_design again; a make is only "
                "charged once."
            ),
        }
    _remember(token, kept=str(path), kind=kind)
    return _kept_answer(token, str(path), known, headers=resp.headers, again=False)


def _kept_answer(
    token: str, path: str, known: dict, *, headers: Any, again: bool
) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "success": True,
        "status": "success",
        "kept": True,
        "artifact_token": token,
        "mesh_path": path,
        "message": (
            f"Already kept: the file is at {path}."
            if again
            else f"Kept. The file is saved at {path}, ready to slice, print or export."
        ),
    }
    if known.get("tool"):
        answer["made_by"] = known["tool"]
    used = headers.get("X-Kiln-Keep-Used") if headers else None
    if used is not None:
        with contextlib.suppress(Exception):
            answer["keep"] = {
                "used": int(used),
                "limit": int(headers.get("X-Kiln-Keep-Limit")),
                "remaining": int(headers.get("X-Kiln-Keep-Remaining")),
                "allowance": str(headers.get("X-Kiln-Keep-Bucket") or ""),
            }
    return answer


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


def _refusal(tool: str, code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "status": "error",
        "code": code,
        "tool": tool,
        "error": message,
        "why": "refused",
    }


def _upload(route: str, path: Path, field_data: dict | None, token_key: str) -> tuple[str | None, str]:
    """POST one file; ``(token, "")`` or ``(None, sentence)``."""
    import httpx

    bearer = _bearer()
    if not bearer:
        from kiln.tiers_and_terms import signed_out_message

        return None, signed_out_message()
    try:
        with path.open("rb") as fh:
            resp = httpx.post(
                f"{_api_base()}{route}",
                headers={"Authorization": f"Bearer {bearer}"},
                files={"file": (path.name, fh, "application/octet-stream")},
                data=field_data or None,
                timeout=_UPLOAD_TIMEOUT_S,
            )
    except Exception:  # noqa: BLE001
        logger.debug("served makes: upload failed", exc_info=True)
        return None, "Kiln's servers could not be reached to receive the file."
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {}
    token = str((body or {}).get(token_key) or "")
    if resp.status_code == 200 and token:
        return token, ""
    said = str((body or {}).get("error") or "").strip()
    return None, said or f"Kiln's servers did not accept the file (HTTP {resp.status_code})."


def _as_local_file(value: Any, suffixes: frozenset[str]) -> Path | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        return None
    try:
        path = Path(value).expanduser()
        if path.suffix.lower() in suffixes and path.is_file():
            return path
    except Exception:  # noqa: BLE001
        return None
    return None


def _upload_file(local: Path) -> tuple[str | None, str]:
    """Send one file a tool reads; ``(token, "")`` or ``(None, sentence)``.

    A G-code goes compressed: it is text, and a long print's is tens of
    megabytes.  The servers decide what the file is from its content.
    """
    import httpx

    bearer = _bearer()
    if not bearer:
        from kiln.tiers_and_terms import signed_out_message

        return None, signed_out_message()
    try:
        size = local.stat().st_size
        if local.suffix.lower() in _GCODE_TYPES:
            if size > MAX_GCODE_BYTES:
                return None, (
                    f"{local.name} is over {MAX_GCODE_BYTES // (1024 * 1024)} MB, "
                    "more than Kiln's servers take."
                )
            payload: Any = gzip.compress(local.read_bytes(), compresslevel=6)
            size = len(payload)
        else:
            payload = None
        if size > MAX_UPLOAD_BYTES:
            return None, (
                f"{local.name} is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                "to send, more than Kiln's servers take."
            )
        with contextlib.ExitStack() as stack:
            body = payload if payload is not None else stack.enter_context(local.open("rb"))
            resp = httpx.post(
                f"{_api_base()}/api/tool-inputs",
                headers={"Authorization": f"Bearer {bearer}"},
                files={"file": (local.name, body, "application/octet-stream")},
                timeout=_FILE_UPLOAD_TIMEOUT_S,
            )
    except Exception:  # noqa: BLE001
        logger.debug("served makes: file upload failed", exc_info=True)
        return None, "Kiln's servers could not be reached to receive the file."
    try:
        answer = resp.json()
    except Exception:  # noqa: BLE001
        answer = {}
    token = str((answer or {}).get("file_token") or "")
    if resp.status_code == 200 and token:
        return token, ""
    said = str((answer or {}).get("error") or "").strip()
    return None, said or f"Kiln's servers did not accept the file (HTTP {resp.status_code})."


def _names_a_path(text: str) -> bool:
    """Whether *text* is written like a file's path, whether or not the
    file is there."""
    if len(text) > 4096 or "\n" in text:
        return False
    return (
        text.startswith(("/", "~", "./", "../"))
        or os.sep in text
        or Path(text).suffix.lower() in _SENDABLE_FILE_TYPES
    )


def _beside_an_obj(obj: Path) -> list[Path]:
    """The files an OBJ needs next to it to carry its colours: the material
    libraries it names, and the pictures those name.  Only plain names in
    the OBJ's own folder; an OBJ with none is sent alone."""
    found: list[Path] = []

    def named(path: Path, keys: tuple[str, ...], limit: int) -> list[str]:
        out: list[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh.read(limit).splitlines():
                    head, _, rest = line.strip().partition(" ")
                    if head.lower() in keys and rest.strip():
                        out.append(rest.strip().split()[-1])
        except OSError:
            pass
        return out

    for name in named(obj, ("mtllib",), 256 * 1024):
        library = obj.parent / name
        if Path(name).name != name or not library.is_file() or library in found:
            continue
        found.append(library)
        for picture_name in named(
            library, ("map_kd", "map_ka", "map_d", "map_bump", "bump"), 1024 * 1024,
        ):
            picture = obj.parent / picture_name
            if (
                Path(picture_name).name == picture_name
                and picture.suffix.lower() in _SENDABLE_IMAGE_TYPES
                and picture.is_file()
                and picture not in found
            ):
                found.append(picture)
    return found[:8]


class _Sending:
    """The files one call brings: each uploaded once, and counted."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.tokens: dict[str, str] = {}

    def entry(self, local: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """``({"token", "name"}, None)`` for *local*, or ``(None, refusal)``."""
        key = str(local.resolve())
        token = self.tokens.get(key)
        if token is None:
            if len(self.tokens) >= MAX_FILES_PER_CALL:
                return None, _refusal(
                    self.tool, "TOO_MANY_FILES",
                    f"{self.tool} was handed more than {MAX_FILES_PER_CALL} files, "
                    "more than one call to Kiln's servers carries.",
                )
            token, said = _upload_file(local)
            if token is None:
                return None, _refusal(
                    self.tool, "FILE_NOT_SENT",
                    f"{self.tool} runs on Kiln's servers and needs {local.name} "
                    f"sent there first, which did not work: {said}",
                )
            self.tokens[key] = token
        return {"token": token, "name": local.name}, None

    def one(self, value: Any) -> tuple[Any, dict[str, Any] | None]:
        """What to send for one value of a file parameter: an entry, or the
        value itself when it names no file on this computer."""
        if not isinstance(value, str) or not value.strip():
            return None, None
        text = value.strip()
        if _TOKEN_SHAPE.match(text) and not Path(text).exists():
            # A make still on the servers, named by its token.
            return {"token": text}, None
        local = _as_local_file(text, _SENDABLE_FILE_TYPES)
        if local is None:
            try:
                there = Path(text).expanduser().is_file()
            except Exception:  # noqa: BLE001
                there = False
            if there:
                return None, _refusal(
                    self.tool, "FILE_KIND_NOT_SENT",
                    f"{self.tool} runs on Kiln's servers, which do not take "
                    f"{Path(text).suffix or 'that kind of'} files, so "
                    f"{Path(text).name} was not sent.",
                )
            if _names_a_path(text):
                return None, _refusal(
                    self.tool, "FILE_NOT_FOUND",
                    f"{self.tool} runs on Kiln's servers and needs the file "
                    f"{text!r} sent there, and no such file is on this computer.",
                )
            return None, None
        entry, refusal = self.entry(local)
        if refusal is not None or entry is None:
            return None, refusal
        if local.suffix.lower() == ".obj":
            beside = []
            for companion in _beside_an_obj(local):
                extra, refusal = self.entry(companion)
                if refusal is not None:
                    return None, refusal
                beside.append(extra)
            if beside:
                entry = {**entry, "with": beside}
        return entry, None


def _send_files(
    tool: str, kwargs: dict[str, Any], files: dict[str, str],
) -> tuple[int, dict[str, Any] | None]:
    """Upload every file the call names in a ``files`` parameter and swap
    the parameter for its token(s).  ``(how many were sent, refusal)``."""
    sending = _Sending(tool)
    tokens: dict[str, Any] = {}
    for param, how in files.items():
        value = kwargs.get(param)
        if how == "inline":
            if not isinstance(value, str) or not value.strip():
                continue
            # Words that may name one file: "photo:/Users/me/logo.png".
            prefix = ""
            local = _as_local_file(value.strip(), _SENDABLE_FILE_TYPES)
            if local is None:
                found = _INLINE_PREFIX.match(value)
                if found:
                    prefix = value[: found.end()]
                    local = _as_local_file(value[found.end():].strip(), _SENDABLE_FILE_TYPES)
            if local is None:
                continue
            entry, refusal = sending.entry(local)
            if refusal is not None:
                return len(sending.tokens), refusal
            kwargs[param] = prefix + _INLINE_FILE_MARK
            tokens[param] = entry
            continue
        many = isinstance(value, (list, tuple))
        entries = []
        for item in (value if many else [value]):
            entry, refusal = sending.one(item)
            if refusal is not None:
                return len(sending.tokens), refusal
            if entry is None:
                entries = []
                break
            entries.append(entry)
        if not entries:
            continue
        kwargs.pop(param, None)
        tokens[param] = entries if (many or how == "many") else entries[0]
    if tokens:
        kwargs["file_tokens"] = tokens
    return len(tokens), None


def _set_outputs_aside(
    kwargs: dict[str, Any], outputs: dict[str, str], trip: dict | None,
) -> None:
    """Take the places the call asked a tool to write out of the request,
    and remember them.  They are places on this computer, which the servers
    cannot write to; the files come back and are saved there on arrival."""
    names: dict[str, str] = {}
    for param, how in outputs.items():
        value = kwargs.get(param)
        if not isinstance(value, str) or not value.strip():
            continue
        kwargs.pop(param, None)
        if how == "file":
            names[param] = Path(value).name
            if trip is not None:
                trip.setdefault("output_files", {})[param] = value
        elif trip is not None:
            trip.setdefault("output_folders", {})[param] = value
    if names:
        kwargs["output_names"] = names


def send_inputs(
    tool: str,
    kwargs: dict[str, Any],
    inputs: dict[str, Any] | None,
    trip: dict | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Make the files a call names reachable by the servers.

    Returns ``(kwargs, None)`` to go ahead, or ``(kwargs, refusal)`` when a
    file the call names could not be sent.  The model parameter may name a
    make on the servers (its ``artifact_token``), the kept copy of one, or
    any model on this computer; the image parameter, an image on this
    computer; each ``files`` parameter, a file (or a list of them) on this
    computer or a make's token.  Anything else is left for the servers to
    answer.

    *trip*, when given, is filled with what :func:`arrive` needs on the way
    back: where the call asked for its outputs, and how many files it sent.
    """
    if not inputs:
        return kwargs, None
    kwargs = dict(kwargs)

    mesh_param = inputs.get("mesh")
    value = kwargs.get(mesh_param) if mesh_param else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        token: str | None = None
        if _TOKEN_SHAPE.match(text) and not Path(text).exists():
            token = text
        else:
            local = _as_local_file(text, _SENDABLE_MODEL_TYPES)
            if local is not None:
                token = token_for_path(str(local))
                if token is None:
                    if local.stat().st_size > MAX_MODEL_BYTES:
                        return kwargs, _refusal(
                            tool, "UPLOAD_TOO_LARGE",
                            f"{local.name} is over {MAX_MODEL_BYTES // (1024 * 1024)} MB, "
                            "more than Kiln's servers take. Simplify it first.",
                        )
                    token, said = _upload(
                        "/api/view/mesh", local, {"source": "1"}, "artifact_token"
                    )
                    if token is None:
                        return kwargs, _refusal(
                            tool, "MODEL_NOT_SENT",
                            f"{tool} runs on Kiln's servers and needs {local.name} "
                            f"sent there first, which did not work: {said}",
                        )
        if token:
            kwargs.pop(mesh_param, None)
            kwargs["source_artifact_token"] = token

    image_param = inputs.get("image")
    value = kwargs.get(image_param) if image_param else None
    single = value[0] if isinstance(value, list) and len(value) == 1 else value
    local = _as_local_file(single, _SENDABLE_IMAGE_TYPES)
    if local is not None:
        if local.stat().st_size > MAX_IMAGE_BYTES:
            return kwargs, _refusal(
                tool, "UPLOAD_TOO_LARGE",
                f"{local.name} is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB, "
                "more than Kiln's servers take for an image.",
            )
        token, said = _upload("/api/images/upload", local, None, "image_token")
        if token is None:
            return kwargs, _refusal(
                tool, "IMAGE_NOT_SENT",
                f"{tool} runs on Kiln's servers and needs {local.name} sent "
                f"there first, which did not work: {said}",
            )
        kwargs.pop(image_param, None)
        kwargs["image_token"] = token

    files = inputs.get("files")
    if isinstance(files, dict) and files:
        sent, refusal = _send_files(tool, kwargs, files)
        if refusal is not None:
            return kwargs, refusal
        if trip is not None and sent:
            trip["files_sent"] = sent

    outputs = inputs.get("outputs")
    if isinstance(outputs, dict) and outputs:
        _set_outputs_aside(kwargs, outputs, trip)

    return kwargs, None
