"""The maps an agent reads first name parameters the tools really have.

The skill manifest, ``get_started`` and the server instructions are where an
agent learns how to call Kiln, and they teach by example:
``slice_model(input_path)``, ``design_session(verb="update", goal_id, answers)``.
Measured 2026-10-13: 34 of those examples named a parameter the tool does not
have -- ``slice_model(file_path)``, ``start_print(file)``,
``generate_from_template(id, params)``, ``design_session(..., session_id)``,
``visualize_model(stl_path)`` -- and an agent copying one got a validation
error back, or quietly dropped the argument.  Nothing checked: the examples are
prose, and every tool test calls the tool, not the map that describes it.

This reads every ``tool(arg, ...)`` example in the three maps and checks each
named argument against the live registry's schema for that tool, so a
parameter renamed later, or an example typed from memory, fails here.

The same maps once called ``compile_scad`` THE default for "make me a ...",
contradicting the front door; the last test holds them to it.
"""

from __future__ import annotations

import re

import pytest

from kiln.front_door import FALLBACK_LOOP, FRONT_DOOR, KEYLESS_MAKE_LINE
from kiln.skill_manifest import SkillManifest

#: ``name(args)`` with no nested parentheses: the shape every example uses.
_CALL = re.compile(r"\b([a-z][a-z0-9_]+)\(([^()]*)\)")
_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


def _strings(value, out: list[str]) -> list[str]:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _strings(v, out)
    return out


def _maps() -> dict[str, list[str]]:
    from kiln import server

    return {
        "skill_manifest": _strings(SkillManifest().to_dict(), []),
        "get_started": _strings(server.get_started(), []),
        "instructions": [server._build_instructions()],
    }


def _registry() -> dict[str, set[str]]:
    from kiln import server

    return {
        t.name: set((t.parameters or {}).get("properties", {}))
        for t in server.mcp._tool_manager.list_tools()
    }


def _named_arguments(text: str, registry: dict[str, set[str]]):
    """(tool, argument) for every named argument in a tool-call example."""
    for name, args in _CALL.findall(text):
        if name not in registry or not args.strip():
            continue
        for part in args.split(","):
            key = part.split("=")[0].strip()
            # A value standing alone -- a placeholder, a quoted string, a
            # literal -- is not a parameter name.
            if key[:1] in ("<", '"', "'", "{", "[") or key[:3] == "...":
                continue
            key = key.strip("`*")
            if _IDENT.fullmatch(key):
                yield name, key


def test_every_example_names_a_real_parameter():
    registry = _registry()
    assert len(registry) > 100, "the live registry did not load"
    checked = 0
    wrong: list[str] = []
    for where, texts in _maps().items():
        for text in texts:
            for tool, arg in _named_arguments(text, registry):
                checked += 1
                if arg not in registry[tool]:
                    wrong.append(f"{where}: {tool}({arg}) -- real: {sorted(registry[tool])}")
    assert checked > 100, f"only {checked} named arguments found -- the maps did not load"
    assert not wrong, "map examples naming a parameter the tool does not have:\n" + "\n".join(
        sorted(set(wrong))
    )


def test_the_check_can_fail():
    registry = {"slice_model": {"input_path", "material"}}
    found = list(_named_arguments("slice_model(file_path) — slice to gcode", registry))
    assert found == [("slice_model", "file_path")]
    assert "file_path" not in registry["slice_model"]
    # Positional names count; quoted values and placeholders do not.
    found = list(_named_arguments('slice_model(input_path, material="PLA", <x>)', registry))
    assert found == [("slice_model", "input_path"), ("slice_model", "material")]


@pytest.mark.parametrize("where", ["skill_manifest", "get_started", "instructions"])
def test_no_map_calls_compile_scad_the_default_without_the_front_door(where):
    """A 'DEFAULT' that names the compile must name the front door too: on an
    install with design_session, a make starts there."""
    offenders = [
        t[:140]
        for t in _maps()[where]
        if "DEFAULT" in t and FALLBACK_LOOP in t and FRONT_DOOR not in t
    ]
    assert not offenders, offenders


def test_the_keyless_make_is_said_one_way():
    maps = _maps()
    workflow = SkillManifest().workflows["create_custom_object_free"]
    assert workflow[0].startswith("DEFAULT for 'make me a ...': design_session(")
    assert KEYLESS_MAKE_LINE in workflow
    assert any(KEYLESS_MAKE_LINE in t for t in maps["get_started"])
    # The design_session workflow is the keyless path, not an opt-in cloud one.
    cloud = [t for t in workflow if t.startswith("Cloud AI providers")]
    assert cloud and "design_and_generate" not in cloud[0]
