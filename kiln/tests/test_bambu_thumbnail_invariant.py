"""A Bambu print file leaves for the printer with its screen preview, or not at all.

Measured on the A1, 2026-09-19, by reading its screen and pulling the
files back over FTPS: every archive the file list draws carries
``Metadata/plate_1_small.png`` (128x128); every one it shows the broken
placeholder for lacks it — an OrcaSlicer painted plate with only
``plate_1.png``, and two unsliced project files with no image at all.
The tile is drawn from the small slot, and the colour in it is whatever
the slicer rendered: Bambu Studio paints the filament colours in, Orca's
painted plate came out grey, Kiln's own completion of the same archive
came out grey because it rendered from the unpainted STL.

Adam's rule, verbatim: sending a file to print on a Bambu without the
3MF thumbnail is supposed to be IMPOSSIBLE for Kiln.  Two chokepoints
make it so — the adapter's upload (every door uploads through it) and
the start-by-name read-back (every door starts through the template) —
and one completion routine renders the family from the archive's OWN
model and colour table, G-code untouched.
"""

from __future__ import annotations

import io
import json
import os
import struct
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from kiln.printers import bambu_3mf
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult


@pytest.fixture
def real_stage(monkeypatch):
    """Opt this test into the stage this machine actually has — the cached
    stage document and a headless browser — or skip honestly: the
    completion photographs the plate on the stage, and nothing else counts."""
    import glob
    import pwd

    # The suite moves HOME, so look under the real one for the doc and the
    # browser the stage still uses.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    doc = real_home / ".kiln" / "stage_cache" / "mesh_viewer.html"
    browsers = sorted(glob.glob(str(real_home / "Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")))
    if not doc.is_file() or not browsers:
        pytest.skip("no cached stage document or headless browser on this machine")
    monkeypatch.setenv("KILN_STAGE_DOC", str(doc))
    monkeypatch.setenv("KILN_STAGE_BROWSER", browsers[-1])
    monkeypatch.delenv("KILN_NO_STAGE_STILLS", raising=False)

_MODEL = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="White" displaycolor="#FFFFFF" />
      <base name="Red" displaycolor="#F72323" />
    </basematerials>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" /><vertex x="20" y="0" z="0" /><vertex x="0" y="20" z="0" />
          <vertex x="20" y="20" z="0" /><vertex x="10" y="10" z="15" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="4" />
          <triangle v1="1" v2="3" v3="4" pid="1" p1="1" />
          <triangle v1="3" v2="2" v3="4" />
          <triangle v1="2" v2="0" v3="4" pid="1" p1="1" />
          <triangle v1="0" v2="2" v3="1" /><triangle v1="1" v2="2" v3="3" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" /></build>
</model>"""

_GCODE = "; sliced\nM73 P0 R60\nG28\nG1 X10 Y10 Z0.2 E1\nM73 P100 R0\n"
_SLICE_INFO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <plate>\n'
    '    <metadata key="index" value="1"/>\n    <metadata key="prediction" value="3600"/>\n'
    '    <metadata key="weight" value="0.00"/>\n  </plate>\n</config>\n'
)
_PLATE_JSON = json.dumps({"filament_colors": ["#FFFFFF", "#F72323"], "version": 2})


def _png(width: int, height: int, rgb=(128, 128, 128)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), rgb).save(buf, format="PNG")
    return buf.getvalue()


def _archive(tmp_path: Path, name: str, extra: dict[str, bytes], *, sliced: bool = True) -> str:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", _MODEL)
        if sliced:
            zf.writestr("Metadata/plate_1.gcode", _GCODE)
            zf.writestr("Metadata/slice_info.config", _SLICE_INFO)
            zf.writestr("Metadata/plate_1.json", _PLATE_JSON)
        for n, d in extra.items():
            zf.writestr(n, d)
    return str(p)


def orca_like(tmp_path: Path) -> str:
    """Only plate_1.png — what OrcaSlicer wrote for the painted jar."""
    return _archive(tmp_path, "orca.gcode.3mf", {"Metadata/plate_1.png": _png(512, 512)})


def project_like(tmp_path: Path) -> str:
    return _archive(tmp_path, "project.3mf", {}, sliced=False)


def studio_like(tmp_path: Path) -> str:
    """Bambu Studio's own set, plate_no_light included: the slicer drew it."""
    slots = {n: _png(*s, rgb=(247, 35, 35)) for n, s in bambu_3mf._BAMBU_THUMBNAIL_SPECS.items()}
    slots["Metadata/plate_no_light_1.png"] = _png(512, 512, rgb=(247, 35, 35))
    return _archive(tmp_path, "studio.gcode.3mf", slots)


def _png_size(data: bytes):
    return struct.unpack(">II", data[16:24]) if data[:8] == b"\x89PNG\r\n\x1a\n" else None


def _members(path: str) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


class TestArchiveProblems:
    def test_a_plate_with_only_the_large_thumbnail_is_named(self, tmp_path):
        problems = bambu_3mf.bambu_archive_problems(orca_like(tmp_path))
        assert any("plate_1_small.png" in p for p in problems), problems

    def test_an_unsliced_project_is_named_as_such(self, tmp_path):
        problems = bambu_3mf.bambu_archive_problems(project_like(tmp_path))
        assert any("not a sliced plate" in p for p in problems), problems

    def test_the_slicers_own_full_set_passes(self, tmp_path):
        assert bambu_3mf.bambu_archive_problems(studio_like(tmp_path)) == []

    def test_a_full_set_without_a_witness_is_named(self, tmp_path):
        """All seven slots present, but nobody vouches that the picture was
        drawn in the plate's colours: neither the slicer's no-light slot
        nor Kiln's own marker.  Grey-jar case."""
        slots = {n: _png(*s) for n, s in bambu_3mf._BAMBU_THUMBNAIL_SPECS.items()}
        problems = bambu_3mf.bambu_archive_problems(_archive(tmp_path, "grey.gcode.3mf", slots))
        assert any("colours" in p for p in problems), problems

    def test_a_wrong_sized_slot_is_named(self, tmp_path):
        slots = {n: _png(*s) for n, s in bambu_3mf._BAMBU_THUMBNAIL_SPECS.items()}
        slots["Metadata/plate_1_small.png"] = _png(512, 512)
        slots["Metadata/plate_no_light_1.png"] = _png(512, 512)
        problems = bambu_3mf.bambu_archive_problems(_archive(tmp_path, "wrong.gcode.3mf", slots))
        assert any("128x128" in p for p in problems), problems

    def test_not_a_3mf_is_not_this_checks_business(self, tmp_path):
        g = tmp_path / "part.gcode"
        g.write_text(_GCODE)
        assert bambu_3mf.bambu_archive_problems(str(g)) == []


# ---------------------------------------------------------------------------
# Completion: the family, in the archive's own colours, G-code untouched
# ---------------------------------------------------------------------------


class TestCompletion:
    def test_completion_fills_every_slot_and_leaves_the_gcode_alone(self, tmp_path):
        path = orca_like(tmp_path)
        before = _members(path)
        out = bambu_3mf.complete_bambu_archive(path)
        assert out == path
        after = _members(path)
        assert after["Metadata/plate_1.gcode"] == before["Metadata/plate_1.gcode"]
        assert after["3D/3dmodel.model"] == before["3D/3dmodel.model"]
        for name, size in bambu_3mf._BAMBU_THUMBNAIL_SPECS.items():
            assert _png_size(after[name]) == size, name
        assert bambu_3mf.bambu_archive_problems(path) == []

    def test_the_picture_carries_the_declared_colours(self, tmp_path):
        from PIL import Image

        path = orca_like(tmp_path)
        bambu_3mf.complete_bambu_archive(path)
        img = Image.open(io.BytesIO(_members(path)["Metadata/plate_1.png"])).convert("RGB")
        pixels = list(img.getdata())

        # Shaded, so hue rather than exact value: red faces read as red,
        # white faces as light and neutral.
        reds = sum(1 for r, g, b in pixels if r > 120 and g < 70 and b < 70)
        whites = sum(1 for r, g, b in pixels if r > 200 and g > 200 and b > 200)
        assert reds > 50 and whites > 50, (reds, whites)
        marker = json.loads(_members(path)["Metadata/kiln_preview.json"])
        assert marker["colors"] == ["#FFFFFF", "#F72323"]
        assert marker["renderer"] == "colored_mesh"

    def test_completion_is_idempotent(self, tmp_path):
        path = orca_like(tmp_path)
        bambu_3mf.complete_bambu_archive(path)
        first = _members(path)
        bambu_3mf.complete_bambu_archive(path)
        assert _members(path).keys() == first.keys()

    def test_a_project_cannot_be_completed_into_a_print(self, tmp_path):
        with pytest.raises(ValueError, match="not a sliced plate"):
            bambu_3mf.complete_bambu_archive(project_like(tmp_path))


# ---------------------------------------------------------------------------
# Chokepoint 1: the upload door
# ---------------------------------------------------------------------------


class TestUploadDoor:
    @pytest.fixture
    def adapter(self):
        from kiln.printers.bambu import BambuAdapter

        with mock.patch.object(BambuAdapter, "_ensure_mqtt", lambda self: None, create=True):
            a = BambuAdapter(host="192.0.2.5", access_code="12345678", serial="01P00A000000000")
        return a

    def _upload(self, adapter, path):
        from kiln.printers.bambu import PrinterError

        ftp = mock.MagicMock()
        ftp.nlst.return_value = ["/model"]
        with mock.patch("kiln.printers.bambu._ImplicitFTP_TLS", return_value=ftp), \
             mock.patch.object(adapter, "_ftp_connect", return_value=ftp), \
             mock.patch.object(adapter, "_detect_storage_path", return_value="/model"):
            try:
                return adapter.upload_file(path), ftp
            except PrinterError as exc:
                return exc, ftp

    def test_a_plate_without_its_preview_never_leaves(self, adapter, tmp_path):
        result, ftp = self._upload(adapter, orca_like(tmp_path))
        assert isinstance(result, Exception), result
        assert "plate_1_small.png" in str(result)
        ftp.storbinary.assert_not_called()

    def test_a_project_file_never_leaves(self, adapter, tmp_path):
        result, ftp = self._upload(adapter, project_like(tmp_path))
        assert isinstance(result, Exception)
        assert "not a sliced plate" in str(result)
        ftp.storbinary.assert_not_called()

    def test_a_completed_plate_leaves(self, adapter, tmp_path):
        path = orca_like(tmp_path)
        bambu_3mf.complete_bambu_archive(path)
        result, ftp = self._upload(adapter, path)
        assert not isinstance(result, Exception), result
        ftp.storbinary.assert_called_once()

    def test_the_slicers_own_plate_leaves(self, adapter, tmp_path):
        result, ftp = self._upload(adapter, studio_like(tmp_path))
        assert not isinstance(result, Exception), result


# ---------------------------------------------------------------------------
# Chokepoint 2: a start by name reads the printer's copy
# ---------------------------------------------------------------------------


def _fake_bambu(data: bytes):
    from kiln.printers.base import PrinterAdapter, PrinterCapabilities

    class _Fake(PrinterAdapter):
        printer_id = "bambu_a1"

        def __init__(self):
            self.impl_calls = []

        @property
        def name(self):
            return "bambu"

        @property
        def capabilities(self):
            return PrinterCapabilities()

        def get_state(self):
            return PrinterState(state=PrinterStatus.IDLE, connected=True)

        def get_job(self):
            from kiln.printers.base import JobProgress

            return JobProgress()

        def list_files(self):
            return []

        def upload_file(self, file_path):
            raise NotImplementedError

        def _start_print_impl(self, file_name, **kwargs):
            self.impl_calls.append(file_name)
            return PrintResult(success=True, message="started")

        def read_print_file(self, file_name):
            return data

        def cancel_print(self):
            return PrintResult(success=True, message="")

        def pause_print(self):
            return PrintResult(success=True, message="")

        def _resume_print_impl(self):
            return PrintResult(success=True, message="")

        def emergency_stop(self):
            return PrintResult(success=True, message="")

        def _load_filament_impl(self, plan):
            raise NotImplementedError

        def _unload_filament_impl(self, plan):
            raise NotImplementedError

        def _purge_filament_impl(self, plan):
            raise NotImplementedError

        def set_tool_temp(self, target):
            return True

        def set_bed_temp(self, target):
            return True

        def send_gcode(self, commands):
            return True

        def delete_file(self, file_path):
            return True

    _Fake.__abstractmethods__ = frozenset()
    return _Fake()


class TestStartByNameDoor:
    @pytest.fixture(autouse=True)
    def _no_signoff(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")

    def test_a_printer_copy_without_its_preview_is_refused(self, tmp_path):
        a = _fake_bambu(Path(orca_like(tmp_path)).read_bytes())
        r = a.start_print("orca.gcode.3mf")
        assert r.success is False, r.message
        assert "plate_1_small.png" in r.message and a.impl_calls == []
        assert "delete_file" in r.message

    def test_a_completed_printer_copy_starts(self, tmp_path):
        path = orca_like(tmp_path)
        bambu_3mf.complete_bambu_archive(path)
        a = _fake_bambu(Path(path).read_bytes())
        r = a.start_print("orca.gcode.3mf")
        assert r.success is True, r.message


# ---------------------------------------------------------------------------
# slice_model hands back a file the doors accept
# ---------------------------------------------------------------------------


def test_the_slice_steer_completes_the_archive_it_recommends(tmp_path):
    from kiln.plugins import slicer_tools

    path = orca_like(tmp_path)
    response: dict = {"output_path": path}
    slicer_tools._steer_to_wrapped_upload(response, path, "bambu_a1")
    assert response["recommended_upload_path"] == path
    assert bambu_3mf.bambu_archive_problems(path) == []


# ---------------------------------------------------------------------------
# The preview a PERSON sees of a painted model is the stage, not a flat render
# ---------------------------------------------------------------------------


class TestPaintedPreviewReachesTheStage:
    """Before this, ``visualize_model`` returned a painted 3MF from the PIL
    renderer before the stage was ever tried, so the picture a person
    signed off from was flat — no plate, no stage lighting — and the gate
    could not accept it (only the stage's look signs off).  Measured on
    the jar, 2026-09-19."""

    def test_the_stage_photographs_a_painted_plate(self, tmp_path, real_stage):
        from PIL import Image

        from kiln.model_visualizer import visualize_model

        path = orca_like(tmp_path)
        result = visualize_model(
            path, output_dir=str(tmp_path / "out"), share_link=False, angles=["isometric"],
        )
        assert result["success"], result
        assert result["renderer"] == "stage", result["renderer"]
        img = Image.open(result["views"][0]["path"]).convert("RGB")
        pixels = list(img.getdata())
        assert sum(1 for r, g, b in pixels if r > 120 and g < 80 and b < 80) > 20, "the paint is missing"

    def test_without_the_stage_the_coloured_renderer_still_draws(self, tmp_path, monkeypatch):
        from kiln.model_visualizer import visualize_model

        monkeypatch.setenv("KILN_STAGE_BROWSER", "/nonexistent/browser")
        monkeypatch.delenv("KILN_STAGE_DOC", raising=False)
        path = orca_like(tmp_path)
        result = visualize_model(
            path, output_dir=str(tmp_path / "out"), share_link=False, angles=["isometric"],
        )
        assert result["success"], result
        assert result["renderer"] == "colored_mesh"
