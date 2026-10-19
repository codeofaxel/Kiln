"""Supports are decided at the slice chokepoint, and said.

2026-10-19: ``slice_and_estimate`` on a desk stand with a 70 mm cantilevered
arm returned a printability block saying ``needs_supports=True`` ("Enable
supports") beside G-code carrying ``support_material = 0`` and no support
lines, and an estimate of that unsupported file.  Only ``kiln slice
--support-mode auto`` ever turned supports on, and that decision lived in
the CLI.  Now :mod:`kiln.slicer_supports` decides inside ``slice_file`` for
every door; these tests pin the rule, the statements it honours, the Orca
crossing and what every result says.  The G-code half -- support lines in
the emitted file, an estimate that includes them -- is
``tests/test_print_artifact_fidelity.py::TestSupportsWhenNeeded``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiln import slicer_supports
from kiln.printability import overhang_threshold_deg, support_verdict
from kiln.slicer_orca import ini_to_settings, settings_to_orca_presets
from kiln.slicer_profiles import profile_origin, profile_with_overrides, resolve_slicer_profile
from kiln.slicer_supports import (
    DECIDED_ANALYSIS,
    DECIDED_NOT_CHECKED,
    DECIDED_STATED,
    SUPPORT_KEY,
    SliceSupports,
    decide_supports,
    ensure_profile_supports,
    support_settings,
)

#: What the engine writes for PLA here: the public 45 degree floor, or the
#: per-material angle when the kiln-pro overlay is present on this machine.
MINIMAL_SUPPORTS = support_settings(overhang_threshold_deg("PLA"))


def _report(*, overhang_pct: float = 0.0, needs: bool = False, bridge_mm: float = 0.0, bridges: bool = False):
    return SimpleNamespace(
        overhangs=SimpleNamespace(needs_supports=needs, overhang_percentage=overhang_pct),
        bridging=SimpleNamespace(needs_supports_for_bridges=bridges, max_bridge_length_mm=bridge_mm),
        overhang_threshold_deg=45.0,
    )


@pytest.fixture
def part(tmp_path: Path) -> str:
    path = tmp_path / "part.stl"
    path.write_bytes(b"\0" * 84)
    return str(path)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestTheRule:
    """The verdict is the printability report's own -- one answer, read by
    every door (tests/test_support_verdict.py walks the doors)."""

    def test_an_overhang_the_engine_flags_needs_them(self):
        assert support_verdict(_report(needs=True, overhang_pct=2.7)) == "overhangs=2.7%"

    def test_there_is_no_size_floor(self):
        """A 10 mm cantilever on a large, finely meshed part is a tenth of
        a percent of its faces and a real overhang; the old 1 % floor read
        it as none.  What a slicer bridges is already excluded by the
        report itself, not by a floor here."""
        assert support_verdict(_report(needs=True, overhang_pct=0.1)) == "overhangs=0.1%"
        assert support_verdict(_report(needs=False, overhang_pct=12.5)) is None

    def test_a_bridge_too_long_for_the_material_does_at_any_count(self):
        assert support_verdict(_report(bridges=True, bridge_mm=52.6)) == "bridges=52.6mm"

    def test_both_are_said(self):
        assert support_verdict(_report(needs=True, overhang_pct=2.7, bridges=True, bridge_mm=52.6)) == (
            "overhangs=2.7%, bridges=52.6mm"
        )

    def test_a_part_that_needs_none_gets_none(self, part: str):
        with patch("kiln.printability.judge_supports", return_value=_report()):
            decision = decide_supports(part, material="PLA")
        assert decision == SliceSupports(False, DECIDED_ANALYSIS, "no overhang or bridge needs them")
        assert decision.note == "no supports: no overhang or bridge needs them"

    def test_a_part_that_needs_them_gets_kilns_minimal_supports(self, part: str):
        with patch("kiln.printability.judge_supports", return_value=_report(needs=True, overhang_pct=2.7)):
            decision = decide_supports(part, material="PLA")
        assert decision.enabled and decision.decided_by == DECIDED_ANALYSIS
        assert decision.settings == support_settings(45.0)
        # The complement: the slicers take the slope from horizontal.
        assert decision.settings["support_material_threshold"] == "45"
        assert decision.settings["support_material_buildplate_only"] == "0"
        assert decision.settings["dont_support_bridges"] == "0"
        assert decision.note == "supports on: overhangs=2.7%"

    def test_the_slicers_threshold_is_the_verdicts_angle(self, part: str):
        """A Pro per-material angle reaches the slice the same way: the
        report says what it counted against, the slice supports exactly
        that."""
        tuned = _report(needs=True, overhang_pct=2.7)
        tuned.overhang_threshold_deg = 35.0
        with patch("kiln.printability.judge_supports", return_value=tuned):
            decision = decide_supports(part, material="TPU")
        assert decision.settings["support_material_threshold"] == "55"

    def test_a_part_the_engine_cannot_read_is_said_not_checked(self, part: str):
        """Never read as a part that needs none: the answer names the miss."""
        with patch("kiln.printability.judge_supports", side_effect=ValueError("no  triangles\nhere")):
            decision = decide_supports(part, material="PLA")
        assert not decision.enabled and decision.decided_by == DECIDED_NOT_CHECKED
        assert decision.reason == "not checked: no triangles here"
        assert "was not checked" in decision.note

    def test_the_engine_is_told_the_material_and_the_nozzle(self, part: str):
        """PLA bridges farther than TPU, and a 0.6 mm nozzle bridges farther
        than a 0.4: the check judges the part the slice is for."""
        with patch("kiln.printability.judge_supports", return_value=_report()) as engine:
            decide_supports(part, material="TPU", nozzle_diameter=0.6)
        assert engine.call_args.kwargs["material"] == "tpu"
        assert engine.call_args.kwargs["nozzle_diameter"] == 0.6

    def test_nothing_is_kept_between_calls(self, part: str):
        """One process serves every customer on the hosted server, so the
        reading is made fresh each time, never kept in a module store."""
        with patch("kiln.printability.judge_supports", return_value=_report()) as engine:
            decide_supports(part, material="PLA")
            decide_supports(part, material="PLA")
        assert engine.call_count == 2
        assert not [name for name, value in vars(slicer_supports).items()
                    if not name.startswith("__") and isinstance(value, (dict, list, set))]


# ---------------------------------------------------------------------------
# Whose decision it is
# ---------------------------------------------------------------------------


class TestWhoseDecision:
    def test_an_unstated_profile_is_decided_and_the_keys_written(self, part: str):
        profile = resolve_slicer_profile("bambu_a1")
        assert ini_to_settings(profile).get(SUPPORT_KEY) != "1"
        with patch("kiln.printability.judge_supports", return_value=_report(needs=True, overhang_pct=2.7)):
            derived, decision = ensure_profile_supports(profile, part, material="PLA")
        assert derived != profile
        written = ini_to_settings(derived)
        # The mocked report counted overhangs against 45 degrees, so the
        # slice supports from 45 -- whatever the overlay here says for PLA.
        assert {key: written[key] for key in MINIMAL_SUPPORTS} == support_settings(45.0)
        # Kiln's own layer, not the caller's: the next part is decided afresh.
        assert SUPPORT_KEY not in profile_origin(derived).stated
        assert profile_origin(derived).printer_id == "bambu_a1"
        assert decision.enabled

    def test_the_generic_profiles_own_zero_is_not_a_statement(self, part: str):
        """The bundled generic profile carries ``support_material = 0`` as a
        default, which is how every unknown printer sliced without them."""
        profile = resolve_slicer_profile("default")
        assert ini_to_settings(profile)[SUPPORT_KEY] == "0"
        with patch("kiln.printability.judge_supports", return_value=_report(needs=True, overhang_pct=2.7)):
            derived, decision = ensure_profile_supports(profile, part, material="PLA")
        assert ini_to_settings(derived)[SUPPORT_KEY] == "1"
        assert decision.decided_by == DECIDED_ANALYSIS

    def test_a_stated_key_is_the_callers(self, part: str):
        """``reslice_with_overrides`` or ``--support-mode off`` said no."""
        profile = resolve_slicer_profile("bambu_a1", overrides={SUPPORT_KEY: "0"})
        with patch("kiln.printability.judge_supports") as engine:
            derived, decision = ensure_profile_supports(profile, part, material="PLA")
        engine.assert_not_called()
        assert derived == profile
        assert decision == SliceSupports(False, DECIDED_STATED, "the caller stated support_material")
        assert decision.note == "supports off, as stated (the caller stated support_material)"

    def test_a_stated_on_is_read_as_on(self, part: str):
        profile = resolve_slicer_profile("bambu_a1", overrides={SUPPORT_KEY: "1"})
        _derived, decision = ensure_profile_supports(profile, part, material="PLA")
        assert decision.enabled and decision.decided_by == DECIDED_STATED

    def test_the_callers_own_profile_is_theirs(self, part: str, tmp_path: Path):
        own = tmp_path / "mine.ini"
        own.write_text("layer_height = 0.2\nsupport_material = 1\n")
        with patch("kiln.printability.judge_supports") as engine:
            derived, decision = ensure_profile_supports(str(own), part, material="PLA")
        engine.assert_not_called()
        assert derived == str(own)
        assert decision.enabled and decision.reason == "the profile is the caller's own"

    def test_the_callers_own_profile_saying_nothing_means_off(self, part: str, tmp_path: Path):
        own = tmp_path / "mine.ini"
        own.write_text("layer_height = 0.2\n")
        _derived, decision = ensure_profile_supports(str(own), part, material="PLA")
        assert not decision.enabled and decision.decided_by == DECIDED_STATED

    def test_no_profile_becomes_an_overrides_only_one(self, part: str):
        with patch("kiln.printability.judge_supports", return_value=_report(needs=True, overhang_pct=2.7)):
            derived, _decision = ensure_profile_supports(None, part, material="PLA")
        assert derived and ini_to_settings(derived)[SUPPORT_KEY] == "1"

    def test_a_part_that_needs_none_leaves_the_profile_alone(self, part: str):
        profile = resolve_slicer_profile("bambu_a1")
        with patch("kiln.printability.judge_supports", return_value=_report()):
            derived, decision = ensure_profile_supports(profile, part, material="PLA")
        assert derived == profile and not decision.settings

    def test_a_profile_naming_no_file_is_handed_on(self, part: str):
        derived, decision = ensure_profile_supports("/nowhere/none.ini", part)
        assert derived == "/nowhere/none.ini"
        assert decision.decided_by == DECIDED_NOT_CHECKED

    def test_a_door_deciding_for_every_copy_states_it(self, part: str):
        """The multi-copy slice decides once and hands each copy a stated
        profile, so the copies are not read one by one."""
        profile = resolve_slicer_profile("bambu_a1")
        with patch("kiln.printability.judge_supports", return_value=_report(needs=True, overhang_pct=2.7)):
            derived, _ = ensure_profile_supports(profile, part, material="PLA", stated=True)
        assert set(MINIMAL_SUPPORTS) <= profile_origin(derived).stated
        with patch("kiln.printability.judge_supports") as engine:
            _, decision = ensure_profile_supports(derived, part, material="PLA")
        engine.assert_not_called()
        assert decision.enabled and decision.decided_by == DECIDED_STATED

    def test_a_key_the_caller_stated_beside_the_decision_is_kept(self, part: str):
        """``reslice_with_overrides(overrides={"support_material_threshold": "30"})``
        said nothing about whether, only where: the engine decides whether
        and leaves their angle alone."""
        profile = resolve_slicer_profile("bambu_a1", overrides={"support_material_threshold": "30"})
        with patch("kiln.printability.judge_supports", return_value=_report(needs=True, overhang_pct=2.7)):
            derived, decision = ensure_profile_supports(profile, part, material="PLA")
        written = ini_to_settings(derived)
        assert decision.enabled and written[SUPPORT_KEY] == "1"
        assert written["support_material_threshold"] == "30"
        assert "support_material_threshold" in profile_origin(derived).stated

    def test_the_nozzle_the_profile_was_written_for_is_handed_to_the_check(self, part: str):
        profile = resolve_slicer_profile("bambu_a1", overrides={"nozzle_diameter": "0.6"})
        with patch("kiln.printability.judge_supports", return_value=_report()) as engine:
            ensure_profile_supports(profile, part, material="PLA")
        assert engine.call_args.kwargs["nozzle_diameter"] == 0.6


# ---------------------------------------------------------------------------
# The file is the witness
# ---------------------------------------------------------------------------


class TestTheFileIsTheWitness:
    """A decision says what Kiln asked for; the gcode says what it got."""

    def _gcode(self, tmp_path: Path, *lines: str) -> str:
        path = tmp_path / "out.gcode"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def test_support_lines_are_counted_in_both_spellings(self, tmp_path: Path):
        from kiln.slicer_supports import gcode_support_lines

        prusa = self._gcode(tmp_path, ";TYPE:Perimeter", ";TYPE:Support material", "G1 X1 E1", ";TYPE:Support material interface")
        assert gcode_support_lines(prusa) == 2
        orca = self._gcode(tmp_path, ";TYPE:Inner wall", ";TYPE:Support", ";TYPE:Support interface", ";TYPE:Supported wall")
        assert gcode_support_lines(orca) == 1
        assert gcode_support_lines(str(tmp_path / "missing.gcode")) == 0

    def test_a_slice_that_asked_for_supports_and_got_none_says_so(self, tmp_path: Path):
        from kiln.slicer_supports import witnessed

        asked = SliceSupports(True, DECIDED_ANALYSIS, "overhangs=11.1%", support_settings(45.0))
        empty = self._gcode(tmp_path, ";TYPE:Perimeter", "G1 X1 E1")
        got = witnessed(asked, empty)
        assert not got.enabled and got.decided_by == DECIDED_ANALYSIS
        assert got.reason == "overhangs=11.1%; the slicer placed none"
        assert got.note == "the part needs supports but the slicer placed none: overhangs=11.1%"
        assert got.settings == asked.settings

    def test_a_slice_whose_file_carries_them_keeps_its_answer(self, tmp_path: Path):
        from kiln.slicer_supports import witnessed

        asked = SliceSupports(True, DECIDED_ANALYSIS, "overhangs=11.1%", support_settings(45.0))
        full = self._gcode(tmp_path, ";TYPE:Support material", "G1 X1 E1")
        assert witnessed(asked, full) is asked

    def test_a_stated_on_the_slicer_ignored_is_said_in_the_callers_words(self, tmp_path: Path):
        from kiln.slicer_supports import witnessed

        stated = SliceSupports(True, DECIDED_STATED, "the caller stated support_material")
        got = witnessed(stated, self._gcode(tmp_path, ";TYPE:Perimeter"))
        assert not got.enabled
        assert got.note == "supports asked for, but the slicer placed none (the caller stated support_material)"

    def test_off_and_not_checked_are_never_second_guessed(self, tmp_path: Path):
        from kiln.slicer_supports import witnessed

        off = SliceSupports(False, DECIDED_STATED, "the caller stated support_material")
        assert witnessed(off, self._gcode(tmp_path, ";TYPE:Support material")) is off
        assert witnessed(off, None) is off


# ---------------------------------------------------------------------------
# The result says it, whichever door
# ---------------------------------------------------------------------------


class TestTheResultSaysIt:
    def test_slice_result_carries_the_decision(self):
        from kiln.slicer import SliceResult

        decision = SliceSupports(True, DECIDED_ANALYSIS, "overhangs=2.7%", dict(MINIMAL_SUPPORTS))
        d = SliceResult(success=True, output_path="/x.gcode", supports=decision).to_dict()
        assert d["supports"]["enabled"] is True
        assert d["supports"]["decided_by"] == "analysis"
        assert d["supports"]["note"] == "supports on: overhangs=2.7%"
        assert "supports" not in SliceResult(success=True).to_dict()

    def test_a_pipeline_step_carries_it(self):
        from kiln.pipelines import _slice_step_data
        from kiln.slicer import SliceResult

        decision = SliceSupports(False, DECIDED_ANALYSIS, "no overhang or bridge needs them")
        data = _slice_step_data(SliceResult(success=True, output_path="/x.gcode", supports=decision))
        assert data["supports"]["enabled"] is False

    def test_slice_file_decides_between_the_material_and_the_slicer(self, part: str, monkeypatch):
        """The layer sits inside ``slice_file`` -- the one place every
        slicing door funnels through -- after the material it is for is
        known and before the slicer runs."""
        import kiln.slicer as slicer_mod

        seen: dict[str, object] = {}

        def fake_ensure(profile, input_path, *, material=None, stated=False):
            seen["profile"] = profile
            seen["material"] = material
            return profile, SliceSupports(True, DECIDED_ANALYSIS, "overhangs=2.7%", dict(MINIMAL_SUPPORTS))

        def fake_run(cmd, **_kwargs):
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text("; generated by fake\n;TYPE:Support material\nG1 X0 Y0 Z0.2 E1\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(slicer_mod, "ensure_profile_supports", fake_ensure)
        monkeypatch.setattr(slicer_mod, "_run_slicer_with_startup_retry", fake_run)
        monkeypatch.setattr(slicer_mod, "find_slicer", lambda *_a, **_k: SimpleNamespace(
            path="/fake/prusa-slicer", name="prusaslicer", version="2.9.4",
        ))
        monkeypatch.setattr(slicer_mod, "_gcode_output_is_complete", lambda *_a: True)
        monkeypatch.setattr(slicer_mod, "_record_slice", lambda *_a, **_k: None)
        monkeypatch.setattr(slicer_mod, "_drawn_mesh_for", lambda p: p)
        result = slicer_mod.slice_file(part, profile=resolve_slicer_profile("bambu_a1"), material="PETG")
        assert seen["material"] == "PETG"
        assert "filament_density" in ini_to_settings(str(seen["profile"]))
        assert result.supports is not None and result.supports.enabled


# ---------------------------------------------------------------------------
# The Orca dialect gets the same decision
# ---------------------------------------------------------------------------


class TestOrcaGetsTheSameDecision:
    def test_all_three_keys_cross(self):
        process = settings_to_orca_presets(support_settings(45.0)).process
        assert process["enable_support"] == "1"
        assert process["support_on_build_plate_only"] == "0"
        assert process["support_threshold_angle"] == support_settings(45.0)["support_material_threshold"]
        assert process["bridge_no_support"] == "0"

    def test_prusaslicers_detect_the_angle_leaves_orcas_own(self):
        """PrusaSlicer's threshold 0 means "detect it"; Orca has no such
        setting, so 0 must not cross as a zero-degree threshold."""
        process = settings_to_orca_presets({SUPPORT_KEY: "1", "support_material_threshold": "0"}).process
        assert process["enable_support"] == "1"
        assert "support_threshold_angle" not in process

    def test_an_overrides_only_profile_is_laid_over_the_generic_one(self):
        """Orca given only a few keys has no bed and refuses the slice;
        the keys are laid over Kiln's generic profile, still stated."""
        from kiln.slicer import _whole_profile_for_orca

        only = profile_with_overrides(None, dict(MINIMAL_SUPPORTS), stated=True)
        whole = _whole_profile_for_orca(only)
        origin = profile_origin(whole)
        assert origin.printer_id == "default"
        assert set(MINIMAL_SUPPORTS) <= origin.stated
        settings = ini_to_settings(whole)
        assert settings[SUPPORT_KEY] == "1" and "layer_height" in settings

    def test_a_profile_with_a_printer_behind_it_is_handed_on(self):
        from kiln.slicer import _whole_profile_for_orca

        profile = resolve_slicer_profile("bambu_a1")
        assert _whole_profile_for_orca(profile) == profile
        assert profile_origin(_whole_profile_for_orca(None)).printer_id == "default"


# ---------------------------------------------------------------------------
# The CLI states its mode, or leaves the slice to decide
# ---------------------------------------------------------------------------


class TestTheCliMode:
    def test_each_mode_is_a_statement_and_auto_is_none(self):
        from kiln.cli.main import _support_statement

        assert _support_statement("off") == {SUPPORT_KEY: "0"}
        assert _support_statement("minimal", "PLA") == {
            **MINIMAL_SUPPORTS, "support_material_buildplate_only": "1",
        }
        assert _support_statement("aggressive", "PLA") == MINIMAL_SUPPORTS
        assert _support_statement("auto") is None
        assert _support_statement(None) is None

    def test_off_is_stated_in_the_profile_so_the_slice_keeps_it(self):
        from kiln.cli.main import _resolve_slice_plan

        plan = _resolve_slice_plan(
            SimpleNamespace(obj={}), input_file="part.stl", profile=None,
            printer_id="bambu_a1", material="PLA", support_mode="off",
        )
        assert SUPPORT_KEY in profile_origin(plan["profile_path"]).stated
        assert ini_to_settings(plan["profile_path"])[SUPPORT_KEY] == "0"
        assert plan["support_style"] == "off" and plan["support_reason"] == "explicit"
        assert not plan["extra_args"]

    def test_auto_states_nothing_and_reports_what_the_slice_decided(self):
        from kiln.cli.main import _resolve_slice_plan, _supports_said

        plan = _resolve_slice_plan(
            SimpleNamespace(obj={}), input_file="part.stl", profile=None,
            printer_id="bambu_a1", material="PLA", support_mode="auto",
        )
        assert SUPPORT_KEY not in profile_origin(plan["profile_path"]).stated
        assert plan["support_style"] is None
        decided = SimpleNamespace(supports=SliceSupports(True, DECIDED_ANALYSIS, "overhangs=2.7%"))
        assert _supports_said(plan, decided) == ("minimal", "overhangs=2.7%")
        none_needed = SimpleNamespace(supports=SliceSupports(False, DECIDED_ANALYSIS, "no overhang or bridge needs them"))
        assert _supports_said(plan, none_needed) == (None, "no overhang or bridge needs them")

    def test_a_mode_over_the_callers_own_profile_is_written_into_it(self, tmp_path: Path):
        """The mode used to be dropped whenever a profile was given."""
        from kiln.cli.main import _resolve_slice_plan

        own = tmp_path / "mine.ini"
        own.write_text("layer_height = 0.2\n")
        plan = _resolve_slice_plan(
            SimpleNamespace(obj={}), input_file="part.stl", profile=str(own),
            printer_id=None, material="PLA", support_mode="minimal",
        )
        written = ini_to_settings(plan["profile_path"])
        assert written["layer_height"] == "0.2"
        assert {key: written[key] for key in MINIMAL_SUPPORTS} == {
            **MINIMAL_SUPPORTS, "support_material_buildplate_only": "1",
        }
