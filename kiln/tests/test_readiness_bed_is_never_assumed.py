"""``check_print_readiness`` holds a part only to a bed somebody stated.

The bed is ``printer_id``'s, else the three bed dimensions, else this
install's printer.  With none of those the fit is not checked: no size is
assumed, no ``too_large`` issue is raised, and ``bed_check`` says the fit
was not checked.  ``bed_check`` names the bed every checked answer used.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from kiln.printer_model_resolver import invalidate_cache
from tests import _real_exports as rx

#: A printer in the bundled catalogue whose bed is smaller than the part.
SMALL_PRINTER = "bambu_a1_mini"


def _box_stl(path: Path, x: float, y: float, z: float) -> str:
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    f = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(f)))
        for tri in f:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for idx in tri:
                fh.write(struct.pack("<3f", *v[idx]))
            fh.write(b"\0\0")
    return str(path)


@pytest.fixture()
def long_part(tmp_path) -> str:
    """A 300 x 40 x 10 mm bar."""
    return _box_stl(tmp_path / "bar.stl", 300.0, 40.0, 10.0)


def _install(tmp_path, monkeypatch, text: str | None) -> None:
    cfg = tmp_path / "config.yaml"
    if text is not None:
        cfg.write_text(text)
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: cfg)
    invalidate_cache()


@pytest.fixture()
def no_printer(tmp_path, monkeypatch):
    """An install with no printer set up."""
    _install(tmp_path, monkeypatch, None)
    yield
    invalidate_cache()


@pytest.fixture()
def small_printer_install(tmp_path, monkeypatch):
    """An install whose one printer is :data:`SMALL_PRINTER`."""
    _install(
        tmp_path, monkeypatch,
        "active_printer: bench\nprinters:\n  bench:\n    type: octoprint\n"
        f"    host: http://127.0.0.1:9\n    api_key: k\n    printer_model: {SMALL_PRINTER}\n",
    )
    yield
    invalidate_cache()


def _ready(**args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, "check_print_readiness", args)


def _too_large(reply: dict) -> list[dict]:
    return [i for i in reply.get("issues", []) if i.get("type") == "too_large"]


def _small_bed() -> list[float]:
    from kiln.printers.bed_fit import get_build_volume

    bed = get_build_volume(SMALL_PRINTER)
    assert bed is not None and max(bed) < 300.0
    return [float(v) for v in bed]


def test_with_no_bed_known_the_fit_is_not_judged(no_printer, long_part):
    reply = _ready(file_path=long_part)

    assert reply["success"] is True, reply
    assert _too_large(reply) == []
    assert reply["can_print"] is True
    assert reply["bed_check"]["checked"] is False
    assert reply["bed_check"]["bed_mm"] is None and reply["bed_check"]["source"] is None
    assert "not checked" in reply["bed_check"]["note"]
    assert "256" not in json.dumps(reply["issues"]) + json.dumps(reply["bed_check"])


def test_a_stated_bed_is_the_bed(no_printer, long_part):
    reply = _ready(file_path=long_part, bed_x_mm=256.0, bed_y_mm=256.0, bed_z_mm=256.0)

    assert len(_too_large(reply)) == 1 and reply["can_print"] is False
    assert reply["bed_check"] == {"checked": True, "bed_mm": [256.0, 256.0, 256.0], "source": "stated"}


def test_this_installs_printer_is_the_bed_when_none_is_stated(small_printer_install, long_part):
    bed = _small_bed()
    reply = _ready(file_path=long_part)

    assert reply["can_print"] is False
    assert reply["bed_check"] == {"checked": True, "bed_mm": bed, "source": "install_printer"}
    (issue,) = _too_large(reply)
    assert f"{bed[0]:.0f}" in issue["detail"] and "256" not in issue["detail"]


def test_a_named_printer_is_the_bed_over_stated_dimensions(no_printer, long_part):
    bed = _small_bed()
    reply = _ready(file_path=long_part, printer_id=SMALL_PRINTER, bed_x_mm=400.0, bed_y_mm=400.0, bed_z_mm=400.0)

    assert reply["bed_check"] == {"checked": True, "bed_mm": bed, "source": "printer"}
    assert len(_too_large(reply)) == 1 and reply["can_print"] is False


def test_some_bed_dimensions_without_the_rest_are_refused(no_printer, long_part):
    reply = _ready(file_path=long_part, bed_x_mm=256.0)

    assert reply.get("success") is False and reply["error"]["code"] == "INVALID_ARGS", reply
