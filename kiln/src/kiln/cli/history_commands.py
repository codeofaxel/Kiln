"""``kiln branch / merge / release / cloud / pr`` on a computer without kiln-pro.

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
from collections.abc import Callable
from typing import Any

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
            click.echo(f"ERROR: {_shown(answer.get('error') or 'Kiln could not do that.', lines=True)}", err=True)
        sys.exit(1)
    if not json_mode:
        click.echo(say(answer))
        _say_after(answer)


def _say_after(answer: dict[str, Any]) -> None:
    """What happened after the change was kept here: an automatic push, the offer."""
    auto = answer.get("auto_push")
    if isinstance(auto, dict) and auto.get("said"):
        click.echo(_shown(auto["said"]))
    offer = answer.get("auto_push_offer")
    if isinstance(offer, dict) and offer.get("said"):
        click.echo(f"{_shown(offer['said'])} Turn it on: {_shown(offer.get('turn_on', ''))}")


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


def _with_recipes(said: str, answer: dict[str, Any], *, sent: bool) -> str:
    """*said*, and what became of the design's proven recipes."""
    recipes = answer.get("proven_recipes")
    if not isinstance(recipes, dict):
        return said
    if recipes.get("error"):
        return f"{said}\n{_shown(recipes.get('note', ''))} ({_shown(recipes['error'])})"
    count = recipes.get("sent") if sent else recipes.get("in_account")
    if not count:
        return said
    noun = "proven recipe" if count == 1 else "proven recipes"
    return f"{said}\n{'Sent' if sent else 'Brought'} {count} {noun} with it."


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
        click.echo(_shown(answer.get("error", ""), lines=True), err=True)
        click.echo("Run again with --replace to replace it.", err=True)
        sys.exit(1)
    _finish(answer, json_mode, lambda a: _with_recipes(
        _shown(a["said"]) if a.get("kept_as") else f"Pushed {design_id} to your Kiln account.", a, sent=True,
    ))


@cloud_group.command("auto-push")
@click.argument("state", type=click.Choice(["on", "off", "status"]), default="status")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_auto_push(state: str, json_mode: bool) -> None:
    """Save each design to your Kiln account after every save here (Kiln Pro).

    Off until you turn it on.  Designs locked to this computer
    (`kiln design lock`) are never saved there.
    """
    from kiln.history_courier import auto_push_setting

    answer = auto_push_setting(state)
    _finish(answer, json_mode, lambda a: _shown(a.get("said", "")))


@cloud_group.command("pull")
@click.argument("design_id")
@click.option("--branch", "branch_name", default=None, help="Branch name (main by default).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_pull(design_id: str, branch_name: str | None, json_mode: bool) -> None:
    """Bring a branch of DESIGN_ID from your Kiln account to this computer."""
    from kiln.history_courier import pull

    answer = pull("cloud_pull_branch", design_id, branch_name=branch_name)
    _finish(answer, json_mode, lambda a: _with_recipes(f"Pulled {design_id} from your Kiln account.", a, sent=False))


@cloud_group.command("clone")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_clone(design_id: str, json_mode: bool) -> None:
    """Bring all of DESIGN_ID from your Kiln account to this computer (Kiln Pro)."""
    from kiln.history_courier import clone

    answer = clone("cloud_clone_design", design_id)
    _finish(answer, json_mode, lambda a: _with_recipes(f"Cloned {design_id} from your Kiln account.", a, sent=False))


@cloud_group.command("list-branches")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_list_branches(design_id: str, json_mode: bool) -> None:
    """The branches of DESIGN_ID in your Kiln account."""
    from kiln.history_courier import remote_branches

    answer = remote_branches("cloud_list_remote_branches", design_id)
    _finish(answer, json_mode, lambda a: "\n".join(b.get("name", "") for b in a.get("branches") or []) or "(none)")


@cloud_group.group("release")
def cloud_release_group() -> None:
    """Signed releases in your Kiln account."""


@cloud_release_group.command("push")
@click.argument("release_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_release_push(release_id: str, json_mode: bool) -> None:
    """Push signed release RELEASE_ID from this computer to your Kiln account."""
    from kiln.history_courier import push_release

    answer = push_release("cloud_push_release", release_id)
    _finish(answer, json_mode, lambda a: (
        f"Pushed release {release_id} to your Kiln account; anyone with the "
        f"release can check it at kiln3d.com/verify."
    ))


@cloud_release_group.command("list")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cloud_release_list(design_id: str, json_mode: bool) -> None:
    """The signed releases of DESIGN_ID in your Kiln account."""
    from kiln.history_courier import remote_releases

    answer = remote_releases("cloud_list_remote_releases", design_id)
    _finish(answer, json_mode, lambda a: "\n".join(
        f"{r.get('name', ''):16s} {r.get('release_id', '')}" for r in a.get("releases") or []
    ) or "(none)")


def _library_group(kind: str, word: str, id_name: str) -> click.Group:
    """``kiln cloud feature`` / ``kiln cloud preset``: one group per library kind."""
    group = click.Group(word, help=f"Push a saved {word}'s history to your Kiln account, or bring it here.")

    @group.command("push")
    @click.argument("item_id", metavar=id_name.upper())
    @click.argument("branch_id")
    @click.option("--design-id", default=None, help="The design it belongs to, if any.")
    @click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
    def push_cmd(item_id: str, branch_id: str, design_id: str | None, json_mode: bool) -> None:
        """Push BRANCH_ID of this item from this computer to your Kiln account."""
        from kiln.history_courier import push_library

        answer = push_library(f"cloud_push_{kind[:-1]}_branch", kind, item_id, branch_id, design_id=design_id)
        _finish(answer, json_mode, lambda a: f"Pushed {item_id} to your Kiln account.")

    @group.command("pull")
    @click.argument("item_id", metavar=id_name.upper())
    @click.option("--branch", "branch_name", default=None, help="Branch name (main by default).")
    @click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
    def pull_cmd(item_id: str, branch_name: str | None, json_mode: bool) -> None:
        """Bring a branch of this item from your Kiln account to this computer."""
        from kiln.history_courier import pull_library

        answer = pull_library(f"cloud_pull_{kind[:-1]}_branch", kind, item_id, branch_name=branch_name)
        _finish(answer, json_mode, lambda a: f"Pulled {item_id} from your Kiln account.")

    @group.command("clone")
    @click.argument("item_id", metavar=id_name.upper())
    @click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
    def clone_cmd(item_id: str, json_mode: bool) -> None:
        """Bring all of this item from your Kiln account to this computer."""
        from kiln.history_courier import clone_library

        answer = clone_library(f"cloud_clone_{kind[:-1]}", kind, item_id)
        _finish(answer, json_mode, lambda a: f"Cloned {item_id} from your Kiln account.")

    release = click.Group("release", help=f"Signed releases of a saved {word}.")

    @release.command("push")
    @click.argument("release_id")
    @click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
    def release_push_cmd(release_id: str, json_mode: bool) -> None:
        """Push signed release RELEASE_ID from this computer to your Kiln account."""
        from kiln.history_courier import push_library_release

        answer = push_library_release(f"cloud_push_{kind[:-1]}_release", kind, release_id)
        _finish(answer, json_mode, lambda a: f"Pushed release {release_id} to your Kiln account.")

    group.add_command(release)
    return group


_feature_group = _library_group("features", "feature", "feature_id")
_preset_group = _library_group("presets", "preset", "preset_id")
cloud_group.add_command(_feature_group, name="feature")
cloud_group.add_command(_preset_group, name="preset")
cloud_group.add_command(_preset_group, name="decoration")


# ---------------------------------------------------------------------------
# kiln release
# ---------------------------------------------------------------------------


@click.group("release")
def release_group() -> None:
    """Signed, tamper-evident releases of a design (Kiln Pro)."""


@release_group.command("sign")
@click.argument("design_id")
@click.argument("version_id")
@click.argument("name")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def release_sign(design_id: str, version_id: str, name: str, json_mode: bool) -> None:
    """Sign VERSION_ID of DESIGN_ID as release NAME, with the key Kiln holds for your account."""
    answer = _served("sign_design_release", design_id=design_id, version_id=version_id, name=name)
    _finish(answer, json_mode, lambda a: (
        f"Signed {design_id} {name} ({(a.get('release') or {}).get('release_id', '')}). "
        "Kept on this computer; `kiln cloud release push` puts it in your account."
    ))


@release_group.command("verify")
@click.argument("release_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def release_verify(release_id: str, json_mode: bool) -> None:
    """Check release RELEASE_ID's signature, and that Kiln's key for your account made it."""
    answer = _served("verify_design_release", release_id=release_id)

    def say(a: dict[str, Any]) -> str:
        verdict = "valid" if a.get("valid") else "NOT valid"
        return f"Release {release_id}: {verdict}" + (f" ({a.get('reason')})" if a.get("reason") else "")

    _finish(answer, json_mode, say)
    if isinstance(answer, dict) and answer.get("valid") is False:
        sys.exit(1)


@release_group.command("list")
@click.argument("design_id")
@click.option("--all", "include_retired", is_flag=True, help="Include retired releases.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def release_list(design_id: str, include_retired: bool, json_mode: bool) -> None:
    """The signed releases of DESIGN_ID on this computer."""
    answer = _served("list_design_releases", design_id=design_id, include_retired=include_retired)
    _finish(answer, json_mode, lambda a: "\n".join(
        f"{r.get('name', ''):16s} {r.get('release_id', '')} {r.get('status', '')}"
        for r in a.get("releases") or []
    ) or "(no releases)")


@release_group.command("retire")
@click.argument("release_id")
@click.option("--reason", default="", help="Why it is retired.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def release_retire(release_id: str, reason: str, json_mode: bool) -> None:
    """Retire release RELEASE_ID: kept for the record, its name no longer resolves."""
    answer = _served("retire_design_release", release_id=release_id, reason=reason)
    _finish(answer, json_mode, lambda a: f"Retired release {release_id}.")


# ---------------------------------------------------------------------------
# kiln pr
# ---------------------------------------------------------------------------


def _account_call(tool: str, **kwargs: Any) -> dict[str, Any]:
    from kiln.history_courier import call_account

    return call_account(tool, **kwargs)


def _shown(value: Any, *, lines: bool = False) -> str:
    """*value* as text safe to print: no control characters.

    Pull request titles, descriptions and comments are written by other
    people on the team; printed raw, an escape sequence in one could rewrite
    what this terminal shows.  Newlines and tabs survive only where the text
    is shown as lines of its own.
    """
    keep = "\n\t" if lines else ""
    return "".join(
        ch if ch in keep or not (ord(ch) < 32 or 127 <= ord(ch) < 160) else "?"
        for ch in str(value if value is not None else "")
    )


def _pr_line(pr: dict[str, Any]) -> str:
    return (
        f"{_shown(pr.get('pr_id'))[:8]} {_shown(pr.get('status')):18s} "
        f"{_shown(pr.get('title')):40s} {_shown(pr.get('source_branch_name'))}"
        f" -> {_shown(pr.get('target_branch_name'))}"
    )


@click.group("pr")
def pr_group() -> None:
    """Pull requests: your team reviews a branch before it merges (Kiln Pro)."""


@pr_group.command("submit")
@click.argument("design_id")
@click.argument("source")
@click.argument("target")
@click.option("--title", required=True, help="What the change is.")
@click.option("--description", default="", help="Why, for the reviewers.")
@click.option("--author", "author_user_id", default=None,
              help="Recorded as your signed-in Kiln account; any other name is refused.")
@click.option("--reviewer", "reviewer_user_id", default=None, help="A teammate to ask first.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def pr_submit(design_id: str, source: str, target: str, title: str, description: str,
              author_user_id: str | None, reviewer_user_id: str | None, json_mode: bool) -> None:
    """Open a pull request from branch SOURCE into TARGET of DESIGN_ID.

    The design and both branches must be in your Kiln account (push them
    with `kiln cloud push`); your team reviews it there.
    """
    extra = {"author_user_id": author_user_id} if author_user_id else {}
    answer = _served(
        "propose_design_change", design_id=design_id,
        source_branch_id=_branch_id(design_id, source),
        target_branch_id=_branch_id(design_id, target),
        title=title, description=description, reviewer_user_id=reviewer_user_id, **extra,
    )
    _finish(answer, json_mode, lambda a: (
        f"Opened pull request {str((a.get('pull_request') or {}).get('pr_id') or '')[:8]}: "
        f"{title}. Your team can review it on kiln3d.com or with `kiln pr review`."
    ))


@pr_group.command("review")
@click.argument("pr_id")
@click.option("--decision", default="COMMENT", show_default=True,
              type=click.Choice(["COMMENT", "APPROVE", "REQUEST_CHANGES", "REJECT"]))
@click.option("--body", default="", help="What you think.")
@click.option("--user", "user_id", default=None,
              help="Recorded as your signed-in Kiln account; any other name is refused.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def pr_review(pr_id: str, decision: str, body: str, user_id: str | None, json_mode: bool) -> None:
    """Comment on, approve, ask for changes on, or reject PR_ID."""
    extra = {"user_id": user_id} if user_id else {}
    answer = _account_call("review_design_pull_request", pr_id=pr_id, decision=decision, body=body, **extra)
    _finish(answer, json_mode, lambda a: (
        f"Recorded {decision.lower().replace('_', ' ')} on {_shown(pr_id)[:8]}; it is now "
        f"{_shown((a.get('pull_request') or {}).get('status') or 'open')}."
    ))


@pr_group.command("list")
@click.option("--design-id", default=None, help="Only this design's requests.")
@click.option("--status", default=None, help="open / approved / changes_requested / merged / rejected / closed.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def pr_list(design_id: str | None, status: str | None, json_mode: bool) -> None:
    """Pull requests you opened or your team's, newest first."""
    answer = _account_call("list_design_pull_requests", design_id=design_id, status=status)
    _finish(answer, json_mode, lambda a: (
        "\n".join(_pr_line(p) for p in a.get("pull_requests") or []) or "(no pull requests)"
    ))


@pr_group.command("show")
@click.argument("pr_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def pr_show(pr_id: str, json_mode: bool) -> None:
    """One pull request: what it changes and what its reviewers said."""
    answer = _account_call("get_design_pull_request", pr_id=pr_id)

    def say(a: dict[str, Any]) -> str:
        pr = a.get("pull_request") or {}
        lines = [_pr_line(pr)]
        if pr.get("description"):
            lines.append(_shown(pr["description"], lines=True))
        for c in pr.get("review_comments") or []:
            lines.append(f"  {_shown(c.get('decision') or 'COMMENT')}: {_shown(c.get('body'), lines=True)}")
        return "\n".join(lines)

    _finish(answer, json_mode, say)


# ---------------------------------------------------------------------------
# kiln design lock / unlock
# ---------------------------------------------------------------------------


def _set_lock(design_id: str, locked: bool, json_mode: bool) -> None:
    from kiln.history_courier import set_lock

    answer = set_lock(design_id, locked)
    _finish(answer, json_mode, lambda a: _shown(a.get("said", "")))


@click.command("lock")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def design_lock(design_id: str, json_mode: bool) -> None:
    """Keep DESIGN_ID on this computer: never saved to your Kiln account."""
    _set_lock(design_id, True, json_mode)


@click.command("unlock")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def design_unlock(design_id: str, json_mode: bool) -> None:
    """Let DESIGN_ID be saved to your Kiln account again."""
    _set_lock(design_id, False, json_mode)


@click.group("design")
def design_group() -> None:
    """Designs saved on this computer."""


def register_design_lock_cli(cli_group: click.Group) -> None:
    """Attach ``kiln design lock`` / ``unlock``: onto the ``design`` group the
    CLI already has (kiln-pro's), else onto a group of their own."""
    group = cli_group.commands.get("design")
    if group is None:
        group = design_group
        cli_group.add_command(group)
    if not isinstance(group, click.Group):
        return
    for command in (design_lock, design_unlock):
        if command.name not in group.commands:
            group.add_command(command)


def register_history_cli(cli_group: click.Group) -> None:
    """Attach the design-history couriers.  Only for an install without kiln-pro."""
    for group in (branch_group, merge_group, release_group, cloud_group, pr_group):
        if group.name not in cli_group.commands:
            cli_group.add_command(group)


__all__ = ["register_design_lock_cli", "register_history_cli"]
