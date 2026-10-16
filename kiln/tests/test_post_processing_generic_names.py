"""The post-processing table names each finish by what it is, not what it is sold as.

A tool list that says "a brush-on two-part epoxy coating that self-levels"
tells a person what to look for in any shop, in any country; a retail product
name tells them one shelf.  Four technique names used to carry a product name,
and a technique name is a lookup key, so the old spellings resolve through
``RENAMED_POST_PROCESSING_TECHNIQUES``: an agent or saved note holding one
still reaches the technique, with its safety note.
"""

from __future__ import annotations

import json
import re

import pytest

from kiln.design_intelligence import (
    _DATA_DIR,
    RENAMED_POST_PROCESSING_TECHNIQUES,
    find_post_processing_technique,
    get_public_post_processing,
)

# The retail names the table carried until it was rewritten in generic terms.
_RETIRED_PRODUCT_NAMES = re.compile(
    r"xtc-?3d|\brit\b|dyemore|plasti ?dip|flex[ _]?seal|krylon|rust-?oleum"
    r"|tamiya|loctite|\btulip\b|jacquard",
    re.IGNORECASE,
)

# (material, a name the table used to carry, the name it carries now)
_OLD_KEYS = [
    ("pla", "Epoxy Coating (XTC-3D)", "Epoxy Coating"),
    ("nylon", "Dyeing with Rit DyeMore", "Dyeing"),
    ("tpu", "Flexible Paint (PlastiDip / Flex Seal)", "Flexible Rubber Paint"),
    ("tpu_85a", "Flexible Coating (PlastiDip / Rubber Coating)", "Flexible Rubber Coating"),
]


@pytest.fixture()
def guide_tool():
    tools: dict[str, object] = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.design_tools import plugin

    plugin.register(_MCP())
    return tools["get_post_processing_guide"]


def test_the_shipped_table_names_no_retail_product():
    text = (_DATA_DIR / "post_processing.json").read_text(encoding="utf-8")
    found = sorted({m.group(0) for m in _RETIRED_PRODUCT_NAMES.finditer(text)})
    assert not found, f"post_processing.json still names a product: {found}"


@pytest.mark.parametrize(("material", "old", "new"), _OLD_KEYS)
def test_an_old_technique_name_still_answers_through_the_tool(
    guide_tool, material, old, new
):
    result = guide_tool(material, old)

    assert result["success"] is True, result
    assert result["goal"] == new
    technique = result["answer"]["technique"]
    assert technique["name"] == new
    assert str(technique.get("safety_notes") or "").strip()


def test_old_names_resolve_in_any_case():
    techniques = get_public_post_processing("pla").techniques
    found = find_post_processing_technique(techniques, "  epoxy coating (xtc-3d) ")
    assert found is not None and found["name"] == "Epoxy Coating"


def test_every_rename_points_at_a_name_the_table_carries():
    carried = set()
    retired = set(RENAMED_POST_PROCESSING_TECHNIQUES)
    raw = json.loads((_DATA_DIR / "post_processing.json").read_text(encoding="utf-8"))
    for key, record in raw.items():
        if key.startswith("_"):
            continue
        for technique in record.get("techniques") or []:
            carried.add(technique["name"])
            assert technique["name"].casefold() not in retired, technique["name"]
    assert set(RENAMED_POST_PROCESSING_TECHNIQUES.values()) <= carried
