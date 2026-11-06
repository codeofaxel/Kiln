"""A printer Kiln cannot drive, or has not tried, is told so at every door.

Kiln lists a few printers it has no connection to, and now and then adds one
before any has been tried on real hardware.  Found on 2026-11-05: an owner
could set such a printer up and be told only that Kiln "checks its prints",
and an install that had done so sat for weeks with nothing said.  No place
recorded which printers these are, so no door could say it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import kiln.printer_support as support
import kiln.safety_profiles as sp
from kiln.printer_setup import set_printer_model

_DATA = Path(support.__file__).resolve().parent / "data"


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Three saved printers of three connection types, none with a model."""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "active_printer": "klip",
                "printers": {
                    "klip": {"type": "moonraker", "host": "http://klip.local"},
                    "bam": {"type": "bambu", "host": "192.168.1.9", "access_code": "12345678", "serial": "32X00A1"},
                    "prusa": {"type": "prusalink", "host": "http://prusa.local", "api_key": "k"},
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: path)
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", path)
    monkeypatch.setattr("kiln.printer_model_resolver._cache", (0.0, None))
    return path


@pytest.fixture(autouse=True)
def _nothing_said_yet():
    sp._nudged.clear()
    sp._pending_nudges.clear()
    yield
    sp._nudged.clear()
    sp._pending_nudges.clear()


class TestTheTable:
    def test_every_row_names_a_catalogue_printer_and_a_known_state(self):
        roster = {k for k in json.loads((_DATA / "printer_intelligence.json").read_text()) if not k.startswith("_")}
        rows = json.loads((_DATA / "printer_support.json").read_text())["printers"]
        assert rows and set(rows) <= roster, sorted(set(rows) - roster)
        for key, row in rows.items():
            assert row["state"] in support.STATES, key
            assert set(row) <= {"state", "start_with"}, key
            if row["state"] == support.NOT_DRIVEN:
                assert row.get("start_with"), f"{key}: a printer Kiln cannot drive says how its owner starts a print"

    def test_a_printer_with_no_row_is_one_kiln_drives(self):
        for ordinary in ("bambu_a1", "prusa_mk4", "ender3", "prusa_core_one", "snapmaker_u1"):
            assert support.support_for(ordinary) is None
            assert support.support_sentence(ordinary) is None


class TestTheSentence:
    @pytest.mark.parametrize("said", ["anycubic_kobra_x", "kobra x", "Kobra X Combo"])
    def test_a_printer_kiln_cannot_drive(self, said):
        sentence = support.support_sentence(said)
        assert sentence.startswith("Kiln cannot drive the Anycubic Kobra X yet")
        assert "design and slice" in sentence and "report_issue" in sentence
        assert not support.support_for(said).driven

    @pytest.mark.parametrize("said", ["bambu_h3c", "h3c", "Bambu Lab H3C"])
    def test_a_printer_not_yet_tried_on_hardware(self, said):
        sentence = support.support_sentence(said)
        assert sentence.startswith("Kiln supports the Bambu Lab H3C")
        assert "has not been tried on a real one" in sentence and "report_issue" in sentence
        assert support.support_for(said).driven

    def test_a_sibling_is_not_given_a_models_row(self):
        assert support.support_for("elegoo_centauri_carbon") is None
        assert support.support_for("bambu_h2c") is None


class TestTheSetupDoor:
    def test_setting_up_a_printer_kiln_cannot_drive_says_so(self, config):
        out = set_printer_model("kobra x", config_path=config)
        assert out["applied"] and out["support"] == support.NOT_DRIVEN
        assert out["message"].count("Kiln cannot drive the Anycubic Kobra X yet") == 1
        # The sentence is this door's own: it is not also filed as the limits
        # note, and it is not left waiting to be said again at the next door.
        assert "cannot drive" not in (out.get("limits_note") or "")
        assert not [s for s in sp._pending_nudges if "cannot drive" in s]

    def test_setting_up_an_untried_printer_asks_for_a_first_print(self, config):
        out = set_printer_model("h3c", printer_name="bam", config_path=config)
        assert out["applied"] and out["support"] == support.UNTESTED
        assert out["message"].count("has not been tried on a real one") == 1

    def test_an_ordinary_printer_hears_nothing_of_it(self, config):
        out = set_printer_model("core one", printer_name="prusa", config_path=config)
        assert out["applied"] and "support" not in out
        assert "cannot drive" not in out["message"] and "not been tried" not in out["message"]


class TestTheStartUpDoor:
    """Every setup door ends at ``warm_profile``; an install that set the
    printer up long ago hears the sentence the next time Kiln starts."""

    def test_the_sentence_is_queued_once_per_process(self):
        sp.warm_profile("anycubic_kobra_x")
        queued = [s for s in sp._pending_nudges if "cannot drive the Anycubic Kobra X" in s]
        assert len(queued) == 1
        sp.warm_profile("anycubic_kobra_x")
        assert len([s for s in sp._pending_nudges if "cannot drive the Anycubic Kobra X" in s]) == 1

    def test_an_ordinary_printer_queues_none(self):
        sp.warm_profile("prusa_mk4")
        assert not [s for s in sp._pending_nudges if "cannot drive" in s or "not been tried" in s]


class TestAFailedConnectionOnAnUntriedPrinter:
    """A connection that fails on a model Kiln has not tried may be Kiln's
    own fault, and its owner is asked to say what happened."""

    def _adapter(self, serial: str, model: str | None = None):
        from kiln.printers.bambu import BambuAdapter

        adapter = BambuAdapter.__new__(BambuAdapter)
        adapter._host, adapter._serial = "192.168.1.9", serial
        if model:
            adapter._safety_profile_id = model
        adapter._other_clients_hint = lambda: ""
        return adapter

    @pytest.mark.parametrize(("serial", "model"), [("32X00A000000001", None), ("0309CA000000001", "bambu_h3c")])
    def test_a_refused_login_adds_the_note(self, serial, model):
        for code in (5, 3):
            message = self._adapter(serial, model)._refusal_message(code, "refused")
            assert "has not been tried on a real one, so this failure may be Kiln's own" in message
            assert "report_issue" in message

    @pytest.mark.parametrize("model", ["bambu_a1", "bambu_h2c", None])
    def test_a_printer_kiln_has_run_adds_nothing(self, model):
        message = self._adapter("0309CA000000001", model)._refusal_message(5, "refused")
        assert "may be Kiln's own" not in message

    def test_only_an_untried_printer_has_the_note(self):
        assert support.failed_connection_note("anycubic_kobra_x") is None
        assert support.failed_connection_note("bambu_a1") is None
        assert support.failed_connection_note("h3c")
