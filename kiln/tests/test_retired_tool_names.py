"""An old tool name has a removal date, and a removed name points onward.

Four aliases outlived their own promise: each docstring said "kept for one
release" or "removed in the next minor release", 1.5.0 shipped with all
four, and nothing could notice, because the only record of the date was a
sentence.  ``kiln.retired_tools`` is now the record, and these tests hold
it: an alias with no row fails, a row whose release has arrived fails, a
retired name that comes back fails, and calling a retired name answers with
the tool that replaced it.

Public Kiln's tools only (``kiln.*``, which includes the stubs a plain
install registers for paid tools); kiln-pro's own tools are judged by its
twin, ``tests/test_retired_tool_names_pro.py`` in kiln-pro, against the
same tables.
"""

from __future__ import annotations

import asyncio

import pytest

from kiln import __version__, retired_tools
from kiln.retired_tools import DEPRECATED, RETIRED, replacement_tool, says_deprecated


def _public_tools() -> dict:
    from kiln import server

    return {
        name: tool
        for name, tool in server.mcp._tool_manager._tools.items()
        if getattr(getattr(tool, "fn", None), "__module__", "").startswith("kiln.")
    }


def _release(version: str) -> tuple[int, ...]:
    """"1.5.1" -> (1, 5, 1); a suffix such as "rc1" or ".dev0" is ignored."""
    parts = []
    for piece in version.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit()) or "0"
        parts.append(int(digits))
    return tuple(parts)


def test_every_deprecated_tool_has_a_removal_release() -> None:
    unrowed = sorted(
        name
        for name, tool in _public_tools().items()
        if says_deprecated(tool.description) and name not in DEPRECATED
    )
    assert not unrowed, (
        f"{unrowed} call themselves deprecated but have no row in "
        "kiln.retired_tools.DEPRECATED: add (replacement, remove_in) for each."
    )


def test_no_deprecated_alias_outlives_its_release() -> None:
    overdue = sorted(
        f"{name} (remove in {remove_in})"
        for name, (_, remove_in) in DEPRECATED.items()
        if _release(__version__) >= _release(remove_in)
    )
    assert not overdue, (
        f"Kiln {__version__} still carries {overdue}: delete each alias, move "
        "its row to RETIRED, and drop it from DEPRECATED."
    )


def test_a_retired_name_stays_retired() -> None:
    back = sorted(set(RETIRED) & set(_public_tools()))
    assert not back, f"{back} are retired in kiln.retired_tools but registered again"


def test_every_pointer_names_a_tool_that_exists() -> None:
    import json
    from importlib import resources

    manifest = json.loads(
        resources.files("kiln").joinpath("pro_tool_manifest.json").read_text(encoding="utf-8")
    )
    entries = manifest["tools"] if isinstance(manifest, dict) and "tools" in manifest else manifest
    paid = {e["name"] for e in (entries if isinstance(entries, list) else entries.values())}
    known = set(_public_tools()) | paid
    pointers = {**RETIRED, **{name: call for name, (call, _) in DEPRECATED.items()}}
    dangling = sorted(
        f"{name} -> {call}" for name, call in pointers.items() if replacement_tool(call) not in known
    )
    assert not dangling, f"these point at a tool that does not exist: {dangling}"


@pytest.mark.parametrize("name", sorted(RETIRED))
def test_calling_a_retired_name_says_what_replaced_it(name: str) -> None:
    from kiln import server

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(server.mcp._tool_manager.call_tool(name, {}))
    message = str(raised.value)
    assert replacement_tool(RETIRED[name]) in message
    assert message == retired_tools.retired_message(name)


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("DEPRECATED — call printer_status instead.", True),
        ("Deprecated alias for repair_mesh(close_holes=True).", True),
        ("**Deprecated:** use the new tool.", True),
        ("Repair a mesh. The deprecated flag is ignored.", False),
        ("", False),
        (None, False),
    ],
)
def test_says_deprecated_reads_the_first_line_only(description, expected) -> None:
    assert says_deprecated(description) is expected
