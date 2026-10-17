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
import logging
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


def records_for_call(arguments: dict[str, Any], vault: Any = None) -> dict[str, Any]:
    """The ``history_records`` a served history call sends."""
    from kiln.history_vault import HistoryVault

    own = vault is None
    vault = vault or HistoryVault()
    try:
        designs, versions = designs_for_call(vault, arguments)
        bundles = []
        left = _MESH_BUDGET
        for design_id in designs[:_MAX_DESIGNS]:
            vault.sync_from_library(design_id)
            if vault.has_design(design_id):
                bundle = vault.bundle(design_id, meshes_for=versions, mesh_budget=left)
                left -= sum(
                    len(s["recipe_dict"].get("mesh_bytes_b64") or "") * 3 // 4
                    for s in bundle["provenance_sidecars"]
                )
                bundles.append(bundle)
        return {"schema_version": SCHEMA_VERSION, "designs": bundles, "named": designs[:_MAX_DESIGNS]}
    finally:
        if own:
            vault.close()


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
        continues = None
        made = answer.get("branch") if isinstance(answer.get("branch"), dict) else {}
        if tool in _CHECKS_OUT and made.get("branch_id") and summaries:
            branch = vault.check_out(str(made.get("design_id") or summaries[0]["design_id"]), made["branch_id"])
            continues = branch.get("name") if branch else None
    finally:
        if own:
            vault.close()
    if summaries:
        kept: dict[str, Any] = {
            "where": "on this computer",
            "designs": [s["design_id"] for s in summaries],
        }
        changed_here = [b for s in summaries for b in s["branches_changed_here"]]
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


def call_served(tool: str, **kwargs: Any) -> dict[str, Any]:
    """Call a served history tool with this computer's records, and keep the change.

    The command line's way in; the MCP stubs do the same around their own
    transport (``kiln.server._register_pro_tool_stubs``).
    """
    from kiln.server import _pro_api_call

    records = records_for_call(kwargs)
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


def run_local(tool: str, mode: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Run the cloud door *mode* for a stub of *tool* on this computer."""
    remote = kwargs.get("remote") or "default"
    if remote != "default":
        return _refused(
            tool, "REMOTE_NOT_AVAILABLE",
            "This install pushes to and pulls from your Kiln account only.",
        )
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
