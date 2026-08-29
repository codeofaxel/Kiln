"""An aimed call reads the machine it NAMES, not the default one.

THE DEFECT (2026-08-28, second pass)
------------------------------------
``_PRINTER_MODEL`` and ``_PRINTER_TYPE`` are frozen at startup from the
ACTIVE printer's config entry, so each describes exactly one machine.
Doors that accept ``printer_name`` read them anyway, and every machine
fact they carry was therefore the default printer's:

* the slicer profile and its temperature cross-check (fixed in the
  first pass — see test_slice_profile_per_printer.py),
* the Bambu 3MF contract: ``slice_and_print`` emptied the start/end
  gcode and switched to relative extrusion whenever the DEFAULT was a
  Bambu.  Aimed at an Ender 3 from a Bambu default, the slice went out
  with no homing and no heat-up; aimed at a Bambu from a non-Bambu
  default, absolute-E gcode carrying PrusaSlicer's own start block got
  wrapped into a 3MF that assumes the opposite,
* the speed table: a Bambu's 250mm/s infill handed to an Ender 3,
* ``print_plate_object``'s wrap: a raw .gcode sent to a Bambu (whose
  firmware ignores it), or a Bambu 3MF sent to a printer that cannot
  open one,
* AMS routing, skipped for an aimed Bambu whenever the default was not
  one — the silent external-spool fallthrough that block exists to
  prevent.

WHAT THIS PINS
--------------
Two resolvers answer for the target — ``_resolve_target_printer_model``
and ``_resolve_target_printer_type`` — and a process global is read
only when the call is actually aimed at the default printer
(``_targets_default_printer``).  A machine we cannot identify yields
``None``/``""`` so callers skip machine-specific handling, rather than
borrowing another printer's answer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiln.printer_model_resolver import invalidate_cache

TWO_MACHINES = """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    serial: 03900D5C_A1
    printer_model: bambu_a1
  bench-ender:
    type: octoprint
    host: 10.0.0.9
    api_key: abc123
    printer_model: ender3
  shop-x1:
    type: bambu
    host: 10.0.0.6
    serial: 03900D5C_X1
    printer_model: bambu_x1c
  custom-bambu:
    type: bambu
    host: 10.0.0.7
    serial: 03900D5C_C1
    printer_model: my-bambu
"""


@pytest.fixture()
def two_machines(tmp_path, monkeypatch):
    """Point EVERY config reader at one temp config.yaml.

    Both seams (``printer_model_resolver._CONFIG_PATH`` and
    ``cli.config.get_config_path``) name the same file, so the model
    resolver and ``_read_config_printers`` cannot disagree about what
    is registered — the drift this whole change is about.
    """
    invalidate_cache()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(TWO_MACHINES)
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: cfg)
    yield cfg
    invalidate_cache()


@pytest.fixture()
def bambu_default(monkeypatch):
    """The process globals as a Bambu-default install froze them."""
    import kiln.server as srv

    monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")
    monkeypatch.setattr(srv, "_PRINTER_TYPE", "bambu")
    monkeypatch.setattr(
        srv, "_resolve_adapter",
        mock.Mock(side_effect=RuntimeError("no adapters in tests")),
    )


class TestTargetsDefaultPrinter:
    """The one question that licenses reading a process global."""

    def test_unnamed_and_default_and_the_active_name_are_the_default(
        self, two_machines, bambu_default
    ):
        import kiln.server as srv

        assert srv._targets_default_printer(None) is True
        assert srv._targets_default_printer("default") is True

    def test_another_registered_machine_is_not(self, two_machines, bambu_default):
        import kiln.server as srv

        assert srv._targets_default_printer("shop-x1") is False
        assert srv._targets_default_printer("bench-ender") is False


class TestTargetPrinterModel:
    def test_named_machine_answers_with_its_own_model(
        self, two_machines, bambu_default
    ):
        import kiln.server as srv

        assert srv._resolve_target_printer_model("bench-ender") == "ender3"
        assert srv._resolve_target_printer_model("shop-x1") == "bambu_x1c"

    def test_default_still_answers_from_the_global(self, two_machines, bambu_default):
        import kiln.server as srv

        assert srv._resolve_target_printer_model(None) == "bambu_a1"

    def test_unknown_machine_answers_nothing(self, two_machines, bambu_default):
        import kiln.server as srv

        assert srv._resolve_target_printer_model("not-registered") is None


class TestTargetPrinterType:
    def test_named_machine_answers_with_its_own_type(
        self, two_machines, bambu_default
    ):
        """The pin: a Bambu default must not make an Ender 3 a Bambu."""
        import kiln.server as srv

        assert srv._resolve_target_printer_type("bench-ender") == "octoprint"
        assert srv._resolve_target_printer_type("shop-x1") == "bambu"

    def test_default_still_answers_from_the_global(self, two_machines, bambu_default):
        import kiln.server as srv

        assert srv._resolve_target_printer_type(None) == "bambu"
        assert srv._resolve_target_printer_type("default") == "bambu"

    def test_a_live_adapter_answers_for_itself(self, two_machines, bambu_default):
        """The adapter that will receive the bytes outranks the file."""
        import kiln.server as srv

        class _Adapter:
            name = "octoprint"

        assert srv._resolve_target_printer_type("shop-x1", _Adapter()) == "octoprint"

    def test_unknown_machine_answers_nothing(self, two_machines, bambu_default):
        """No type is better than the wrong one: callers then skip the
        type-specific handling instead of applying another machine's."""
        import kiln.server as srv

        assert srv._resolve_target_printer_type("not-registered") == ""


def _slicer_available() -> bool:
    try:
        from kiln.slicer import find_slicer

        find_slicer()
        return True
    except Exception:
        return False


def _register_slicer_tools() -> dict:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return tools


def _box_stl(tmp_path: Path) -> str:
    """A solid 12-triangle binary-STL box, 20x20x10mm at the origin."""
    import struct

    x, y, z = 20.0, 20.0, 10.0
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0),
         (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    f = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    path = tmp_path / "box.stl"
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(f)))
        for tri in f:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for idx in tri:
                fh.write(struct.pack("<3f", *v[idx]))
            fh.write(struct.pack("<H", 0))
    return str(path)


@pytest.mark.skipif(not _slicer_available(), reason="no PrusaSlicer/OrcaSlicer installed")
class TestSliceAndPrintObeysTheTargetMachine:
    """What actually reached the slicer — not what we asked it to do."""

    def _profile_reached_by(self, tmp_path, printer_name: str | None) -> str:
        import kiln.slicer as _slicer

        tools = _register_slicer_tools()
        stl = _box_stl(tmp_path)
        real_slice = _slicer.slice_file
        seen: dict[str, Any] = {}

        def _spy(input_path, **kw):
            seen["profile"] = kw.get("profile")
            return real_slice(input_path, **kw)

        with mock.patch("kiln.slicer.slice_file", side_effect=_spy):
            # Runs through slicing and then fails at the printer (no live
            # adapter in tests) — the artifact is what we assert on.
            tools["slice_and_print"](
                input_path=stl, printer_name=printer_name, skip_validation=True,
            )
        profile = seen.get("profile")
        assert profile, f"no profile reached the slicer for printer_name={printer_name!r}"
        return Path(profile).read_text(encoding="utf-8")

    def test_an_aimed_ender_is_not_sliced_to_the_bambu_contract(
        self, tmp_path, two_machines, bambu_default
    ):
        """The damaging direction: relative E + an emptied start block is
        only safe because a Bambu 3MF wrap supplies its own.  On a machine
        that is never wrapped it means no homing and no heat-up."""
        body = self._profile_reached_by(tmp_path, "bench-ender")

        assert "use_relative_e_distances = 1" not in body
        assert "infill_speed = 250" not in body, (
            "a Bambu's infill speed reached an Ender 3 because a Bambu "
            "happened to be the default printer"
        )
        # The Bambu branch injects EMPTY start/end blocks because the 3MF
        # wrap supplies its own.  On a machine that is never wrapped that
        # is a slice with no homing and no heat-up, so the injection must
        # not have happened at all.
        emptied = [
            ln for ln in body.splitlines()
            if ln.replace(" ", "") in ("start_gcode=", "end_gcode=")
        ]
        assert not emptied, (
            f"the Ender's start/end block was emptied for a wrap that never "
            f"happens: {emptied}"
        )

    def test_an_aimed_bambu_still_gets_the_bambu_contract(
        self, tmp_path, two_machines, monkeypatch
    ):
        """The mirror: a non-Bambu default must not strip the contract from
        a slice bound for a Bambu, which IS wrapped.

        Aimed at ``custom-bambu`` — a Bambu whose model string maps to no
        bundled profile ("my-bambu"), the 2026-08 case.  A mappable Bambu
        would hide the bug: its bundled profile already carries relative E
        and empty start/end blocks, so the branch has nothing left to
        prove.  Here the branch is the ONLY source of the contract, and
        without it PrusaSlicer's own start block gets wrapped into a 3MF
        that assumes the opposite.
        """
        import kiln.server as srv

        monkeypatch.setattr(srv, "_PRINTER_MODEL", "ender3")
        monkeypatch.setattr(srv, "_PRINTER_TYPE", "octoprint")
        monkeypatch.setattr(
            srv, "_resolve_adapter",
            mock.Mock(side_effect=RuntimeError("no adapters in tests")),
        )

        body = self._profile_reached_by(tmp_path, "custom-bambu")
        # The EMPTIED start/end blocks are what this branch injects; the
        # bundled Bambu profile carries relative E on its own, so asserting
        # only on that would pass without the branch ever running.
        emptied = {ln.replace(" ", "") for ln in body.splitlines()} & {
            "start_gcode=", "end_gcode=",
        }
        assert emptied == {"start_gcode=", "end_gcode="}, (
            "the Bambu 3MF contract never reached a slice bound for a Bambu, "
            "because the default connection was not one"
        )


class TestPlateObjectWrapsForItsTarget:
    def _run(self, tmp_path, printer_name: str) -> bool:
        """Returns whether the Bambu 3MF repackager fired."""
        import kiln.server as srv

        gcode = tmp_path / "plate.gcode"
        gcode.write_text("G28\n")
        wrapped: dict[str, Any] = {}

        def _fake_repackage(src, dst, **kw):
            wrapped["dst"] = dst
            Path(dst).write_bytes(b"PK\x03\x04")

        with mock.patch("kiln.server._check_auth", return_value=None), \
                mock.patch(
                    "kiln.generation.validation.extract_plate_object_gcode",
                    return_value={
                        "output_path": str(gcode),
                        "matched_object": "cube",
                        "estimated_time_minutes": 10,
                    },
                ), \
                mock.patch(
                    "kiln.printers.bambu_3mf.repackage_gcode_as_bambu_3mf",
                    side_effect=_fake_repackage,
                ), \
                mock.patch.object(
                    srv, "upload_file",
                    return_value={"success": False, "error": "no printer in tests"},
                ):
            srv.print_plate_object(
                file_path=str(tmp_path / "plate.gcode.3mf"),
                object_name="cube",
                printer_name=printer_name,
            )
        return "dst" in wrapped

    def test_aimed_at_a_non_bambu_it_does_not_wrap(
        self, tmp_path, two_machines, bambu_default
    ):
        assert self._run(tmp_path, "bench-ender") is False, (
            "an OctoPrint machine was handed a Bambu 3MF it cannot open"
        )

    def test_aimed_at_a_bambu_it_wraps(self, tmp_path, two_machines, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_PRINTER_TYPE", "octoprint")
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "ender3")
        assert self._run(tmp_path, "shop-x1") is True, (
            "a Bambu was handed raw .gcode, which its firmware ignores"
        )


class TestSecondaryDoorsAcceptTheAim:
    """Doors that could not be aimed at all before this change."""

    def test_slice_model_and_reslice_take_a_printer_name(self):
        import inspect

        tools = _register_slicer_tools()
        for tool in ("slice_model", "reslice_with_overrides", "slice_and_print"):
            params = inspect.signature(tools[tool]).parameters
            assert "printer_name" in params, f"{tool} cannot be aimed"

    def test_slice_and_estimate_takes_a_printer_name(self):
        import inspect

        from kiln.plugins.estimate_tools import plugin

        tools: dict[str, Any] = {}

        class _FakeMcp:
            def tool(self, name: str | None = None):
                def decorator(fn):
                    tools[name or fn.__name__] = fn
                    return fn

                return decorator

        plugin.register(_FakeMcp())
        assert "printer_name" in inspect.signature(tools["slice_and_estimate"]).parameters

    def test_reslice_resolves_the_named_machines_profile(
        self, two_machines, bambu_default
    ):
        """Aiming the reslice door resolves the target's profile id, not
        the default printer's."""
        import kiln.server as srv

        assert srv._resolve_printer_profile_id(None, "bench-ender") == "ender3"


class TestCliAimedAtANamedPrinter:
    def test_the_named_printers_config_outranks_the_env_var(
        self, two_machines, monkeypatch
    ):
        """``KILN_PRINTER_MODEL`` states the DEFAULT machine's model.  With
        ``--printer shop-x1`` the machine's own entry has the better claim."""
        import click

        from kiln.cli.main import _autodetect_printer_profile_id

        monkeypatch.setenv("KILN_PRINTER_MODEL", "bambu_a1")
        ctx = click.Context(click.Command("slice"))
        ctx.obj = {"printer": "shop-x1"}

        assert _autodetect_printer_profile_id(ctx) == "bambu_x1c"

    def test_an_unaimed_call_still_reads_the_env_var_first(
        self, two_machines, monkeypatch
    ):
        """Unaimed, the variable IS about the printer being asked after.

        A no-regression guard rather than a fix pin: it passes on both
        sides of this change, and is here so a future tightening of the
        aimed path cannot quietly take the unaimed one with it.
        """
        import click

        from kiln.cli.main import _autodetect_printer_profile_id

        monkeypatch.setenv("KILN_PRINTER_MODEL", "prusa_mk4")
        ctx = click.Context(click.Command("slice"))
        ctx.obj = {"printer": None}

        assert _autodetect_printer_profile_id(ctx) == "prusa_mk4"
