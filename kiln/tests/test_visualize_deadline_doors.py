"""Every door into ``visualize_model`` declares its own window, or none.

The engine assumes no deadline.  The MCP tools are the callers with a
request window (~60 s observed), so each hands the engine
:func:`kiln.model_visualizer.host_window_deadline`; ``compare_renders``
hands the ONE instant it was given to every model it renders, so four
models share a clock instead of each starting a fresh one; the CLI has
no window and passes nothing.  Pinned per door, because a budget that
one door forgets is the original bug with a different sign on it.
"""

from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from kiln import model_visualizer, server
from kiln.model_visualizer import compare_renders


@pytest.fixture
def stl(tmp_path: Path) -> str:
    tri = (
        struct.pack("<fff", 0, 0, 1) + struct.pack("<fff", 0, 0, 0)
        + struct.pack("<fff", 1, 0, 0) + struct.pack("<fff", 0, 1, 0)
        + struct.pack("<H", 0)
    )
    p = tmp_path / "m.stl"
    p.write_bytes(b"\x00" * 80 + struct.pack("<I", 1) + tri)
    return str(p)


@pytest.fixture
def host_clock(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(model_visualizer.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(model_visualizer, "_CALL_BUDGET_S", 40.0)
    return 1040.0


def _png(path: Path) -> str:
    from PIL import Image

    Image.new("RGB", (8, 8)).save(path)
    return str(path)


def test_the_mcp_visualize_door_sets_the_host_window(stl: str, host_clock: float) -> None:
    seen: dict = {}

    def fake(file_path, **kw):
        seen.update(kw)
        return {"success": True, "views": []}

    with patch("kiln.model_visualizer.visualize_model", side_effect=fake):
        server.visualize_model(file_path=stl)
    assert seen["deadline"] == pytest.approx(host_clock)


def test_the_deprecated_preview_door_sets_the_host_window(stl: str, host_clock: float) -> None:
    seen: dict = {}

    def fake(file_path, **kw):
        seen.update(kw)
        return {"success": False, "error": "x"}

    with patch("kiln.model_visualizer.visualize_model", side_effect=fake):
        server.render_model_preview(file_path=stl)
    assert seen["deadline"] == pytest.approx(host_clock)


def test_the_mcp_compare_door_sets_the_host_window(stl: str, host_clock: float) -> None:
    seen: dict = {}

    def fake(paths, **kw):
        seen.update(kw)
        return {"success": False, "error": "x"}

    with patch("kiln.model_visualizer.compare_renders", side_effect=fake):
        server.compare_renders(paths=[stl, stl])
    assert seen["deadline"] == pytest.approx(host_clock)


def test_compare_renders_shares_one_instant_across_its_models(stl: str, tmp_path: Path) -> None:
    """Four models, one clock.  The third and fourth come back as skipped
    with the reason carried up, and the message says so."""
    deadlines: list = []
    n = [0]

    def fake(file_path, *, deadline=None, **kw):
        deadlines.append(deadline)
        n[0] += 1
        if n[0] <= 2:
            return {
                "success": True,
                "views": [{"angle": "isometric", "description": "d",
                           "path": _png(tmp_path / f"r{n[0]}.png")}],
            }
        return {
            "success": False,
            "views": [{"angle": "isometric", "description": "d", "path": None,
                       "skipped": "budget", "error": "Skipped: the call's 40s time budget ran out"}],
        }

    with patch("kiln.model_visualizer.visualize_model", side_effect=fake):
        result = compare_renders([stl] * 4, deadline=1234.0, output_path=str(tmp_path / "c.png"))

    assert deadlines == [1234.0] * 4
    assert result["success"] is True
    assert [m.get("skipped") for m in result["models"]] == [None, None, "budget", "budget"]
    assert "2 model(s) skipped" in result["message"]
    assert "time budget" in result["message"]


def test_the_cli_door_passes_no_window(stl: str, host_clock: float) -> None:
    """A terminal has no request window; it waits for every angle."""
    from click.testing import CliRunner

    from kiln.cli.main import cli

    seen: dict = {"called": False}

    def fake(file_path, **kw):
        seen["called"] = True
        seen.update(kw)
        return {"success": True, "views": [], "rendered": 0, "failed": 0, "output_dir": ""}

    with patch("kiln.model_visualizer.visualize_model", side_effect=fake):
        CliRunner().invoke(cli, ["preview", stl, "--no-open"])
    assert seen["called"]
    assert seen.get("deadline") is None
