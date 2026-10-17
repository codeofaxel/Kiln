"""The post-processing fire, fume and dust warnings are on disk, in public.

A plain install with no account and no network still has to say "acetone is
extremely flammable" when it names acetone smoothing.  These read the shipped
table through the free door, never a mock, so a warning that falls out of the
file or out of the door's key list goes red here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiln

_TABLE = Path(kiln.__file__).parent / "data" / "design_knowledge" / "post_processing.json"


@pytest.fixture()
def tools():
    captured: dict[str, callable] = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.design_tools import plugin

    plugin.register(_MCP())
    return captured


def _table() -> dict:
    raw = json.loads(_TABLE.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_every_public_technique_carries_a_safety_note():
    missing = [
        f"{mid}: {t.get('name')}"
        for mid, rec in _table().items()
        for t in rec.get("techniques", [])
        if not str(t.get("safety_notes") or "").strip()
    ]
    assert not missing, f"techniques with no safety note on disk: {missing}"


def test_the_free_door_says_acetone_is_flammable(tools):
    result = tools["get_post_processing_guide"]("abs", "Acetone Vapor Smoothing")
    assert result["success"] is True
    note = result["answer"]["technique"]["safety_notes"]
    assert "flammable" in note.lower()


def test_the_overview_names_no_technique_without_its_warning(tools):
    result = tools["get_post_processing_guide"]("polycarbonate")
    assert result["success"] is True
    by_name = {t["name"]: t for t in result["techniques"]}
    assert "fatal" in by_name["Solvent Welding (Dichloromethane)"]["safety_notes"]


def test_the_surface_finish_answer_carries_the_ipa_fire_warning(tools):
    result = tools["get_post_processing_guide"]("pvb", "surface_finish")
    text = json.dumps(result["answer"]).lower()
    assert "ipa vapor is flammable" in text


def test_the_diagnostic_tip_that_recommends_ipa_smoothing_warns_of_fire():
    from kiln.design_intelligence import get_print_diagnostic

    diag = get_print_diagnostic("pvb")
    tips = [t for t in diag.post_processing_tips if t.startswith("IPA Vapor Smoothing")]
    assert tips, diag.post_processing_tips
    assert "flammable" in tips[0].lower()
