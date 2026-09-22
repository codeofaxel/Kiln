"""A nozzle's wear milestone is said once, when it is crossed -- never per print.

What has to hold, each pinned: a rung is announced once per nozzle; a
higher rung is announced again; a swap starts the ladder over; the budget
refusal fires every time and its override still works; a start that could
not check says so only for a nozzle already flagged; the pre-flight always
shows state and never consumes a notice; the hosted server remembers
nothing; the memory outlives the process; and the words pass the voice
lint (in ``test_hosted_doors_roster``).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from kiln import nozzle_milestones as nm


def _verdict(status: str, *, grams: float = 400.0, material: str = "brass", **more) -> dict:
    return {"status": status, "narrative": f"{status} narrative", "percent_used": 60.0,
            "nozzle_material": material, "nozzle_grams_through_before": grams, **more}


@pytest.fixture(autouse=True)
def _own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)


class TestOncePerRung:
    def test_a_rung_is_said_once_and_a_higher_rung_again(self):
        first = nm.notice_for("a1", _verdict("approaching"))
        assert first is not None and first["crossed"] is True and first["status"] == "approaching"
        assert "Your nozzle is approaching" in first["line"] and "approaching narrative" in first["line"]
        assert nm.notice_for("a1", _verdict("approaching", grams=450.0)) is None  # the next print: not news
        higher = nm.notice_for("a1", _verdict("exceeded_p50", grams=600.0))
        assert higher is not None and higher["status"] == "exceeded_p50"
        assert nm.notice_for("a1", _verdict("exceeded_p50", grams=650.0)) is None
        assert nm.notice_for("a1", _verdict("approaching", grams=700.0)) is None  # a softer verdict is not news either
        assert nm.last_rung("a1") == "approaching"  # ...but is remembered, so a re-climb is said again
        assert nm.notice_for("a1", _verdict("exceeded_p50", grams=720.0)) is not None

    def test_safe_is_remembered_but_never_said(self):
        assert nm.notice_for("a1", _verdict("safe")) is None
        assert nm.last_rung("a1") == "safe" and nm.is_flagged("a1") is False

    def test_a_status_off_the_ladder_is_neither_said_nor_remembered(self):
        for status in ("unknown_nozzle", "unknown_baseline", "invalid_input", "", None):
            assert nm.notice_for("a1", _verdict(status)) is None
        assert nm.last_rung("a1") is None

    def test_no_printer_or_no_verdict_is_nothing(self):
        assert nm.notice_for("", _verdict("approaching")) is None
        assert nm.notice_for("a1", None) is None


class TestPerNozzle:
    def test_a_swap_starts_the_ladder_again(self):
        assert nm.notice_for("a1", _verdict("exceeded_p50", grams=600.0)) is not None
        assert nm.notice_for("a1", _verdict("exceeded_p50", grams=650.0)) is None
        # Fewer grams through than last time: a fresh nozzle.
        assert nm.notice_for("a1", _verdict("approaching", grams=40.0)) is not None
        assert nm.last_rung("a1") == "approaching"

    def test_another_material_is_another_nozzle(self):
        assert nm.notice_for("a1", _verdict("approaching", material="brass")) is not None
        assert nm.notice_for("a1", _verdict("approaching", material="hardened steel", grams=900.0)) is not None

    def test_a_verdict_with_no_identity_still_counts_once(self):
        bare = {"status": "approaching", "narrative": "n"}
        assert nm.notice_for("a1", bare) is not None
        assert nm.notice_for("a1", bare) is None

    def test_two_printers_do_not_share_a_ladder(self):
        assert nm.notice_for("a1", _verdict("approaching")) is not None
        assert nm.notice_for("b2", _verdict("approaching")) is not None

    def test_forget_drops_the_record(self):
        nm.notice_for("a1", _verdict("exceeded_p50"))
        nm.forget("a1")
        assert nm.last_rung("a1") is None and nm.notice_for("a1", _verdict("approaching")) is not None


class TestTheMemory:
    def test_it_outlives_the_process(self, tmp_path):
        nm.notice_for("a1", _verdict("approaching"))
        path = tmp_path / "kiln-home" / "nozzle_milestones.json"
        assert path.is_file() and '"rung": "approaching"' in path.read_text()
        # A fresh read (as a new process would do) sees the same ladder.
        assert nm.last_rung("a1") == "approaching" and nm.is_flagged("a1") is True

    def test_a_torn_file_is_an_empty_memory(self, tmp_path):
        home = tmp_path / "kiln-home"
        home.mkdir(parents=True)
        (home / "nozzle_milestones.json").write_text("{not json")
        assert nm.last_rung("a1") is None
        assert nm.notice_for("a1", _verdict("approaching")) is not None

    def test_the_hosted_server_remembers_nothing_and_says_every_crossing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nm, "_hosted", lambda: True)
        assert nm.notice_for("a1", _verdict("approaching")) is not None
        assert nm.notice_for("a1", _verdict("approaching")) is not None  # said again: no per-tenant memory here
        assert nm.last_rung("a1") is None and nm.is_flagged("a1") is False
        assert not (tmp_path / "kiln-home" / "nozzle_milestones.json").exists()

    def test_a_deployment_it_cannot_read_is_read_as_the_shared_server(self, tmp_path, monkeypatch):
        """The guard fails CLOSED: unreadable means "assume shared".

        Every other test here reaches past ``_hosted`` by replacing it, so
        what the predicate does when it cannot be READ was never pinned --
        and it answered "not the shared server", which turns the memory back
        ON there.  One disk serves every account, the key is a printer name
        the CALLER chose, so two accounts that both call their machine
        "default" share a record: the first is told about the crossing and
        the second is silenced by it.  Being told twice is noise; not being
        told is a nozzle past its budget that nobody mentioned.
        """
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        # A partial or circular import during startup, as the machinery sees it.
        monkeypatch.setitem(sys.modules, "kiln.runtime_env", None)
        assert nm._hosted() is True

        first_account = nm.notice_for("default", _verdict("approaching"))
        second_account = nm.notice_for("default", _verdict("approaching"))
        assert first_account is not None
        assert second_account is not None, "silenced by another account's record"
        assert nm.last_rung("default") is None and nm.is_flagged("default") is False
        assert not (tmp_path / "kiln-home" / "nozzle_milestones.json").exists()

    def test_a_predicate_that_raises_writes_nothing(self, monkeypatch):
        """The guarded import fails closed; a guard that RAISES is not
        caught here on purpose, because a broad handler around a guard is
        the shape that turns a refusal into an ordinary failure.  What
        matters is what reaches the disk: nothing."""
        import kiln.runtime_env as re_mod

        monkeypatch.setattr(re_mod, "is_hosted_multitenant", lambda: (_ for _ in ()).throw(RuntimeError("no env")))
        with pytest.raises(RuntimeError):
            nm.notice_for("a1", _verdict("approaching"))
        monkeypatch.undo()
        assert nm.last_rung("a1") is None  # nothing was remembered

    def test_a_record_that_cannot_be_written_still_lets_the_notice_through(self, monkeypatch):
        monkeypatch.setattr(nm, "_write_all", lambda data: (_ for _ in ()).throw(RuntimeError("disk")))
        assert nm.notice_for("a1", _verdict("approaching")) is not None


class TestTheWords:
    def test_each_notice_leads_with_the_crossing_then_the_verdicts_words_then_the_next_step(self):
        for rung, (headline, next_step) in nm._WORDS.items():
            out = nm.notice_for(f"p-{rung}", _verdict(rung))
            assert out["line"].startswith(headline) and out["line"].endswith(next_step)
            assert f"{rung} narrative." in out["line"]

    def test_the_community_hint_rides_beside_the_notice(self):
        out = nm.notice_for("a1", _verdict("approaching", upgrade_hint="Kiln Pro shows how long owners like you got."))
        assert out["upgrade_hint"].startswith("Kiln Pro") and "Kiln Pro" not in out["line"]


class TestTheStartDoor:
    """The start says a crossing once, refuses at the budget every time, and
    names an unchecked nozzle only when it was already flagged."""

    def _start(self, monkeypatch, verdict):
        import kiln.server as srv
        from kiln.printers.base import PrinterFile

        from .test_every_start_says_so import _two_printers

        garage, _ = _two_printers(monkeypatch)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(garage, "list_files", lambda: [PrinterFile(name="part.gcode", path="part.gcode", filament_used_mm=5000.0)])
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        monkeypatch.setattr("kiln._pro_nozzle_bridge.consult_capacity", lambda **kw: verdict)
        with patch.dict(os.environ, {"KILN_SKIP_PREFLIGHT": "1", "KILN_SKIP_PREVIEW_GATE": "1", "KILN_SKIP_NOZZLE_CHECK": ""}):
            return srv.start_print(file_name="part.gcode", printer_name="garage"), garage

    def test_a_crossing_is_said_once_across_two_starts(self, monkeypatch):
        out, garage = self._start(monkeypatch, _verdict("approaching"))
        assert out["success"] is not False and out["nozzle_advisory"]["crossed"] is True
        out, garage = self._start(monkeypatch, _verdict("approaching", grams=450.0))
        assert out["success"] is not False and "nozzle_advisory" not in out

    def test_the_budget_refuses_every_time_and_the_refusal_carries_the_first_notice(self, monkeypatch):
        out, garage = self._start(monkeypatch, _verdict("exceeded_p90"))
        assert out["success"] is False and out["error"]["code"] == "NOZZLE_CAPACITY_EXCEEDED"
        assert out["nozzle_advisory"]["status"] == "exceeded_p90" and garage.started == []
        out, garage = self._start(monkeypatch, _verdict("exceeded_p90", grams=450.0))
        assert out["success"] is False and "nozzle_advisory" not in out and garage.started == []

    def test_the_override_still_works(self, monkeypatch):
        import kiln.server as srv
        from kiln.printers.base import PrinterFile

        from .test_every_start_says_so import _two_printers

        garage, _ = _two_printers(monkeypatch)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
        monkeypatch.setattr(garage, "list_files", lambda: [PrinterFile(name="part.gcode", path="part.gcode", filament_used_mm=5000.0)])
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        monkeypatch.setattr("kiln._pro_nozzle_bridge.consult_capacity", lambda **kw: _verdict("exceeded_p90"))
        with patch.dict(os.environ, {"KILN_SKIP_PREFLIGHT": "1", "KILN_SKIP_PREVIEW_GATE": "1", "KILN_SKIP_NOZZLE_CHECK": "1"}):
            out = srv.start_print(file_name="part.gcode", printer_name="garage")
        assert out["success"] is not False and garage.started == ["part.gcode"]

    def test_an_unchecked_nozzle_is_named_only_once_flagged(self, monkeypatch):
        from kiln import _pro_nozzle_bridge as bridge
        from kiln.served_answer import Miss

        monkeypatch.setattr(bridge, "_last_miss", {"garage": Miss("offline")})
        out, garage = self._start(monkeypatch, None)  # the consult missed
        assert out["success"] is not False and "nozzle_check" not in out  # never flagged: silence
        self._start(monkeypatch, _verdict("approaching"))  # now it is
        monkeypatch.setattr(bridge, "_last_miss", {"garage": Miss("offline")})
        out, garage = self._start(monkeypatch, None)
        assert out["nozzle_check"]["why"] == "offline" and "started this one without that check" in out["nozzle_check"]["line"]


class TestThePreflight:
    def test_the_preflight_shows_state_every_time_and_consumes_no_notice(self, tmp_path, monkeypatch):
        from .test_served_refusals_say_why import _preflight_with_file

        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n; filament used [g] = 340.0\n")
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        monkeypatch.setattr("kiln._pro_nozzle_bridge.consult_capacity", lambda **kw: _verdict("approaching"))
        for _ in range(2):
            result = _preflight_with_file(monkeypatch, str(gcode))
            line = [c for c in result["checks"] if c["name"] == "nozzle_capacity"]
            assert len(line) == 1 and line[0]["status"] == "approaching"
        assert nm.last_rung("a1") is None  # looking is not a notice
