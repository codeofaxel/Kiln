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
    assert "kiln bridge stop && kiln bridge start" in body
    # It is already downloaded; do not send anyone to pip for it.
    assert "pip install" not in body


def test_a_newer_release_on_pypi_asks_for_both_steps():
    v = describe(running="1.3.2", installed="1.3.2", latest="1.4.0")
    assert v.state == UPDATE_AVAILABLE
    body = " ".join(v.lines)
    assert "1.4.0" in body
    # Installing alone never reaches the running daemon, so the restart is
    # not a footnote — it is half the instruction.
    assert "pip install --upgrade kiln3d && kiln bridge stop && kiln bridge start" in body


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


def test_a_login_managed_bridge_is_told_the_command_that_works():
    """`kiln bridge start` against a login-managed bridge prints "Already set
    to start on login" and does nothing — advice that fails silently is worse
    than none, because the operator watches the version not change."""
    managed = describe(running="1.2.0", installed="1.3.2", enabled=True)
    assert "kiln bridge disable && kiln bridge enable" in " ".join(managed.lines)
    assert "kiln bridge stop" not in " ".join(managed.lines)

    session = describe(running="1.2.0", installed="1.3.2", enabled=False)
    assert "kiln bridge stop && kiln bridge start" in " ".join(session.lines)


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
)

# Executing an installer.  Naming the pip command as TEXT is required — the
# verdict tells the user what to run — so this bans running, not saying.
_FORBIDDEN_CALLS = frozenset({
    "perform_upgrade", "check_call", "check_output", "system", "execv", "execvp",
})
_FORBIDDEN_IMPORTS = frozenset({"pip", "kiln.self_update"})


def _module_source(name: str) -> tuple[Path, ast.Module]:
    path = Path(__file__).resolve().parents[1] / "src" / "kiln" / name
    return path, ast.parse(path.read_text(encoding="utf-8"))


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
