"""Tests for the free single-color recipe tool + the hex normalizer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kiln.design_recipe import (
    DesignPart,
    DesignRecipe,
    find_recipe,
    load_recipe,
    normalize_hex_color,
    save_recipe,
)


# --------------------------------------------------------------------------- #
# normalize_hex_color
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "inp,out",
    [
        ("#16B2A3", "#16B2A3"),
        ("16b2a3", "#16B2A3"),
        ("#fff", "#FFFFFF"),
        ("fff", "#FFFFFF"),
        ("7B4FC0FF", "#7B4FC0"),      # AMS RRGGBBAA — alpha dropped
        ("  #16b2a3  ", "#16B2A3"),   # trimmed
        ("teal", None),              # a color NAME → None (caller passes through)
        ("", None),
        (None, None),
        ("#12345", None),            # wrong length
        ("xyzxyz", None),            # not hex digits
    ],
)
def test_normalize_hex_color(inp, out):
    assert normalize_hex_color(inp) == out


# --------------------------------------------------------------------------- #
# set_design_color tool
# --------------------------------------------------------------------------- #

@pytest.fixture()
def registered_tools():
    tools: dict[str, callable] = {}

    class MockMCP:
        def tool(self):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.design_tools import plugin

    plugin.register(MockMCP())
    return tools


def _make_recipe(tmp_path, parts, *, name="coaster", version=1) -> str:
    d = tmp_path / "design"
    d.mkdir(exist_ok=True)
    recipe = DesignRecipe(
        name=name,
        created=datetime.now(timezone.utc).isoformat(),
        parts=parts,
        version=version,
    )
    save_recipe(recipe, str(d))
    return str(d)


def test_tool_is_registered(registered_tools):
    assert "set_design_color" in registered_tools


def test_sets_color_bumps_version_and_persists(registered_tools, tmp_path):
    d = _make_recipe(tmp_path, [DesignPart("body", "structural", "body.stl", "white")])
    res = registered_tools["set_design_color"](recipe_path=d, color="#16b2a3")

    assert res["success"] is True
    assert res["color"] == "#16B2A3"
    assert res["part_name"] == "body"
    assert res["version"] == 2

    saved = load_recipe(find_recipe(d))
    assert saved.parts[0].color == "#16B2A3"
    assert saved.version == 2
    assert saved.changes == {"body.color": "white -> #16B2A3"}
    assert saved.parent_version is not None  # links back to v1


def test_color_name_passes_through(registered_tools, tmp_path):
    d = _make_recipe(tmp_path, [DesignPart("body", "structural", "body.stl", "white")])
    res = registered_tools["set_design_color"](recipe_path=d, color="teal")
    assert res["success"] is True
    assert res["color"] == "teal"
    assert load_recipe(find_recipe(d)).parts[0].color == "teal"


def test_ams_alpha_is_dropped(registered_tools, tmp_path):
    d = _make_recipe(tmp_path, [DesignPart("body", "structural", "body.stl", "white")])
    res = registered_tools["set_design_color"](recipe_path=d, color="2FB6A8FF")
    assert res["color"] == "#2FB6A8"


def test_multi_part_directs_to_pro(registered_tools, tmp_path):
    d = _make_recipe(
        tmp_path,
        [
            DesignPart("body", "structural", "body.stl", "white"),
            DesignPart("rim", "decoration", "rim.stl", "black"),
        ],
    )
    res = registered_tools["set_design_color"](recipe_path=d, color="red")
    assert res["success"] is False
    assert "change_part_color" in res["error"]
    assert res["part_names"] == ["body", "rim"]
    # nothing was mutated
    assert load_recipe(find_recipe(d)).parts[0].color == "white"


def test_missing_recipe_errors(registered_tools, tmp_path):
    res = registered_tools["set_design_color"](recipe_path=str(tmp_path / "nope"), color="red")
    assert res["success"] is False
    assert "No design recipe" in res["error"]


def test_empty_color_rejected(registered_tools, tmp_path):
    d = _make_recipe(tmp_path, [DesignPart("body", "structural", "body.stl", "white")])
    res = registered_tools["set_design_color"](recipe_path=d, color="   ")
    assert res["success"] is False
