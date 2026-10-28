"""A tool marked read-only reaches nothing that changes the person's things.

``test_every_tool_is_annotated.py`` proves each tool CARRIES a label; this
proves a read-only label is TRUE.  A host runs a read-only tool without
asking, so one that reaches a printer command, a saved-record write, a spent
allowance, a stored secret or another tool that changes things runs that
change unasked.  The walk and its rules live in ``tests/_read_only_reach.py``,
shared with kiln-pro's twin so both repos judge alike.

A finding is fixed by relabelling the tool (or moving the change out of it),
never by widening the rules.  ``DECIDED`` is for a hit that really changes
nothing the person owns, with the reason; a row the walk no longer finds is
stale and fails, so the list cannot outlive what it excuses.
"""

from __future__ import annotations

import kiln.server as server
from tests import _read_only_reach as scan

#: tool -> why a hit the walk reports changes nothing the person owns.
DECIDED: dict[str, str] = {}


def _findings() -> dict[str, list[scan.Reach]]:
    server._ensure_internal_tool_plugins_registered()
    registry = dict(server.mcp._tool_manager._tools)
    read_only, changing = scan.read_only_tools(registry)
    found: dict[str, list[scan.Reach]] = {}
    for name, fn in sorted(read_only.items()):
        if (getattr(fn, "__module__", "") or "").startswith("kiln_pro"):
            continue  # kiln-pro's own tests judge kiln-pro's tools
        hits = scan.reach(name, fn, changing)
        if hits:
            found[name] = hits
    return found


def test_no_read_only_tool_reaches_a_change() -> None:
    found = _findings()
    undecided = [str(hit) for name, hits in found.items() if name not in DECIDED for hit in hits]
    assert not undecided, (
        "read-only tools that reach a change (relabel them, or move the change out):\n  "
        + "\n  ".join(undecided)
    )
    stale = sorted(set(DECIDED) - set(found))
    assert not stale, f"DECIDED rows the walk no longer finds: {stale}"


def test_the_walk_sees_a_printer_command_two_calls_down() -> None:
    """The rule can fail: a helper that starts a print is found through a helper."""

    class _Adapter:
        def start_print(self, path: str) -> None: ...

    adapter = _Adapter()

    def _go(path: str) -> None:
        adapter.start_print(path)

    def _middle(path: str) -> None:
        _go(path)

    def looks_harmless(path: str) -> dict:
        _middle(path)
        return {}

    _go.__module__ = _middle.__module__ = looks_harmless.__module__ = "kiln.fake"
    hits = scan.reach("looks_harmless", looks_harmless, {})
    assert any("printer command start_print" in str(hit) for hit in hits), hits


def test_reads_of_a_printer_stay_read_only() -> None:
    commands = scan.printer_commands()
    for read in ("get_state", "get_job", "get_snapshot", "list_files", "async_get_state"):
        assert read not in commands
    for command in ("start_print", "cancel_print", "send_gcode", "set_tool_temp", "home_axes"):
        assert command in commands
