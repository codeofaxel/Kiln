"""The one-printer rule covers every command an adapter has, not a list.

Below the fleet tier Kiln drives one machine at a time.  Until 2026-10-11 the
rule was a hand-kept list of fifteen command names, and every adapter command
nobody had thought to list -- fans, the light, speed modes, skipping objects,
clearing an error, calibration, firmware updates, the camera -- drove a second
machine on the free plan.  Every one of them existed before the rule was
written; the only test of the list compared it with a copy of itself.

Pinned here, against the real adapter classes rather than a list:

* every public method on every adapter is either gated or named in
  ``UNGATED_METHODS`` with a reason, and no row names a method that is gone;
* every gated method on every adapter refuses a second machine BEFORE it runs
  -- called on a bare instance whose own body would fail if it ever ran;
* every ungated method really is reachable without the rule asking;
* the refusal for a command that acts on a machine says that machine's own
  controls still work; the one for a camera or status read says it still
  shows its own status; the one for a read of what it holds -- files, spools,
  settings -- says its own screen still shows them.

Reads of a second machine are gated on purpose (2026-10-11): what it holds
and how it is set show on its own screen, and reading them through Kiln is
the fleet experience the fleet tier sells.  Only identity (to register it),
catalogue facts and Kiln's own plumbing stay outside the rule.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import pkgutil

import pytest

import kiln.printers as printers_pkg
from kiln.printers import engagement
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterEngagementError,
    PrinterState,
    PrinterStatus,
)
from kiln.registry import PrinterRegistry


def _adapter_classes() -> list[type]:
    for module in pkgutil.iter_modules(printers_pkg.__path__):
        try:
            importlib.import_module(f"kiln.printers.{module.name}")
        except Exception:  # noqa: BLE001 -- an optional backend's missing dependency
            continue

    def below(cls: type) -> set[type]:
        found: set[type] = set()
        for sub in cls.__subclasses__():
            found.add(sub)
            found |= below(sub)
        return found

    real = [c for c in below(PrinterAdapter) if c.__module__.startswith("kiln.printers.")]
    return [PrinterAdapter, *sorted(real, key=lambda c: c.__name__)]


ADAPTERS = _adapter_classes()


def _commands(cls: type) -> list[str]:
    """Every public plain method on *cls* (inherited ones for the base)."""
    names = dir(cls) if cls is PrinterAdapter else list(cls.__dict__)
    out = []
    for name in names:
        if name.startswith("_"):
            continue
        raw = inspect.getattr_static(cls, name, None)
        if inspect.isfunction(raw):
            out.append(name)
    return sorted(out)


def test_the_adapters_were_found():
    """A sweep that found nothing would pass every test below."""
    assert "/kiln/src/kiln/" in engagement.__file__.replace("\\", "/")
    names = {c.__name__ for c in ADAPTERS}
    for expected in ("BambuAdapter", "MoonrakerAdapter", "OctoPrintAdapter", "PrusaLinkAdapter"):
        assert expected in names, expected


def test_every_adapter_command_is_gated_or_named_with_a_reason():
    unnamed = sorted(
        {
            f"{cls.__name__}.{name}"
            for cls in ADAPTERS
            for name in _commands(cls)
            if name not in engagement.GATED_ACTIONS and name not in engagement.UNGATED_METHODS
        }
    )
    assert not unnamed, (
        "Adapter commands the one-printer rule has not been told about. The rule GATES them "
        "already (it covers every command not named in UNGATED_METHODS); name each in "
        "engagement.GATED_ACTIONS, or in UNGATED_METHODS with the reason it is not a command "
        f"that acts on or watches a machine: {unnamed}"
    )


def test_no_row_names_a_command_that_is_gone():
    present = {name for cls in ADAPTERS for name in _commands(cls)}
    stale = sorted((set(engagement.GATED_ACTIONS) | set(engagement.UNGATED_METHODS)) - present)
    assert not stale, f"rows naming no adapter command: {stale}"
    assert not set(engagement.GATED_ACTIONS) & set(engagement.UNGATED_METHODS)
    assert all(reason.strip() for reason in engagement.UNGATED_METHODS.values())


def test_a_command_nobody_named_is_gated_by_default():
    """A command added tomorrow answers to the rule without anyone listing it."""
    assert engagement.is_gated("set_chamber_vent_tomorrow")
    assert not engagement.is_gated("_private_helper")
    assert not engagement.is_gated("get_printer_info")  # registering a printer is free
    for read in ("upload_file", "list_files", "get_ams_status", "get_filament_status",
                 "read_nozzle_setting", "get_bed_mesh", "get_firmware_status", "get_printer_config"):
        assert engagement.is_gated(read), read


# ---------------------------------------------------------------------------
# The behaviour, on the real classes
# ---------------------------------------------------------------------------


class _Engaged:
    """The machine Kiln is driving: a real object, not a Mock (a Mock invents
    ``serial`` and ``host`` and would fingerprint as anything)."""

    name = "moonraker"
    serial = "ENGAGED-A1"
    host = ""
    capabilities = PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=PrinterStatus.PRINTING)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name="bracket.gcode", print_time_seconds=600)


@pytest.fixture
def another_machine_is_driven(monkeypatch):
    monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
    monkeypatch.setattr(engagement, "_still_engaged", lambda *_a, **_k: True)
    a = _Engaged()
    registry = PrinterRegistry()
    registry.register("a1", a)
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    engagement.engage(a, a.get_job(), reason="started", label="a1")
    return a  # the record lives in the per-test home conftest isolates


def _bare(cls: type) -> object:
    """An instance whose own methods cannot run: no connection, no state.

    If the rule lets a gated call through, its body touches attributes that
    were never set and fails with something OTHER than the rule's refusal."""
    inst = object.__new__(cls)
    for attr, value in (("_serial", "SECOND-B2"), ("serial", "SECOND-B2"), ("_host", "10.0.0.99")):
        with contextlib.suppress(AttributeError, TypeError):
            object.__setattr__(inst, attr, value)
    return inst


def _dummy_args(fn) -> list:
    params = list(inspect.signature(fn).parameters.values())[1:]
    return [
        "x"
        for p in params
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]


GATED_CASES = [
    pytest.param(cls, name, id=f"{cls.__name__}.{name}")
    for cls in ADAPTERS
    if cls is not PrinterAdapter
    for name in sorted({*dir(cls)})
    # From the classification, NOT from is_gated: a sweep chosen by the code
    # it tests would shrink with it, and pass.
    if not name.startswith("_") and name not in engagement.UNGATED_METHODS
    and inspect.isfunction(inspect.getattr_static(cls, name, None))
    and not getattr(inspect.getattr_static(cls, name), "__isabstractmethod__", False)
]


def test_the_sweep_has_cases():
    assert len(GATED_CASES) > 100
    swept = {param.values[1] for param in GATED_CASES}
    for name in ("set_fan", "set_light", "skip_objects", "set_speed_profile", "get_snapshot",
                 "clear_error", "run_calibration", "update_firmware",
                 "upload_file", "list_files", "get_ams_status", "read_nozzle_setting"):
        assert name in swept, name


@pytest.mark.parametrize(("cls", "name"), GATED_CASES)
def test_every_gated_command_refuses_a_second_machine_before_it_runs(cls, name, another_machine_is_driven):
    inst = _bare(cls)
    method = getattr(inst, name)
    with pytest.raises(PrinterEngagementError) as caught:
        result = method(*_dummy_args(getattr(cls, name)))
        if inspect.iscoroutine(result):
            import asyncio

            asyncio.run(result)
    assert caught.value.verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT"
    assert caught.value.verdict["action"] == name
    assert "a1" in caught.value.verdict["reason"]


@pytest.mark.parametrize("name", sorted(engagement.UNGATED_METHODS))
def test_an_ungated_command_is_never_asked_about(name, another_machine_is_driven):
    inst = _bare(next(c for c in ADAPTERS if c is not PrinterAdapter and hasattr(c, name)))
    assert engagement.check_command(inst, name) is None


@pytest.mark.parametrize(
    ("action", "says"),
    [
        ("set_fan", "own controls work as always"),
        ("set_light", "own controls work as always"),
        ("skip_objects", "own controls work as always"),
        ("upload_file", "own controls work as always"),
        ("get_snapshot", "still shows its own status"),
        ("get_state", "still shows its own status"),
        ("list_files", "own screen still shows its spools, files and settings"),
        ("get_ams_status", "own screen still shows its spools, files and settings"),
        ("read_nozzle_setting", "own screen still shows its spools, files and settings"),
    ],
)
def test_the_refusal_says_what_still_works_for_that_kind_of_command(action, says, another_machine_is_driven):
    inst = _bare(next(c for c in ADAPTERS if c is not PrinterAdapter and hasattr(c, action)))
    verdict = engagement.check_command(inst, action)
    assert verdict is not None and says in " ".join(verdict["suggestions"])


def test_setting_up_a_second_printer_still_reads_its_id_and_its_camera(another_machine_is_driven):
    """Registering is free at every tier, including while Kiln drives another
    machine.  Every setup door reads the new printer's own id (so a printer
    that moves is recognised) and asks whether its camera answers; both used
    to be refused here, so the id was lost without a word and the camera line
    said the handshake "failed unexpectedly".  The camera check a person asks
    for later is a look at the machine, and stays refused."""
    from unittest.mock import patch

    from kiln.camera_check import camera_setup_line
    from kiln.printers.base import CameraHandshake, read_machine_identity_bounded
    from kiln.printers.moonraker import MoonrakerAdapter

    second = MoonrakerAdapter(host="http://10.0.0.99")
    replies = {
        "/machine/system_info": {"result": {"system_info": {"cpu_info": {"serial_number": "PI-SERIAL-9"}}}},
        "/server/webcams/list": {"result": {"webcams": [{"name": "cam", "stream_url": "/webcam/?action=stream"}]}},
    }
    answered = CameraHandshake(True, "moonraker_webcam_list", "The camera answered.")
    with (
        patch.object(second, "_get_json", side_effect=lambda path: replies.get(path, {"result": {}})),
        patch("kiln.camera_check.handshake_probes", return_value=answered) as asked,
    ):
        identity = read_machine_identity_bounded(second.read_machine_identity)
        handshake, line = camera_setup_line(second, door="tool")
        with pytest.raises(PrinterEngagementError):
            second.camera_probes()

    assert identity is not None and identity.value == "PI-SERIAL-9"
    assert handshake is answered and line == "Camera: answering (built-in)."
    assert asked.call_args.args[0][0].probe_id == "moonraker_webcam_list"
