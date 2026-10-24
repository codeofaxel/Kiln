"""Carries this computer's design history to Kiln's servers and back.

The design-history tools (branch, merge, cherry-pick, the history views)
run on Kiln's servers.  On a computer without kiln-pro each one is a stub
that forwards the call; this module is what the stub adds for them:

* before the call, the records of every design the call names
  (:mod:`kiln.history_vault`) ride in the body as ``history_records``;
* after it, the ``history_delta`` the server answers with is written back
  into those records and into the design library, and the answer says the
  history was kept on this computer.

The server keeps none of it.  Which tools take part, and how, is the
``history`` block of each tool's entry in ``pro_tool_manifest.json``:
``served`` for the tools above, or the name of a cloud door this module
runs itself (``push``, ``pull``, ``clone``, ``remote_branches``), against
Kiln's cloud routes with the person's own sign-in.  Pushing is how history
leaves this computer: when the person asks, or after each save when they
turned automatic saving on in their Kiln account (Kiln Pro).

**A design locked to this computer is never saved to the account.**  Every
door that would save one there (a push, an automatic push, a release push,
a pull request) asks :func:`lock_refusal` first and is refused in plain
words.  The served history calls still run: the design passes through Kiln's
servers for the length of the call and is not kept there.

**Two computers, one branch.**  When the account's branch moved on another
computer, a push keeps this computer's versions in the account as a branch
of their own, ``<branch>@<computer>``, and says how to merge it.  Nothing is
ever merged automatically.

A push sends each model once: it asks the account which models it already
keeps (``POST /api/cloud/meshes/stored``), sends the missing ones as their
own bytes, one a request (``PUT /api/cloud/meshes/<sha256>``), and pushes
the history naming each version's model by ``mesh_sha256``.  A server that
does not know the question gets the models inside the push instead.

A pull or a clone brings each model once the same way: it asks for an
answer naming each version's model, fetches each one this computer does not
keep, one a request (``GET /api/cloud/meshes/<sha256>``), checks each against
the fingerprint it asked for, and only then records the history.  A server
that answers with the models inside (one that predates this) is read as
before, and so is the answer asked for again when a model cannot come on
its own.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import shlex
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
#: The most designs one call sends, matching what the server takes.
_MAX_DESIGNS = 4
#: The mesh bytes one call carries, across every design it sends; base64 and
#: the records around them stay under the 24 MiB the server reads for a
#: history call.
_MESH_BUDGET = 16 * 1024 * 1024
_WAIT_S = 120.0
#: The most fingerprints one "which models do you keep?" request names,
#: matching what the server takes.
_MAX_FINGERPRINTS_ASKED = 1000
#: How much of a model file is read at a time while it is fingerprinted.
_HASH_CHUNK = 1024 * 1024
#: The field a pull or clone asks with to have its models named rather than
#: carried, and the value; Kiln's answer carries the same pair when it did.
_MESH_TRANSPORT = "mesh_transport"
_BY_FINGERPRINT = "by_sha256"
#: The longest wait on the API's request rate one model's fetch sits out
#: (seconds), and how many times it waits for the same model.  A longer wait
#: is an allowance for the day: said, never sat out.
_RATE_WAIT_MAX_S = 120
_RATE_WAITS_PER_MODEL = 5

_DESIGN_KEYS = ("design_id",)
_ARTIFACT_KEY = "artifact_id"
_ARTIFACT_TYPE_KEY = "artifact_type"


def _ids(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def _about_designs(arguments: dict[str, Any]) -> bool:
    kind = (arguments or {}).get(_ARTIFACT_TYPE_KEY)
    return kind is None or str(kind).strip().lower() in ("", "design")


def designs_for_call(vault: Any, arguments: dict[str, Any]) -> tuple[list[str], set[str]]:
    """The designs a call names, and the versions it names, by argument name.

    The same rule Kiln's server reads a call by: a ``design_id`` (or an
    ``artifact_id`` of a design), and anything ending in ``branch_id`` /
    ``version_id`` / ``release_id``, looked up in this computer's records.
    """
    designs: list[str] = []
    versions: set[str] = set()
    if not _about_designs(arguments):
        return designs, versions

    def add(design_id: Any) -> None:
        if isinstance(design_id, str) and design_id and design_id not in designs:
            designs.append(design_id)

    for key, value in (arguments or {}).items():
        name = str(key)
        singular = name[:-1] if name.endswith("_ids") else name
        if singular in _DESIGN_KEYS or singular.endswith("_design_id") or singular == _ARTIFACT_KEY:
            for design_id in _ids(value):
                add(design_id)
        elif singular.endswith("branch_id"):
            for branch_id in _ids(value):
                add(vault.design_of_branch(branch_id))
        elif singular.endswith("version_id"):
            for version_id in _ids(value):
                versions.add(version_id)
                add(vault.design_of_version(version_id))
        elif singular == "release_id":
            for release_id in _ids(value):
                add(vault.design_of_release(release_id))
    return designs, versions


#: The most saved features or presets one call sends (a listing sends all).
_MAX_LIBRARY_ITEMS = 200


def library_items_for_call(vault: Any, kind: str, arguments: dict[str, Any], tool: str = "") -> list[str]:
    """The features or presets a call is about, by argument name.

    The item's own id, anything ending in ``version_id`` / ``branch_id``, or a
    ``release_id``, looked up in this computer's records.  A listing that
    names none is about every one this computer keeps.
    """
    from kiln.history_vault import LIBRARY_KINDS

    keys = LIBRARY_KINDS[kind]
    found: list[str] = []

    def add(item_id: Any) -> None:
        if isinstance(item_id, str) and item_id and item_id not in found:
            found.append(item_id)

    for key, value in (arguments or {}).items():
        name = str(key)
        singular = name[:-1] if name.endswith("_ids") else name
        if singular == keys["id"]:
            for item_id in _ids(value):
                add(item_id)
        elif singular.endswith("version_id"):
            for vid in _ids(value):
                add(vault.library_item_of(kind, "version", vid))
        elif singular.endswith("branch_id"):
            for bid in _ids(value):
                add(vault.library_item_of(kind, "branch", bid))
        elif singular == "release_id":
            for rid in _ids(value):
                add(vault.library_item_of(kind, "release", rid))
    if not found and tool.startswith("list_"):
        found = vault.library_items(kind)
    return found[:_MAX_LIBRARY_ITEMS]


def records_for_call(
    arguments: dict[str, Any], vault: Any = None, *, kinds: tuple[str, ...] = ("designs",), tool: str = "",
    file_param: str = "",
) -> dict[str, Any]:
    """The ``history_records`` a served history call sends.

    *kinds* is the manifest's say of which records the tool works on: design
    history (the default), ``features`` or ``presets``.
    """
    from kiln.history_vault import LIBRARY_KINDS, HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "designs": [], "named": []}
        if "designs" in kinds:
            designs, versions = designs_for_call(vault, arguments)
            left = _MESH_BUDGET
            for design_id in designs[:_MAX_DESIGNS]:
                vault.sync_from_library(design_id)
                if vault.has_design(design_id):
                    bundle = vault.bundle(design_id, meshes_for=versions, mesh_budget=left)
                    left -= sum(
                        len(s["recipe_dict"].get("mesh_bytes_b64") or "") * 3 // 4
                        for s in bundle["provenance_sidecars"]
                    )
                    out["designs"].append(bundle)
            out["named"] = designs[:_MAX_DESIGNS]
        for kind in kinds:
            if kind not in LIBRARY_KINDS:
                continue
            budget = [8 * 1024 * 1024]
            items = library_items_for_call(vault, kind, arguments, tool)
            out[kind] = [vault.library_bundle(kind, i, budget=budget) for i in items if vault.has_library_item(kind, i)]
            out[f"named_{kind}"] = items
        if "reflog" in kinds or "bisect" in kinds:
            _add_log_rows(vault, out, arguments, tool)
            if tool in _LOG_SAVES:
                out["reflog"] = [
                    e for e in out["reflog"]
                    if not (e.get("artifact_type") == "design" and vault.is_locked(str(e.get("artifact_id") or "")))
                ]
        if "files" in kinds and file_param:
            out["files"] = files_for_call(tool, arguments.get(file_param))
        if "proven" in kinds:
            out["proven"] = vault.proven_recipes()
        return out
    finally:
        if own:
            vault.close()


#: The most log entries one call sends, matching what the server takes.
_MAX_LOG_ENTRIES = 2000
#: Which library kind a log entry's ``artifact_type`` names.
_LOG_KINDS = {"feature": "features", "preset": "presets"}


def _add_log_rows(vault: Any, out: dict[str, Any], arguments: dict[str, Any], tool: str) -> None:
    """Add the log entries and bisect sessions a history-log or bisect call reads.

    With them goes the one design, feature or preset a head move or a new
    search is about: an undo needs the branch it moves, a search the versions
    it walks.  A listing sends every entry or every open search; a call about
    one entry or one search sends that one.
    """
    artifact: tuple[str, str] | None = None
    reflog: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    reflog_id = str(arguments.get("reflog_id") or "")
    if tool in ("show_reflog", "get_reflog_entry", "push_reflog_to_cloud"):
        reflog = vault.log_entries(limit=_MAX_LOG_ENTRIES)
    elif tool == "restore_from_reflog_entry":
        entry = vault.log_entry(reflog_id) if reflog_id else None
        reflog = [entry] if entry else []
        if entry:
            artifact = (entry["artifact_type"], entry["artifact_id"])
    elif tool == "undo_last_head_change":
        branch_id = str(arguments.get("branch_id") or "")
        reflog = vault.log_entries(branch_id=branch_id, limit=50) if branch_id else []
        if reflog:
            artifact = (reflog[0]["artifact_type"], reflog[0]["artifact_id"])
    elif tool == "bisect_start":
        if isinstance(arguments.get("artifact_type"), str) and isinstance(arguments.get("artifact_id"), str):
            artifact = (arguments["artifact_type"], arguments["artifact_id"])
    elif tool == "list_bisect_sessions":
        sessions = vault.bisect_sessions(active_only=True)
    else:   # a call about one search
        session = vault.bisect_session(str(arguments.get("session_id") or ""))
        sessions = [session] if session else []
    out["reflog"] = reflog
    out["bisect"] = sessions
    if artifact is None:
        return
    kind, item_id = artifact
    if kind == "design":
        vault.sync_from_library(item_id)
        if vault.has_design(item_id) and not any(b.get("design_id") == item_id for b in out["designs"]):
            out["designs"].append(vault.bundle(item_id, meshes_for=set(), mesh_budget=_MESH_BUDGET))
        out["named"] = list(dict.fromkeys([*out.get("named", []), item_id]))
    elif kind in _LOG_KINDS:
        library = _LOG_KINDS[kind]
        if vault.has_library_item(library, item_id):
            out[library] = [vault.library_bundle(library, item_id, budget=[8 * 1024 * 1024])]
        out[f"named_{library}"] = [item_id]


#: The most recipe files one call sends (a start and fifty ancestors), and the
#: largest one read.
_MAX_FILES = 51
_MAX_FILE_BYTES = 256 * 1024
#: The model files whose recipe sits beside them in their folder.
_MODEL_SUFFIXES = (".stl", ".3mf")


def _is_recipe_name(name: str) -> bool:
    """A recipe (``.kiln_recipe.json``, ``.kiln_recipe.v3.json``) or a sidecar (``part.kiln.json``)."""
    return (
        name == ".kiln_recipe.json"
        or (name.startswith(".kiln_recipe.") and name.endswith(".json"))
        or (name.endswith(".kiln.json") and len(name) > len(".kiln.json"))
    )


def _json_file(path: Path) -> dict[str, Any] | None:
    """A recipe or sidecar file's record, or ``None``.

    Only a regular file named as one (never a link, never any other file):
    a pointer inside a recipe is the recipe's say, and what it names leaves
    this computer with the call.
    """
    try:
        if not _is_recipe_name(path.name) or path.is_symlink():
            return None
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def files_for_call(tool: str, value: Any) -> dict[str, Any]:
    """This computer's own recipe files a provenance reader opens, to send with it.

    The ancestry reader opens a recipe and every ``parent_version`` it names;
    the sidecar reader, the recipe in a model's folder and the ``.kiln.json``
    beside it, or the recipe it is given.  Kiln's servers open nothing of
    this computer's: they read copies of these.
    """
    arg = value if isinstance(value, str) else ""
    items: list[dict[str, Any]] = []
    if not arg:
        return {"arg": arg, "items": items}
    start = Path(arg).expanduser()
    if tool == "read_design_sidecar":
        candidates = (
            [start.parent / ".kiln_recipe.json", start.with_suffix(".kiln.json")]
            if start.suffix.lower() in _MODEL_SUFFIXES else [start]
        )
        for path in candidates:
            record = _json_file(path)
            if record is not None:
                items.append({"path": str(path), "record": record})
        return {"arg": arg, "items": items}
    seen: set[str] = set()
    current: Any = str(start)
    while isinstance(current, str) and current and current not in seen and len(items) < _MAX_FILES:
        seen.add(current)
        record = _json_file(Path(current))
        if record is None:
            break
        items.append({"path": current, "record": record})
        current = record.get("parent_version")
    return {"arg": str(start), "items": items}


#: How an answer names a file this computer sent (``kiln-file:<n>``, or
#: ``kiln-file:arg`` for the one the call named).
_FILE_MARK = "kiln-file:"


def _own_paths(value: Any, files: dict[str, Any]) -> Any:
    """*value* with each sent file named by this computer's own path again."""
    if isinstance(value, dict):
        return {k: _own_paths(v, files) for k, v in value.items()}
    if isinstance(value, list):
        return [_own_paths(v, files) for v in value]
    if isinstance(value, str) and value.startswith(_FILE_MARK):
        key = value[len(_FILE_MARK):]
        if key == "arg":
            return files.get("arg") or value
        items = files.get("items") or []
        if key.isdigit() and int(key) < len(items):
            return items[int(key)]["path"]
    return value


#: The tools whose new branch this computer's saves continue, as
#: ``create_design_branch`` describes ("Subsequent saves on this design flow
#: to the new branch").
_CHECKS_OUT = frozenset({"create_design_branch"})


def take_delta(
    answer: Any, sent: dict[str, Any] | None = None, vault: Any = None, *, tool: str = "",
) -> Any:
    """Write a served call's ``history_delta`` into this computer's records.

    Only the designs the call was about (*sent*, what :func:`records_for_call`
    returned) are written: an answer naming any other design changes nothing
    here.  A branch made by *tool* ``create_design_branch`` is checked out, so
    the next version saved here continues it.

    Each branch the answer moved or made is then saved to the person's Kiln
    account when they turned automatic saving on (:func:`_after_change`),
    through the same :func:`push` as ``kiln cloud push``; the answer says
    what became of it under ``auto_push``.
    """
    if isinstance(answer, dict) and isinstance((sent or {}).get("files"), dict):
        answer = _own_paths(answer, sent["files"])
    if not isinstance(answer, dict) or not isinstance(answer.get("history_delta"), dict):
        return answer
    from kiln.history_vault import HistoryVault

    delta = answer.pop("history_delta")
    sent = sent or {}
    about = set(sent.get("named") or []) | {
        b.get("design_id") for b in sent.get("designs") or [] if isinstance(b, dict)
    }
    deltas = [
        d for d in delta.get("designs") or []
        if isinstance(d, dict) and d.get("design_id") in about
    ]
    own = vault is None
    vault = vault or HistoryVault()
    after: dict[str, Any] = {}
    try:
        heads_before = {
            str(d["design_id"]): _heads(vault, str(d["design_id"])) for d in deltas if d.get("design_id")
        }
        summaries = [vault.apply_delta(d) for d in deltas]
        library = _take_library_deltas(vault, delta, sent)
        log = vault.apply_log_delta(delta)
        # A recipe is filed only beside a design this call was about: one it
        # named, or one whose recipes it sent.  An answer naming any other
        # design's recipe file changes nothing here.
        recipes_of = about | {
            r.get("design_id") for r in sent.get("proven") or [] if isinstance(r, dict)
        }
        proven = vault.keep_proven_recipes([
            r for r in delta.get("proven") or []
            if isinstance(r, dict) and r.get("design_id") in recipes_of
        ])
        if proven:
            log = {**log, "proven_recipes": proven}
        continues = None
        made = answer.get("branch") if isinstance(answer.get("branch"), dict) else {}
        if tool in _CHECKS_OUT and made.get("branch_id") and summaries:
            branch = vault.check_out(str(made.get("design_id") or summaries[0]["design_id"]), made["branch_id"])
            continues = branch.get("name") if branch else None
        changed, made_one = _changed_branches(vault, heads_before)
        try:
            after = _after_change(changed, offer_due=made_one, vault=vault)
        except Exception:  # noqa: BLE001 -- saving to the account never undoes what was kept here
            logger.warning("history: the automatic push after %s failed", tool or "a call", exc_info=True)
    finally:
        if own:
            vault.close()
    if summaries or library or any(log.values()):
        kept: dict[str, Any] = {"where": "on this computer"}
        if summaries:
            kept["designs"] = [s["design_id"] for s in summaries]
        for key, count in log.items():
            if count:
                kept[key] = count
        for kind in ("features", "presets"):
            items = [s["item_id"] for s in library if s["kind"] == kind]
            if items:
                kept[kind] = items
        changed_here = [b for s in summaries + library for b in s["branches_changed_here"]]
        if changed_here:
            kept["not_moved"] = changed_here
            kept["note"] = (
                "A branch changed on this computer while Kiln's servers were "
                "working, so that branch was left as it is here. Run the call "
                "again to work from it as it is now."
            )
        if continues:
            kept["saves_continue"] = continues
        answer["history_kept"] = kept
    answer.update(after)
    return answer


def _heads(vault: Any, design_id: str) -> dict[str, Any]:
    return {b["branch_id"]: b.get("head_version_id") for b in vault.branches(design_id)}


def _changed_branches(vault: Any, heads_before: dict[str, dict[str, Any]]) -> tuple[dict[str, list[str]], bool]:
    """Each design's branches whose head moved since *heads_before*, and
    whether one of them is new."""
    changed: dict[str, list[str]] = {}
    made = False
    for design_id, before in heads_before.items():
        for b in vault.branches(design_id):
            if b.get("status") == "retired" or not b.get("head_version_id"):
                continue
            new = b["branch_id"] not in before
            made = made or new
            if new or before[b["branch_id"]] != b["head_version_id"]:
                changed.setdefault(design_id, []).append(b["branch_id"])
    return changed, made


def _take_library_deltas(vault: Any, delta: dict[str, Any], sent: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep each feature or preset a call changed: one it named, or a new one.

    An item this computer already keeps and the call did not name is left as
    it is, whatever the answer says about it.
    """
    from kiln.history_vault import LIBRARY_KINDS

    out = []
    for kind, keys in LIBRARY_KINDS.items():
        named = set(sent.get(f"named_{kind}") or [])
        for d in delta.get(kind) or []:
            if not isinstance(d, dict):
                continue
            item_id = d.get(keys["id"])
            if not isinstance(item_id, str) or not item_id:
                continue
            if item_id not in named and vault.has_library_item(kind, item_id):
                continue
            summary = vault.apply_library_delta(kind, d)
            if summary is not None:
                out.append(summary)
    return out


def call_account(tool: str, **kwargs: Any) -> dict[str, Any]:
    """Call a tool that works on the person's Kiln account alone.

    Pull requests live in the account, shared with the design's team, so a
    review, a listing or a read sends nothing of this computer's history.
    """
    from kiln.server import _pro_api_call

    return _pro_api_call(tool, **kwargs)


def call_served(tool: str, *, kinds: tuple[str, ...] = ("designs",), **kwargs: Any) -> dict[str, Any]:
    """Call a served history tool with this computer's records, and keep the change.

    The command line's way in; the MCP stubs do the same around their own
    transport (``kiln.server._register_pro_tool_stubs``).
    """
    from kiln.server import _pro_api_call

    records = records_for_call(kwargs, kinds=kinds, tool=tool)
    refused = served_refusal(tool, records)
    if refused is not None:
        return refused
    return take_delta(_pro_api_call(tool, **kwargs, history_records=records), records, tool=tool)


# ---------------------------------------------------------------------------
# The cloud doors this computer runs itself
# ---------------------------------------------------------------------------


def _request(
    tool: str, method: str, path: str, body: dict[str, Any] | None = None, *,
    timeout: float | None = None, content: bytes | None = None, raw: bool = False,
) -> dict[str, Any]:
    """Call one of Kiln's cloud routes as the signed-in person.

    *body* goes as JSON; *content*, when given, goes instead as the file's
    own bytes (``application/octet-stream``).  With *raw*, a success comes
    back as ``{"content": <the answer's own bytes>}``.
    """
    import httpx

    from kiln.auth_session import _api_base, resolve_api_bearer
    from kiln.served_answer import envelope_for_http, envelope_for_transport

    resolved = resolve_api_bearer()
    bearer = resolved.token
    if not bearer:
        from kiln.tiers_and_terms import signed_out_message, signin_hint_fields

        if resolved.state == "needs_signin":
            # A session that can no longer be renewed: a returning owner,
            # told to sign back in, never offered an account.
            return {
                "status": "error",
                "success": False,
                "error": str(resolved.detail or "") or "Your Kiln session has expired. Sign back in to continue.",
                "code": "KILN_SESSION_EXPIRED",
                "tool": tool,
                "why": "signed_out",
                **signin_hint_fields(),
            }
        from kiln.signin_offer import FIELD, history_offer

        answer = {
            "status": "error",
            "success": False,
            "error": signed_out_message(),
            "code": "KILN_ACCOUNT_NOT_PAIRED",
            "tool": tool,
            "why": "signed_out",
            **signin_hint_fields(),
        }
        block = history_offer(tool)
        if block is not None:
            answer["error"] = f"{block['message']} {answer['error']}"
            answer[FIELD] = block
        return answer
    try:
        from kiln.version_check import _current_version

        version = _current_version()
    except Exception:  # noqa: BLE001 — a header is never worth failing over
        version = ""
    headers = {"Authorization": f"Bearer {bearer}", "X-Kiln-Client-Version": version}
    sent: dict[str, Any] = {"json": body}
    if content is not None:
        sent = {"content": content}
        headers["Content-Type"] = "application/octet-stream"
    try:
        resp = httpx.request(
            method, f"{_api_base()}{path}", headers=headers, timeout=timeout or _WAIT_S, **sent,
        )
    except Exception as exc:  # noqa: BLE001
        return envelope_for_transport(tool, exc, host=_api_base(), kind="sync")
    if raw and resp.status_code < 400:
        return {"content": resp.content}
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    if resp.status_code >= 400:
        # A route's own refusal can be as bare as {"error": "..."}: it is an
        # error whatever its keys, or "not found" reads as an empty success.
        answer = dict(envelope_for_http(tool, resp.status_code, data, kind="sync"))
        answer.setdefault("status", "error")
        answer["success"] = False
        answer.setdefault("code", "NOT_FOUND" if resp.status_code == 404 else "KILN_API_HTTP_ERROR")
        answer.setdefault("tool", tool)
        answer.setdefault("http_status", resp.status_code)
        if resp.status_code == 429 and str(resp.headers.get("Retry-After") or "").isdigit():
            answer.setdefault("retry_after", int(resp.headers["Retry-After"]))
        return answer
    if isinstance(data, list):
        return {"items": data}
    return data if isinstance(data, dict) else {}


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _refused(tool: str, code: str, error: str) -> dict[str, Any]:
    return {"status": "error", "success": False, "code": code, "error": error, "tool": tool}


@contextlib.contextmanager
def _opened(vault: Any = None) -> Iterator[Any]:
    """*vault*, or this computer's records opened for the block and closed after."""
    if vault is not None:
        yield vault
        return
    from kiln.history_vault import HistoryVault

    own = HistoryVault()
    try:
        yield own
    finally:
        own.close()


def _shell(value: str) -> str:
    return shlex.quote(str(value))


# ---------------------------------------------------------------------------
# A design locked to this computer
# ---------------------------------------------------------------------------

LOCKED_CODE = "DESIGN_LOCKED"


def _design_name(design_id: Any) -> str | None:
    """The name of the design folder *design_id* resolves to, or ``None``.

    A push sends *design_id* to the account as the design's name, so it must
    BE that name: ``./mug`` reaches the folder ``mug`` and would be a second
    design there, and the lock judges the folder, not the spelling.
    """
    from kiln.history_vault import _design_folder

    folder = _design_folder(design_id)
    return None if folder is None else folder.name


def lock_refusal(
    tool: str, design_ids: Any, *, vault: Any = None, doing: str = "save it to your Kiln account",
) -> dict[str, Any] | None:
    """The refusal for saving any of *design_ids* to the person's Kiln account
    when one is locked to this computer, or ``None``.

    The one check every door that saves a design to the account makes.
    """
    ids = [d for d in dict.fromkeys(_ids(design_ids)) if d]
    with _opened(vault) as v:
        locked = [d for d in ids if v.is_locked(d)]
    if not locked:
        return None
    design_id = locked[0]
    return {
        **_refused(
            tool, LOCKED_CODE,
            f"{design_id} is locked to this computer, so Kiln did not {doing}. It is kept on "
            f"this computer; to change that, unlock it first with `kiln design unlock {_shell(design_id)}`.",
        ),
        "design_id": design_id,
        "locked_designs": locked,
        "unlock": {
            "command": f"kiln design unlock {_shell(design_id)}",
            "tool": "lock_design",
            "arguments": {"design_id": design_id, "locked": False},
        },
    }


def locked_design_label(design_id: Any, kind: str = "features", *, vault: Any = None) -> str | None:
    """What to say when a feature or preset is saved for a design locked here, or ``None``.

    A feature or preset carries its own files, never the design's; the one
    thing of the design it would send is the design's name, as a label.  A
    locked design's name stays on this computer, and the feature or preset
    is still saved.
    """
    if not isinstance(design_id, str) or not design_id:
        return None
    with _opened(vault) as v:
        if not v.is_locked(design_id):
            return None
    noun = "preset" if kind == "presets" else "feature"
    return (
        f"{design_id} is locked to this computer, so its name was left off this {noun}. "
        f"The {noun} itself was saved to your Kiln account."
    )


def without_locked_designs(
    design_id: Any, payload: dict[str, Any], kind: str = "features", *, vault: Any = None,
) -> tuple[Any, dict[str, Any], str | None]:
    """A feature or preset push with every design locked here taken out of it.

    The label (*design_id*) and the ``design_id`` on the branch and each
    version record are all names of a design; a locked one stays on this
    computer.  Returns ``(design_id, payload, said)``: the label or ``None``,
    the payload with locked names cleared, and the sentence for the answer
    (``None`` when nothing was locked).
    """
    rows = [payload.get("branch")] + list(payload.get("versions") or [])
    names = [design_id] + [r.get("design_id") for r in rows if isinstance(r, dict)]
    locked = []
    for name in dict.fromkeys(n for n in names if isinstance(n, str) and n):
        if locked_design_label(name, kind, vault=vault):
            locked.append(name)
    if not locked:
        return design_id, payload, None

    def _clear(row: Any) -> Any:
        if isinstance(row, dict) and row.get("design_id") in locked:
            return {**row, "design_id": None}
        return row

    cleared = {
        **payload,
        "branch": _clear(payload.get("branch")),
        "versions": [_clear(v) for v in payload.get("versions") or []],
    }
    return (None if design_id in locked else design_id), cleared, locked_design_label(locked[0], kind, vault=vault)


#: Served tools whose call keeps something of a design in the person's Kiln
#: account, and what the refusal says Kiln did not do.
_KEEPS_IN_ACCOUNT = {"propose_design_change": "open a pull request for it in your Kiln account"}
#: Served tools that save this computer's history log to the account: a
#: locked design's entries stay here.
_LOG_SAVES = frozenset({"push_reflog_to_cloud"})


def served_refusal(tool: str, sent: dict[str, Any] | None, *, vault: Any = None) -> dict[str, Any] | None:
    """The refusal for a served call that would keep a locked design in the account."""
    if tool not in _KEEPS_IN_ACCOUNT:
        return None
    return lock_refusal(tool, (sent or {}).get("named") or [], vault=vault, doing=_KEEPS_IN_ACCOUNT[tool])


def set_lock(design_id: str, locked: bool = True, *, vault: Any = None) -> dict[str, Any]:
    """Lock *design_id* to this computer, or unlock it, and say what that means."""
    from kiln.history_vault import _design_folder

    tool = "lock_design"
    if _design_folder(design_id) is None:
        return _refused(tool, "INVALID_INPUT", f"design_id must be a simple name, not a path: {design_id!r}")
    with _opened(vault) as v:
        now = v.set_locked(design_id, bool(locked))
    if now:
        said = (
            f"{design_id} is locked to this computer: Kiln will not save it to your Kiln account "
            "(no push, automatic save, release push or pull request) until you unlock it. "
            "Branching, merging and the history views still work; when Kiln's servers run one of "
            "those, the design passes through them for that call only and is not kept there. "
            "Anything of it already in your Kiln account stays there."
        )
    else:
        said = (
            f"{design_id} is unlocked: Kiln saves it to your Kiln account when you push it, "
            "or after each save when automatic saving is on."
        )
    return {"status": "success", "ok": True, "design_id": design_id, "locked": now, "said": said}


#: The most proven recipes one push sends (Kiln's servers keep no more per push).
_MAX_RECIPES_SENT = 500


def _send_recipes(tool: str, design_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Send *design_id*'s proven recipes to the account; what became of them."""
    sent = rows[:_MAX_RECIPES_SENT]
    answer = _request(tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/proven/push", {"recipes": sent})
    if answer.get("status") == "error":
        return {
            "sent": 0,
            "error": answer.get("error") or "Kiln's servers did not keep them.",
            "code": answer.get("code"),
            "note": "The branch is in your Kiln account; its proven recipes are not, so run the push again later.",
        }
    out: dict[str, Any] = {"sent": len(sent), "kept": answer.get("kept", 0)}
    if len(rows) > len(sent):
        out["left_out"] = len(rows) - len(sent)
    return out


def _bring_recipes(tool: str, design_id: str, vault: Any) -> dict[str, Any]:
    """Bring the account's proven recipes for *design_id* beside it here."""
    answer = _request(tool, "GET", f"/api/cloud/designs/{_quote(design_id)}/proven")
    if answer.get("status") == "error":
        return {
            "error": answer.get("error") or "Kiln's servers did not answer.",
            "code": answer.get("code"),
            "note": "The design is here; its proven recipes are not, so pull again later.",
        }
    rows = answer.get("recipes") if isinstance(answer.get("recipes"), list) else []
    return {"in_account": len(rows), "files_changed": vault.merge_proven_rows(design_id, rows)}


def push(tool: str, design_id: str, branch_id: str | None = None, *, make_room_for: str | None = None, vault: Any = None) -> dict[str, Any]:
    """Send one branch of this computer's history, and the design's proven
    recipes, to the person's Kiln account.

    Refused here, sending nothing, when the design is locked to this
    computer.  When the account's branch moved on another computer, this
    computer's versions are kept there as a branch of their own instead
    (:func:`_keep_as_this_computers`).  *make_room_for* is the person's
    answer when their plan's designs are full (``FREE_SLOT_IN_USE``): the
    design in their account to archive so this one fits.  Kiln's servers
    archive it, never delete it, and only when this push is kept.
    """
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        if _design_name(design_id) != design_id:
            return _refused(
                tool, "INVALID_INPUT",
                f"design_id must be the design's own name, as `kiln design` lists it: {design_id!r}",
            )
        locked = lock_refusal(tool, [design_id], vault=vault)
        if locked is not None:
            return locked
        recipes = vault.proven_rows(design_id)
        vault.sync_from_library(design_id)
        branch = vault.branch(branch_id) if branch_id else vault.branch_named(design_id, "main")
        if branch is None or branch.get("design_id") != design_id:
            return _refused(tool, "BRANCH_NOT_FOUND", f"No branch {branch_id or 'main'} of {design_id} on this computer.")
        members = vault.memberships(branch["branch_id"])
        versions, sidecars = [], []
        meshes: dict[str, Path] = {}
        for m in members:
            v = vault.version(m["version_id"])
            if v is None:
                continue
            recipe = dict(v["recipe"])
            mesh = vault.mesh_path(v.get("mesh_sha256"))
            if mesh is not None:
                meshes[v["version_id"]] = mesh
                recipe["stl_path"] = mesh.name
            versions.append({
                "version_id": v["version_id"],
                "parent_version_id": v["parent_version_id"],
                "created_at": v["created_at"],
            })
            sidecars.append({"version_id": v["version_id"], "recipe_dict": recipe})
    finally:
        if own:
            vault.close()
    from kiln.history_vault import _without_local_paths

    body = {
        "branch": branch,
        "versions": versions,
        "memberships": members,
        "provenance_sidecars": _without_local_paths(sidecars),
    }
    if make_room_for:
        body["make_room_for"] = make_room_for
    refused = _send_models(tool, body, meshes)
    if refused is not None:
        return refused
    answer = _request(tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/branches/push", body)
    if _diverged(answer):
        out = _keep_as_this_computers(tool, design_id, branch, body, answer, vault=None if own else vault)
        if out.get("status") == "error":
            return out
    elif answer.get("status") == "error":
        return answer
    else:
        out = {"status": "success", "push": answer, "design_id": design_id, "branch_id": branch["branch_id"]}
        made_room = answer.get("made_room") if isinstance(answer, dict) else None
        if isinstance(made_room, dict) and made_room.get("note"):
            # Which of their designs was archived to make room, said at the
            # top of the answer so an agent tells the person what moved.
            out["made_room"] = made_room
    if recipes:
        out["proven_recipes"] = _send_recipes(tool, design_id, recipes)
    return out


def _diverged(answer: dict[str, Any]) -> bool:
    """Whether a push was refused because the account's branch moved on."""
    if answer.get("status") != "error":
        return False
    return answer.get("code") == "BRANCH_DIVERGED" or (
        answer.get("http_status") == 409 and answer.get("error") == "branch diverged"
    )


def _plan_refusal(answer: dict[str, Any]) -> bool:
    """Whether a refusal is the person's plan not including what was asked."""
    code = str(answer.get("code") or "")
    return (
        answer.get("http_status") == 402 or code == "PRO_REQUIRED" or code.startswith("FREE_")
        or bool(answer.get("required_tier"))
    )


def _own_branch_id(branch_id: str, computer: str) -> str:
    """The id of this computer's own copy of *branch_id*: the same every push."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"kiln-history-computer:{branch_id}:{computer}").hex


def _keep_as_this_computers(
    tool: str, design_id: str, branch: dict[str, Any], body: dict[str, Any], refused: dict[str, Any],
    *, vault: Any = None,
) -> dict[str, Any]:
    """Keep this computer's versions of a branch the account moved on, as
    ``<branch>@<computer>``: a branch of their own, the moved one untouched.

    A refusal of the plan's (a design the plan does not keep) leaves the
    server's own answer (*refused*) as it was; nothing is recorded here
    unless the account kept the branch.  Never merges, and never makes room:
    this design is already in the account.
    """
    with _opened(vault) as v:
        computer = v.computer_name()
        name = str(branch.get("name") or "main")
        if name.endswith(f"@{computer}"):
            return refused
        own_id = _own_branch_id(branch["branch_id"], computer)
        own_name = f"{name}@{computer}"
        before = v.branch(own_id) or {}
    own = {
        **branch,
        "branch_id": own_id,
        "name": own_name,
        "parent_branch_id": branch["branch_id"],
        "created_at": before.get("created_at") or time.time(),
        "status": "active",
    }
    members = [{**m, "branch_id": own_id} for m in body.get("memberships") or []]
    sent = {k: val for k, val in body.items() if k != "make_room_for"}
    answer = _request(
        tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/branches/push",
        {**sent, "branch": own, "memberships": members},
    )
    if answer.get("status") == "error":
        if _plan_refusal(answer):
            return refused
        return {**refused, "kept_as_refused": {"code": answer.get("code"), "error": answer.get("error")}}
    with _opened(vault) as v:
        v.keep_branch_copy(own, members)
    pull_cmd = f"kiln cloud pull {_shell(design_id)}" + ("" if name == "main" else f" --branch {_shell(name)}")
    check = f"kiln merge check {_shell(design_id)} {_shell(own_name)} {_shell(name)}"
    run = f"kiln merge run {_shell(design_id)} {_shell(own_name)} {_shell(name)}"
    return {
        "status": "success",
        "push": answer,
        "design_id": design_id,
        "branch_id": own_id,
        "kept_as": own_name,
        "diverged": {
            "branch": name,
            "branch_id": branch["branch_id"],
            "account_head": refused.get("remote_head"),
        },
        "merge": {"pull": pull_cmd, "check": check, "run": run},
        "said": (
            f"The {name} branch of {design_id} in your Kiln account moved on another computer, so "
            f"Kiln kept this computer's work there as {own_name} and replaced nothing. Nothing was "
            f"merged. To bring them together, run `{pull_cmd}`, then `{check}` and `{run}`."
        ),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _carry_models(body: dict[str, Any], meshes: dict[str, Path]) -> None:
    """Put each model inside the push, for a server that predates sending them once."""
    for sidecar in body["provenance_sidecars"]:
        mesh = meshes.get(sidecar["version_id"])
        if mesh is not None:
            sidecar["recipe_dict"]["mesh_bytes_b64"] = base64.b64encode(mesh.read_bytes()).decode("ascii")


def _send_models(tool: str, body: dict[str, Any], meshes: dict[str, Path]) -> dict[str, Any] | None:
    """Put each model a push uses in the person's account once, by fingerprint.

    Asks which of them the account keeps, sends only the missing ones, one a
    request, and names each version's model by ``mesh_sha256`` in *body*;
    Kiln's server refuses a push naming a model the account does not keep.
    A server that does not know the question gets the models inside *body*
    instead, as pushes always carried them.  Returns ``None`` when the push
    may go, or the refusal to answer with when a model was not kept: nothing
    is pushed then.
    """
    shas: dict[str, str] = {}
    for version_id, mesh in meshes.items():
        try:
            shas[version_id] = _file_sha256(mesh)
        except OSError as exc:
            return _refused(
                tool, "MODEL_UNREADABLE",
                f"Kiln could not read the model of version {version_id} on this computer "
                f"({exc}), so nothing was pushed.",
            )
    wanted = sorted(set(shas.values()))
    kept: set[str] = set()
    for start in range(0, len(wanted), _MAX_FINGERPRINTS_ASKED):
        answer = _request(
            tool, "POST", "/api/cloud/meshes/stored",
            {"sha256s": wanted[start:start + _MAX_FINGERPRINTS_ASKED]},
        )
        if answer.get("status") == "error" and answer.get("http_status") not in (404, 405):
            return answer
        stored = answer.get("stored")
        if not isinstance(stored, list):
            _carry_models(body, meshes)
            return None
        kept |= {s for s in stored if isinstance(s, str)}
    for version_id, sha in shas.items():
        if sha in kept:
            continue
        answer = _request(tool, "PUT", f"/api/cloud/meshes/{sha}", content=meshes[version_id].read_bytes())
        if answer.get("status") == "error":
            return {
                **answer,
                "version_id": version_id,
                "error": (
                    f"The model of version {version_id} was not kept in your Kiln account, so "
                    f"nothing was pushed: {answer.get('error') or 'Kiln did not say why.'}"
                ),
            }
        kept.add(sha)
    for version in body["versions"]:
        if version["version_id"] in shas:
            version["mesh_sha256"] = shas[version["version_id"]]
    return None


def pull(
    tool: str,
    design_id: str,
    branch_id: str | None = None,
    branch_name: str | None = None,
    since_version_id: str | None = None,
    *,
    vault: Any = None,
) -> dict[str, Any]:
    """Bring one branch of the person's Kiln account, and the design's proven
    recipes, onto this computer."""
    if not branch_id and not branch_name:
        branch_name = "main"
    body = {"branch_id": branch_id, "branch_name": branch_name, "since_version_id": since_version_id}
    path = f"/api/cloud/designs/{_quote(design_id)}/branches/pull"
    return _bring_here(tool, design_id, lambda named: _request(tool, "POST", path, _asking(body, named)), vault)


def clone(tool: str, design_id: str, *, vault: Any = None) -> dict[str, Any]:
    """Bring a whole design, and its proven recipes, from the person's Kiln
    account onto this computer."""
    path = f"/api/cloud/designs/{_quote(design_id)}/clone"
    return _bring_here(tool, design_id, lambda named: _request(tool, "POST", path, _asking({}, named)), vault)


def _asking(body: dict[str, Any], models_named: bool) -> dict[str, Any]:
    """*body*, asking for an answer that names its models when *models_named*."""
    return {**body, _MESH_TRANSPORT: _BY_FINGERPRINT} if models_named else body


def _bring_here(tool: str, design_id: str, ask: Any, vault: Any) -> dict[str, Any]:
    """Ask for a pull or clone answer naming its models, fetch the ones this
    computer lacks, and keep the history here.

    *ask(models_named)* sends the request.  An answer carrying its models (a
    server that predates naming them) is kept as it came; when a model
    cannot come on its own the request is sent again for an answer carrying
    them.  A model whose bytes are not the fingerprint asked for, or one
    that could not be fetched, stops the pull before anything is recorded.
    """
    from kiln.history_vault import HistoryVault

    answer = ask(True)
    if answer.get("status") == "error":
        return answer
    own = vault is None
    vault = vault or HistoryVault()
    try:
        kept: dict[str, str] = {}
        models: dict[str, Any] | None = None
        if answer.get(_MESH_TRANSPORT) == _BY_FINGERPRINT:
            fetched = _fetch_models(tool, answer, vault)
            if fetched is None:
                answer = ask(False)
                if answer.get("status") == "error":
                    return answer
            elif fetched.get("status") == "error":
                return fetched
            else:
                kept, models = fetched["kept"], fetched["models"]
        applied = vault.apply_remote({**answer, "design_id": design_id}, kept_models=kept)
        recipes = _bring_recipes(tool, design_id, vault)
    finally:
        if own:
            vault.close()
    out = {"status": "success", "applied": applied, "design_id": design_id, "proven_recipes": recipes}
    if models is not None:
        out["models"] = models
    return out


def _fetch_models(tool: str, answer: dict[str, Any], vault: Any) -> dict[str, Any] | None:
    """Keep here, once each, the models an answer names and this computer lacks.

    Returns ``{"kept": {version_id: sha256}, "models": <what happened>}``;
    ``None`` when a model cannot come on its own (a server without the
    route, or a model larger than one fetch hands back), so the caller asks
    for the models inside the answer; or the refusal to answer with, when a
    model's bytes are not the fingerprint asked for or it could not be
    fetched.  Models fetched before a refusal stay kept here, each under its
    own fingerprint, and are not fetched again.
    """
    recipes = {
        r["version_id"]: r.get("recipe_dict") if isinstance(r.get("recipe_dict"), dict) else {}
        for r in answer.get("provenance_sidecars") or []
        if isinstance(r, dict) and isinstance(r.get("version_id"), str)
    }
    kept: dict[str, str] = {}
    missing: dict[str, str] = {}
    fetched: set[str] = set()
    already_here: set[str] = set()
    for version in answer.get("versions") or []:
        if not isinstance(version, dict):
            continue
        version_id, sha = version.get("version_id"), version.get("mesh_sha256")
        if not isinstance(version_id, str) or not _is_fingerprint(sha):
            continue
        if sha in missing:
            continue
        if sha in fetched or vault.mesh_path(sha) is not None:
            kept[version_id] = sha
            if sha not in fetched:
                already_here.add(sha)
            continue
        got = _fetch_model(tool, sha)
        if got.get("status") == "error":
            status, code = got.get("http_status"), got.get("code")
            if status == 404 and code == "MESH_NOT_FOUND":
                # Kiln keeps no bytes for it: the version is recorded without
                # its model, as an answer carrying the models would have had it.
                missing[sha] = version_id
                continue
            if status in (404, 405) or code == "MESH_TOO_LARGE":
                return None
            return {
                **got,
                "version_id": version_id,
                "error": (
                    f"The model of version {version_id} could not be brought from your Kiln "
                    f"account, so nothing was pulled: {got.get('error') or 'Kiln did not say why.'}"
                ),
            }
        data = got["content"]
        read = hashlib.sha256(data).hexdigest()
        if read != sha:
            return {
                **_refused(
                    tool, "MESH_FINGERPRINT_MISMATCH",
                    f"The model Kiln's servers sent for version {version_id} is not {sha[:12]} "
                    f"(its bytes are {read[:12]}), so nothing was pulled.",
                ),
                "version_id": version_id,
            }
        vault.keep_mesh(data, recipes.get(version_id) or {})
        kept[version_id] = sha
        fetched.add(sha)
    return {
        "kept": kept,
        "models": {
            "by_fingerprint": True,
            "fetched": len(fetched),
            "already_here": len(already_here),
            "missing": [{"version_id": v, "mesh_sha256": s} for s, v in missing.items()],
        },
    }


def _is_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _fetch_model(tool: str, sha: str) -> dict[str, Any]:
    """One model by its fingerprint, sitting out the API's request rate."""
    waits = 0
    while True:
        got = _request(tool, "GET", f"/api/cloud/meshes/{sha}", raw=True)
        wait = got.get("retry_after") if got.get("http_status") == 429 else None
        if not isinstance(wait, int) or waits >= _RATE_WAITS_PER_MODEL or not 0 < wait <= _RATE_WAIT_MAX_S:
            return got
        waits += 1
        _sleep(wait)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def remote_branches(tool: str, design_id: str) -> dict[str, Any]:
    """The branches of a design in the person's Kiln account."""
    answer = _request(tool, "GET", f"/api/cloud/designs/{_quote(design_id)}/branches")
    if answer.get("status") == "error":
        return answer
    branches = answer.get("items") if "items" in answer else answer.get("branches", [])
    return {"status": "success", "design_id": design_id, "branches": branches or []}


#: The fields of a release row the cloud release push takes.
_RELEASE_PUSH_FIELDS = (
    "release_id", "version_id", "name", "naming_scheme", "manifest",
    "signature_b64", "public_key_pem", "signed_at", "signed_by",
)


def push_release(tool: str, release_id: str, *, vault: Any = None) -> dict[str, Any]:
    """Send one signed release from this computer's records to the person's account."""
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        row = vault._conn.execute(
            "SELECT design_id, record FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()
        locked = None if row is None else lock_refusal(
            tool, [str(row["design_id"])], vault=vault, doing="save its release to your Kiln account",
        )
    finally:
        if own:
            vault.close()
    if row is None:
        return _refused(tool, "RELEASE_NOT_FOUND", f"No release {release_id} on this computer.")
    if locked is not None:
        return locked
    import json

    record = json.loads(row["record"])
    body = {k: record.get(k) for k in _RELEASE_PUSH_FIELDS}
    design_id = str(row["design_id"])
    answer = _request(tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/releases/push", body)
    if answer.get("status") == "error":
        return answer
    return {"status": "success", "release_id": release_id, "design_id": design_id, "push": answer}


def remote_releases(tool: str, design_id: str) -> dict[str, Any]:
    """The signed releases of a design in the person's Kiln account."""
    answer = _request(tool, "GET", f"/api/cloud/designs/{_quote(design_id)}/releases")
    if answer.get("status") == "error":
        return answer
    releases = answer.get("items") if "items" in answer else answer.get("releases", [])
    return {"status": "success", "design_id": design_id, "releases": releases or []}


# ---------------------------------------------------------------------------
# Automatic saving to the account (Kiln Pro, off until the person turns it on)
# ---------------------------------------------------------------------------

#: The account's switch, where it lives and how long a reading of it stands.
_PREFS_PATH = "/api/cloud/user/prefs"
_PREFS_TOOL = "cloud_auto_push"
_PREFS_KEY = "account_prefs"
_PREFS_FRESH_S = 60.0
#: A save waits this long at most for the switch to be read.
_PREFS_WAIT_S = 10.0
#: Kept here once the person has been told automatic saving exists.
_OFFER_KEY = "auto_push_offered"
#: The saved version of a design at which the offer is made.
_OFFER_AT_VERSIONS = 3
#: What an automatic push is, to the push it runs.
_AUTO_PUSH_TOOL = "cloud_push_branch"
#: Turning it on asks for the person's second factor, which only the account
#: page can, so the offer names that page rather than a command.
_TURN_ON = "app.kiln3d.com/settings/account"


def _read_prefs(vault: Any, *, fresh: bool = False) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The account's automatic-saving switch, as ``(prefs, miss)``.

    Read at most once a minute and kept in this computer's records between
    readings.  When it cannot be read, *prefs* is ``None`` and *miss* is why,
    with the last reading under ``last``: a push waits for a reading it can
    trust rather than act on an old one.  ``can_auto_backup`` is what the
    account allows; an answer that does not say counts as not allowed.
    """
    kept = vault.setting(_PREFS_KEY)
    kept = kept if isinstance(kept, dict) else None
    if not fresh and kept is not None:
        age = time.time() - float(kept.get("read_at") or 0.0)
        if 0.0 <= age < _PREFS_FRESH_S:
            return kept, None
    answer = _request(_PREFS_TOOL, "GET", _PREFS_PATH, timeout=_PREFS_WAIT_S)
    if answer.get("status") == "error":
        return None, {**answer, "last": kept}
    prefs = {
        "auto_backup": answer.get("auto_backup") is True,
        "is_set": answer.get("is_set") is True,
        "can_auto_backup": answer.get("can_auto_backup") if isinstance(answer.get("can_auto_backup"), bool) else None,
        "read_at": time.time(),
    }
    vault.set_setting(_PREFS_KEY, prefs)
    return prefs, None


def _pushes(prefs: dict[str, Any] | None) -> bool:
    return bool(prefs and prefs.get("auto_backup") is True and prefs.get("can_auto_backup") is True)


def _after_change(changed: dict[str, list[str]], *, offer_due: bool, vault: Any) -> dict[str, Any]:
    """What a change kept on this computer adds to its answer.

    ``auto_push``: each branch in *changed* (design id to branch ids) pushed
    to the account when automatic saving is on, through :func:`push`, so a
    lock and everything else a push honours apply.  ``auto_push_offer``: the
    one-time word that automatic saving exists, when *offer_due*, the
    account allows it and the person has never set it.  Neither is added
    when there is nothing to say.
    """
    offer_due = offer_due and not vault.setting(_OFFER_KEY)
    if not changed and not offer_due:
        return {}
    prefs, miss = _read_prefs(vault)
    out: dict[str, Any] = {}
    if changed and prefs is None and _pushes((miss or {}).get("last")):
        out["auto_push"] = {
            "pushed": False,
            "code": (miss or {}).get("code"),
            "said": (
                "Kiln could not check your automatic-saving setting just now, so nothing was "
                "saved to your Kiln account. It is kept on this computer; `kiln cloud push` sends it."
            ),
            "branches": [],
        }
    elif changed and _pushes(prefs):
        out["auto_push"] = _auto_push(changed, vault)
    if offer_due and prefs is not None and not prefs.get("is_set") and prefs.get("can_auto_backup") is True:
        out["auto_push_offer"] = {
            "said": (
                "Kiln can save each design's history to your Kiln account after every save on "
                "this computer; it stays off until you turn it on."
            ),
            "turn_on": _TURN_ON,
        }
        vault.set_setting(_OFFER_KEY, {"offered_at": time.time()})
    return out


def _auto_push(changed: dict[str, list[str]], vault: Any) -> dict[str, Any]:
    """Push each changed branch; one line per branch on what became of it."""
    rows: list[dict[str, Any]] = []
    for design_id, branch_ids in changed.items():
        for branch_id in branch_ids:
            name = (vault.branch(branch_id) or {}).get("name") or branch_id
            answer = push(_AUTO_PUSH_TOOL, design_id, branch_id, vault=vault)
            row: dict[str, Any] = {
                "design_id": design_id, "branch_id": branch_id, "branch": name,
                "pushed": answer.get("status") == "success",
            }
            if answer.get("kept_as"):
                row["kept_as"] = answer["kept_as"]
                row["said"] = answer["said"]
            elif row["pushed"]:
                row["said"] = f"Saved {name} of {design_id} to your Kiln account."
            elif answer.get("code") == LOCKED_CODE:
                row["code"] = LOCKED_CODE
                row["said"] = answer["error"]
            else:
                row["code"] = answer.get("code")
                why = str(answer.get("error") or "Kiln's servers did not keep it.").rstrip(".")
                if _diverged(answer):
                    why = "the branch in your Kiln account moved on another computer"
                row["said"] = (
                    f"Kiln did not save {name} of {design_id} to your Kiln account automatically "
                    f"({why}). It is kept on this computer; `kiln cloud push {_shell(design_id)}` sends it."
                )
            rows.append(row)
    return {"pushed": all(r["pushed"] for r in rows), "said": " ".join(r["said"] for r in rows), "branches": rows}


def after_local_save(design_id: str, *, vault: Any = None) -> dict[str, Any]:
    """What a version saved on this computer adds to the save's answer.

    For an install without kiln-pro (kiln-pro saves its own): the version
    is recorded on the design's checked-out branch, that branch is pushed
    to the account when automatic saving is on, and the one-time offer is
    made when the design reaches its third saved version.  Best effort: a
    push that fails or is refused never fails the save, and says so.
    """
    try:
        with _opened(vault) as v:
            if not v.sync_from_library(design_id):
                return {}
            branch = v.checked_out(design_id)
            changed = {design_id: [branch["branch_id"]]} if branch else {}
            offer_due = v.version_count(design_id) >= _OFFER_AT_VERSIONS
            return _after_change(changed, offer_due=offer_due, vault=v)
    except Exception:  # noqa: BLE001 -- the version is saved; this is the extra
        logger.warning("history: the step after saving %s failed", design_id, exc_info=True)
        return {}


def auto_push_setting(state: str, *, vault: Any = None) -> dict[str, Any]:
    """Turn automatic saving to the account ``on`` or ``off``, or read its ``status``.

    The switch lives in the person's Kiln account; Kiln's servers decide
    whether their plan includes it and refuse in their own words when not.
    """
    if state not in ("on", "off", "status"):
        return _refused(_PREFS_TOOL, "INVALID_INPUT", "Say on, off or status.")
    if state != "status":
        answer = _request(_PREFS_TOOL, "PUT", _PREFS_PATH, {"auto_backup": state == "on"}, timeout=_PREFS_WAIT_S)
        if answer.get("status") == "error":
            return answer
    with _opened(vault) as v:
        prefs, miss = _read_prefs(v, fresh=True)
    if prefs is None:
        if state == "status":
            return {k: val for k, val in (miss or {}).items() if k != "last"}
        prefs = {"auto_backup": state == "on", "is_set": True, "can_auto_backup": None}
    on, allowed = prefs.get("auto_backup") is True, prefs.get("can_auto_backup")
    if on and allowed is True:
        said = (
            "Automatic saving is on: after each save on this computer, Kiln saves the design's "
            "history to your Kiln account. Designs locked to this computer are never saved there."
        )
    elif on and allowed is False:
        said = (
            "Automatic saving is on in your Kiln account, but your plan does not include it, "
            "so nothing is saved automatically."
        )
    elif on:
        said = (
            "Automatic saving is on in your Kiln account, but Kiln's servers did not confirm "
            "your plan includes it, so nothing is saved automatically yet."
        )
    else:
        said = "Automatic saving is off: a design is saved to your Kiln account only when you push it."
    return {
        "status": "success",
        "auto_push": "on" if on else "off",
        "is_set": prefs.get("is_set") is True,
        "can_auto_backup": allowed is True,
        "said": said,
    }


#: The cloud routes of each library kind, and the CLI's word for it.
_LIBRARY_ROUTE = {"features": "features", "presets": "presets"}


def push_library(tool: str, kind: str, item_id: str, branch_id: str, *, design_id: str | None = None,
                 vault: Any = None) -> dict[str, Any]:
    """Send one branch of a feature or preset from this computer to the account."""
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        payload = vault.library_branch_payload(kind, item_id, branch_id)
        if payload is None:
            return _refused(tool, "BRANCH_NOT_FOUND", f"No branch {branch_id} of {item_id} on this computer.")
        design_id, payload, left_out = without_locked_designs(design_id, payload, kind, vault=vault)
    finally:
        if own:
            vault.close()
    body = {**payload, "design_id": design_id}
    answer = _request(tool, "POST", f"/api/cloud/{_LIBRARY_ROUTE[kind]}/{_quote(item_id)}/branches/push", body)
    if answer.get("status") == "error":
        return answer
    out = {"status": "success", "push": answer, "item_id": item_id, "branch_id": branch_id}
    if left_out:
        out["design_left_out"] = left_out
    return out


def pull_library(tool: str, kind: str, item_id: str, branch_id: str | None = None,
                 branch_name: str | None = None, *, vault: Any = None) -> dict[str, Any]:
    """Bring one branch of a feature or preset from the account to this computer."""
    if not branch_id and not branch_name:
        branch_name = "main"
    body = {"branch_id": branch_id, "branch_name": branch_name}
    answer = _request(tool, "POST", f"/api/cloud/{_LIBRARY_ROUTE[kind]}/{_quote(item_id)}/branches/pull", body)
    if answer.get("status") == "error":
        return answer
    return _keep_library_remote(kind, item_id, answer, vault)


def clone_library(tool: str, kind: str, item_id: str, *, vault: Any = None) -> dict[str, Any]:
    """Bring a whole feature or preset from the account to this computer."""
    answer = _request(tool, "POST", f"/api/cloud/{_LIBRARY_ROUTE[kind]}/{_quote(item_id)}/clone", {})
    if answer.get("status") == "error":
        return answer
    return _keep_library_remote(kind, item_id, answer, vault)


def _keep_library_remote(kind: str, item_id: str, answer: dict[str, Any], vault: Any) -> dict[str, Any]:
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        applied = vault.apply_library_remote(kind, item_id, answer)
    finally:
        if own:
            vault.close()
    return {"status": "success", "applied": applied, "item_id": item_id}


def push_library_release(tool: str, kind: str, release_id: str, *, vault: Any = None) -> dict[str, Any]:
    """Send a feature or preset release kept on this computer to the account."""
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        item_id = vault.library_item_of(kind, "release", release_id)
        record = next(
            (r for r in vault._lib_rows(kind, "release", item_id) if r.get("release_id") == release_id),
            None,
        ) if item_id else None
    finally:
        if own:
            vault.close()
    if record is None:
        return _refused(tool, "RELEASE_NOT_FOUND", f"No release {release_id} on this computer.")
    answer = _request(tool, "POST", f"/api/cloud/{_LIBRARY_ROUTE[kind]}/{_quote(item_id)}/releases/push", record)
    if answer.get("status") == "error":
        return answer
    return {"status": "success", "release_id": release_id, "item_id": item_id, "push": answer}


#: The library cloud doors: the manifest's mode -> (kind, what it does).
_LIBRARY_MODES = {
    "feature_push": ("features", "push"), "feature_pull": ("features", "pull"),
    "feature_clone": ("features", "clone"), "feature_release_push": ("features", "release_push"),
    "preset_push": ("presets", "push"), "preset_pull": ("presets", "pull"),
    "preset_clone": ("presets", "clone"), "preset_release_push": ("presets", "release_push"),
}


def run_local(tool: str, mode: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run the cloud door *mode* for a stub of *tool* on this computer."""
    remote = kwargs.get("remote") or "default"
    if remote != "default":
        return _refused(
            tool, "REMOTE_NOT_AVAILABLE",
            "This install pushes to and pulls from your Kiln account only.",
        )
    if mode in _LIBRARY_MODES:
        kind, does = _LIBRARY_MODES[mode]
        from kiln.history_vault import LIBRARY_KINDS

        if does == "release_push":
            release_id = str(kwargs.get("release_id") or "")
            if not release_id:
                return _refused(tool, "INVALID_INPUT", "release_id is required.")
            return push_library_release(tool, kind, release_id)
        item_id = str(kwargs.get(LIBRARY_KINDS[kind]["id"]) or "")
        if not item_id:
            return _refused(tool, "INVALID_INPUT", f"{LIBRARY_KINDS[kind]['id']} is required.")
        if does == "push":
            branch_id = str(kwargs.get("branch_id") or "")
            if not branch_id:
                return _refused(tool, "INVALID_INPUT", "branch_id is required.")
            return push_library(tool, kind, item_id, branch_id, design_id=kwargs.get("design_id"))
        if does == "pull":
            return pull_library(tool, kind, item_id, kwargs.get("branch_id"), kwargs.get("branch_name"))
        return clone_library(tool, kind, item_id)
    if mode == "release_push":
        release_id = str(kwargs.get("release_id") or "")
        if not release_id:
            return _refused(tool, "INVALID_INPUT", "release_id is required.")
        return push_release(tool, release_id)
    design_id = str(kwargs.get("design_id") or "")
    if not design_id:
        return _refused(tool, "INVALID_INPUT", "design_id is required.")
    if mode == "remote_releases":
        return remote_releases(tool, design_id)
    if mode == "push":
        return push(
            tool, design_id, kwargs.get("branch_id") or None,
            make_room_for=kwargs.get("make_room_for") or None,
        )
    if mode == "pull":
        return pull(
            tool, design_id, kwargs.get("branch_id") or None,
            kwargs.get("branch_name") or None, kwargs.get("since_version_id") or None,
        )
    if mode == "clone":
        return clone(tool, design_id)
    if mode == "remote_branches":
        return remote_branches(tool, design_id)
    return _refused(tool, "UNKNOWN_HISTORY_DOOR", f"Kiln has no way to run {tool} on this computer.")


__all__ = [
    "LOCKED_CODE",
    "after_local_save",
    "auto_push_setting",
    "lock_refusal",
    "served_refusal",
    "set_lock",
    "clone_library",
    "library_items_for_call",
    "pull_library",
    "push_library",
    "push_library_release",
    "SCHEMA_VERSION",
    "call_served",
    "clone",
    "designs_for_call",
    "pull",
    "push",
    "push_release",
    "records_for_call",
    "remote_branches",
    "remote_releases",
    "run_local",
    "take_delta",
]
