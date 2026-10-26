"""``kiln`` commands for Kiln's served tools, on a computer without kiln-pro.

Each command is the terminal's door to a tool Kiln's servers run: it calls
the same function an agent on this install calls for that tool
(:func:`kiln.server.served_tool`), so a design's history goes up with the
call and what changed comes back to this computer, a file the call names is
sent, and a miss (offline, signed out, no answer, refused) is said in the
same words.  The servers decide what the person's plan includes and say so;
nothing here decides it.

Registered only when kiln-pro is not installed (``kiln.cli.main``): with it,
kiln-pro's own commands of the same names run on this computer instead.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from kiln.cli.history_commands import _finish, _shown

_ARTIFACT_TYPES = click.Choice(["design", "preset", "feature"])


def _ask(tool: str, **kwargs: Any) -> dict[str, Any]:
    """Call the served tool *tool* the way an agent on this install would.

    An argument left as ``None`` is not sent, so the tool's own default
    applies, as it does when an agent leaves it out.
    """
    from kiln.server import served_tool

    stub = served_tool(tool)
    if stub is None:
        return {
            "status": "error",
            "success": False,
            "code": "NOT_SERVED",
            "error": f"This version of Kiln does not list {tool}. Update Kiln (`kiln self-update`) and try again.",
        }
    return stub(**{k: v for k, v in kwargs.items() if v is not None})


def _say_summary(answer: dict[str, Any]) -> str:
    """The answer's own sentence, else a short dump of what came back."""
    for key in ("summary", "message", "note"):
        value = answer.get(key)
        if isinstance(value, str) and value.strip():
            return _shown(value, lines=True)
    shown = {k: v for k, v in answer.items() if k not in ("status", "success", "history_delta", "history_saved")}
    return json.dumps(shown, indent=2, default=str)


def _say_branch(verb: str) -> Callable[[dict[str, Any]], str]:
    def say(answer: dict[str, Any]) -> str:
        branch = answer.get("branch") if isinstance(answer.get("branch"), dict) else answer
        return f"{verb} branch {_shown(branch.get('name') or branch.get('branch_id') or '')}."

    return say


def _say_diagram(raw: bool) -> Callable[[dict[str, Any]], str]:
    def say(answer: dict[str, Any]) -> str:
        preview = answer.get("preview") if isinstance(answer.get("preview"), dict) else {}
        source = str(preview.get("source") or answer.get("mermaid") or "")
        if not source:
            return _say_summary(answer)
        return source if raw else f"```mermaid\n{source}\n```"

    return say


# ---------------------------------------------------------------------------
# kiln branch annotate / rename / retire
# ---------------------------------------------------------------------------


@click.command("annotate")
@click.argument("branch_id")
@click.argument("annotation")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_annotate(branch_id: str, annotation: str, json_mode: bool) -> None:
    """Say what BRANCH_ID is trying out."""
    _finish(_ask("annotate_design_branch", branch_id=branch_id, annotation=annotation),
            json_mode, _say_branch("Annotated"))


@click.command("rename")
@click.argument("branch_id")
@click.argument("new_name")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_rename(branch_id: str, new_name: str, json_mode: bool) -> None:
    """Rename BRANCH_ID to NEW_NAME."""
    _finish(_ask("rename_design_branch", branch_id=branch_id, new_name=new_name),
            json_mode, _say_branch("Renamed to"))


@click.command("retire")
@click.argument("branch_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def branch_retire(branch_id: str, json_mode: bool) -> None:
    """Retire BRANCH_ID.  Its versions are kept."""
    _finish(_ask("retire_design_branch", branch_id=branch_id), json_mode, _say_branch("Retired"))


# ---------------------------------------------------------------------------
# kiln release yank (the name kiln-pro's command line uses for retire)
# ---------------------------------------------------------------------------


@click.command("yank")
@click.argument("release_id")
@click.option("--reason", default="", help="Why the release was retired.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def release_yank(release_id: str, reason: str, json_mode: bool) -> None:
    """Retire RELEASE_ID (the same as `kiln release retire`).  The record is kept."""
    _finish(_ask("retire_design_release", release_id=release_id, reason=reason),
            json_mode, lambda a: f"Retired release {_shown(release_id)}.")


# ---------------------------------------------------------------------------
# kiln merge source
# ---------------------------------------------------------------------------


@click.command("source")
@click.option("--base", "base_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="The OpenSCAD source both branches started from.")
@click.option("--left", "left_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="One branch's OpenSCAD source.")
@click.option("--right", "right_path", required=True, type=click.Path(exists=True, dir_okay=False),
              help="The other branch's OpenSCAD source.")
@click.option("--show-base/--no-show-base", default=False,
              help="Show the starting text inside each conflict too.")
def merge_source(base_path: str, left_path: str, right_path: str, show_base: bool) -> None:
    """Merge two edits of one OpenSCAD file.

    Writes the merged source to stdout, with <<<<<<< / ======= / >>>>>>>
    around anything both sides changed.  Exits 1 when there is a conflict.
    """
    answer = _ask(
        "source_merge_three_way",
        base_source=Path(base_path).read_text(encoding="utf-8"),
        left_source=Path(left_path).read_text(encoding="utf-8"),
        right_source=Path(right_path).read_text(encoding="utf-8"),
        show_base_in_markers=show_base,
    )
    if not isinstance(answer, dict) or answer.get("status") == "error" or answer.get("success") is False:
        _finish(answer, False, _say_summary)
        return
    sys.stdout.write(str(answer.get("merged_text") or ""))
    if answer.get("had_conflicts"):
        click.echo("(merge has unresolved conflicts)", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# kiln cherrypick
# ---------------------------------------------------------------------------


@click.group("cherrypick")
def cherrypick_group() -> None:
    """Carry one change from a version onto another (Kiln Pro)."""


@cherrypick_group.command("list")
@click.argument("ancestor_version_id")
@click.argument("source_version_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cherrypick_list(ancestor_version_id: str, source_version_id: str, json_mode: bool) -> None:
    """The changes SOURCE made relative to ANCESTOR that can be carried across."""
    answer = _ask("list_cherry_pick_candidates",
                  ancestor_version_id=ancestor_version_id, source_version_id=source_version_id)

    def say(a: dict[str, Any]) -> str:
        rows = a.get("modifications") or a.get("candidates") or []
        return "\n".join(
            f"  {_shown(m.get('modification_kind')):14s} {_shown(m.get('feature_id'))}" for m in rows
        ) or "(no changes that can be carried across)"

    _finish(answer, json_mode, say)


@cherrypick_group.command("apply")
@click.argument("ancestor_version_id")
@click.argument("source_version_id")
@click.argument("target_version_id")
@click.argument("feature_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def cherrypick_apply(ancestor_version_id: str, source_version_id: str, target_version_id: str,
                     feature_id: str, json_mode: bool) -> None:
    """Carry FEATURE_ID from SOURCE onto TARGET (relative to ANCESTOR)."""
    _finish(
        _ask("cherry_pick_modification", ancestor_version_id=ancestor_version_id,
             source_version_id=source_version_id, target_version_id=target_version_id,
             feature_id=feature_id),
        json_mode, lambda a: f"Carried {_shown(feature_id)} onto {_shown(target_version_id)}.",
    )


# ---------------------------------------------------------------------------
# The history views
# ---------------------------------------------------------------------------


@click.command("visualize-branch-tree")
@click.option("--artifact-id", required=True, help="Design, preset, or feature id.")
@click.option("--artifact-type", type=_ARTIFACT_TYPES, default="design", show_default=True)
@click.option("--detail", type=click.Choice(["simple", "rich", "timeline"]), default="rich", show_default=True,
              help="rich shows each change, what moved and how it printed; simple only the change.")
@click.option("--max-commits", default=50, type=int, show_default=True)
@click.option("--raw", is_flag=True, help="Only the diagram source, without the ```mermaid fences.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def visualize_branch_tree(artifact_id: str, artifact_type: str, detail: str, max_commits: int,
                          raw: bool, json_mode: bool) -> None:
    """Draw a design's branches as a Mermaid diagram."""
    _finish(_ask("visualize_branch_tree", artifact_id=artifact_id, artifact_type=artifact_type,
                 detail=detail, max_commits=max_commits), json_mode, _say_diagram(raw))


@click.command("design-lineage")
@click.option("--artifact-id", required=True, help="Design id.")
@click.option("--no-outcomes", is_flag=True, help="Leave out how each version printed.")
@click.option("--max-commits", default=50, type=int, show_default=True)
@click.option("--raw", is_flag=True, help="Only the diagram source, without the ```mermaid fences.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def design_lineage(artifact_id: str, no_outcomes: bool, max_commits: int, raw: bool, json_mode: bool) -> None:
    """A design's versions and where each came from, with how each printed."""
    _finish(_ask("show_design_lineage", artifact_id=artifact_id, annotate_outcomes=not no_outcomes,
                 max_commits=max_commits), json_mode, _say_diagram(raw))


@click.command("compare-branches")
@click.option("--artifact-id", required=True, help="Design, preset, or feature id.")
@click.option("--branch-a", required=True, help="One branch's name.")
@click.option("--branch-b", required=True, help="The other branch's name.")
@click.option("--artifact-type", type=_ARTIFACT_TYPES, default="design", show_default=True)
@click.option("--raw", is_flag=True, help="Only the diagram source, without the ```mermaid fences.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def compare_branches(artifact_id: str, branch_a: str, branch_b: str, artifact_type: str,
                     raw: bool, json_mode: bool) -> None:
    """Two branches side by side, before you merge one into the other."""
    _finish(_ask("compare_branches", artifact_id=artifact_id, branch_a=branch_a, branch_b=branch_b,
                 artifact_type=artifact_type), json_mode, _say_diagram(raw))


@click.command("show-branch-history")
@click.option("--design-id", required=True, help="Design id.")
@click.option("--branch", "branch_name", required=True, help="Branch name.")
@click.option("--limit", default=50, type=int, show_default=True, help="The newest this many; 0 for all.")
@click.option("--no-outcomes", is_flag=True, help="Leave out how each version printed.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def show_branch_history(design_id: str, branch_name: str, limit: int, no_outcomes: bool, json_mode: bool) -> None:
    """The versions on one branch, newest first (the same as `kiln branch history`)."""
    answer = _ask("show_branch_history", design_id=design_id, branch_name=branch_name, limit=limit,
                  annotate_outcomes=not no_outcomes)

    def say(a: dict[str, Any]) -> str:
        rows = a.get("commits") or []
        return "\n".join(
            f"{_shown(c.get('version') or '-'):6s} {_shown(c.get('version_id'))[:12]}  "
            f"{_shown(c.get('change_summary'))}"
            for c in rows
        ) or "(no versions)"

    _finish(answer, json_mode, say)


# ---------------------------------------------------------------------------
# kiln reflog
# ---------------------------------------------------------------------------


@click.group("reflog")
def reflog_group() -> None:
    """Every time a branch's head moved, and the way back."""


@reflog_group.command("show")
@click.option("--artifact-type", type=_ARTIFACT_TYPES, default=None)
@click.option("--artifact-id", default=None)
@click.option("--branch-id", default=None)
@click.option("--limit", default=100, type=int, show_default=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def reflog_show(artifact_type: str | None, artifact_id: str | None, branch_id: str | None,
                limit: int, json_mode: bool) -> None:
    """Head moves, newest first."""
    answer = _ask("show_reflog", artifact_type=artifact_type, artifact_id=artifact_id,
                  branch_id=branch_id, limit=limit)

    def say(a: dict[str, Any]) -> str:
        rows = a.get("entries") or []
        return "\n".join(
            f"{_shown(e.get('created_at') or e.get('timestamp') or '')[:19]}  "
            f"{_shown(e.get('operation')):14s} {_shown(e.get('branch_name') or e.get('branch_id'))}"
            for e in rows
        ) or "(no head moves)"

    _finish(answer, json_mode, say)


@reflog_group.command("undo-last")
@click.option("--artifact-type", type=_ARTIFACT_TYPES, required=True)
@click.option("--branch-id", required=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def reflog_undo(artifact_type: str, branch_id: str, json_mode: bool) -> None:
    """Move BRANCH back to where it was before its last head move."""
    _finish(_ask("undo_last_head_change", artifact_type=artifact_type, branch_id=branch_id),
            json_mode, _say_summary)


# ---------------------------------------------------------------------------
# kiln bisect
# ---------------------------------------------------------------------------


@click.group("bisect")
def bisect_group() -> None:
    """Find the version where something went wrong (Kiln Pro)."""


@bisect_group.command("start")
@click.option("--artifact-type", type=_ARTIFACT_TYPES, required=True)
@click.option("--artifact-id", required=True)
@click.option("--good", "good_version_id", required=True, help="A version that was right.")
@click.option("--bad", "bad_version_id", required=True, help="A later version that is wrong.")
@click.option("--check", "target_check", default=None,
              help="Designs only: name a spec check ('cradle slot') and each version is judged by "
                   "what it was measured to be, instead of asking you.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def bisect_start(artifact_type: str, artifact_id: str, good_version_id: str, bad_version_id: str,
                 target_check: str | None, json_mode: bool) -> None:
    """Start a search between a good version and a bad one."""
    _finish(_ask("bisect_start", artifact_type=artifact_type, artifact_id=artifact_id,
                 good_version_id=good_version_id, bad_version_id=bad_version_id,
                 target_check=target_check), json_mode, _say_summary)


def _bisect_mark(tool: str, session_id: str, version_id: str | None, json_mode: bool) -> None:
    _finish(_ask(tool, session_id=session_id, version_id=version_id), json_mode, _say_summary)


@bisect_group.command("good")
@click.option("--session-id", required=True)
@click.option("--version-id", default=None, help="Defaults to the version the search is on.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def bisect_good(session_id: str, version_id: str | None, json_mode: bool) -> None:
    """The version is right."""
    _bisect_mark("bisect_good", session_id, version_id, json_mode)


@bisect_group.command("bad")
@click.option("--session-id", required=True)
@click.option("--version-id", default=None, help="Defaults to the version the search is on.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def bisect_bad(session_id: str, version_id: str | None, json_mode: bool) -> None:
    """The version is wrong."""
    _bisect_mark("bisect_bad", session_id, version_id, json_mode)


@bisect_group.command("skip")
@click.option("--session-id", required=True)
@click.option("--version-id", default=None, help="Defaults to the version the search is on.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def bisect_skip(session_id: str, version_id: str | None, json_mode: bool) -> None:
    """The version can't be judged; try another."""
    _bisect_mark("bisect_skip", session_id, version_id, json_mode)


@bisect_group.command("current")
@click.option("--session-id", required=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def bisect_current(session_id: str, json_mode: bool) -> None:
    """Where the search is."""
    _finish(_ask("bisect_current", session_id=session_id), json_mode, _say_summary)


@bisect_group.command("end")
@click.option("--session-id", required=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def bisect_end(session_id: str, json_mode: bool) -> None:
    """End the search."""
    _finish(_ask("bisect_end", session_id=session_id), json_mode, _say_summary)


# ---------------------------------------------------------------------------
# kiln recipes / kiln versions best / kiln visual-diff
# ---------------------------------------------------------------------------


@click.group("recipes")
def recipes_group() -> None:
    """The settings that printed a decoration well, kept to use again (Kiln Pro)."""


@recipes_group.command("list")
@click.option("--design-id", default=None, help="Only this design's recipes.")
@click.option("--limit", default=50, type=int, show_default=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def recipes_list(design_id: str | None, limit: int, json_mode: bool) -> None:
    """Every proven recipe on this computer."""
    answer = _ask("list_proven_recipes", design_id=design_id, limit=limit)

    def say(a: dict[str, Any]) -> str:
        rows = a.get("recipes") or []
        if not rows:
            return "No proven recipes yet. Your AI agent records one with record_proven_recipe after a good print."
        return "\n".join(
            f"  {_shown(r.get('design_id'))} — {_shown(r.get('decoration_type'))} on {_shown(r.get('product_type'))}"
            for r in rows
        )

    _finish(answer, json_mode, say)


@click.command("best")
@click.argument("version_ids", nargs=-1, required=True)
@click.option("--material", default=None, help="Only prints in this material (e.g. PETG).")
@click.option("--printer", "printer_id", default=None, help="Only prints on this printer.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def versions_best(version_ids: tuple[str, ...], material: str | None, printer_id: str | None,
                  json_mode: bool) -> None:
    """Which of these versions printed best, from your recorded prints (Kiln Pro)."""
    answer = _ask("best_design_version", version_ids=list(version_ids), material=material,
                  printer_id=printer_id)

    def say(a: dict[str, Any]) -> str:
        best = a.get("best_version_id") or a.get("version_id")
        if not best:
            return _say_summary(a)
        rate = a.get("success_rate")
        count = a.get("outcome_count") or 0
        said = f"{rate * 100:.0f}% printed well" if isinstance(rate, (int, float)) else "no rate yet"
        return f"Best: {_shown(best)[:12]} — {said}, across {count} print(s)."

    _finish(answer, json_mode, say)


@click.command("visual-diff")
@click.option("--before", "before_mesh_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--after", "after_mesh_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--angle", "angles", multiple=True, default=("iso",), show_default=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def visual_diff(before_mesh_path: str, after_mesh_path: str, angles: tuple[str, ...], json_mode: bool) -> None:
    """One picture of what changed between two models: red removed, green added (Kiln Pro)."""
    _finish(_ask("visual_diff_meshes", before_mesh_path=before_mesh_path, after_mesh_path=after_mesh_path,
                 angles=list(angles)), json_mode, _say_summary)


@click.group("ab")
def ab_group() -> None:
    """Which branch of a design printed better, from your recorded prints (Kiln Pro)."""


@ab_group.command("report")
@click.argument("design_id")
@click.option("--min-outcomes", "min_outcomes", default=3, type=int, show_default=True,
              help="Leave out branches with fewer recorded prints than this.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def ab_report(design_id: str, min_outcomes: int, json_mode: bool) -> None:
    """Rank what each branch of DESIGN_ID changed by how its prints came out."""
    _finish(_ask("cross_branch_ab_report", design_id=design_id, min_outcomes_per_branch=min_outcomes),
            json_mode, _say_summary)


@click.group("creator")
def creator_group() -> None:
    """The creator credit and licence Kiln puts on your signed files."""


@creator_group.command("show")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def creator_show(json_mode: bool) -> None:
    """Your creator credit settings."""
    _finish(_ask("get_creator_credit"), json_mode, lambda a: json.dumps({"profile": a.get("profile")}, indent=2))


@creator_group.command("set")
@click.option("--handle", required=True, help="Your Kiln username (pass your current one to keep it).")
@click.option("--show/--hide", "show_on_artifacts", default=None, help="Show the credit on your files.")
@click.option("--link", "links", multiple=True, help="Replace your links with LABEL=HTTPS_URL. Repeat for more.")
@click.option("--clear-links", is_flag=True, help="Remove every saved link.")
@click.option("--license-name", default=None)
@click.option("--license-url", default=None)
@click.option("--license-text", default=None)
@click.option("--clear-license", is_flag=True, help="Remove the saved licence terms.")
@click.option("--license-on/--license-off", default=None, help="Show the licence on your files.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def creator_set(handle: str, show_on_artifacts: bool | None, links: tuple[str, ...], clear_links: bool,
                license_name: str | None, license_url: str | None, license_text: str | None,
                clear_license: bool, license_on: bool | None, json_mode: bool) -> None:
    """Save the creator credit for your future signed files (Kiln Pro).

    Anything left out stays as it is.
    """
    parsed: list[dict[str, str]] | None = None
    if clear_links:
        parsed = []
    elif links:
        parsed = []
        for item in links:
            label, separator, url = item.partition("=")
            if not separator:
                raise click.UsageError("Each --link must look like LABEL=HTTPS_URL.")
            parsed.append({"label": label, "url": url})
    licence: dict[str, Any] | None = None
    if clear_license:
        licence = {}
    elif license_name or license_url or license_text:
        licence = {k: v for k, v in (("name", license_name), ("terms_url", license_url),
                                     ("terms_text", license_text)) if v}
    _finish(
        _ask("set_creator_credit", handle=handle, show_on_artifacts=show_on_artifacts, links=parsed,
             default_license=licence, show_license_on_artifacts=license_on),
        json_mode, lambda a: json.dumps({"profile": a.get("profile")}, indent=2),
    )


def _say_archive(answer: dict[str, Any]) -> str:
    verb = "Archived" if answer.get("archived") else "Unarchived"
    note = answer.get("note")
    return _shown(note) if note else f"{verb} {_shown(answer.get('design_id'))}: every version is kept."


@click.command("archive")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def design_archive(design_id: str, json_mode: bool) -> None:
    """Hide DESIGN_ID from your lists, in your Kiln account and on this computer.  Nothing is deleted."""
    _finish(_ask("archive_design", design_id=design_id), json_mode, _say_archive)


@click.command("unarchive")
@click.argument("design_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def design_unarchive(design_id: str, json_mode: bool) -> None:
    """Bring an archived DESIGN_ID back to your lists."""
    _finish(_ask("unarchive_design", design_id=design_id), json_mode, _say_archive)


# ---------------------------------------------------------------------------
# kiln manual (the history of a design's assembly manuals)
# ---------------------------------------------------------------------------


@click.group("manual")
def manual_group() -> None:
    """A design's assembly manuals, kept in your Kiln account (Kiln Business)."""


def _verify_file(path: str, *, release_id: str | None = None, design_id: str | None = None,
                 release_name: str | None = None) -> dict[str, Any]:
    """Ask Kiln's public verifier about one file.

    The same check the page a manual's QR code opens makes, anonymous by
    design: whether a file matches a signed release is a fact about a
    release its maker published.  The file is checked and not kept.
    """
    import httpx

    from kiln.auth_session import _api_base
    from kiln.served_answer import envelope_for_http, envelope_for_transport

    tool = "verify_manual_authenticity"
    fields = {k: v for k, v in (("release_id", release_id), ("design_id", design_id),
                                ("release_name", release_name)) if v}
    try:
        with open(path, "rb") as handle:
            resp = httpx.post(f"{_api_base()}/api/public/verify", files={"file": (Path(path).name, handle)},
                              data=fields, timeout=120.0)
    except OSError as exc:
        return {"status": "error", "success": False, "code": "PDF_UNREADABLE",
                "error": f"Could not read {path}: {exc.strerror or exc}"}
    except Exception as exc:  # noqa: BLE001 — no answer is worded, never a yes
        return envelope_for_transport(tool, exc, host=_api_base(), kind="verdict")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    if resp.status_code >= 400:
        answer = dict(envelope_for_http(tool, resp.status_code, body, kind="verdict"))
        answer.setdefault("status", "error")
        answer["success"] = False
        return answer
    return body if isinstance(body, dict) else {}


def _verdict(answer: dict[str, Any]) -> dict[str, Any]:
    return answer.get("data") if isinstance(answer.get("data"), dict) else answer


@manual_group.command("verify")
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--release-id", default=None, help="The release to check against.")
@click.option("--design-id", default=None, help="The design (with --release-name).")
@click.option("--release-name", default=None, help="The release's name (with --design-id).")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manual_verify(pdf_path: str, release_id: str | None, design_id: str | None, release_name: str | None,
                  json_mode: bool) -> None:
    """Is this assembly manual (a PDF or 3MF) the one its maker signed?

    Exit 0 when it is, 1 when it is not or could not be checked.
    """
    answer = _verify_file(pdf_path, release_id=release_id, design_id=design_id, release_name=release_name)
    verdict = _verdict(answer)
    if answer.get("status") == "error" or answer.get("success") is False:
        _finish(answer, json_mode, _say_summary)
        return
    if json_mode:
        click.echo(json.dumps(answer, indent=2, default=str))
    elif verdict.get("authentic"):
        click.echo(f"Authentic: signed by key {_shown(verdict.get('public_key_fingerprint'))}, "
                   f"{_shown(verdict.get('design_id'))}/{_shown(verdict.get('release_name'))}.")
    else:
        click.echo(f"NOT authentic: {_shown(verdict.get('reason') or 'it does not match the signed release')}",
                   err=True)
    if not verdict.get("authentic"):
        sys.exit(1)


@manual_group.command("verify-batch")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--release-id", default=None, help="Check every file against this one release.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manual_verify_batch(paths: tuple[str, ...], release_id: str | None, json_mode: bool) -> None:
    """Check many manuals at once.  Exit 0 only when every one is authentic."""
    results = []
    for path in paths:
        answer = _verify_file(path, release_id=release_id)
        verdict = _verdict(answer)
        failed = answer.get("status") == "error" or answer.get("success") is False
        results.append({"path": path, "authentic": bool(verdict.get("authentic")) and not failed,
                        "error": answer.get("error") if failed else None,
                        "reason": None if failed else verdict.get("reason")})
    authentic = sum(1 for r in results if r["authentic"])
    if json_mode:
        click.echo(json.dumps({"results": results, "authentic": authentic, "total": len(results)}, indent=2))
    else:
        for r in results:
            mark = "authentic " if r["authentic"] else ("error     " if r["error"] else "mismatch  ")
            why = r["error"] or r["reason"] or ""
            click.echo(f"{mark}{_shown(r['path'])}" + (f"  ·  {_shown(why)}" if why and not r["authentic"] else ""))
        click.echo(f"{authentic} / {len(results)} authentic.")
    if authentic != len(results):
        sys.exit(1)


@manual_group.command("list-history")
@click.argument("design_id")
@click.option("--include-retired", is_flag=True)
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manual_list_history(design_id: str, include_retired: bool, json_mode: bool) -> None:
    """Every manual made for DESIGN_ID, oldest first."""
    answer = _ask("list_manual_history", design_id=design_id, include_retired=include_retired)

    def say(a: dict[str, Any]) -> str:
        data = a.get("data") if isinstance(a.get("data"), dict) else a
        rows = data.get("revisions") or []
        if not rows:
            return f"No manuals for {_shown(design_id)}."
        return "\n".join(
            f"{_shown(r.get('manual_id'))[:8]}  {_shown(r.get('generated_at'))[:19]}  "
            f"v{_shown(r.get('version_id'))[:8]}  {'[retired]' if r.get('retired') else ''}"
            for r in rows
        )

    _finish(answer, json_mode, say)


@manual_group.command("get-revision")
@click.argument("manual_id")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manual_get_revision(manual_id: str, json_mode: bool) -> None:
    """One manual revision."""
    _finish(_ask("get_manual_revision", manual_id=manual_id), json_mode, _say_summary)


@manual_group.command("diff")
@click.option("--old", "old_id", default=None, help="The older revision's manual_id.")
@click.option("--new", "new_id", default=None, help="The newer revision's manual_id.")
@click.option("--spec-old", "spec_old_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="The older manual's spec.json, instead of --old.")
@click.option("--spec-new", "spec_new_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="The newer manual's spec.json, instead of --new.")
@click.option("--assembly-old", "assembly_old_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="The older assembly JSON (needed for the PDF).")
@click.option("--assembly-new", "assembly_new_path", default=None, type=click.Path(exists=True, dir_okay=False),
              help="The newer assembly JSON (needed for the PDF).")
@click.option("--out", "output_pdf_path", default=None, type=click.Path(dir_okay=False),
              help="Where to save the side-by-side PDF; leave out for the summary only.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manual_diff(old_id: str | None, new_id: str | None, spec_old_path: str | None, spec_new_path: str | None,
                assembly_old_path: str | None, assembly_new_path: str | None, output_pdf_path: str | None,
                json_mode: bool) -> None:
    """What changed between two manual revisions."""
    answer = _ask(
        "diff_assembly_manuals", manual_id_old=old_id, manual_id_new=new_id,
        spec_old_path=spec_old_path, spec_new_path=spec_new_path,
        assembly_old_json=Path(assembly_old_path).read_text(encoding="utf-8") if assembly_old_path else None,
        assembly_new_json=Path(assembly_new_path).read_text(encoding="utf-8") if assembly_new_path else None,
        output_pdf_path=output_pdf_path,
    )

    def say(a: dict[str, Any]) -> str:
        data = a.get("data") if isinstance(a.get("data"), dict) else a
        counts = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        lines = [f"{label:10s} {counts.get(key, 0)}" for label, key in (
            ("changed:", "changed_count"), ("added:", "added_count"),
            ("removed:", "removed_count"), ("unchanged:", "unchanged_count"),
        )]
        if data.get("pdf_path"):
            lines.append(f"pdf:       {_shown(data['pdf_path'])}")
        return "\n".join(lines)

    _finish(answer, json_mode, say)


@manual_group.command("retire")
@click.argument("manual_id")
@click.option("--reason", required=True, help="Why this revision is retired.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manual_retire(manual_id: str, reason: str, json_mode: bool) -> None:
    """Retire a manual revision.  The record is kept."""
    _finish(_ask("retire_manual_revision", manual_id=manual_id, reason=reason),
            json_mode, lambda a: f"Retired {_shown(manual_id)[:8]}: {_shown(reason)}")


# ---------------------------------------------------------------------------
# kiln subscribe / kiln manage-billing
# ---------------------------------------------------------------------------

#: Where a person chooses, changes or cancels a plan and updates a card: the
#: Billing page of Kiln's web app, signed in to their own account.  Choosing a
#: plan is never done from a terminal or by an agent.
BILLING_PAGE = "https://app.kiln3d.com/settings/billing"


def _open_page(url: str, said: str, no_open: bool, json_mode: bool) -> None:
    if json_mode:
        click.echo(json.dumps({"status": "success", "url": url}))
        return
    click.echo(said)
    click.echo(url)
    if no_open:
        return
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — the address is printed above
        pass


@click.command("subscribe")
@click.option("--no-open", is_flag=True, help="Print the address instead of opening your browser.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def subscribe(no_open: bool, json_mode: bool) -> None:
    """Choose a Kiln plan: opens Billing in Kiln's web app, signed in to your account."""
    _open_page(BILLING_PAGE, "Choose and confirm a plan on the Billing page of your Kiln account:", no_open, json_mode)


@click.command("manage-billing")
@click.option("--no-open", is_flag=True, help="Print the address instead of opening your browser.")
@click.option("--json", "json_mode", is_flag=True, help="Output JSON.")
def manage_billing(no_open: bool, json_mode: bool) -> None:
    """Change or cancel your plan, update your card, see invoices: opens Billing in Kiln's web app."""
    _open_page(BILLING_PAGE, "Your plan, card and invoices are on the Billing page of your Kiln account:",
               no_open, json_mode)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

#: Commands added to a group a plain install already has, by group name.
_INTO_GROUPS: dict[str, tuple[click.Command, ...]] = {
    "branch": (branch_annotate, branch_rename, branch_retire),
    "release": (release_yank,),
    "merge": (merge_source,),
    "versions": (versions_best,),
    "design": (design_archive, design_unarchive),
}

_TOP_LEVEL: tuple[click.Command, ...] = (
    cherrypick_group, reflog_group, bisect_group, recipes_group, manual_group, ab_group, creator_group,
    visualize_branch_tree, design_lineage, compare_branches, show_branch_history, visual_diff,
    subscribe, manage_billing,
)


def register_served_cli(cli_group: click.Group) -> None:
    """Attach the served-tool commands.  Only for an install without kiln-pro.

    Runs after the design-history couriers, whose groups it adds to, and
    before ``kiln design lock``, which joins the ``design`` group made here.
    """
    if "design" not in cli_group.commands:
        from kiln.cli.history_commands import design_group

        cli_group.add_command(design_group)
    for group_name, commands in _INTO_GROUPS.items():
        group = cli_group.commands.get(group_name)
        if not isinstance(group, click.Group):
            continue
        for command in commands:
            if command.name not in group.commands:
                group.add_command(command)
    for command in _TOP_LEVEL:
        if command.name not in cli_group.commands:
            cli_group.add_command(command)


__all__ = ["register_served_cli"]
