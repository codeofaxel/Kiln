"""A model with no real size gets one the person chooses before it is priced.

Pinned here:

* ``longest_side_mm`` sets the longest side exactly, up or down, keeping
  the shape; ``max_dimension_mm`` still only shrinks;
* the call the size check advises, run as written, sets the size;
* the note of where a model came from follows a resize, marked as a real
  size when the person chose one and left as it was by a fit to a bed;
* every door that asks a print service for a price refuses a model whose
  size nobody chose, before anything is uploaded, and a priced model's
  size is said beside the price.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from kiln import _pro_order_bridge as bridge
from kiln import arrival
from kiln.arrival import GENERATED, Arrival
from kiln.generation.validation import rescale_stl
from tests import _order_service as order_service

_PROMPT = "a small dragon curled around an egg"
_POSE = {"status": "standing", "said": "It stands the way it arrived.", "turn": {"x": 0.0, "y": 0.0, "z": 0.0}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", None)
    monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)


def _box(path: Path, extents: tuple[float, float, float]) -> str:
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=extents).export(str(path))
    return str(path)


def _extents(path: str) -> tuple[float, float, float]:
    size = arrival.measure(path)[2]
    assert size is not None
    return size


def _unsized(tmp_path: Path, name: str = "dragon.stl", extents=(1.5, 1.0, 0.5)) -> str:
    path = _box(tmp_path / name, extents)
    arrival.record(path, Arrival(kind=GENERATED, by="CloudGen", prompt=_PROMPT, job_id="job-7", real_size=False))
    return path


def _tools(module: str) -> dict:
    tools: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    __import__(module, fromlist=["plugin"]).plugin.register(_MCP())
    return tools


def _must_not_reach(*_a, **_kw):
    raise AssertionError("a model with no real size was sent to be priced")


class TestTheLongestSide:
    def test_a_model_one_unit_across_grows_to_the_size_asked(self, tmp_path):
        path = _box(tmp_path / "small.stl", (1.5, 1.0, 0.5))

        rescale_stl(path, longest_side_mm=120)

        x, y, z = _extents(path)
        assert max(x, y, z) == pytest.approx(120.0, abs=1e-3)
        assert (y / x, z / x) == pytest.approx((1.0 / 1.5, 0.5 / 1.5), rel=1e-5)

    def test_a_large_model_shrinks_to_the_size_asked(self, tmp_path):
        path = _box(tmp_path / "large.stl", (300.0, 150.0, 60.0))

        result = rescale_stl(path, longest_side_mm=120)

        x, y, z = _extents(path)
        assert max(x, y, z) == pytest.approx(120.0, abs=1e-3)
        assert (y / x, z / x) == pytest.approx((0.5, 0.2), rel=1e-5)
        assert result["new_dimensions"] == {"width_mm": 120.0, "depth_mm": 60.0, "height_mm": 24.0}

    @pytest.mark.parametrize("bad", [0, -5, math.nan, math.inf])
    def test_a_size_that_is_not_a_size_is_refused(self, tmp_path, bad):
        path = _box(tmp_path / "small.stl", (1.5, 1.0, 0.5))
        with pytest.raises(ValueError, match="longest_side_mm"):
            rescale_stl(path, longest_side_mm=bad)

    def test_it_is_one_of_the_uniform_options(self, tmp_path):
        path = _box(tmp_path / "small.stl", (1.5, 1.0, 0.5))
        with pytest.raises(ValueError, match="Exactly one"):
            rescale_stl(path, longest_side_mm=100, max_dimension_mm=50)
        with pytest.raises(ValueError, match="per-axis"):
            rescale_stl(path, longest_side_mm=100, scale_x=2)


class TestTheAdviceWorks:
    def test_the_call_the_size_check_names_sets_the_size(self, tmp_path):
        path = _unsized(tmp_path)
        check = arrival.size_check(arrival.read(path), _extents(path))

        call = re.search(r"rescale_model\(file_path, (\w+)=<[^>]+>\)", check)
        assert call, check
        assert "Ask the person how big it should be" in check
        reply = _tools("kiln.plugins.mesh_tools")["rescale_model"](file_path=path, **{call.group(1): 90.0})

        assert reply["success"] is True, reply
        assert max(_extents(path)) == pytest.approx(90.0, abs=1e-3)

    def test_a_gltf_read_as_millimeters_names_the_same_call(self):
        downloaded = Arrival(kind="downloaded", by="Sketchfab", name="Lamp", url="https://example.test/lamp")
        check = arrival.size_check(downloaded, (1.2, 0.45, 0.4), arrived_as=".glb", read_scale=1.0)
        assert arrival.RESIZE_CALL in check and "max_dimension_mm" not in check


class TestTheNoteFollowsTheResize:
    def test_a_chosen_size_is_recorded_as_real_and_the_rest_is_kept(self, tmp_path):
        path = _unsized(tmp_path)
        arrival.record_pose(path, _POSE)

        reply = _tools("kiln.plugins.mesh_tools")["rescale_model"](file_path=path, longest_side_mm=120.0)

        assert reply["success"] is True, reply
        noted = arrival.read(path)
        assert noted is not None
        assert (noted.by, noted.prompt, noted.job_id, noted.real_size) == ("CloudGen", _PROMPT, "job-7", True)
        assert arrival.read_pose(path)["said"] == _POSE["said"]

    @pytest.mark.parametrize(
        "option",
        [{"target_height_mm": 40.0}, {"scale_factor": 50.0}, {"scale_x": 20.0, "scale_y": 20.0}],
    )
    def test_every_size_the_person_names_counts_as_chosen(self, tmp_path, option):
        path = _unsized(tmp_path)
        _tools("kiln.plugins.mesh_tools")["rescale_model"](file_path=path, **option)
        assert arrival.read(path).real_size is True

    def test_a_fit_to_the_bed_is_not_a_chosen_size(self, tmp_path):
        path = _unsized(tmp_path, extents=(900.0, 600.0, 300.0))
        arrival.record_pose(path, _POSE)

        _tools("kiln.plugins.mesh_tools")["rescale_model"](file_path=path, max_dimension_mm=200.0)

        assert max(_extents(path)) == pytest.approx(200.0, abs=1e-3)
        noted = arrival.read(path)
        assert noted is not None and noted.real_size is False and noted.prompt == _PROMPT
        assert arrival.read_pose(path) is not None

    def test_scaling_to_fit_a_bed_keeps_the_note_unchanged(self, tmp_path):
        path = _unsized(tmp_path, extents=(900.0, 600.0, 300.0))
        out = str(tmp_path / "fitted.stl")

        reply = _tools("kiln.plugins.mesh_tools")["scale_mesh_to_fit"](file_path=path, output_path=out)

        assert reply["success"] is True, reply
        assert max(_extents(out)) == pytest.approx(256.0, abs=1e-2)
        noted = arrival.read(out)
        assert noted is not None and noted.real_size is False and noted.by == "CloudGen"

    def test_a_unit_correction_follows_the_copy_unchanged(self, tmp_path):
        from kiln.plugins._validation_pipeline_internals import _apply_scale

        path = _unsized(tmp_path, extents=(0.06, 0.04, 0.02))
        copy, factor = _apply_scale(path, 1000.0)

        assert copy is not None and factor == 1000.0
        noted = arrival.read(copy)
        assert noted is not None and noted.real_size is False and noted.prompt == _PROMPT


def _served_quote_answer():
    return bridge.Answer(
        body={"quote": {"quote_id": "q-9", "total_price": 10.5}, "tell_the_person": ["1 copy: 10.50 USD."]}
    )


class TestAQuoteWaitsForASize:
    def test_the_served_quote_is_not_asked_for_an_unsized_model(self, tmp_path, monkeypatch):
        order_service.remove(monkeypatch)
        monkeypatch.setattr(bridge, "quote", _must_not_reach)
        path = _unsized(tmp_path)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=path, material_id="pla")

        assert reply["success"] is False
        assert reply["error"]["code"] == "SIZE_NOT_CHOSEN"
        assert reply["error"]["message"] == (
            "This model came from CloudGen with no real size (it measures 1.5 x 1 x 0.5 across in no real "
            "unit). A print service would price those numbers as millimeters, so nothing was quoted. Ask the "
            "person how big it should be, set it with rescale_model(file_path, longest_side_mm=<the size the "
            "person wants>), then quote again."
        )
        assert reply["extents"] == pytest.approx([1.5, 1.0, 0.5])
        assert reply["next_call"]["tool"] == "rescale_model"
        assert reply["next_call"]["args"]["file_path"] == path

    def test_the_local_order_service_is_not_asked_for_an_unsized_model(self, tmp_path, monkeypatch):
        import kiln.server as server

        order_service.provide(monkeypatch)
        monkeypatch.setattr(server, "_get_fulfillment", _must_not_reach)
        path = _unsized(tmp_path)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=path, material_id="pla")

        assert reply["error"]["code"] == "SIZE_NOT_CHOSEN"

    def test_once_sized_the_served_quote_says_the_size_it_priced(self, tmp_path, monkeypatch):
        order_service.remove(monkeypatch)
        asked: list = []
        monkeypatch.setattr(bridge, "quote", lambda *a, **k: asked.append(a) or _served_quote_answer())
        path = _unsized(tmp_path)
        _tools("kiln.plugins.mesh_tools")["rescale_model"](file_path=path, longest_side_mm=120.0)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=path, material_id="pla")

        assert reply["success"] is True, reply
        assert len(asked) == 1
        assert reply["priced_size_mm"]["width_mm"] == pytest.approx(120.0, abs=0.01)
        assert reply["priced_size_mm"]["depth_mm"] == pytest.approx(80.0, abs=0.01)
        assert reply["priced_size_mm"]["height_mm"] == pytest.approx(40.0, abs=0.01)
        assert reply["tell_the_person"] == [
            "1 copy: 10.50 USD.",
            "Priced at the size in the file: 120.0 x 80.0 x 40.0 mm (width x depth x height).",
        ]

    def test_once_sized_the_local_quote_says_the_size_it_priced(self, tmp_path, monkeypatch):
        import kiln.server as server

        order_service.provide(monkeypatch)
        quote = SimpleNamespace(
            quote_id="q-1",
            provider="craftcloud",
            material="pla",
            quantity=1,
            total_price=10.0,
            currency="USD",
            lead_time_days=5,
            shipping_options=[],
            to_dict=lambda: {"quote_id": "q-1"},
        )
        monkeypatch.setattr(
            server, "_get_fulfillment", lambda: SimpleNamespace(name="craftcloud", get_quote=lambda r: quote)
        )
        monkeypatch.setattr(server, "_get_billing", lambda: None)
        monkeypatch.setattr(server, "_get_payment_mgr", lambda: SimpleNamespace(available_rails=[]))
        monkeypatch.setattr(server, "_provider_routing_metadata", lambda provider, **_kw: {"provider": provider})
        path = _unsized(tmp_path)
        _tools("kiln.plugins.mesh_tools")["rescale_model"](file_path=path, longest_side_mm=60.0)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=path, material_id="pla")

        assert reply["success"] is True, reply
        assert reply["priced_size_mm"]["summary"] == "60.0 x 40.0 x 20.0 mm"
        assert reply["tell_the_person"][-1].startswith("Priced at the size in the file: 60.0 x 40.0 x 20.0 mm")

    def test_a_file_with_no_note_is_quoted_as_before(self, tmp_path, monkeypatch):
        order_service.remove(monkeypatch)
        monkeypatch.setattr(bridge, "quote", lambda *a, **k: _served_quote_answer())
        path = _box(tmp_path / "bracket.stl", (40.0, 20.0, 10.0))

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=path, material_id="pla")

        assert reply["success"] is True and reply["priced_size_mm"]["summary"] == "40.0 x 20.0 x 10.0 mm"

    def test_a_model_that_cannot_be_measured_is_still_quoted(self, tmp_path, monkeypatch):
        order_service.remove(monkeypatch)
        monkeypatch.setattr(bridge, "quote", lambda *a, **k: _served_quote_answer())
        path = tmp_path / "empty.stl"
        path.write_bytes(b"solid empty\nendsolid empty\n")

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=str(path), material_id="pla")

        assert reply["success"] is True and "priced_size_mm" not in reply
        assert reply["tell_the_person"] == ["1 copy: 10.50 USD."]


class TestTheOtherPricingDoors:
    def test_compare_print_options_does_not_price_an_unsized_model(self, tmp_path, monkeypatch):
        import kiln.server as server

        order_service.remove(monkeypatch)
        monkeypatch.setattr(bridge, "quote", _must_not_reach)
        monkeypatch.setattr(server, "_get_fulfillment", _must_not_reach)
        path = _unsized(tmp_path)

        result = server.compare_print_options(file_path=path, fulfillment_material_id="pla")

        assert result["fulfillment"]["available"] is False
        assert result["fulfillment"]["code"] == "SIZE_NOT_CHOSEN"
        assert "no real size" in result["fulfillment"]["error"]
        assert result["fulfillment"]["next_call"]["tool"] == "rescale_model"

    def test_compare_print_options_says_the_size_it_priced(self, tmp_path, monkeypatch):
        import kiln.server as server

        order_service.remove(monkeypatch)
        monkeypatch.setattr(bridge, "quote", lambda *a, **k: _served_quote_answer())
        path = _box(tmp_path / "bracket.stl", (40.0, 20.0, 10.0))

        result = server.compare_print_options(file_path=path, fulfillment_material_id="pla")

        assert result["fulfillment"]["available"] is True
        assert result["fulfillment"]["priced_size_mm"]["summary"] == "40.0 x 20.0 x 10.0 mm"

    def test_kiln_order_quote_does_not_price_an_unsized_model(self, tmp_path, monkeypatch):
        import kiln.cli.main as cli_main

        monkeypatch.setattr(cli_main, "_order_service_or_served", _must_not_reach)
        path = _unsized(tmp_path)

        out = CliRunner().invoke(cli_main.cli, ["order", "quote", path, "-m", "pla", "--json"])

        assert out.exit_code == 1, out.output
        assert "SIZE_NOT_CHOSEN" in out.output and "rescale_model(file_path, longest_side_mm=" in out.output

    def test_kiln_compare_cost_does_not_price_an_unsized_model(self, tmp_path, monkeypatch):
        import json

        import kiln.cli.main as cli_main

        monkeypatch.setattr(cli_main, "_order_service_or_served", _must_not_reach)
        path = _unsized(tmp_path)

        out = CliRunner().invoke(cli_main.cli, ["compare-cost", path, "--fulfillment-material", "pla", "--json"])

        assert out.exit_code == 0, out.output
        fulfillment = json.loads(out.output)["data"]["fulfillment"]
        assert fulfillment["available"] is False and fulfillment["code"] == "SIZE_NOT_CHOSEN"
