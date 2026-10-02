"""A GLB that arrives from outside Kiln is handed on as an STL, at every door.

Measured 2026-10-02 on public main: a GLB from ``download_model`` arrived with
no stage, no measurement (so no size check could fire) and no way to print it,
because the stage, its browser link and the slicer all refuse a GLB.  A
generator's GLB was converted at one door and kept raw at the CLI's, and the
one-shot generator refused it outright.

Pinned here:

* every door a file arrives through hands a GLB on as the STL beside it, with
  the original kept on disk and named in ``conversion``;
* a listing's own STL beside its GLB is never replaced;
* a glTF saved in metres is read in metres first;
* a GLB that cannot be read says so instead of arriving silently;
* every download call in the source reaches the one helper, or says why not.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import kiln
from kiln import arrival, local_stage, server
from kiln.generation.base import GenerationJob, GenerationResult, GenerationStatus
from kiln.marketplaces import MarketplaceRegistry
from tests.test_arrival import (  # noqa: F401 — _isolated is this file's autouse fixture too
    _LISTING,
    _PROMPT,
    _call,
    _Cloud,
    _generation_door,
    _isolated,
    _Market,
    _marketplace_door,
    _served,
)
from tests.test_watchdog_attached_to_every_print import (  # noqa: F401 — _fresh_process is autouse
    _Bench,
    _fresh_process,
    _on_the_server,
    _tool,
)

trimesh = pytest.importorskip("trimesh")


def _glb_bytes(tmp_path: Path, extents: tuple[float, float, float] = (40.0, 10.0, 20.0)) -> bytes:
    """A box written as glTF, whose own frame puts up on +Y: it stands
    40 x 20 x 10 once read the way it was made."""
    scratch = tmp_path / "_made.glb"
    trimesh.Scene(trimesh.creation.box(extents=extents)).export(str(scratch))
    data = scratch.read_bytes()
    scratch.unlink()
    return data


def _extents(path: str) -> tuple[float, ...]:
    from kiln.mesh_frame import load_mesh

    return tuple(round(float(v), 3) for v in load_mesh(path).extents)


class _GlbCloud(_Cloud):
    """A cloud generator whose download is a GLB, about one unit across."""

    def download_result(self, job_id, output_dir=""):
        self._out.mkdir(parents=True, exist_ok=True)
        path = self._out / f"{job_id}.glb"
        trimesh.Scene(trimesh.creation.box(extents=(1.0, 0.9, 0.6))).export(str(path))
        return GenerationResult(job_id, self.name, str(path), "glb", path.stat().st_size, _PROMPT)


# ---------------------------------------------------------------------------
# The marketplace door
# ---------------------------------------------------------------------------


class TestTheMarketplaceDoor:
    def _download(self, monkeypatch, tmp_path, files, listing=_LISTING) -> tuple[dict, Path]:
        folder = tmp_path / "downloads"
        sc = _call(
            _marketplace_door(monkeypatch, _Market(files, listing=listing)),
            "download_model",
            model_id="m-1",
            source="fakemarket",
            dest_dir=str(folder),
        )
        return sc, folder

    def test_a_glb_arrives_on_the_stage_as_the_stl_beside_it(self, tmp_path, monkeypatch):
        sc, folder = self._download(monkeypatch, tmp_path, {"part.glb": _glb_bytes(tmp_path)})

        stl, glb = str(folder / "part.stl"), str(folder / "part.glb")
        entry = sc["downloaded"][0]
        assert entry["local_path"] == stl
        assert (entry["conversion"]["from_format"], entry["conversion"]["original_path"]) == ("glb", glb)
        assert Path(glb).is_file()
        assert sc["stage_mesh_path"] == stl
        assert local_stage.resolve(sc["artifact"]["artifact_token"]) == stl
        # Measured, and standing the way it was made.
        assert sc["dimensions"]["summary"] == "40.0 x 20.0 x 10.0 mm"
        assert "size_check" not in sc
        # The listing reached the original and the copy alike.
        assert sc["came_from"].startswith('Downloaded from FakeMarket: "Hinged box" by Ada')
        assert arrival.read(stl).creator == arrival.read(glb).creator == "Ada"

    def test_a_listing_that_ships_its_own_stl_keeps_it(self, tmp_path, monkeypatch):
        sc, folder = self._download(
            monkeypatch, tmp_path, {"part.stl": 30.0, "part.glb": _glb_bytes(tmp_path)}
        )

        assert _extents(str(folder / "part.stl")) == (30.0, 30.0, 30.0)
        by_name = {entry["file_name"]: entry for entry in sc["downloaded"]}
        assert by_name["part.stl"]["local_path"] == str(folder / "part.stl")
        assert "conversion" not in by_name["part.stl"]
        assert by_name["part.glb"]["local_path"] == str(folder / "part.glb.stl")
        assert _extents(str(folder / "part.glb.stl")) == (40.0, 20.0, 10.0)

    def test_a_glb_saved_in_metres_is_read_in_metres(self, tmp_path, monkeypatch):
        sc, _folder = self._download(
            monkeypatch, tmp_path, {"part.glb": _glb_bytes(tmp_path, extents=(0.05, 0.02, 0.03))}
        )
        assert sc["size_check"].startswith(
            "This is a glTF file, and glTF measures in metres: read that way it is 50 mm at its largest"
        )
        assert "inches" not in sc["size_check"]

    def test_a_glb_that_cannot_be_read_says_so(self, tmp_path, monkeypatch):
        sc, folder = self._download(monkeypatch, tmp_path, {"part.glb": b"glTF\x02\x00\x00\x00not a model"})

        entry = sc["downloaded"][0]
        assert entry["local_path"] == str(folder / "part.glb")
        assert entry["conversion_failed"].startswith("part.glb could not be turned into an STL")
        assert entry["conversion_failed"].endswith("so the 3D stage and the slicer cannot open it.")
        assert sc["came_from"].startswith("Downloaded from FakeMarket")
        assert "stage_mesh_path" not in sc
        assert sorted(p.name for p in folder.iterdir() if not p.name.endswith(".json")) == ["part.glb"]

    def test_the_single_file_door_hands_on_the_stl_too(self, tmp_path, monkeypatch):
        glb_bytes = _glb_bytes(tmp_path)

        class _Client:
            def download_file(self, file_id, dest_dir, *, file_name=None):
                path = Path(dest_dir) / "thing.glb"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(glb_bytes)
                return str(path)

        monkeypatch.setattr("kiln.server._get_thingiverse", lambda: _Client())
        from kiln.plugins.marketplace_tools import plugin

        folder = tmp_path / "downloads"
        sc = _call(_served(plugin), "download_model", file_id=7, dest_dir=str(folder))

        stl = str(folder / "thing.stl")
        assert sc["local_path"] == stl
        assert sc["conversion"]["original_path"] == str(folder / "thing.glb")
        assert sc["stage_mesh_path"] == stl
        assert sc["message"].endswith(f"Saved to {stl}.")


# ---------------------------------------------------------------------------
# The printer door
# ---------------------------------------------------------------------------


class _Recording(_Bench):
    """A bench printer that remembers which file it was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.uploaded: list[str] = []

    def upload_file(self, file_path):
        self.uploaded.append(file_path)
        return super().upload_file(file_path)


class _Shelf(_Market):
    """A marketplace whose files land in one folder, whatever folder it is asked for."""

    def __init__(self, files, folder: Path) -> None:
        super().__init__(files, listing=_LISTING)
        self._folder = folder

    def download_file(self, file_id, dest_dir, *, file_name=None):
        self._folder.mkdir(parents=True, exist_ok=True)
        path = self._folder / file_id
        path.write_bytes(self._files[file_id])
        return str(path)


class TestThePrinterDoor:
    def _printer_and_market(self, tmp_path, monkeypatch) -> tuple[_Recording, Path]:
        printer = _Recording()
        _on_the_server(workshop=printer)
        shelf = tmp_path / "shelf"
        registry = MarketplaceRegistry()
        registry.register(_Shelf({"part.glb": _glb_bytes(tmp_path)}, shelf))
        monkeypatch.setattr(server, "_marketplace_registry", registry)
        return printer, shelf

    def test_a_single_glb_goes_to_the_printer_as_an_stl(self, tmp_path, monkeypatch):
        printer, shelf = self._printer_and_market(tmp_path, monkeypatch)

        out = _tool(server.download_and_upload)(file_id="part.glb", source="fakemarket", printer_name="workshop")

        assert out["success"] is True, out
        assert printer.uploaded == [str(shelf / "part.stl")]
        assert out["conversion"]["original_path"] == str(shelf / "part.glb")

    def test_a_listings_glb_is_uploaded_as_an_stl(self, tmp_path, monkeypatch):
        printer, shelf = self._printer_and_market(tmp_path, monkeypatch)

        out = _tool(server.download_and_upload)(model_id="m-1", source="fakemarket", printer_name="workshop")

        assert out["uploaded_count"] == 1, out
        assert printer.uploaded == [str(shelf / "part.stl")]
        assert out["uploaded"][0]["conversion"]["from_format"] == "glb"


# ---------------------------------------------------------------------------
# The generation doors
# ---------------------------------------------------------------------------


class TestTheGenerationDoors:
    def test_the_download_tool_still_hands_on_the_stl(self, tmp_path, monkeypatch):
        """It always converted; this pins that it still does through the
        shared helper, and that a generator's GLB is not read in metres."""
        monkeypatch.setattr("kiln.server._get_generation_provider", lambda name: _GlbCloud(tmp_path / "gen"))
        sc = _call(_generation_door(), "download_generated_model", job_id="job-1", provider="cloudgen")

        stl = str(tmp_path / "gen" / "job-1.stl")
        assert (sc["result"]["local_path"], sc["result"]["format"]) == (stl, "stl")
        assert sc["conversion"]["original_path"] == str(tmp_path / "gen" / "job-1.glb")
        assert sc["stage_mesh_path"] == stl
        assert "CloudGen was asked for a shape, not a size" in sc["size_check"]
        assert "glTF" not in sc["size_check"]

    def test_the_cli_download_hands_on_the_stl(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.cli.main._resolve_generation_provider", lambda provider: _GlbCloud(tmp_path / "gen"))
        ran = CliRunner().invoke(cli, ["generate-download", "job-3", "--provider", "meshy", "--json"])

        assert ran.exit_code == 0, ran.output
        data = json.loads(ran.output)["data"]
        assert data["result"]["local_path"] == str(tmp_path / "gen" / "job-3.stl")
        assert data["conversion"]["original_path"] == str(tmp_path / "gen" / "job-3.glb")

    def test_the_one_shot_generator_audits_a_glb_instead_of_refusing_it(self, tmp_path, monkeypatch):
        from kiln.original_design import generate_original_design

        cloud = _GlbCloud(tmp_path / "gen")
        monkeypatch.setattr(
            cloud,
            "generate",
            lambda prompt, **kwargs: GenerationJob("job-4", cloud.name, prompt, GenerationStatus.SUCCEEDED),
            raising=False,
        )
        monkeypatch.setattr(
            "kiln.original_design._resolve_original_design_provider",
            lambda *args, **kwargs: ("cloudgen", cloud, "the only provider here"),
        )

        session = generate_original_design("a small calibration cube", provider="auto", max_attempts=1)

        attempt = session.attempts[0]
        assert attempt.status != "error", attempt.error
        assert attempt.result["format"] == "stl"
        assert attempt.result["conversion"]["from_format"] == "glb"


# ---------------------------------------------------------------------------
# Every door reaches the helper
# ---------------------------------------------------------------------------

#: The calls that hand a door a file from outside Kiln.
_DOWNLOADS = frozenset({"download_result", "download_file"})

#: The one helper, in its two shapes (a path, a generator's result).
_HELPERS = frozenset({"convert_on_arrival", "convert_generated_result"})

#: Download calls that hand nothing on from outside Kiln, and why.
_NOT_AN_ARRIVAL = {
    ("parametric.py", "compile_scad_code"): "an OpenSCAD compile on this machine: always an STL",
    ("server.py", "generate_from_template"): "an OpenSCAD compile on this machine: always an STL",
    ("plugins/generation_ai_tools.py", "generate_template_variations"): (
        "an OpenSCAD compile on this machine: always an STL"
    ),
    ("marketplaces/thingiverse.py", "download_file"): (
        "the adapter itself: the doors that call it hand its file on"
    ),
}


def _called_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _own_calls(fn: ast.AST) -> list[ast.Call]:
    """The calls in *fn*'s own body, not in the functions nested inside it."""
    found: list[ast.Call] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _download_sites() -> dict[tuple[str, str], bool]:
    """``(file, function) -> reaches the helper`` for every download call.

    A function reaches the helper when it calls it, or calls a module-level
    function of its own file that does (``_arrive`` in the marketplace tools).
    """
    src = Path(kiln.__file__).parent
    sites: dict[tuple[str, str], bool] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        reaching = {
            fn.name
            for fn in tree.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(_called_name(c) in _HELPERS for c in _own_calls(fn))
        }
        for fn in functions:
            calls = _own_calls(fn)
            if not any(_called_name(c) in _DOWNLOADS and isinstance(c.func, ast.Attribute) for c in calls):
                continue
            names = {_called_name(c) for c in calls}
            sites[(path.relative_to(src).as_posix(), fn.name)] = bool(names & (_HELPERS | reaching))
    return sites


class TestEveryDownloadReachesTheHelper:
    def test_every_download_hands_a_glb_on_as_an_stl_or_says_why_not(self):
        sites = _download_sites()
        assert sites, "no download calls found: the scan is looking in the wrong place"
        unrouted = sorted(site for site, routed in sites.items() if not routed and site not in _NOT_AN_ARRIVAL)
        assert unrouted == [], (
            "These download calls hand a file on without kiln.format_conversion's helper, "
            "so a GLB would reach the stage, the slicer or a printer as a GLB.  Route them "
            f"through convert_on_arrival / convert_generated_result, or record why not: {unrouted}"
        )

    def test_the_doors_that_must_convert_do(self):
        sites = _download_sites()
        for door in (
            ("plugins/marketplace_tools.py", "download_model"),
            ("server.py", "download_and_upload"),
            ("plugins/generation_ai_tools.py", "download_generated_model"),
            ("plugins/generation_ai_tools.py", "generate_and_print"),
            ("cli/main.py", "generate"),
            ("cli/main.py", "generate_download"),
            ("cli/main.py", "generate_and_print_cmd"),
            ("original_design.py", "generate_original_design"),
            ("plugins/design_reasoning_tools.py", "iterate_design"),
        ):
            assert sites.get(door) is True, door

    def test_every_recorded_exception_is_still_a_download_call(self):
        sites = _download_sites()
        stale = sorted(site for site in _NOT_AN_ARRIVAL if site not in sites)
        assert stale == [], f"recorded exceptions with no download call left: {stale}"
