"""A part that arrives as CAD is estimated as it is.

2026-10-01: ``estimate_mesh_print_time`` handed a ``.step`` path answered
"Unsupported format: .step" — the tool took the path at its door and the
parser below it refused.  The three estimate tools now go through the shared
STEP door first, like the tools around them.
"""

from __future__ import annotations

import struct

import pytest

from kiln import step_import


def _box_stl(path, x: float, y: float, z: float) -> str:
    """A closed binary-STL box, corner at the origin."""
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    data = bytearray(b"\x00" * 80) + struct.pack("<I", len(faces))
    for a, b, c in faces:
        data += struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0)
    path.write_bytes(bytes(data))
    return str(path)


@pytest.fixture()
def tools():
    captured: dict[str, callable] = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

    from kiln.plugins.design_tools import plugin as design_plugin
    from kiln.plugins.mesh_tools import plugin as mesh_plugin

    mesh_plugin.register(_MCP())
    design_plugin.register(_MCP())
    return captured


@pytest.fixture()
def cad_part(tmp_path, monkeypatch):
    """A ``.step`` path, and a converter that turns it into a 20x20x10 box."""
    monkeypatch.setenv("KILN_AUTH_ENABLED", "false")
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")
    mesh = _box_stl(tmp_path / "part.stl", 20, 20, 10)
    monkeypatch.setattr(
        step_import, "ensure_mesh_path",
        lambda path, **kw: (mesh, "converted", None) if kw.get("with_record") else (mesh, "converted"),
    )
    return str(step)


def test_print_time_is_estimated_for_a_step_file(tools, cad_part):
    got = tools["estimate_mesh_print_time"](cad_part)
    assert got.get("success") is True, got
    assert got["layers"] == 50  # 10 mm tall at the default 0.2 mm layer


def test_weight_is_estimated_for_a_step_file(tools, cad_part):
    got = tools["estimate_mesh_weight"](cad_part)
    assert got.get("success") is True, got


def test_cost_is_estimated_for_a_step_file(tools, cad_part):
    got = tools["estimate_print_cost_from_mesh"](cad_part)
    assert got.get("success") is True or got.get("status") == "success", got


def test_a_step_file_nothing_can_convert_is_refused_with_the_fix(tools, tmp_path, monkeypatch):
    step = tmp_path / "part.step"
    step.write_text("ISO-10303-21;")

    def _no_backend(path, **kw):
        raise step_import.NoBackendError()

    monkeypatch.setattr(step_import, "ensure_mesh_path", _no_backend)
    got = tools["estimate_mesh_print_time"](str(step))
    assert got.get("success") is False
    assert got["error"]["code"] == "NO_BACKEND" and "remedy" in got
