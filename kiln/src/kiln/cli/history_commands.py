"""``kiln branch / merge / cloud / release`` on a computer without kiln-pro.

Each command is a courier: it sends the design's history records from this
computer (:mod:`kiln.history_vault`) with a call to Kiln's servers, which do
the work, and writes back what changed (:mod:`kiln.history_courier`).  The
cloud commands push and pull between this computer and the person's Kiln
account, only when they are run.  Branching, merging and signed releases
need a Kiln account on a plan that includes them; the servers say so when
it does not.

Registered only when kiln-pro is not installed (``kiln.cli.main``): with it,
kiln-pro's own commands of the same names run on this computer instead.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

import click


def _vault():
    from kiln.history_vault import HistoryVault

    return HistoryVault()


def _served(tool: str, **kwargs: Any) -> dict[str, Any]:
    from kiln.history_courier import call_served

    return call_served(tool, **kwargs)


def _finish(answer: dict[str, Any], json_mode: bool, say: Callable[[dict[str, Any]], str]) -> None:
    if json_mode:
        click.echo(json.dumps(answer, indent=2, default=str))
    failed = not isinstance(answer, dict) or answer.get("status") == "error" or answer.get("success") is False
    if failed:
        if not json_mode:
            click.echo(f"ERROR: {answer.get('error') or 'Kiln could not do that.'}", err=True)
        sys.exit(1)
    if not json_mode:
        click.echo(say(answer))


def _branch_id(design_id: str, name_or_id: str) -> str:
    vault = _vault()
    try:
        vault.sync_from_library(design_id)
        branch = vault.branch(name_or_id) or vault.branch_named(design_id, name_or_id)
        return branch["branch_id"] if branch else name_or_id
    finally:
        vault.close()


# ---------------------------------------------------------------------------
# kiln branch
# ---------------------------------------------------------------------------


@click.group("branch")
def branch_group() -> None:
    """Branches of a design: try an idea without touching main (Kiln Pro)."""


@branch_group.command("create")
@click.argument("design_id")
@click.argument("name")
@click.option("--from", "from_branch", default="main", show_default=True, help="Branch to start from.")
@click.option("--base-version-id", default=None, help="Version to start from (instead of --from's head).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_create(design_id: str, name: str, from_branch: str, base_version_id: str | None, json_mode: bool) -> None:
    """Make branch NAME of DESIGN_ID."""
    if not base_version_id:
        vault = _vault()
        try:
            vault.sync_from_library(design_id)
            start = vault.branch(from_branch) or vault.branch_named(design_id, from_branch)
            base_version_id = (start or {}).get("head_version_id")
        finally:
            vault.close()
        if not base_version_id:
            click.echo(f"ERROR: {design_id} has no branch {from_branch} with a version on this computer.", err=True)
            sys.exit(1)
    answer = _served("create_design_branch", design_id=design_id, name=name, base_version_id=base_version_id)
    _finish(answer, json_mode, lambda a: (
        f"Made branch {name} of {design_id}, from {from_branch}. "
        f"Saves of {design_id} on this computer now continue {name}."
    ))


@branch_group.command("list")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_list(design_id: str, json_mode: bool) -> None:
    """List the branches of DESIGN_ID."""
    answer = _served("list_design_branches", design_id=design_id)

    def say(a: dict[str, Any]) -> str:
        rows = a.get("branches") or []
        if not rows:
            return "(no branches)"
        return "\n".join(f"{b.get('name'):30s} head={(b.get('head_version_id') or '-')[:12]}" for b in rows)

    _finish(answer, json_mode, say)


@branch_group.command("history")
@click.argument("design_id")
@click.argument("branch")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_history(design_id: str, branch: str, json_mode: bool) -> None:
    """The versions on BRANCH of DESIGN_ID, newest first."""
    answer = _served("show_branch_history", design_id=design_id, branch_name=branch)

    def say(a: dict[str, Any]) -> str:
        rows = a.get("commits") or []
        return "\n".join(
            f"{c.get('version') or '-':6s} {(c.get('version_id') or '')[:12]}  {c.get('change_summary') or ''}"
            for c in rows
        ) or "(no versions)"

    _finish(answer, json_mode, say)


@branch_group.command("switch")
@click.argument("design_id")
@click.argument("branch")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_switch(design_id: str, branch: str, json_mode: bool) -> None:
    """Continue BRANCH of DESIGN_ID: the next version saved here goes on it."""
    vault = _vault()
    try:
        vault.sync_from_library(design_id)
        found = vault.branch(branch) or vault.branch_named(design_id, branch)
        switched = vault.check_out(design_id, found["branch_id"]) if found else None
    finally:
        vault.close()
    if switched is None:
        answer = {"status": "error", "code": "BRANCH_NOT_FOUND",
                  "error": f"{design_id} has no branch {branch} on this computer."}
    else:
        answer = {"status": "success", "design_id": design_id, "branch": switched}
    _finish(answer, json_mode, lambda a: f"Saves of {design_id} on this computer now continue {a['branch']['name']}.")


# ---------------------------------------------------------------------------
# kiln merge
# ---------------------------------------------------------------------------


@click.group("merge")
def merge_group() -> None:
    """Merge one branch of a design into another (Kiln Pro)."""


def _answers(pairs: tuple[str, ...], take: str | None) -> dict[str, str] | None:
    out: dict[str, str] = {}
    if take:
        out["*"] = {"source": "prefer_branch_a", "target": "prefer_branch_b"}[take]
    for pair in pairs:
        item, _, choice = pair.partition("=")
        if item and choice:
            out[item] = choice
    return out or None


@merge_group.command("check")
@click.argument("design_id")
@click.argument("source")
@click.argument("target", default="main")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def merge_check(design_id: str, source: str, target: str, json_mode: bool) -> None:
    """Whether SOURCE merges into TARGET cleanly, and what to answer if not."""
    answer = _served(
        "detect_merge_conflicts",
        source_branch_id=_branch_id(design_id, source),
        target_branch_id=_branch_id(design_id, target),
    )

    def say(a: dict[str, Any]) -> str:
        lines = [a.get("summary") or ("Merges cleanly." if a.get("ready") else "Questions first.")]
        for q in a.get("questions") or []:
            choices = ", ".join(o.get("choice", "") for o in q.get("options") or [])
            lines.append(f"  {q.get('item_id')}: {q.get('description')} [{choices}]")
        return "\n".join(lines)

    _finish(answer, json_mode, say)


@merge_group.command("run")
@click.argument("design_id")
@click.argument("source")
@click.argument("target", default="main")
@click.option("--answer", "pairs", multiple=True, help="ITEM=CHOICE, from `kiln merge check`.")
@click.option("--take", type=click.Choice(["source", "target"]), default=None,
              help="Answer every question the same way.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def merge_run(design_id: str, source: str, target: str, pairs: tuple[str, ...], take: str | None, json_mode: bool) -> None:
    """Merge SOURCE into TARGET (main by default)."""
    resolutions = _answers(pairs, take)
    kwargs: dict[str, Any] = {
        "source_branch_id": _branch_id(design_id, source),
        "target_branch_id": _branch_id(design_id, target),
    }
    if resolutions:
        kwargs["resolutions"] = resolutions
    answer = _served("merge_design_branches", **kwargs)
    _finish(answer, json_mode, lambda a: f"Merged {source} into {target}: version {a.get('merged_version_id')}.")


# ---------------------------------------------------------------------------
# kiln cloud
# ---------------------------------------------------------------------------


@click.group("cloud")
def cloud_group() -> None:
    """Push a design's history to your Kiln account, or bring it here."""


@cloud_group.command("push")
@click.argument("design_id")
@click.argument("branch_id", required=False)
@click.option("--replace", is_flag=True, help="Free plan: replace the design saved in your account.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_push(design_id: str, branch_id: str | None, replace: bool, json_mode: bool) -> None:
    """Push BRANCH_ID of DESIGN_ID (main by default) to your Kiln account."""
    from kiln.history_courier import push

    branch = _branch_id(design_id, branch_id) if branch_id else None
    answer = push("cloud_push_branch", design_id, branch, replace_confirmed=replace)
    if answer.get("code") == "FREE_SLOT_IN_USE" and not json_mode:
        click.echo(answer.get("error", ""), err=True)
        click.echo("Run again with --replace to replace it.", err=True)
        sys.exit(1)
    _finish(answer, json_mode, lambda a: f"Pushed {design_id} to your Kiln account.")


@cloud_group.command("pull")
@click.argument("design_id")
@click.option("--branch", "branch_name", default=None, help="Branch name (main by default).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_pull(design_id: str, branch_name: str | None, json_mode: bool) -> None:
    """Bring a branch of DESIGN_ID from your Kiln account to this computer."""
    from kiln.history_courier import pull

    answer = pull("cloud_pull_branch", design_id, branch_name=branch_name)
    _finish(answer, json_mode, lambda a: f"Pulled {design_id} from your Kiln account.")


@cloud_group.command("clone")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_clone(design_id: str, json_mode: bool) -> None:
    """Bring all of DESIGN_ID from your Kiln account to this computer (Kiln Pro)."""
    from kiln.history_courier import clone

    answer = clone("cloud_clone_design", design_id)
    _finish(answer, json_mode, lambda a: f"Cloned {design_id} from your Kiln account.")


@cloud_group.command("list-branches")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_list_branches(design_id: str, json_mode: bool) -> None:
    """The branches of DESIGN_ID in your Kiln account."""
    from kiln.history_courier import remote_branches

    answer = remote_branches("cloud_list_remote_branches", design_id)
    _finish(answer, json_mode, lambda a: "\n".join(b.get("name", "") for b in a.get("branches") or []) or "(none)")


def register_history_cli(cli_group: click.Group) -> None:
    """Attach the design-history couriers.  Only for an install without kiln-pro."""
    for group in (branch_group, merge_group, cloud_group):
        if group.name not in cli_group.commands:
            cli_group.add_command(group)


__all__ = ["register_history_cli"]
