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
leaves this computer, and only when the person asks.
"""

from __future__ import annotations

import base64
import json
import logging
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
        if "files" in kinds and file_param:
            out["files"] = files_for_call(tool, arguments.get(file_param))
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
    try:
        summaries = [vault.apply_delta(d) for d in deltas]
        library = _take_library_deltas(vault, delta, sent)
        log = vault.apply_log_delta(delta)
        continues = None
        made = answer.get("branch") if isinstance(answer.get("branch"), dict) else {}
        if tool in _CHECKS_OUT and made.get("branch_id") and summaries:
            branch = vault.check_out(str(made.get("design_id") or summaries[0]["design_id"]), made["branch_id"])
            continues = branch.get("name") if branch else None
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
    return answer


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


def call_served(tool: str, *, kinds: tuple[str, ...] = ("designs",), **kwargs: Any) -> dict[str, Any]:
    """Call a served history tool with this computer's records, and keep the change.

    The command line's way in; the MCP stubs do the same around their own
    transport (``kiln.server._register_pro_tool_stubs``).
    """
    from kiln.server import _pro_api_call

    records = records_for_call(kwargs, kinds=kinds, tool=tool)
    return take_delta(_pro_api_call(tool, **kwargs, history_records=records), records, tool=tool)


# ---------------------------------------------------------------------------
# The cloud doors this computer runs itself
# ---------------------------------------------------------------------------


def _request(tool: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    import httpx

    from kiln.auth_session import _api_base, resolve_api_bearer
    from kiln.served_answer import envelope_for_http, envelope_for_transport

    bearer = resolve_api_bearer().token
    if not bearer:
        from kiln.tiers_and_terms import signed_out_message, signin_hint_fields

        return {
            "status": "error",
            "success": False,
            "error": signed_out_message(),
            "code": "KILN_ACCOUNT_NOT_PAIRED",
            "tool": tool,
            "why": "signed_out",
            **signin_hint_fields(),
        }
    try:
        from kiln.version_check import _current_version

        version = _current_version()
    except Exception:  # noqa: BLE001 — a header is never worth failing over
        version = ""
    headers = {"Authorization": f"Bearer {bearer}", "X-Kiln-Client-Version": version}
    try:
        resp = httpx.request(method, f"{_api_base()}{path}", json=body, headers=headers, timeout=_WAIT_S)
    except Exception as exc:  # noqa: BLE001
        return envelope_for_transport(tool, exc, host=_api_base(), kind="sync")
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
        return answer
    if isinstance(data, list):
        return {"items": data}
    return data if isinstance(data, dict) else {}


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _refused(tool: str, code: str, error: str) -> dict[str, Any]:
    return {"status": "error", "success": False, "code": code, "error": error, "tool": tool}


def push(tool: str, design_id: str, branch_id: str | None = None, *, replace_confirmed: bool = False, vault: Any = None) -> dict[str, Any]:
    """Send one branch of this computer's history to the person's Kiln account."""
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        vault.sync_from_library(design_id)
        branch = vault.branch(branch_id) if branch_id else vault.branch_named(design_id, "main")
        if branch is None or branch.get("design_id") != design_id:
            return _refused(tool, "BRANCH_NOT_FOUND", f"No branch {branch_id or 'main'} of {design_id} on this computer.")
        members = vault.memberships(branch["branch_id"])
        versions, sidecars = [], []
        for m in members:
            v = vault.version(m["version_id"])
            if v is None:
                continue
            recipe = dict(v["recipe"])
            mesh = vault.mesh_path(v.get("mesh_sha256"))
            if mesh is not None:
                recipe["mesh_bytes_b64"] = base64.b64encode(mesh.read_bytes()).decode("ascii")
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
    if replace_confirmed:
        body["replace_confirmed"] = True
    answer = _request(tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/branches/push", body)
    if answer.get("status") == "error":
        return answer
    return {"status": "success", "push": answer, "design_id": design_id, "branch_id": branch["branch_id"]}


def pull(
    tool: str,
    design_id: str,
    branch_id: str | None = None,
    branch_name: str | None = None,
    since_version_id: str | None = None,
    *,
    vault: Any = None,
) -> dict[str, Any]:
    """Bring one branch of the person's Kiln account onto this computer."""
    if not branch_id and not branch_name:
        branch_name = "main"
    body = {"branch_id": branch_id, "branch_name": branch_name, "since_version_id": since_version_id}
    answer = _request(tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/branches/pull", body)
    if answer.get("status") == "error":
        return answer
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        applied = vault.apply_remote({**answer, "design_id": design_id})
    finally:
        if own:
            vault.close()
    return {"status": "success", "applied": applied, "design_id": design_id}


def clone(tool: str, design_id: str, *, vault: Any = None) -> dict[str, Any]:
    """Bring a whole design from the person's Kiln account onto this computer."""
    answer = _request(tool, "POST", f"/api/cloud/designs/{_quote(design_id)}/clone", {})
    if answer.get("status") == "error":
        return answer
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        applied = vault.apply_remote({**answer, "design_id": design_id})
    finally:
        if own:
            vault.close()
    return {"status": "success", "applied": applied, "design_id": design_id}


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
    finally:
        if own:
            vault.close()
    if row is None:
        return _refused(tool, "RELEASE_NOT_FOUND", f"No release {release_id} on this computer.")
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
    finally:
        if own:
            vault.close()
    if payload is None:
        return _refused(tool, "BRANCH_NOT_FOUND", f"No branch {branch_id} of {item_id} on this computer.")
    body = {**payload, "design_id": design_id}
    answer = _request(tool, "POST", f"/api/cloud/{_LIBRARY_ROUTE[kind]}/{_quote(item_id)}/branches/push", body)
    if answer.get("status") == "error":
        return answer
    return {"status": "success", "push": answer, "item_id": item_id, "branch_id": branch_id}


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
        return push(tool, design_id, kwargs.get("branch_id") or None)
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
