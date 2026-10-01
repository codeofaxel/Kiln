"""The failure predictor's wall floor follows the nozzle.

``predict_print_failure`` called a wall thin below 0.8 mm whatever nozzle
the part was for.  0.8 mm is two lines of a 0.4 mm nozzle: a 1.0 mm wall
is fine there and too thin for a 0.6, whose two lines are 1.2 mm.  These
pin the floor to the nozzle, the tool to the fitted nozzle, and the answer
to saying which nozzle it judged for.
"""

from __future__ import annotations

import pytest

import kiln._pro_nozzle_bridge as bridge
import kiln.assumed_nozzle as assumed
from kiln.generation.validation import predict_print_failures

trimesh = pytest.importorskip("trimesh")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No record, no readable printer, nothing remembered, until a test says so."""
    monkeypatch.setattr(assumed, "_setting_memo", {})
    monkeypatch.setattr(bridge, "_record_memo", {})
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": None, "answered": True})
    monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)


def _plate(tmp_path, thickness: float) -> str:
    path = str(tmp_path / f"plate_{thickness:g}.stl")
    trimesh.creation.box(extents=(10.0, 10.0, thickness)).export(path)
    return path


def _types(result) -> dict[str, str]:
    return {f["type"]: f["severity"] for f in result["failures"]}


class TestTheFloorFollowsTheNozzle:
    @pytest.mark.parametrize(("nozzle", "floor"), [(0.2, 0.4), (0.4, 0.8), (0.6, 1.2), (0.8, 1.6)])
    def test_the_floor_is_two_lines_of_the_nozzle(self, tmp_path, nozzle, floor):
        result = predict_print_failures(_plate(tmp_path, 3.0), nozzle_mm=nozzle)
        assert result["min_wall_mm"] == pytest.approx(floor)
        assert result["min_wall_basis"] == "nozzle"

    def test_no_nozzle_given_is_the_default_nozzle(self, tmp_path):
        result = predict_print_failures(_plate(tmp_path, 3.0))
        assert result["min_wall_mm"] == pytest.approx(2 * assumed.DEFAULT_MM)

    def test_a_stated_floor_wins_and_says_so(self, tmp_path):
        result = predict_print_failures(_plate(tmp_path, 3.0), min_wall_mm=2.0, nozzle_mm=0.6)
        assert (result["min_wall_mm"], result["min_wall_basis"]) == (2.0, "stated")

    def test_a_one_millimetre_wall_is_thin_for_a_wide_nozzle_only(self, tmp_path):
        plate = _plate(tmp_path, 1.0)
        assert "thin_walls" not in _types(predict_print_failures(plate, nozzle_mm=0.4))
        assert "thin_walls" in _types(predict_print_failures(plate, nozzle_mm=0.6))

    def test_a_feature_narrower_than_the_nozzle_is_the_severe_kind(self, tmp_path):
        sliver = _plate(tmp_path, 0.5)
        assert _types(predict_print_failures(sliver, nozzle_mm=0.4))["small_features"] == "medium"
        assert _types(predict_print_failures(sliver, nozzle_mm=0.6))["small_features"] == "high"


class TestTheToolUsesTheFittedNozzle:
    def test_the_nozzle_on_record_sets_the_floor(self, tmp_path, monkeypatch):
        import kiln.server as server

        monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": 0.6, "answered": True})
        result = server.predict_print_failure(_plate(tmp_path, 1.0), printer_id="bambu_a1")
        assert result["success"] is True
        assert result["min_wall_mm"] == pytest.approx(1.2)
        assert (result["nozzle"]["diameter_mm"], result["nozzle"]["source"]) == (0.6, "record")
        assert "thin_walls" in _types(result)

    def test_a_stated_nozzle_wins(self, tmp_path, monkeypatch):
        import kiln.server as server

        monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": 0.6, "answered": True})
        result = server.predict_print_failure(_plate(tmp_path, 1.0), printer_id="bambu_a1", nozzle_mm=0.4)
        assert result["nozzle"]["source"] == "stated"
        assert "thin_walls" not in _types(result)

    def test_the_answer_says_which_nozzle_it_judged_for(self, tmp_path):
        import kiln.server as server

        result = server.predict_print_failure(_plate(tmp_path, 3.0), nozzle_mm=0.6)
        assert "0.6 mm nozzle" in result["message"]

    def test_a_guess_is_said_as_a_guess(self, tmp_path, monkeypatch):
        import kiln.server as server

        monkeypatch.setattr(assumed, "_registered_machines", lambda: [])
        result = server.predict_print_failure(_plate(tmp_path, 3.0))
        assert result["nozzle"]["source"] == "default"
        assert "Name the printer" in result["message"]

    def test_a_stated_floor_still_passes_through(self, tmp_path):
        import kiln.server as server

        result = server.predict_print_failure(_plate(tmp_path, 3.0), min_wall_mm=2.5, nozzle_mm=0.4)
        assert (result["min_wall_mm"], result["min_wall_basis"]) == (2.5, "stated")
