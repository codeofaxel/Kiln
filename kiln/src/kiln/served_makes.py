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

**Send.**  A served tool that works on the caller's own model or image
cannot read this computer's disk.  :func:`send_inputs` uploads the file the
call names and hands the tool a token for it; a make that is still on the
servers is named by its ``artifact_token`` and nothing is uploaded at all.

Which parameter takes a model and which an image comes from the manifest
entry (``inputs``), written by the side that fills them.
"""

from __future__ import annotations

import contextlib
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

_FETCH_TIMEOUT_S = 30.0
_UPLOAD_TIMEOUT_S = 60.0

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
    return bool(path.suffix) and not path.exists()


def arrive(tool: str, answer: Any, *, allowance: dict | None = None) -> Any:
    """*answer* from a served tool, made true for this computer.

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
        artifact = answer.get("artifact")
        if not isinstance(artifact, dict):
            return answer
        token = str(artifact.get("artifact_token") or "").strip()
        if not _TOKEN_SHAPE.match(token):
            return answer
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


def send_inputs(
    tool: str, kwargs: dict[str, Any], inputs: dict[str, str] | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Make the call's model and image reachable by the servers.

    Returns ``(kwargs, None)`` to go ahead, or ``(kwargs, refusal)`` when a
    file the call names could not be sent.  The model parameter may name a
    make on the servers (its ``artifact_token``), the kept copy of one, or
    any model on this computer; the image parameter, an image on this
    computer.  Anything else is left for the servers to answer.
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

    return kwargs, None
