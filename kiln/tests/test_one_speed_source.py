"""Every door prints the speeds its estimate was made with.

Until 2026-09-30 the two print doors -- ``slice_and_print`` and
``run_reslice_and_print`` -- laid a second speed table over a printer's own
profile: speeds and accelerations derived from
``printer_intelligence._SPEED_CAPABILITIES``, then the per-type table in
``kiln.server``.  Every other door, the estimate among them, used the
profile.  So the time Kiln quoted was for a different file than the one it
printed; the capability table's prefix match gave the Ender-3 V3, V3 KE,
V3 Plus and V4 the original Ender 3's speeds (45 mm/s against their
profiles' 130); and the fill landed in a dict merged OVER the caller's own
settings, beneath a comment saying it never did.

Now a printer with a bundled profile prints that profile at every door, and
the tables fill in only for a machine Kiln has no profile for
(``kiln.server._speed_fill_for_slice``).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import kiln.server as srv
from kiln.slicer_orca import ini_to_settings
from kiln.slicer_profiles import list_slicer_profiles, resolve_slicer_profile

_SPEEDS = (
    "perimeter_speed",
    "external_perimeter_speed",
    "infill_speed",
    "solid_infill_speed",
    "top_solid_infill_speed",
    "first_layer_speed",
    "travel_speed",
)


class TestTheFill:
    def test_a_printer_with_its_own_profile_gets_none(self) -> None:
        for pid in list_slicer_profiles():
            if pid == "default":
                continue
            for printer_type in (None, *srv._PRINTER_SPEED_OVERRIDES):
                assert srv._speed_fill_for_slice(pid, printer_type, caller_profile=False) == {}, (
                    pid, printer_type,
                )

    def test_a_callers_own_profile_gets_none(self) -> None:
        assert srv._speed_fill_for_slice(None, "bambu", caller_profile=True) == {}

    def test_an_unprofiled_printer_of_a_known_type_gets_that_types_speeds(self) -> None:
        fill = srv._speed_fill_for_slice("a_printer_kiln_has_no_profile_for", "bambu", caller_profile=False)
        assert fill == srv._PRINTER_SPEED_OVERRIDES["bambu"]

    def test_nothing_known_fills_nothing(self) -> None:
        assert srv._speed_fill_for_slice(None, None, caller_profile=False) == {}


def _box(path: Path) -> str:
    v = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0), (0, 0, 10), (20, 0, 10), (20, 20, 10), (0, 20, 10)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    lines = ["solid box"]
    for a, b, c in faces:
        lines.append(" facet normal 0 0 0\n  outer loop")
        lines.extend(f"   vertex {x} {y} {z}" for x, y, z in (v[a], v[b], v[c]))
        lines.append("  endloop\n endfacet")
    lines.append("endsolid box")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return str(path)


def _register_slicer_tools() -> dict[str, Any]:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return tools


class _Stop(Exception):
    """Raised by the slicer spy: the profile is what is being judged."""


def _profile_slice_and_print_hands_the_slicer(tmp_path: Path, printer_type: str, model: str) -> dict:
    seen: dict[str, Any] = {}

    def _spy(_input_path, **kwargs):
        seen["profile"] = kwargs.get("profile")
        raise _Stop

    tools = _register_slicer_tools()
    with patch("kiln.server._check_auth", return_value=None), \
            patch("kiln.server._PRINTER_TYPE", printer_type), \
            patch("kiln.server._PRINTER_MODEL", model), \
            patch("kiln.slicer.slice_file", side_effect=_spy), contextlib.suppress(_Stop):
        tools["slice_and_print"](input_path=_box(tmp_path / "box.stl"), skip_validation=True)
    assert seen.get("profile"), "no profile reached the slicer"
    return ini_to_settings(seen["profile"])


class TestTheDoorsPrintTheProfile:
    def test_an_ender_3_v3_ke_prints_its_own_speeds(self, tmp_path: Path) -> None:
        """Not the original Ender 3's 45 mm/s through a prefix match."""
        printed = _profile_slice_and_print_hands_the_slicer(tmp_path, "prusalink", "ender3_v3_ke")
        own = ini_to_settings(resolve_slicer_profile("ender3_v3_ke"))
        for key in _SPEEDS:
            assert printed[key] == own[key], (key, printed[key], own[key])

    def test_an_a1_prints_the_file_its_estimate_was_made_with(self, tmp_path: Path) -> None:
        printed = _profile_slice_and_print_hands_the_slicer(tmp_path, "bambu", "bambu_a1")
        own = ini_to_settings(resolve_slicer_profile("bambu_a1"))
        for key in _SPEEDS:
            assert printed[key] == own[key], (key, printed[key], own[key])
        # The table's accelerations (M204 per role) are gone too: the machine
        # runs what its own start sequence set, as the estimate assumes.
        assert "default_acceleration" not in printed

    def test_the_reslice_door_hands_its_pipeline_no_table_speeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        result = MagicMock()
        result.success = True
        result.to_dict.return_value = {"success": True}
        result.steps = []
        with patch("kiln.server._check_auth", return_value=None), \
                patch("kiln.server._PRINTER_TYPE", "bambu"), \
                patch("kiln.server._PRINTER_MODEL", "bambu_a1"), \
                patch("kiln.server._pipeline_reslice_and_print", return_value=result) as pipeline:
            srv.run_reslice_and_print(
                model_path=_box(tmp_path / "box.stl"),
                printer_id="bambu_a1",
                overrides={"infill_speed": "120"},
            )
        overrides = pipeline.call_args.kwargs["overrides"]
        # The caller's own speed reaches the pipeline untouched, and nothing
        # from the tables rides along with it.
        assert overrides == {"infill_speed": "120"}
