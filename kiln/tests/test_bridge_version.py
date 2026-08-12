"""The bridge's version verdict — and the promise that it never installs.

Two halves.  The first walks the comparison: a daemon holding older code than
the machine it runs on is the fact nothing else could see, and the one worth
getting exactly right in both directions (never claim it wrongly, never miss
it).  The second is structural — :mod:`kiln.bridge_version` reports and does
not upgrade, and that stays true only if something checks.
"""
from __future__ import annotations

import ast
from pathlib import Path

from kiln.bridge_version import (
    CURRENT,
    RESTART_PENDING,
    UPDATE_AVAILABLE,
    describe,
)

# --- the comparison --------------------------------------------------------


def test_nothing_to_say_when_everything_agrees():
    v = describe(running="1.3.2", installed="1.3.2", latest="1.3.2")
    assert v.state == CURRENT
    # Empty exactly when there is no news, so callers render unconditionally.
    assert v.lines == ()


def test_a_daemon_older_than_the_install_asks_only_for_a_restart():
    """The gap nothing else could see: launchd restarts a bridge that dies,
    never one that is merely old, so a pip upgrade leaves the daemon serving
    the code it booted with — connected, answering, and behind."""
    v = describe(running="1.2.0", installed="1.3.2", latest="1.3.2")
    assert v.state == RESTART_PENDING
    body = " ".join(v.lines)
    assert "1.2.0" in body and "1.3.2" in body
    assert "kiln bridge restart" in body
    # It is already downloaded; do not send anyone to pip for it.
    assert "pip install" not in body


def test_a_newer_release_on_pypi_asks_for_both_steps():
    v = describe(running="1.3.2", installed="1.3.2", latest="1.4.0")
    assert v.state == UPDATE_AVAILABLE
    body = " ".join(v.lines)
    assert "1.4.0" in body
    # Installing alone never reaches the running daemon, so the restart is
    # not a footnote — it is half the instruction, and still one paste.
    assert "pip install --upgrade kiln3d && kiln bridge restart" in body


def test_behind_on_both_counts_names_both():
    """running < installed < latest.  A restart alone would land on 1.2.5,
    which is still not current — so the report has to say all three."""
    v = describe(running="1.2.0", installed="1.2.5", latest="1.4.0")
    assert v.state == UPDATE_AVAILABLE
    body = " ".join(v.lines)
    assert "1.2.0" in body, "the version actually serving prints went unmentioned"
    assert "1.2.5" in body, "the already-installed version went unmentioned"
    assert "1.4.0" in body


def test_restart_pending_survives_having_no_network():
    """The sharpest signal is the offline one.  A cold cache, or a user who
    set KILN_NO_UPDATE_CHECK, still gets the fact that needs no PyPI."""
    v = describe(running="1.2.0", installed="1.3.2", latest=None)
    assert v.state == RESTART_PENDING


def test_the_advice_is_one_verb_and_never_a_command_pair():
    """This used to branch: `disable && enable` for a login-managed bridge,
    `stop && start` for a session one — two commands to do one thing, and the
    wrong pair does nothing at all.  The verb exists now, so the branch is
    gone and there is nothing left to pick wrong."""
    for state in (
        describe(running="1.2.0", installed="1.3.2"),
        describe(running="1.2.0", installed="1.2.5", latest="1.4.0"),
        describe(running="1.3.2", installed="1.3.2", latest="1.4.0"),
    ):
        body = " ".join(state.lines)
        assert "kiln bridge restart" in body
        for pair in ("disable && kiln bridge enable", "stop && kiln bridge start"):
            assert pair not in body, f"advice regrew a command pair: {pair}"


def test_a_daemon_that_reports_no_version_is_not_guessed_about():
    """Every bridge running right now predates the state-file version field.
    An honest silence beats inventing which of two numbers is newer."""
    for missing in (None, "", "   "):
        v = describe(running=missing, installed="1.3.2", latest="1.3.2")
        assert v.state == CURRENT, f"invented a verdict from running={missing!r}"


def test_an_unreadable_version_is_no_news_rather_than_a_wrong_verdict():
    assert describe(running="who-knows", installed="1.3.2").state == CURRENT
    assert describe(running="1.2.0", installed="not-a-version").state == CURRENT


def test_a_daemon_ahead_of_the_install_is_not_called_stale():
    """Downgrades and editable checkouts happen; a newer daemon than the disk
    is odd but it is not something to restart."""
    assert describe(running="1.4.0", installed="1.3.2").state == CURRENT


def test_an_unknown_running_version_still_reports_a_pypi_update():
    """The two halves are independent — one missing fact must not suppress
    the other."""
    v = describe(running=None, installed="1.3.2", latest="1.4.0")
    assert v.state == UPDATE_AVAILABLE
    assert "None" not in " ".join(v.lines), "leaked a missing value into the copy"


# --- the promise: this module reports, it does not install -----------------
#
# The decision (module docstring) is that the bridge tells you and lets you
# choose.  A decision nothing enforces is a preference, and the next session
# to look at this file will be looking at it precisely because someone wants
# an upgrade to happen automatically.  So the ban is checked, not trusted.

_REPORTS_ONLY = (
    "bridge_version.py",       # the version brain
    "bridge_supervisor.py",    # the one place an update could be applied
    "cli/bridge_commands.py",  # where `kiln bridge restart` lives
)

# Executing an installer.  Naming the pip command as TEXT is required — the
# verdict tells the user what to run — so this bans running, not saying.
_FORBIDDEN_CALLS = frozenset({
    "perform_upgrade", "check_call", "check_output", "system", "execv", "execvp",
})
_FORBIDDEN_IMPORTS = frozenset({"pip", "kiln.self_update"})

#: Calls that start a process.  The pip ban applies to these ONLY, because the
#: rule is about what the bridge EXECUTES.  `_preflight` legitimately raises an
#: exception whose text tells the user to `pip install websockets`, and telling
#: someone to install something is the opposite of doing it behind their back.
_PROCESS_LAUNCHERS = frozenset({"run", "Popen", "call"})


def _module_source(name: str) -> tuple[Path, ast.Module]:
    path = Path(__file__).resolve().parents[1] / "src" / "kiln" / name
    return path, ast.parse(path.read_text(encoding="utf-8"))


def _literal_args(call: ast.Call) -> list[str]:
    """Every string literal passed to *call*, including inside a list/tuple.

    `bridge_commands` legitimately shells out (launchctl, systemctl), so the
    ban there cannot be on subprocess itself — it has to be on WHAT is run.
    Reading only call arguments keeps the prose out of it: the docstrings in
    these modules discuss pip at length, and must stay free to.
    """
    found: list[str] = []
    stack: list[ast.expr] = [*call.args, *(kw.value for kw in call.keywords)]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            stack.extend(node.elts)
    return found


def test_the_bridge_never_upgrades_itself():
    """Structural, because "never during a print" cannot be a promise made by
    a runtime check that might be wrong.  It is kept by there being no
    installer on this path to get the timing wrong with.
    """
    for name in _REPORTS_ONLY:
        path, tree = _module_source(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                assert called not in _FORBIDDEN_CALLS, (
                    f"{path.name} calls {called}() — the bridge is not allowed to "
                    "upgrade itself. See the decision in bridge_version.py's docstring; "
                    "reopening it is a conscious change, not a quiet one."
                )
                if called in _PROCESS_LAUNCHERS:
                    for literal in _literal_args(node):
                        assert literal != "pip" and "pip install" not in literal, (
                            f"{path.name} runs pip ({literal!r}). `kiln bridge "
                            "restart` cycles a process; it does not install software."
                        )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in _FORBIDDEN_IMPORTS, (
                        f"{path.name} imports {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod not in _FORBIDDEN_IMPORTS, f"{path.name} imports from {mod}"
                assert "self_update" not in [a.name for a in node.names], (
                    f"{path.name} imports the upgrade machinery"
                )


def test_the_ban_would_actually_catch_an_upgrade_being_added():
    """A gate that cannot fail is decoration.  Prove the walk sees the shape
    it is looking for, using the exact call a real auto-update would make.
    """
    tree = ast.parse("from kiln import self_update\nself_update.perform_upgrade()\n")
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert any(c in _FORBIDDEN_CALLS for c in calls)
    imported = [
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        for a in n.names
    ]
    assert "self_update" in imported


def test_the_ban_would_catch_a_restart_that_quietly_ran_pip():
    """The other way in, and the one `restart` makes plausible: not importing
    the upgrade machinery, just shelling out — in a module that already shells
    out to launchctl and systemctl for perfectly good reasons.
    """
    tree = ast.parse(
        'subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "kiln3d"])'
    )
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert call.func.attr in _PROCESS_LAUNCHERS
    assert "pip" in _literal_args(call), "the detector cannot see a pip argument"

    # And it does not fire on the shell-outs that belong there.
    benign = ast.parse('subprocess.run(["launchctl", "load", "-w", path])')
    call = next(n for n in ast.walk(benign) if isinstance(n, ast.Call))
    assert not any(a == "pip" or "pip install" in a for a in _literal_args(call))


def test_telling_someone_to_install_something_is_not_installing_it():
    """The distinction the ban has to hold, and the one it got wrong first
    time: `_preflight` raises an exception whose TEXT says
    "pip install websockets".  Advice is the opposite of a silent install, and
    a gate that cannot tell them apart is a gate that gets deleted.
    """
    tree = ast.parse(
        'raise click.ClickException("not installed\\n  pip install websockets")'
    )
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    called = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
    assert called not in _PROCESS_LAUNCHERS, "advice text would be read as an install"
