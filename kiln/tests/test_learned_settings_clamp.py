"""A learned or community-median setting is held under the machine's ceiling.

The community profile fix stops a stranger RAISING your hotend ceiling.  This
is the other end of the same pipe: the numbers produced downstream of a
ceiling.  ``predict_settings`` medians the free-form ``settings`` dicts of
other people's successful prints; ``get_optimal_settings`` medians the local
outcome store.  Nothing upstream of either checks what temperature a recorded
print claimed, so one row logged against the wrong machine carries a hotter
number into every later recommendation for the right one.

Both are advisory, and both name a specific printer — which is exactly when a
curated ceiling exists to measure against.
"""

from __future__ import annotations

import json

import pytest

import kiln.safety_profiles as sp
from kiln.print_dna import _aggregate_prediction


class _Row(dict):
    """print_dna rows arrive as sqlite3.Row; dict() over them is all the
    aggregator does."""


def _rows(*settings: dict) -> list[_Row]:
    return [_Row(settings=json.dumps(s)) for s in settings]


class TestCommunityMediansAreHeldUnderTheCeiling:
    def test_a_hotter_corpus_cannot_raise_this_machine(self):
        curated = sp.get_profile("ender3").max_hotend_temp
        pred = _aggregate_prediction(
            _rows({"temp_tool": 300}, {"temp_tool": 300}, {"temp_tool": 300}),
            source="exact_match",
            printer_model="ender3",
        )
        assert pred.recommended_settings["temp_tool"] == curated

    def test_a_cooler_corpus_is_passed_through(self):
        pred = _aggregate_prediction(
            _rows({"temp_tool": 210}, {"temp_tool": 215}, {"temp_tool": 220}),
            source="exact_match",
            printer_model="ender3",
        )
        assert pred.recommended_settings["temp_tool"] == 215

    def test_non_ceiling_keys_survive_the_clamp(self):
        """The clamp must not eat the rest of the recommendation."""
        pred = _aggregate_prediction(
            _rows(
                {"temp_tool": 300, "layer_height": 0.2, "fill_pattern": "gyroid"},
                {"temp_tool": 300, "layer_height": 0.2, "fill_pattern": "gyroid"},
            ),
            source="exact_match",
            printer_model="ender3",
        )
        assert pred.recommended_settings["layer_height"] == 0.2
        assert pred.recommended_settings["fill_pattern"] == "gyroid"

    def test_no_printer_named_means_no_clamp(self):
        pred = _aggregate_prediction(
            _rows({"temp_tool": 300}), source="exact_match", printer_model=None
        )
        assert pred.recommended_settings["temp_tool"] == 300


class TestTheLocalLearningAggregateIsHeldToo:
    """``get_optimal_settings`` goes through the same helper, after the
    calibration overlay has had its say."""

    def test_a_bad_row_cannot_carry_a_hotter_number_forward(self, monkeypatch):
        import kiln.server as _srv
        from kiln.plugins import learning_tools  # noqa: F401  (registers the tool)

        curated = sp.get_profile("ender3")
        recommended = {"temp_tool": 300.0, "temp_bed": 190.0, "speed": 60.0}
        held = sp.clamp_settings_to_profile(recommended, "ender3")

        assert held.settings["temp_tool"] == curated.max_hotend_temp
        assert held.settings["temp_bed"] == curated.max_bed_temp
        assert held.settings["speed"] == 60.0, "speed has no unambiguous ceiling"
        assert len(held.clamped) == 2, "each clamp is stated for the rationale"

    def test_the_tool_source_wires_the_clamp(self):
        """A behavioural test cannot easily reach the tool body — it needs the
        DB and the MCP registration — so pin the WIRE instead, by AST rather
        than by substring: a mention in a comment or a docstring must not
        count, and a reformatting must not break the test."""
        import ast
        import inspect

        from kiln.plugins import learning_tools

        tree = ast.parse(inspect.getsource(learning_tools))
        called = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "clamp_settings_to_profile"
            for n in ast.walk(tree)
        )
        assert called, "get_optimal_settings must route its aggregate through the clamp"


class TestTheClampNeverRemovesTheLimit:
    """The constraint that outranks the rest: clamping toward the conservative
    value, never refusing and leaving no limit at all."""

    @pytest.mark.parametrize("bad", [None, {}, {"temp_tool": "hot"}, {"temp_tool": None}])
    def test_unusable_input_returns_something_usable(self, bad):
        held = sp.clamp_settings_to_profile(bad, "ender3")
        assert isinstance(held.settings, dict)

    def test_an_unknown_printer_falls_back_to_the_same_authority_validate_uses(self):
        """get_profile resolves an unknown id to `default`, and validate_gcode
        would judge that id the same way — so the recommendation can never
        disagree with the enforcement that follows it."""
        unknown = sp.get_profile("no_such_printer_at_all")
        held = sp.clamp_settings_to_profile({"temp_tool": 999.0}, "no_such_printer_at_all")
        assert held.settings["temp_tool"] == unknown.max_hotend_temp
