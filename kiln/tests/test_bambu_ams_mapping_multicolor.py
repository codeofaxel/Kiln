"""AMS routing for a plate that declares several filaments.

The last link in the multicolor chain, and the one the code already
CLAIMED to have: ``slice_and_print`` notes that the 3MF's declared
filaments supersede single-tray routing, but the adapter only built that
mapping when no ``ams_mapping`` had been supplied — and the routing step
upstream always supplies one, picked from a single loaded tray before it
has looked at the file.  A three-filament plate therefore printed with a
one-entry mapping: filaments 2 and 3 addressed nothing, and every color
after the first came out of whichever tray was chosen.

A mapping too short to cover the plate is not a choice worth respecting;
one long enough is left exactly as given.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from kiln.printers.bambu import BambuAdapter


def _plate_3mf(path: Path, colors: list[str], ids: list[int] | None = None) -> str:
    """A 3MF carrying just the plate metadata the router reads."""
    meta = {"filament_colors": colors}
    if ids is not None:
        meta["filament_ids"] = ids
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/plate_1.json", json.dumps(meta))
    return str(path)


@pytest.fixture
def adapter(tmp_path, monkeypatch) -> BambuAdapter:
    """A BambuAdapter with MQTT stubbed, so start_print reaches the
    routing decision and stops at a captured publish."""
    monkeypatch.setenv(
        "KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"),
    )
    a = BambuAdapter(
        host="192.0.2.10", access_code="12345678",
        serial="01P00A000000000", timeout=2,
    )
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    publish_result = mock.MagicMock()
    publish_result.wait_for_publish = mock.MagicMock()
    a._mqtt_client.publish.return_value = publish_result
    a._last_status = {"gcode_state": "running"}
    a._last_state_time = float("inf")
    return a


def _published_mapping(adapter: BambuAdapter) -> dict:
    """The print command the adapter actually put on the wire."""
    payload = json.loads(adapter._mqtt_client.publish.call_args[0][1])
    return payload["print"]


class TestFilamentCountAndMapping:
    def test_three_color_plate_maps_every_filament(self, tmp_path):
        f = _plate_3mf(
            tmp_path / "mc.3mf", ["#F2F2F2", "#D32F2F", "#1A1A1A"], [0, 1, 2],
        )
        assert BambuAdapter.filament_count_3mf(f) == 3
        assert BambuAdapter._build_ams_mapping_from_3mf(f) == [0, 1, 2]

    def test_gapped_filament_ids_get_placeholders(self, tmp_path):
        f = _plate_3mf(tmp_path / "gap.3mf", ["#AAA000", "#00BB00"], [0, 2])
        assert BambuAdapter._build_ams_mapping_from_3mf(f) == [0, -1, 1]

    def test_single_color_plate_has_no_mapping(self, tmp_path):
        f = _plate_3mf(tmp_path / "one.3mf", ["#FFFFFF"], [0])
        assert BambuAdapter._build_ams_mapping_from_3mf(f) is None


class TestShortMappingIsSuperseded:
    """Driven through the real adapter, asserting on the command it
    actually publishes — the mapping the printer is told to use."""

    def test_one_tray_mapping_loses_to_a_three_filament_plate(
        self, adapter, tmp_path,
    ):
        f = _plate_3mf(
            tmp_path / "mc.3mf", ["#F2F2F2", "#D32F2F", "#1A1A1A"], [0, 1, 2],
        )
        result = adapter.start_print(
            "mc.3mf", local_file_path=f, ams_mapping=[1], use_ams=True,
        )
        assert result.success
        assert _published_mapping(adapter)["ams_mapping"] == [0, 1, 2]

    def test_full_explicit_mapping_is_respected(self, adapter, tmp_path):
        """A caller who addressed every filament chose its trays; the
        file must not overrule that."""
        f = _plate_3mf(
            tmp_path / "mc.3mf", ["#F2F2F2", "#D32F2F", "#1A1A1A"], [0, 1, 2],
        )
        adapter.start_print(
            "mc.3mf", local_file_path=f, ams_mapping=[3, 2, 1], use_ams=True,
        )
        assert _published_mapping(adapter)["ams_mapping"] == [3, 2, 1]

    def test_no_mapping_on_a_multicolor_plate_gets_the_files_own(
        self, adapter, tmp_path,
    ):
        f = _plate_3mf(
            tmp_path / "mc.3mf", ["#F2F2F2", "#D32F2F", "#1A1A1A"], [0, 1, 2],
        )
        adapter.start_print("mc.3mf", local_file_path=f)
        published = _published_mapping(adapter)
        assert published["ams_mapping"] == [0, 1, 2]
        assert published["use_ams"] is True

    def test_single_filament_plate_keeps_its_tray(self, adapter, tmp_path):
        """The ordinary one-color print is untouched — no supersede, no
        multi-material mapping invented."""
        f = _plate_3mf(tmp_path / "one.3mf", ["#FFFFFF"], [0])
        adapter.start_print(
            "one.3mf", local_file_path=f, ams_mapping=[2], use_ams=True,
        )
        assert _published_mapping(adapter)["ams_mapping"] == [2]

    def test_unreadable_plate_keeps_the_supplied_mapping(
        self, adapter, tmp_path,
    ):
        junk = tmp_path / "junk.3mf"
        junk.write_bytes(b"not a zip")
        adapter.start_print(
            "junk.3mf", local_file_path=str(junk), ams_mapping=[1],
            use_ams=True,
        )
        assert _published_mapping(adapter)["ams_mapping"] == [1]

    def test_the_user_is_told_when_their_mapping_was_replaced(
        self, adapter, tmp_path,
    ):
        f = _plate_3mf(
            tmp_path / "mc.3mf", ["#F2F2F2", "#D32F2F", "#1A1A1A"], [0, 1, 2],
        )
        result = adapter.start_print(
            "mc.3mf", local_file_path=f, ams_mapping=[1], use_ams=True,
        )
        assert "3 filaments" in result.message


class TestEndToEndWrapFeedsTheRouter:
    """The whole chain in one assertion: painted 3MF → Orca gcode →
    Bambu wrap → the mapping the printer is actually told to use."""

    def test_wrapped_multicolor_gcode_routes_every_filament(self, tmp_path):
        from kiln.printers.bambu_3mf import _reset_cache, build_bambu_3mf

        _reset_cache()
        body = "\n".join(
            [
                "; generated by PrusaSlicer",
                "M83",
                ";BEFORE_LAYER_CHANGE", ";Z:0.2", ";LAYER_CHANGE",
                "G1 Z0.2 F600", "T0", "G1 X10 Y10 E0.5",
                ";BEFORE_LAYER_CHANGE", ";Z:0.4", ";LAYER_CHANGE",
                "G1 Z0.4 F600", "T1", "G1 X20 Y20 E0.5",
                ";BEFORE_LAYER_CHANGE", ";Z:0.6", ";LAYER_CHANGE",
                "G1 Z0.6 F600", "T2", "G1 X30 Y30 E0.5",
                "; filament_colour = #F2F2F2;#D32F2F;#1A1A1A",
            ]
        ) + "\n"
        out = tmp_path / "wrapped.3mf"
        try:
            build_bambu_3mf(body, str(out))
        finally:
            _reset_cache()

        assert BambuAdapter.filament_count_3mf(str(out)) == 3
        assert BambuAdapter._build_ams_mapping_from_3mf(str(out)) == [0, 1, 2]
