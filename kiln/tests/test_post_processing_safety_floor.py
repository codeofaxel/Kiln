"""The finishing fire, fume and dust warnings are on disk, in public.

A plain install with no account and no network still has to say "acetone is
extremely flammable" when acetone smoothing comes up, and say how annealing
can change a part wherever annealing is suggested.  The rest of the
finishing guide is answered by Kiln's servers; these warnings are the floor a
miss still says.  Each case runs the real door with kiln-pro absent, signed
out through the real served door (no stand-in) and offline, and reads the
shipped cautions table, never a mock of it, so a warning that falls out of
the file or out of the door's answer goes red here.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

import pytest

from kiln import _pro_finishing_bridge as bridge


@pytest.fixture()
def guide(monkeypatch, tmp_path):
    """The real ``get_post_processing_guide`` on a plain install: kiln-pro
    cannot be imported and no account is signed in."""
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    tools: dict[str, Any] = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.design_tools import plugin

    plugin.register(_MCP())
    return tools["get_post_processing_guide"]


def _offline(monkeypatch) -> None:
    import kiln.server as srv

    def no_network(*_a, **_k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(srv, "_pro_api_call", no_network)


_POSTURES = ["signed_out", "offline"]


def _ask(guide, monkeypatch, posture: str, *args: Any) -> dict[str, Any]:
    if posture == "offline":
        _offline(monkeypatch)
    out = guide(*args)
    assert out["success"] is True and out["basic"] is True, out
    assert out["why"] == posture, out
    return out


def _table() -> dict[str, Any]:
    raw = json.loads(bridge.CAUTIONS_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_every_finish_on_disk_carries_a_safety_note():
    missing = [
        f"{mid}: {t.get('name')}"
        for mid, rec in _table().items()
        for t in rec.get("techniques", [])
        if not str(t.get("safety_notes") or "").strip()
    ]
    assert not missing, f"finishes with no safety note on disk: {missing}"


def test_every_catalogued_plastic_has_its_finishes_on_disk():
    from kiln.design_intelligence import _DATA_DIR

    raw = json.loads((_DATA_DIR / "materials.json").read_text(encoding="utf-8"))
    raw = raw.get("materials", raw)
    catalogued = {k for k in raw if not k.startswith("_")}
    table = _table()
    assert set(table) == catalogued
    assert all(rec.get("techniques") for rec in table.values())


def test_the_table_holds_names_and_warnings_only():
    extra = {
        key
        for rec in _table().values()
        for key in [
            *rec,
            *(k for t in rec.get("techniques", []) for k in t),
            *(k for c in rec.get("strengthening", []) for k in c),
        ]
        if key not in {"techniques", "strengthening", "name", "method", "safety_notes"}
    }
    assert not extra, extra


def test_every_strengthening_caution_on_disk_carries_a_note():
    cautions = [c for rec in _table().values() for c in rec.get("strengthening", [])]
    assert cautions, "the annealing caution is on disk"
    assert all(c.get("method") and str(c.get("safety_notes") or "").strip() for c in cautions)


@pytest.mark.parametrize("posture", _POSTURES)
@pytest.mark.parametrize("goal", [None, "strengthen", "annealing"])
def test_a_suggested_annealing_is_said_with_its_caution(guide, monkeypatch, posture, goal):
    out = _ask(guide, monkeypatch, posture, "pla") if goal is None else _ask(guide, monkeypatch, posture, "pla", goal)
    annealing = [c for c in out["strengthening_cautions"] if c["method"] == "Annealing"]
    assert annealing, out
    note = annealing[0]["safety_notes"]
    assert "warp" in note and "70" in note and "small change of size" in note


@pytest.mark.parametrize("posture", _POSTURES)
@pytest.mark.parametrize("material", ["peek", "pekk", "pps", "pa612_esd"])
def test_an_annealing_finish_says_how_annealing_can_change_a_part(guide, monkeypatch, posture, material):
    out = _ask(guide, monkeypatch, posture, material)
    annealing = [t for t in out["techniques"] if t["name"] == "Annealing"]
    assert annealing, out
    note = annealing[0]["safety_notes"]
    assert "warp" in note and "small change of size" in note, note


@pytest.mark.parametrize("posture", _POSTURES)
def test_a_named_acetone_smoothing_says_acetone_is_flammable(guide, monkeypatch, posture):
    out = _ask(guide, monkeypatch, posture, "abs", "Acetone Vapor Smoothing")
    assert [t["name"] for t in out["techniques"]] == ["Acetone Vapor Smoothing"]
    assert "flammable" in out["techniques"][0]["safety_notes"].lower()


@pytest.mark.parametrize("posture", _POSTURES)
def test_the_overview_names_no_finish_without_its_warning(guide, monkeypatch, posture):
    out = _ask(guide, monkeypatch, posture, "polycarbonate")
    by_name = {t["name"]: t for t in out["techniques"]}
    assert "fatal" in by_name["Solvent Welding (Dichloromethane)"]["safety_notes"]
    assert all(t["safety_notes"].strip() for t in out["techniques"])


@pytest.mark.parametrize("posture", _POSTURES)
def test_the_surface_finish_answer_carries_the_ipa_fire_warning(guide, monkeypatch, posture):
    out = _ask(guide, monkeypatch, posture, "pvb", "surface_finish")
    assert "ipa vapor is flammable" in json.dumps(out["techniques"]).lower()


@pytest.mark.parametrize("posture", _POSTURES)
@pytest.mark.parametrize("material", ["abs", "pvb", "polycarbonate", "pla", "cf_nylon"])
def test_every_goal_says_each_of_the_plastics_warnings_word_for_word(guide, monkeypatch, posture, material):
    if posture == "offline":
        _offline(monkeypatch)
    expected = _table()[material]["techniques"]
    for goal in (None, "surface_finish", "paint", "strengthen"):
        out = guide(material) if goal is None else guide(material, goal)
        assert out["basic"] is True and out["why"] == posture, out
        assert out["techniques"] == expected, (goal, out)
        assert out["strengthening_cautions"] == _table()[material].get("strengthening", []), (goal, out)


@pytest.mark.parametrize("posture", _POSTURES)
def test_a_miss_gives_no_tool_paint_or_strength_figure(guide, monkeypatch, posture):
    out = _ask(guide, monkeypatch, posture, "abs")
    assert not {"steps", "answer", "paintability", "strengthening", "upgrade_hint"} & set(out)
    assert all(set(t) == {"name", "safety_notes"} for t in out["techniques"])
    assert "Kiln can't" in out["message"]
