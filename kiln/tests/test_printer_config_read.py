"""Every printer adapter either reads its machine's config or says why not.

``get_printer_config`` is how Kiln asks a machine what its firmware
configuration actually contains — the seam the start-gcode handoff (and
anything else that adapts to the machine's own config) hangs off.  A new
adapter added without a decision here silently opts its printers out of
every config-aware behavior, the same one-door drift that let 52 profiles
ship without a start G-code.  So the decision is forced: implement the
method, or record the platform reason it cannot exist.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from unittest import mock

import pytest
import requests

import kiln.printers
from kiln.printers.base import PrinterAdapter, PrinterError

# Adapters whose platform has no readable firmware config, and why.  An
# entry here is a claim about a protocol, not a punt — cite the protocol.
_CONFIG_READ_EXEMPT: dict[str, str] = {
    "BambuAdapter": (
        "Bambu's MQTT protocol exposes no firmware config; start G-code "
        "is intentionally empty and injected by kiln.printers.bambu_3mf."
    ),
    "PrusaLinkAdapter": (
        "PrusaLink fronts Buddy/Marlin firmware — configuration is "
        "compiled in, not a readable file."
    ),
    "OctoPrintAdapter": (
        "The OctoPrint REST API exposes no endpoint for the firmware's "
        "configuration."
    ),
    "DuetAdapter": (
        "RepRapFirmware start logic lives in sys/*.g files, not a Klipper "
        "config; no gcode_macro concept exists to read."
    ),
    "ElegooAdapter": (
        "The SDCP protocol exposes no config read."
    ),
    "SerialPrinterAdapter": (
        "A raw serial link has no config API at all."
    ),
}


def _all_adapter_classes() -> list[type]:
    classes: dict[str, type] = {}
    for info in pkgutil.iter_modules(kiln.printers.__path__):
        try:
            module = importlib.import_module(f"kiln.printers.{info.name}")
        except Exception:
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, PrinterAdapter)
                and obj is not PrinterAdapter
                and obj.__module__ == module.__name__
            ):
                classes[obj.__name__] = obj
    return list(classes.values())


class TestEveryAdapterDecides:
    def test_config_read_or_documented_exemption(self):
        adapters = _all_adapter_classes()
        assert len(adapters) >= 8, "adapter discovery broke — found too few"
        undecided = []
        for cls in adapters:
            has_method = callable(getattr(cls, "get_printer_config", None))
            exempt = cls.__name__ in _CONFIG_READ_EXEMPT
            if has_method and exempt:
                undecided.append(f"{cls.__name__}: both implements and exempt — pick one")
            elif not has_method and not exempt:
                undecided.append(
                    f"{cls.__name__}: neither implements get_printer_config nor "
                    "carries an exemption in _CONFIG_READ_EXEMPT"
                )
        assert not undecided, (
            "Adapters without a config-read decision:\n  " + "\n  ".join(undecided)
        )

    def test_exemption_list_carries_no_stale_names(self):
        names = {cls.__name__ for cls in _all_adapter_classes()}
        stale = set(_CONFIG_READ_EXEMPT) - names
        assert not stale, f"exempt adapters that no longer exist: {stale}"


class TestMoonrakerConfigRead:
    """The concrete implementation, against Moonraker's wire shape."""

    def _adapter(self):
        from kiln.printers.moonraker import MoonrakerAdapter
        return MoonrakerAdapter(host="http://klipper.local:7125", timeout=5, retries=1)

    def _resp(self, json_data):
        resp = mock.MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = json_data
        return resp

    def test_returns_config_sections(self):
        adapter = self._adapter()
        payload = {
            "result": {"status": {"configfile": {"config": {
                "printer": {"kinematics": "corexy"},
                "gcode_macro PRINT_START": {"gcode": "M190 S{params.BED}"},
            }}}}
        }
        with mock.patch.object(adapter._session, "request", return_value=self._resp(payload)) as req:
            config = adapter.get_printer_config()
        assert "gcode_macro PRINT_START" in config
        assert req.call_args.kwargs.get("params") == {"configfile": ""}

    def test_missing_configfile_object_returns_none(self):
        adapter = self._adapter()
        with mock.patch.object(
            adapter._session, "request",
            return_value=self._resp({"result": {"status": {}}}),
        ):
            assert adapter.get_printer_config() is None

    def test_unreachable_raises_not_swallows(self):
        adapter = self._adapter()
        with mock.patch.object(
            adapter._session, "request",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ), mock.patch("kiln.printers.moonraker.time.sleep"), pytest.raises(PrinterError):
            adapter.get_printer_config()


class TestCrealityDelegation:
    def test_forwards_to_moonraker_backend(self):
        from kiln.printers.creality import CrealityAdapter

        adapter = CrealityAdapter.__new__(CrealityAdapter)
        backend = mock.MagicMock()
        backend.get_printer_config.return_value = {"printer": {}}
        adapter._backend = backend
        assert adapter.get_printer_config() == {"printer": {}}
        backend.get_printer_config.assert_called_once_with()


class TestPublicSeam:
    """start_gcode_override_from_printer — the one public seam."""

    def test_delegates_to_bridge_when_present(self):
        import sys

        from kiln.slicer_profiles import start_gcode_override_from_printer

        bridge = mock.MagicMock()
        bridge.pro_features.start_gcode_override.return_value = (
            {"start_gcode": "PRINT_START BED=65"}, "handoff:PRINT_START",
        )
        with mock.patch.dict(sys.modules, {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": bridge}):
            patch, reason = start_gcode_override_from_printer(object(), "voron_2", None)
        assert patch == {"start_gcode": "PRINT_START BED=65"}
        assert reason == "handoff:PRINT_START"

    def test_bridge_exception_never_escapes(self):
        import sys

        from kiln.slicer_profiles import start_gcode_override_from_printer

        bridge = mock.MagicMock()
        bridge.pro_features.start_gcode_override.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, {"kiln_pro": mock.MagicMock(), "kiln_pro.bridge": bridge}):
            patch, reason = start_gcode_override_from_printer(object(), "voron_2", None)
        assert patch is None and reason == "handoff-error"

    def test_handoff_patch_survives_profile_resolution(self):
        """The full public contract: a handoff start_gcode, resolved
        through the ordinary door, reaches the .ini verbatim and the
        floor stays out of its way."""
        from pathlib import Path

        from kiln.slicer_profiles import resolve_slicer_profile

        ini = Path(resolve_slicer_profile(
            "k1_max", overrides={"start_gcode": "PRINT_START EXTRUDER=225 BED=60"}
        )).read_text(encoding="utf-8")
        lines = [ln for ln in ini.splitlines() if ln.startswith("start_gcode")]
        assert lines == ["start_gcode = PRINT_START EXTRUDER=225 BED=60"]
        assert "M190" not in lines[0] and "G28" not in lines[0]
