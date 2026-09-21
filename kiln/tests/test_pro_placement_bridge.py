"""Where a plate-clearance verdict comes from, and why there is none.

Two sources, tried in order -- local kiln-pro, the hosted service -- and no
cache at all: a verdict is about the plate as it stands now.  Pinned with
fakes for both, so the order, the wire form, the three reasons a door can
name when nothing answers, and the absence of a cache cannot drift.
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
import types
from types import SimpleNamespace

import pytest

from kiln import _pro_placement_bridge as bridge


def _verdict(ok: bool = True, at=(40.0, 40.0)) -> dict:
    return {
        "schema": bridge.SCHEMA, "ok": ok, "placed_by": "agent", "at_mm": list(at),
        "tower_at_mm": None, "footprint_mm": [40.0, 40.0, 60.0, 60.0], "clearance_mm": 12.0,
        "refusals": [] if ok else [{"code": "TOO_CLOSE", "sentence": "the head would clip the jar"}],
        "conflicts": [], "switched_off": {}, "spots": [{"at_mm": [40.0, 40.0], "clearance_mm": 12.0}],
        "occupancy": {"kind": bridge.OCCUPANCY_KIND, "bed_mm": [256.0, 256.0], "occupied": [], "reserved": [],
                      "proposed": None, "source": "record_box"},
        "record": {"printer_id": "bambu_a1", "measured": True, "source": "overlay"},
        "tier": {"verdict": "free", "plan": "pro"},
    }


def _request(**over) -> dict:
    base = {
        "schema": bridge.REQUEST_SCHEMA, "printer_id": "bambu_a1", "serial": "01P00A000000001",
        "plate": {"status": "occupied", "job": {"file": "jar.gcode.3mf", "footprint_mm": [90, 90, 160, 160], "max_z_mm": 42.0},
                  "since": "2026-09-21T18:12:00+00:00"},
        "occupant_gcode": None, "part": {"size_mm": [20, 20, 20], "layer_height_mm": 0.2, "tower_mm": None,
                                          "colour_changes_at_mm": [], "skirt_mm": 6.0},
        "sliced_gcode": None, "placement": [40.0, 40.0], "keep_at_mm": None, "placed_by": "agent", "suppress": None,
    }
    base.update(over)
    return base


def _machine(serial: str = "01P00A000000001") -> SimpleNamespace:
    return SimpleNamespace(name="bambu", serial=serial, _printer_model="bambu_a1")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    import kiln.server as srv

    # the served door is never the real network in a test
    monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "error": "no network in tests"})


def _install_local_pro(monkeypatch, build_verdict):
    pkg = types.ModuleType("kiln_pro")
    placement = types.ModuleType("kiln_pro.placement")
    mod = types.ModuleType("kiln_pro.placement.bridge")
    mod.build_verdict = build_verdict
    placement.bridge = mod
    pkg.placement = placement
    monkeypatch.setitem(sys.modules, "kiln_pro", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.placement", placement)
    monkeypatch.setitem(sys.modules, "kiln_pro.placement.bridge", mod)


class TestTheSources:
    def test_no_source_means_no_verdict_and_a_reason(self):
        assert bridge.verdict_for(_request()) is None
        assert bridge.ask(_request()) == (None, bridge.NOT_ANSWERED)

    def test_an_undeclared_model_asks_nobody(self, monkeypatch):
        import kiln.server as srv

        asked: list = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: asked.append(tool) or {"verdict": _verdict()})
        assert bridge.ask(_request(printer_id="")) == (None, bridge.NOT_ANSWERED)
        assert asked == []

    def test_local_kiln_pro_answers_and_the_service_is_not_asked(self, monkeypatch):
        import kiln.server as srv

        seen: list = []
        _install_local_pro(monkeypatch, lambda request: seen.append(request) or _verdict())
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: pytest.fail("the service must not be asked"))
        doc, reason = bridge.ask(_request())
        assert doc["ok"] is True and reason is None
        assert seen[0]["placement"] == [40.0, 40.0], "the local builder is handed the request document"
        assert bridge.available() is True

    def test_a_local_builder_written_to_keyword_arguments_is_met_halfway(self, monkeypatch):
        def build_verdict(*, schema, printer_id, **rest):
            assert schema == bridge.REQUEST_SCHEMA and printer_id == "bambu_a1"
            return _verdict()

        _install_local_pro(monkeypatch, build_verdict)
        assert bridge.ask(_request())[0]["ok"] is True

    def test_a_local_builder_that_raises_falls_through_to_the_service(self, monkeypatch):
        import kiln.server as srv

        def boom(request):
            raise RuntimeError("overlay missing")

        _install_local_pro(monkeypatch, boom)
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"verdict": _verdict(ok=False)})
        doc, reason = bridge.ask(_request())
        assert doc["ok"] is False and reason is None, "a refusing verdict is still a verdict"

    def test_the_service_answers_with_a_verdict_envelope_or_bare(self, monkeypatch):
        import kiln.server as srv

        calls: list = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: calls.append((tool, kw)) or {"verdict": _verdict()})
        assert bridge.ask(_request())[0]["ok"] is True
        assert calls[0][0] == "placement_plan"
        assert calls[0][1]["serial"] == "01P00A000000001" and calls[0][1]["placement"] == [40.0, 40.0]
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: _verdict())
        assert bridge.ask(_request())[0]["schema"] == bridge.SCHEMA

    def test_a_malformed_answer_is_no_verdict(self, monkeypatch):
        import kiln.server as srv

        for bad in ({"verdict": {"schema": "other", "ok": True}}, {"verdict": "yes"}, {"schema": bridge.SCHEMA, "ok": "yes"}, "ok", None):
            monkeypatch.setattr(srv, "_pro_api_call", lambda tool, _bad=bad, **kw: _bad)
            assert bridge.ask(_request()) == (None, bridge.NOT_ANSWERED)


class TestTheReasons:
    """The door words one sentence from these; the codes never leave the bridge."""

    def test_the_network_raising_is_offline(self, monkeypatch):
        import kiln.server as srv

        def _boom(tool, **kw):
            raise OSError("dns")

        monkeypatch.setattr(srv, "_pro_api_call", _boom)
        assert bridge.ask(_request()) == (None, bridge.OFFLINE)

    def test_the_door_reporting_unreachable_is_offline(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": "SERVER_UNREACHABLE", "error": "timeout"})
        assert bridge.ask(_request()) == (None, bridge.OFFLINE)

    @pytest.mark.parametrize("code", ["KILN_ACCOUNT_NOT_PAIRED", "KILN_SIGNIN_REQUIRED", "KILN_SESSION_EXPIRED"])
    def test_no_sign_in_is_signed_out(self, monkeypatch, code):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": code, "error": "sign in"})
        assert bridge.ask(_request()) == (None, bridge.SIGNED_OUT)

    def test_a_refusal_for_this_machine_or_an_http_error_is_not_answered(self, monkeypatch):
        import kiln.server as srv

        for code in ("MACHINE_NOT_PAIRED", "KILN_API_HTTP_ERROR", "NOT_SERVED_HERE", ""):
            monkeypatch.setattr(srv, "_pro_api_call", lambda tool, _c=code, **kw: {"status": "error", "code": _c, "error": "no"})
            assert bridge.ask(_request()) == (None, bridge.NOT_ANSWERED)


class TestNoCache:
    def test_every_ask_reaches_the_service_and_nothing_is_kept(self, monkeypatch):
        import kiln.server as srv

        calls: list = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: calls.append(tool) or {"verdict": _verdict()})
        assert bridge.ask(_request())[0]["ok"] is True
        assert bridge.ask(_request())[0]["ok"] is True
        assert len(calls) == 2
        # The plate may have changed since the last answer: an unreachable
        # service is "no verdict", never the previous verdict.
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": "SERVER_UNREACHABLE", "error": "off"})
        assert bridge.ask(_request()) == (None, bridge.OFFLINE)
        assert not hasattr(bridge, "_cache") and "cache" not in {n.lower() for n in dir(bridge)}


class TestTheWireForm:
    def test_a_local_path_travels_gzipped_and_round_trips(self, tmp_path):
        gcode = tmp_path / "jar.gcode"
        gcode.write_text(";LAYER_CHANGE\nG1 X1 Y1 E1\n" * 50)
        req = _request(occupant_gcode={"path": str(gcode)}, sliced_gcode={"path": str(gcode)})
        wire = bridge.hosted_form(req)
        for field in ("occupant_gcode", "sliced_gcode"):
            assert wire[field]["name"] == "jar.gcode" and "path" not in wire[field]
            assert gzip.decompress(base64.b64decode(wire[field]["gz_b64"])) == gcode.read_bytes()
        assert req["occupant_gcode"] == {"path": str(gcode)}, "the request itself is not modified"
        json.dumps(wire)

    def test_the_service_is_handed_the_wire_form_never_a_path(self, monkeypatch, tmp_path):
        import kiln.server as srv

        gcode = tmp_path / "jar.gcode"
        gcode.write_text("G1 X1\n")
        sent: list = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: sent.append(kw) or {"verdict": _verdict()})
        bridge.ask(_request(occupant_gcode={"path": str(gcode)}))
        assert "gz_b64" in sent[0]["occupant_gcode"] and "path" not in sent[0]["occupant_gcode"]

    def test_over_the_cap_or_unreadable_travels_as_null(self, monkeypatch, tmp_path):
        gcode = tmp_path / "big.gcode"
        gcode.write_bytes(b"x" * 64)
        monkeypatch.setattr(bridge, "MAX_GZ_BYTES", 8)
        assert bridge.hosted_form(_request(sliced_gcode={"path": str(gcode)}))["sliced_gcode"] is None
        assert bridge.hosted_form(_request(sliced_gcode={"path": str(tmp_path / "gone.gcode")}))["sliced_gcode"] is None
        already = {"name": "a.gcode", "gz_b64": "AA=="}
        assert bridge.hosted_form(_request(sliced_gcode=already))["sliced_gcode"] == already


class TestTheRequest:
    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path, monkeypatch):
        from kiln import monitor_twin

        d = tmp_path / "twin"
        d.mkdir()
        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", d)
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", d / "slices.json")
        monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", d / "active.json")

    def test_the_plate_block_is_the_record_and_the_occupant_gcode_is_the_ledgers(self, tmp_path):
        from kiln import monitor_twin
        from kiln.plate_state import PlateJob, mark_occupied

        m = _machine()
        mark_occupied(m, PlateJob(file="jar.gcode.3mf", footprint_mm=[90, 90, 160, 160], max_z_mm=42.0, printer_id="bambu_a1"))
        gcode = tmp_path / "jar.gcode"
        gcode.write_text("G1 X1\n")
        monitor_twin.note_sliced(str(tmp_path / "jar.stl"), str(gcode))
        monitor_twin.note_wrapped(str(gcode), str(tmp_path / "jar.gcode.3mf"))

        req = bridge.request_for(m, "Bambu_A1", placement=[40, 40], part={"size_mm": [20, 20, 20]})
        assert req["schema"] == bridge.REQUEST_SCHEMA
        assert req["printer_id"] == "bambu_a1" and req["serial"] == "01P00A000000001"
        assert req["plate"]["status"] == "occupied"
        assert req["plate"]["job"] == {"file": "jar.gcode.3mf", "footprint_mm": [90.0, 90.0, 160.0, 160.0], "max_z_mm": 42.0}
        assert req["plate"]["since"]
        assert req["occupant_gcode"] == {"path": str(gcode)}
        assert req["placement"] == [40.0, 40.0] and req["placed_by"] == "agent"
        assert req["sliced_gcode"] is None and req["keep_at_mm"] is None and req["suppress"] is None

    def test_who_placed_it_defaults_from_the_placement_and_a_human_can_be_named(self):
        m = _machine()
        assert bridge.request_for(m, "bambu_a1", placement="keep", part=None, keep_at=[10, 12])["placed_by"] == "keep"
        assert bridge.request_for(m, "bambu_a1", placement="keep", part=None, keep_at=[10, 12])["keep_at_mm"] == [10.0, 12.0]
        assert bridge.request_for(m, "bambu_a1", placement="auto", part=None)["placed_by"] == "auto"
        assert bridge.request_for(m, "bambu_a1", placement=(5, 6), part=None, placed_by="human")["placed_by"] == "human"

    def test_a_clear_or_unrecorded_plate_carries_no_occupant(self):
        req = bridge.request_for(_machine(), "bambu_a1", placement="auto", part=None)
        assert req["plate"] == {"status": "unknown", "job": None, "since": None}
        assert req["occupant_gcode"] is None

    def test_the_post_slice_pass_names_the_sliced_file(self, tmp_path):
        req = bridge.request_for(_machine(), "bambu_a1", placement=[1, 2], part=None, sliced_gcode_path=str(tmp_path / "out.gcode"))
        assert req["sliced_gcode"] == {"path": str(tmp_path / "out.gcode")}

    def test_the_twins_retained_copy_answers_when_the_ledger_has_no_file(self, tmp_path):
        from kiln import monitor_twin

        retained = tmp_path / "default-current.gcode"
        retained.write_text("G1 X1\n")
        monitor_twin._write_json(monitor_twin._ACTIVE_FILE, {"default": {"file_name": "jar.gcode.3mf", "started_at": "2026-09-21T18:12:00", "gcode": str(retained)}})
        assert bridge.occupant_gcode_for("jar.gcode.3mf") == {"path": str(retained)}
        assert bridge.occupant_gcode_for("other.gcode.3mf") is None
        assert bridge.occupant_gcode_for("") is None
