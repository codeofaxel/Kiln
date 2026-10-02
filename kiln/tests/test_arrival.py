"""A model that arrives from outside Kiln opens on the stage and says where it came from.

Measured 2026-09-30, a live Tripo job: ``download_generated_model`` handed
back a bare path for a "40 mm calibration cube" that arrived 1.0 units
across, and ``download_model`` did the same for a marketplace download.  No
stage opened on either, and nothing remembered where the file came from.

Pinned here:

* the ENGINES leave the note — every provider's ``download_result`` and every
  marketplace's ``download_file``, through their base classes — so a door
  added later is covered without remembering to;
* the note is about the file's bytes: another file saved under the same
  name is never credited to the wrong source, and a conversion carries it;
* both download doors open the stage on what arrived and say where it came
  from, with the size check a shape nobody measured needs;
* the stage on an existing file and the source lookup read the same note.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiln import arrival, local_stage, preview_evidence, stage_cache
from kiln.arrival import DOWNLOADED, GENERATED, Arrival
from kiln.generation.base import GenerationJob, GenerationProvider, GenerationResult, GenerationStatus
from kiln.marketplaces.base import MarketplaceAdapter, MarketplaceError, ModelDetail, ModelFile
from tests.test_an_existing_file_on_the_stage import _through_the_hook
from tests.test_local_stage import _cache_the_stage, _Caps, _Host

_UI = local_stage.MCP_APPS_EXTENSION_ID
_PROMPT = "a simple 40 mm calibration cube with the letter K embossed on top"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh stage ledgers, the stage on, no link door, and the free install:
    no design-history bundle rides these doors."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(local_stage._OPT_OUT_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", None)
    monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    preview_evidence._reset_for_tests()
    yield
    local_stage._reset_for_tests()
    stage_cache._reset_for_tests()
    preview_evidence._reset_for_tests()


def _cube(path: Path, side: float) -> str:
    trimesh = pytest.importorskip("trimesh")
    trimesh.creation.box(extents=(side, side, side)).export(str(path))
    return str(path)


class _Cloud(GenerationProvider):
    """A cloud generator asked for a shape: its file arrives one unit across."""

    side = 1.0

    def __init__(self, out_dir: Path) -> None:
        self._out = out_dir

    @property
    def name(self) -> str:
        return "cloudgen"

    @property
    def display_name(self) -> str:
        return "CloudGen"

    def generate(self, prompt, **kwargs):
        raise NotImplementedError

    def get_job_status(self, job_id):
        return GenerationJob(job_id, self.name, _PROMPT, GenerationStatus.SUCCEEDED)

    def download_result(self, job_id, output_dir=""):
        self._out.mkdir(parents=True, exist_ok=True)
        path = _cube(self._out / f"{job_id}.stl", self.side)
        return GenerationResult(job_id, self.name, path, "stl", os.path.getsize(path), _PROMPT)


class _MillimetreCloud(_Cloud):
    sets_real_size = True
    side = 40.0


class _LocalCompiler(_Cloud):
    drawn_elsewhere = False


class _Market(MarketplaceAdapter):
    """A marketplace serving a listing of real files from a folder."""

    def __init__(self, files: dict[str, bytes | float], *, listing: ModelDetail | None = None) -> None:
        self._files = files
        self._listing = listing

    @property
    def name(self) -> str:
        return "fakemarket"

    @property
    def display_name(self) -> str:
        return "FakeMarket"

    def search(self, query, **kwargs):
        return []

    def get_details(self, model_id):
        if self._listing is None:
            raise MarketplaceError("listing unavailable")
        return self._listing

    def get_files(self, model_id):
        return [ModelFile(id=name, name=name) for name in self._files]

    def download_file(self, file_id, dest_dir, *, file_name=None):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        target = Path(dest_dir) / (file_name or file_id)
        body = self._files[file_id]
        if isinstance(body, float):
            return _cube(target, body)
        target.write_bytes(body)
        return str(target)


_LISTING = ModelDetail(
    id="m-1",
    name="Hinged box",
    url="https://fakemarket.example/m-1",
    creator="Ada",
    source="fakemarket",
    license="CC BY 4.0",
)


# ---------------------------------------------------------------------------
# The engines leave the note
# ---------------------------------------------------------------------------


class TestTheEnginesLeaveTheNote:
    def test_a_cloud_provider_download_leaves_a_note(self, tmp_path):
        result = _Cloud(tmp_path).download_result("job-1")
        assert arrival.read(result.local_path) == Arrival(
            kind=GENERATED,
            by="CloudGen",
            prompt=_PROMPT,
            job_id="job-1",
            real_size=False,
        )

    def test_a_provider_that_draws_in_millimetres_says_so(self, tmp_path):
        result = _MillimetreCloud(tmp_path).download_result("job-2")
        assert arrival.read(result.local_path).real_size is True

    def test_a_compiler_on_this_machine_leaves_none(self, tmp_path):
        result = _LocalCompiler(tmp_path).download_result("job-3")
        assert arrival.read(result.local_path) is None
        assert not Path(arrival.note_path_for(result.local_path)).exists()

    def test_the_shipped_providers_say_what_they_draw(self):
        from kiln.generation.gemini import GeminiDeepThinkProvider
        from kiln.generation.meshy import MeshyProvider
        from kiln.generation.openscad import OpenSCADProvider
        from kiln.generation.stability import StabilityProvider
        from kiln.generation.tripo3d import Tripo3DProvider

        assert (OpenSCADProvider.sets_real_size, OpenSCADProvider.drawn_elsewhere) == (True, False)
        assert (GeminiDeepThinkProvider.sets_real_size, GeminiDeepThinkProvider.drawn_elsewhere) == (True, True)
        for asked_for_a_shape in (Tripo3DProvider, MeshyProvider, StabilityProvider):
            assert asked_for_a_shape.sets_real_size is False, asked_for_a_shape
            assert asked_for_a_shape.drawn_elsewhere is True, asked_for_a_shape

    def test_a_marketplace_download_leaves_a_note(self, tmp_path):
        path = _Market({"part.stl": 20.0}).download_file("part.stl", str(tmp_path))
        assert arrival.read(path) == Arrival(kind=DOWNLOADED, by="FakeMarket", file_id="part.stl")

    def test_a_download_with_nowhere_to_note_still_arrives(self, tmp_path):
        class _Gone(_Cloud):
            def download_result(self, job_id, output_dir=""):
                return GenerationResult(job_id, self.name, str(tmp_path / "missing.stl"), "stl", 0, "")

        assert _Gone(tmp_path).download_result("job-4").job_id == "job-4"


class TestTheNoteIsAboutTheBytes:
    def test_another_file_under_the_same_name_is_not_credited(self, tmp_path):
        result = _Cloud(tmp_path).download_result("job-5")
        _cube(Path(result.local_path), 30.0)
        assert arrival.read(result.local_path) is None

    def test_a_conversion_carries_the_note(self, tmp_path):
        trimesh = pytest.importorskip("trimesh")
        obj = tmp_path / "model.obj"
        trimesh.creation.box(extents=(1.0, 1.0, 1.0)).export(str(obj))
        noted = Arrival(kind=GENERATED, by="CloudGen", prompt=_PROMPT, real_size=False)
        arrival.record(str(obj), noted)

        from kiln.format_conversion import convert_to_stl_recorded

        stl, _record = convert_to_stl_recorded(str(obj), tool="test")
        assert arrival.read(stl) == noted

    def test_an_unreadable_note_is_no_note(self, tmp_path):
        path = _cube(tmp_path / "part.stl", 10.0)
        Path(arrival.note_path_for(path)).write_text("{not json", encoding="utf-8")
        assert arrival.read(path) is None


class TestTheLine:
    def test_a_generated_model_names_the_generator_and_the_prompt(self):
        noted = Arrival(kind=GENERATED, by="Tripo3D", prompt=_PROMPT)
        assert noted.line() == f'Generated with Tripo3D from "{_PROMPT}".'

    def test_a_long_prompt_is_cut_at_a_word(self):
        line = Arrival(kind=GENERATED, by="Meshy", prompt="word " * 40).line()
        assert line.endswith('…".') and len(line) < 120

    def test_a_download_names_the_listing_its_designer_and_license(self):
        noted = Arrival(
            kind=DOWNLOADED,
            by="FakeMarket",
            name="Hinged box",
            creator="Ada",
            license="CC BY 4.0",
            url="https://fakemarket.example/m-1",
        )
        assert noted.line() == (
            'Downloaded from FakeMarket: "Hinged box" by Ada, licensed CC BY 4.0 (https://fakemarket.example/m-1).'
        )

    def test_a_listing_with_no_license_says_so(self):
        line = Arrival(kind=DOWNLOADED, by="FakeMarket", name="Hinged box", creator="Ada").line()
        assert "no license stated on the listing" in line

    def test_the_stage_caption_drops_the_link_and_the_period(self):
        noted = Arrival(
            kind=DOWNLOADED,
            by="FakeMarket",
            name="Hinged box",
            creator="Ada",
            license="CC BY 4.0",
            url="https://fakemarket.example/m-1",
        )
        assert noted.caption() == 'Downloaded from FakeMarket: "Hinged box" by Ada, licensed CC BY 4.0'
        assert Arrival(kind=GENERATED, by="Meshy").caption() == "Generated with Meshy"
        assert Arrival(kind=DOWNLOADED, by="FakeMarket").caption() == (
            "Downloaded from FakeMarket · designer and license not recorded"
        )

    def test_the_stage_reads_the_caption_and_whether_the_size_is_real(self, tmp_path):
        path = _cube(tmp_path / "part.stl", 1.0)
        noted = Arrival(kind=GENERATED, by="Tripo3D", prompt=_PROMPT, real_size=False)
        arrival.record(path, noted)
        assert arrival.stage_block(path) == {
            "kind": "kiln.arrival.v1",
            "came_from": noted.caption(),
            "real_size": False,
        }
        assert arrival.stage_block(_cube(tmp_path / "mine.stl", 1.0)) is None

    def test_an_unread_listing_says_so_rather_than_guessing(self):
        line = Arrival(kind=DOWNLOADED, by="FakeMarket", file_id="7").line()
        assert line == (
            "Downloaded from FakeMarket. Its listing was not read, so who designed it and its license are not recorded."
        )


class TestTheSizeCheck:
    def test_a_shape_with_no_size_is_told_to_get_one(self):
        check = arrival.size_check(Arrival(kind=GENERATED, by="Tripo3D", real_size=False), (1.0, 0.924, 0.932))
        assert "Tripo3D was asked for a shape, not a size" in check
        assert "1 x 0.924 x 0.932" in check and "rescale_model(" in check

    def test_a_generator_told_to_draw_in_millimetres_still_gets_the_units_reading(self):
        drawn_in_mm = Arrival(kind=GENERATED, by="Gemini Deep Think", real_size=True)
        assert arrival.size_check(drawn_in_mm, (40.0, 40.0, 40.0)) == ""
        assert "meters" in arrival.size_check(drawn_in_mm, (0.04, 0.04, 0.04))
        assert "shape, not a size" not in arrival.size_check(drawn_in_mm, (0.04, 0.04, 0.04))

    def test_a_download_is_judged_by_its_units(self):
        downloaded = Arrival(kind=DOWNLOADED, by="FakeMarket")
        assert arrival.size_check(downloaded, (40.0, 30.0, 20.0)) == ""
        assert "meters" in arrival.size_check(downloaded, (0.04, 0.03, 0.02))


class TestAGltfIsReadInMetresFirst:
    """glTF 2.0, Coordinate System and Units: "The units for all linear
    distances are meters."  A 50 mm part saved to that rule arrives as 0.05,
    and the general reading offered metres beside inches as equal guesses —
    or, for a 1.2 m bench, never mentioned metres at all."""

    _DOWNLOADED = Arrival(kind=DOWNLOADED, by="FakeMarket")

    def test_a_part_saved_in_metres_is_read_in_metres(self):
        check = arrival.size_check(self._DOWNLOADED, (0.05, 0.03, 0.02), arrived_as="glb")
        assert check == (
            "This is a glTF file, and glTF measures in metres: read that way it is 50 mm at its "
            "largest (as millimetres, 0.05 mm). Nothing was rescaled. If it is in metres, "
            "rescale_model(file_path, scale_factor=1000) sets it to 50 mm."
        )

    def test_a_model_bigger_than_any_printer_in_metres_says_so(self):
        check = arrival.size_check(self._DOWNLOADED, (1.2, 0.45, 0.4), arrived_as=".glb")
        assert check.startswith("This is a glTF file, and glTF measures in metres: read that way it is 1200 mm")
        assert "bigger than any printer in Kiln's catalog" in check
        assert "max_dimension_mm=" in check and "split_mesh_to_fit" in check
        assert "centimeters" not in check and "inches" not in check

    def test_a_gltf_already_in_millimetres_is_left_alone(self):
        assert arrival.size_check(self._DOWNLOADED, (40.0, 20.0, 10.0), arrived_as="glb") == ""

    def test_a_generators_glb_still_has_no_size(self):
        unsized = Arrival(kind=GENERATED, by="Tripo3D", real_size=False)
        check = arrival.size_check(unsized, (1.0, 0.9, 0.6), arrived_as="glb")
        assert "Tripo3D was asked for a shape, not a size" in check
        assert "glTF" not in check

    def test_another_format_keeps_the_general_reading(self):
        check = arrival.size_check(self._DOWNLOADED, (0.05, 0.03, 0.02), arrived_as="stl")
        assert "glTF" not in check and "inches" in check

    def test_the_file_suffix_speaks_when_no_door_says_otherwise(self, tmp_path):
        path = _cube(tmp_path / "part.glb", 0.05)
        arrival.record(path, self._DOWNLOADED)
        result = arrival.announce({}, path, size=(0.05, 0.05, 0.05))
        assert result["size_check"].startswith("This is a glTF file")


# ---------------------------------------------------------------------------
# The download doors open the stage
# ---------------------------------------------------------------------------


def _served(*plugins):
    from kiln.mcp_compat import FastMCP

    mcp = FastMCP("test")
    for plugin in plugins:
        plugin.register(mcp)
    _cache_the_stage()
    local_stage.install(mcp)
    return mcp


def _call(mcp, name: str, **kwargs) -> dict:
    """The door's own return, handed to the real result hook as a panel host."""
    body = mcp._tool_manager._tools[name].fn(**kwargs)  # type: ignore[attr-defined]
    return _through_the_hook(mcp, _Host(_Caps(extensions={_UI: {}})), body, name=name)


def _generation_door():
    from kiln.plugins.generation_ai_tools import plugin

    return _served(plugin)


def _marketplace_door(monkeypatch, market: _Market):
    from kiln.marketplaces import MarketplaceRegistry
    from kiln.plugins.marketplace_tools import plugin

    registry = MarketplaceRegistry()
    registry.register(market)
    monkeypatch.setattr("kiln.server._marketplace_registry", registry)
    return _served(plugin)


class TestTheDownloadDoorsOpenTheStage:
    def test_both_download_doors_are_on_the_roster(self):
        assert {"download_generated_model", "download_model"} <= local_stage.VIEWER_TOOLS

    def test_a_generated_model_arrives_on_the_stage_saying_where_it_came_from(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiln.server._get_generation_provider", lambda name: _Cloud(tmp_path / "gen"))
        sc = _call(
            _generation_door(),
            "download_generated_model",
            job_id="job-9",
            provider="cloudgen",
            output_path=str(tmp_path / "out"),
        )

        stl = str(tmp_path / "gen" / "job-9.stl")
        assert sc["success"] is True
        assert sc["stage_mesh_path"] == stl
        assert local_stage.resolve(sc["artifact"]["artifact_token"]) == stl
        assert sc["came_from"] == f'Generated with CloudGen from "{_PROMPT}".'
        assert sc["message"].startswith(sc["came_from"])
        assert "CloudGen was asked for a shape, not a size" in sc["size_check"]
        assert "mm" not in sc["dimensions"]["summary"]

    def test_a_model_drawn_in_millimetres_keeps_its_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiln.server._get_generation_provider", lambda name: _MillimetreCloud(tmp_path / "gen"))
        sc = _call(_generation_door(), "download_generated_model", job_id="job-10", provider="cloudgen")
        assert "size_check" not in sc
        assert sc["dimensions"]["summary"].endswith(" mm")

    def test_a_marketplace_model_arrives_on_the_stage_with_its_listing(self, tmp_path, monkeypatch):
        market = _Market({"readme.pdf": b"%PDF-1.4", "box.stl": 40.0, "lid.stl": 40.0}, listing=_LISTING)
        sc = _call(
            _marketplace_door(monkeypatch, market),
            "download_model",
            model_id="m-1",
            source="fakemarket",
            dest_dir=str(tmp_path),
        )

        box = str(tmp_path / "box.stl")
        assert sc["success"] is True and sc["downloaded_count"] == 3
        assert sc["stage_mesh_path"] == box  # the first file the stage can draw
        assert local_stage.resolve(sc["artifact"]["artifact_token"]) == box
        assert sc["came_from"] == (
            'Downloaded from FakeMarket: "Hinged box" by Ada, licensed CC BY 4.0 (https://fakemarket.example/m-1).'
        )
        assert sc["dimensions"]["summary"] == "40.0 x 40.0 x 40.0 mm"
        assert "size_check" not in sc
        for name in ("readme.pdf", "box.stl", "lid.stl"):
            noted = arrival.read(str(tmp_path / name))
            assert (noted.name, noted.creator, noted.license, noted.file_id) == (
                "Hinged box",
                "Ada",
                "CC BY 4.0",
                name,
            )

    def test_an_unread_listing_is_said_and_the_files_still_arrive(self, tmp_path, monkeypatch):
        market = _Market({"box.stl": 40.0}, listing=None)
        sc = _call(
            _marketplace_door(monkeypatch, market),
            "download_model",
            model_id="m-1",
            source="fakemarket",
            dest_dir=str(tmp_path),
        )
        assert sc["success"] is True
        assert "Its listing was not read" in sc["came_from"]
        assert sc["stage_mesh_path"] == str(tmp_path / "box.stl")

    def test_a_units_mix_up_in_a_download_is_named(self, tmp_path, monkeypatch):
        market = _Market({"box.stl": 0.04}, listing=_LISTING)
        sc = _call(
            _marketplace_door(monkeypatch, market),
            "download_model",
            model_id="m-1",
            source="fakemarket",
            dest_dir=str(tmp_path),
        )
        assert "meters" in sc["size_check"]

    def test_a_download_the_stage_cannot_draw_still_says_where_it_came_from(self, tmp_path, monkeypatch):
        market = _Market({"model.zip": b"PK\x03\x04"}, listing=_LISTING)
        sc = _call(
            _marketplace_door(monkeypatch, market),
            "download_model",
            model_id="m-1",
            source="fakemarket",
            dest_dir=str(tmp_path),
        )
        assert "stage_mesh_path" not in sc
        assert sc["came_from"].startswith('Downloaded from FakeMarket: "Hinged box"')
        assert sc["shown"]["door"] == "none"

    def test_the_single_file_thingiverse_door_arrives_the_same_way(self, tmp_path, monkeypatch):
        class _Client:
            def download_file(self, file_id, dest_dir, *, file_name=None):
                return _cube(Path(dest_dir) / "thing.stl", 25.0)

        monkeypatch.setattr("kiln.server._get_thingiverse", lambda: _Client())
        from kiln.plugins.marketplace_tools import plugin

        sc = _call(_served(plugin), "download_model", file_id=7, dest_dir=str(tmp_path))
        thing = str(tmp_path / "thing.stl")
        assert sc["stage_mesh_path"] == thing
        assert local_stage.resolve(sc["artifact"]["artifact_token"]) == thing
        assert sc["came_from"].startswith("Downloaded from Thingiverse. Its listing was not read")
        assert arrival.read(thing).file_id == "7"

    def test_the_one_shot_generator_says_where_its_best_attempt_came_from(self, tmp_path, monkeypatch):
        from kiln.original_design import OriginalDesignGeneration, OriginalDesignGenerationAttempt

        best = _Cloud(tmp_path).download_result("job-11").local_path
        session = OriginalDesignGeneration(
            requirements_text="a calibration cube",
            provider_requested="cloudgen",
            provider_used="cloudgen",
            provider_selection_reason="",
            material=None,
            printer_model=None,
            style=None,
            max_attempts=1,
            attempts_made=1,
            ready_for_print=False,
            best_attempt_number=1,
            best_readiness_score=0,
            best_readiness_grade="F",
            best_result_path=best,
            summary="One attempt.",
            next_actions=[],
            design_requirements={},
            initial_prompt={},
            attempts=[
                OriginalDesignGenerationAttempt(
                    attempt_number=1,
                    prompt_used=_PROMPT,
                    provider="cloudgen",
                    status="audited",
                    ready_for_print=False,
                    readiness_score=0,
                    readiness_grade="F",
                    mesh_validation={
                        "bounding_box": {
                            "x_min": -0.5,
                            "x_max": 0.5,
                            "y_min": -0.5,
                            "y_max": 0.5,
                            "z_min": -0.5,
                            "z_max": 0.5,
                        }
                    },
                )
            ],
        )
        monkeypatch.setattr("kiln.original_design.generate_original_design", lambda *a, **k: session)
        from kiln.plugins.generation_tools import plugin

        sc = _call(_served(plugin), "generate_model_with_provider", requirements="a calibration cube")
        assert local_stage.resolve(sc["artifact"]["artifact_token"]) == best
        assert sc["came_from"] == f'Generated with CloudGen from "{_PROMPT}".'
        assert "CloudGen was asked for a shape, not a size" in sc["size_check"]


# ---------------------------------------------------------------------------
# Later doors read the same note
# ---------------------------------------------------------------------------


class TestLaterDoorsReadTheNote:
    def _show(self, path: str) -> dict:
        from kiln.plugins.mesh_tools import plugin

        return _call(_served(plugin), "show_on_stage", file_path=path)

    def test_the_stage_says_where_a_download_came_from(self, tmp_path):
        path = _Market({"box.stl": 40.0}).download_file("box.stl", str(tmp_path))
        sc = self._show(path)
        assert sc["stage_mesh_path"] == path
        assert sc["came_from"].startswith("Downloaded from FakeMarket.")

    def test_the_stage_says_nothing_of_a_file_with_no_note(self, tmp_path):
        sc = self._show(_cube(tmp_path / "mine.stl", 20.0))
        assert sc["success"] is True and "came_from" not in sc

    def test_the_source_lookup_answers_from_the_note_for_any_format(self, tmp_path):
        from kiln.server import resolve_model_source

        market = _Market({"box.stl": 40.0}, listing=_LISTING)
        path = market.download_file("box.stl", str(tmp_path))
        arrival.record(
            path,
            Arrival(
                kind=DOWNLOADED,
                by="FakeMarket",
                name="Hinged box",
                creator="Ada",
                license="CC BY 4.0",
                url=_LISTING.url,
            ),
        )
        found = resolve_model_source(path)
        assert (found["source"], found["title"], found["designer"], found["license"], found["model_url"]) == (
            "FakeMarket",
            "Hinged box",
            "Ada",
            "CC BY 4.0",
            _LISTING.url,
        )
        assert json.loads(json.dumps(found))["came_from"].startswith("Downloaded from FakeMarket")
