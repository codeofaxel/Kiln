"""A delete, on a computer that keeps its own copy of what is deleted.

Kiln's servers hold the copy in the person's Kiln account and nothing of this
computer's.  So a delete has two halves: the servers do the account's, and
this module does this computer's, only when the person asked for it.

What a person can ask for, the same at every door:

* delete it (the default): archived in both places, brought back any time;
* ``permanent=True, where="computer"``: this computer's copy is removed and
  the account's is left alone;
* ``permanent=True, where="account"``: the account's copy is erased; the copy
  here is kept and locked to this computer, so it is not saved back;
* ``permanent=True``: erased in both places.

The first call never deletes: it says what is kept where, and what would be
lost.  The second, with the id typed back, does it.  Removing this computer's
copy needs no account and no connection.
"""

from __future__ import annotations

import logging
import shutil
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: The delete tools this computer has a half of, and the argument naming the item.
TOOLS: dict[str, str] = {
    "delete_design": "design_id",
    "delete_feature": "feature_id",
    "delete_decoration": "preset_id",
}
#: The arguments this module adds to a forwarded call; never sent on.
PASS, NO_RECORDS = "_kiln_delete_pass", "_kiln_delete_no_records"

EVERYWHERE, ACCOUNT, COMPUTER = "everywhere", "account", "computer"

ONE_PLACE_IS_PERMANENT = (
    "Removing it from one place only is a permanent removal from that place, "
    "so nothing was changed. Call again with permanent=True to remove it there "
    "(the other copy is kept), or leave where out to archive it everywhere, "
    "which can be undone."
)
SERVERS_NOT_ASKED = (
    "Nothing was deleted on this computer either: deleting it everywhere "
    "needs Kiln's servers. To remove only this computer's copy, call again "
    "with permanent=True and where='computer'."
)

Forward = Callable[..., Any]


def _refusal(code: str, message: str, **more: Any) -> dict[str, Any]:
    return {"status": "error", "success": False, "code": code, "error": message, **more}


def _shared_server() -> bool:
    """Whether this process serves many accounts.  Such a process keeps no
    person's own copy, so this module reads and removes nothing there."""
    from kiln.runtime_env import is_hosted_multitenant

    return bool(is_hosted_multitenant())


def copy_here(design_id: str) -> dict[str, Any] | None:
    """What this computer keeps of *design_id*, or ``None`` when it keeps none."""
    from kiln.history_vault import HistoryVault, _design_folder

    if _shared_server():
        return None
    folder = _design_folder(design_id)
    if folder is None:
        return None
    vault = HistoryVault()
    try:
        if folder.is_dir():
            vault.sync_from_library(design_id)
        ids = sorted(vault.version_ids(design_id))
    except Exception:  # noqa: BLE001 -- unread records: the folder alone is the copy
        logger.debug("delete: history of %s unread", design_id, exc_info=True)
        ids = []
    finally:
        vault.close()
    if not folder.is_dir() and not ids:
        return None
    return {"versions": len(ids), "version_ids": ids, "folder": folder.is_dir()}


def erase_here(design_id: str) -> dict[str, Any]:
    """Remove this computer's copy of *design_id*: its folder and its records."""
    from kiln.history_vault import HistoryVault, _design_folder, _open_version_store

    if _shared_server():
        raise RuntimeError("a shared server keeps no copy of a person's design to remove")
    folder = _design_folder(design_id)
    removed_folder = False
    if folder is not None and folder.is_dir():
        shutil.rmtree(folder)
        removed_folder = True
    vault = HistoryVault()
    try:
        gone = vault.forget_design(design_id)
    finally:
        vault.close()
    store = _open_version_store()
    if store is not None:
        try:
            with store._lock:
                store._conn.execute("DELETE FROM design_versions WHERE design_id = ?", (design_id,))
                store._conn.commit()
        except Exception:  # noqa: BLE001 -- an index row left behind names no file
            logger.debug("delete: version index of %s not cleared", design_id, exc_info=True)
        finally:
            store.close()
    return {"folder_removed": removed_folder, **gone}


def _account_facts(answer: Any) -> dict[str, Any]:
    """What the servers said the account holds; ``held`` is ``None`` when they did not say."""
    facts = answer.get("in_your_account") if isinstance(answer, dict) else None
    if isinstance(facts, dict) and isinstance(facts.get("held"), bool):
        return facts
    return {"held": None}


def _missing_from_account(here: dict[str, Any], facts: dict[str, Any]) -> int | None:
    """How many versions here the account does not keep; ``None`` when unknown."""
    if facts.get("held") is None:
        return None
    if not facts.get("held"):
        return here["versions"]
    kept = set(facts.get("version_ids") or [])
    return len([v for v in here["version_ids"] if v not in kept])


def _what_is_lost(here: dict[str, Any], facts: dict[str, Any]) -> str:
    missing = _missing_from_account(here, facts)
    if missing is None:
        return "Kiln couldn't check your Kiln account, so this may be the only copy."
    if not facts.get("held"):
        return "This is the only copy: your Kiln account doesn't keep this design."
    if missing:
        return (
            f"{missing} of its {here['versions']} versions were never saved to your "
            "Kiln account and will be gone for good."
        )
    return "Your Kiln account keeps every version, so you can bring it back here any time."


def _brought_back(here: dict[str, Any], facts: dict[str, Any], design_id: str) -> str:
    missing = _missing_from_account(here, facts)
    if missing == 0 and facts.get("held"):
        from kiln.cli_command import kiln_command

        return (
            "All of it: your Kiln account keeps every version. "
            f"`{kiln_command('cloud clone', design_id)}` brings it back here."
        )
    if missing is None:
        return "Whatever your Kiln account keeps of it, if anything: Kiln couldn't check."
    if not facts.get("held"):
        return "Nothing: this was the only copy."
    return f"The versions your Kiln account keeps; the {missing} that were only here are gone."


def _versions(n: int) -> str:
    return f"{n} version" if n == 1 else f"{n} versions"


def _shown(here: dict[str, Any]) -> dict[str, Any]:
    return {"versions": here["versions"]}


def _was_deleted(answer: Any) -> bool:
    return isinstance(answer, dict) and answer.get("status") == "success" and answer.get("deleted") is True


def _not_in_account(answer: Any) -> bool:
    return isinstance(answer, dict) and answer.get("code") == "NOT_FOUND"


def _lock_here(design_id: str) -> bool:
    from kiln.history_vault import HistoryVault

    if _shared_server():
        return False
    vault = HistoryVault()
    try:
        return bool(vault.set_locked(design_id, True))
    except Exception:  # noqa: BLE001
        logger.debug("delete: %s not locked here", design_id, exc_info=True)
        return False
    finally:
        vault.close()


def _from_this_computer(design_id: str, permanent: bool, confirm: str, forward: Forward) -> dict[str, Any]:
    if not permanent:
        return _refusal("WHERE_NEEDS_PERMANENT", ONE_PLACE_IS_PERMANENT)
    here = copy_here(design_id)
    if here is None:
        return _refusal("NOT_FOUND", f"No design '{design_id}' on this computer.")
    # Asked only for what the account holds; the servers change nothing.
    facts = _account_facts(forward(design_id=design_id, permanent=True, where=COMPUTER))
    lost = _what_is_lost(here, facts)
    if confirm != design_id:
        out: dict[str, Any] = {
            "status": "success", "confirm_required": True, "action": "permanent_delete",
            "kind": "design", "artifact_id": design_id, "where": COMPUTER,
            "reversible": _missing_from_account(here, facts) == 0,
            "on_this_computer": _shown(here),
            "in_your_account": {k: v for k, v in facts.items() if k != "version_ids"},
            "not_in_your_account": _missing_from_account(here, facts),
            "what_is_lost": lost,
            "confirm_with": design_id,
            "message": (
                f"This removes design '{design_id}' ({_versions(here['versions'])}) from this "
                f"computer. {lost} To proceed, call again with `permanent=True`, "
                f"`where='computer'` and `confirm='{design_id}'`."
            ),
        }
        if confirm:
            out["mismatch"] = f"confirmation '{confirm}' does not match '{design_id}', so nothing was deleted."
        return out
    removed = erase_here(design_id)
    kept = facts.get("held") is True and _missing_from_account(here, facts) == 0
    return {
        "status": "success", "deleted": True, "action": "permanent_delete", "kind": "design",
        "artifact_id": design_id, "where": COMPUTER, "reversible": kept,
        "what_happened": {"computer": "erased", "account": "not touched"},
        "removed_here": removed,
        "bring_back": _brought_back(here, facts, design_id),
    }


def _from_the_account(design_id: str, call: dict[str, Any], forward: Forward) -> Any:
    answer = forward(**call)
    if not (_was_deleted(answer) and answer.get("reversible") is False):
        return answer
    if copy_here(design_id) is None:
        return answer
    locked = _lock_here(design_id)
    said = (
        "kept, and locked to this computer so it is not saved back to your account. "
        "Unlock it when you want it there again."
        if locked else "kept"
    )
    return {**answer, "what_happened": {**(answer.get("what_happened") or {}), "computer": said}}


def _everywhere(design_id: str, permanent: bool, confirm: str, call: dict[str, Any], forward: Forward) -> Any:
    from kiln.history_vault import mark_archived_here

    here = copy_here(design_id)
    answer = forward(**call)
    if here is None or not isinstance(answer, dict):
        return answer
    confirmed = confirm == design_id
    absent = _not_in_account(answer)
    if not (answer.get("status") == "success" or absent):
        # Offline, signed out, refused: nothing was deleted there or here.
        return {**answer, "this_computer": SERVERS_NOT_ASKED}
    if not confirmed:
        if absent:
            return {
                "status": "success", "confirm_required": True, "kind": "design",
                "action": "permanent_delete" if permanent else "retire",
                "reversible": not permanent, "artifact_id": design_id, "where": EVERYWHERE,
                "on_this_computer": _shown(here), "in_your_account": {"held": False},
                "confirm_with": design_id,
                "message": (
                    f"Design '{design_id}' ({_versions(here['versions'])}) is on this computer only. "
                    + ("This is the only copy: it will be gone for good. " if permanent
                       else "It will be archived here: hidden, kept whole, and brought back any time. ")
                    + f"To proceed, call again with confirm='{design_id}'."
                ),
            }
        return {**answer, "on_this_computer": _shown(here)}
    if not permanent:
        if not (_was_deleted(answer) or absent):
            return answer
        changed = mark_archived_here(design_id, True)
        base = answer if _was_deleted(answer) else {
            "status": "success", "deleted": True, "action": "retire", "reversible": True,
            "kind": "design", "artifact_id": design_id, "where": EVERYWHERE,
            "bring_back": "All of it, any time: unarchive it.",
        }
        return {
            **base,
            "what_happened": {
                "account": (answer.get("what_happened") or {}).get("account", "archived")
                if _was_deleted(answer) else "not there",
                "computer": "archived",
            },
            "on_this_computer": {"recipe_files_updated": changed},
        }
    if not (_was_deleted(answer) or absent):
        return answer
    removed = erase_here(design_id)
    return {
        **(answer if _was_deleted(answer) else {
            "status": "success", "deleted": True, "action": "permanent_delete", "reversible": False,
            "kind": "design", "artifact_id": design_id, "where": EVERYWHERE,
        }),
        "what_happened": {"account": "erased" if _was_deleted(answer) else "not there", "computer": "erased"},
        "removed_here": removed,
        "bring_back": "Nothing: it is gone from your Kiln account and from this computer.",
    }


def _design(call: dict[str, Any], forward: Forward) -> Any:
    design_id = str(call.get("design_id") or "").strip()
    where = str(call.get("where") or EVERYWHERE).strip().lower()
    permanent = call.get("permanent") is True
    confirm = str(call.get("confirm") or "").strip()
    if not design_id or where not in (EVERYWHERE, ACCOUNT, COMPUTER):
        return forward(**call)   # worded by the servers, as at every door
    if where == COMPUTER:
        return _from_this_computer(design_id, permanent, confirm, forward)
    if where == ACCOUNT:
        return _from_the_account(design_id, call, forward)
    return _everywhere(design_id, permanent, confirm, call, forward)


def _library(call: dict[str, Any], forward: Forward) -> Any:
    """A saved feature or decoration preset.  A call that carries this
    computer's records works on this computer's copy; one that does not works
    on the account's."""
    where = str(call.get("where") or EVERYWHERE).strip().lower()
    permanent = call.get("permanent") is True
    if where == ACCOUNT:
        return forward(**call, **{NO_RECORDS: True})
    if where == COMPUTER or not permanent:
        return forward(**call)
    here = forward(**{**call, "where": COMPUTER})
    if not _was_deleted(here):
        return here
    there = forward(**{**call, "where": ACCOUNT}, **{NO_RECORDS: True})
    return {
        **here,
        "where": EVERYWHERE,
        "what_happened": {
            "computer": "erased",
            "account": "erased" if _was_deleted(there) else "not there" if _not_in_account(there) else "not erased",
        },
        **({} if _was_deleted(there) or _not_in_account(there) else {"account_answer": there}),
    }


def run(name: str, call: dict[str, Any], forward: Forward) -> Any:
    """Carry out the delete tool *name* for *call*.  *forward* asks Kiln's
    servers; it is the only way anything of the account is touched."""
    if _shared_server():
        # Only the account's copy exists there: nothing of this disk is
        # read, so no records ride with the call.
        return forward(**call, **{NO_RECORDS: True})
    if name == "delete_design":
        return _design(dict(call), forward)
    return _library(dict(call), forward)


__all__ = ["NO_RECORDS", "PASS", "TOOLS", "copy_here", "erase_here", "run"]
