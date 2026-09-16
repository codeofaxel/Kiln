"""What the machine itself says about its detectors' own switches.

The coverage statement's whole job is to stop an owner walking away because
they believe a detector is watching.  A detector the owner switched OFF on
the machine's own screen is the one case where the matrix and the machine
disagree — and the matrix is the one that gets believed, because it is the
one that is read.  These pin the reader that asks the machine, through the
adapter contract every backend shares, and pin that the coverage door
actually passes the answer on.
"""

from __future__ import annotations

import pytest

from kiln.detector_switches import detector_switches
from kiln.printers.base import NozzleClumpingDetection


def _reading(**kw) -> NozzleClumpingDetection:
    base = {"enabled": True, "source": "print.home_flag bit 24", "supported": True}
    base.update(kw)
    return NozzleClumpingDetection(**base)


class _Adapter:
    """Any backend: the reader asks the contract, never the brand."""

    def __init__(self, reading):
        self._reading = reading

    def read_nozzle_clumping_detection(self):
        if isinstance(self._reading, Exception):
            raise self._reading
        return self._reading


class TestTheReaderAsksTheContract:
    def test_a_switch_the_machine_reports_on_reads_on(self):
        assert detector_switches(_Adapter(_reading(enabled=True))) == {
            "nozzle_clumping_detection": True
        }

    def test_a_switch_the_machine_reports_off_reads_off(self):
        assert detector_switches(_Adapter(_reading(enabled=False))) == {
            "nozzle_clumping_detection": False
        }

    def test_a_reading_nobody_could_verify_is_unknown_never_off(self):
        """Three states, never two: the whole point of the exercise."""
        switches = detector_switches(
            _Adapter(_reading(enabled=None, unverified_reason="model not measured"))
        )
        assert switches == {"nozzle_clumping_detection": None}
        assert switches["nozzle_clumping_detection"] is not False

    def test_a_machine_that_has_no_such_setting_says_nothing(self):
        assert detector_switches(_Adapter(_reading(enabled=None, supported=False))) == {}

    def test_a_backend_with_no_switch_to_report_says_nothing(self):
        assert detector_switches(_Adapter(None)) == {}

    def test_a_backend_that_raises_says_nothing_rather_than_off(self):
        assert detector_switches(_Adapter(RuntimeError("no connection"))) == {}

    def test_no_adapter_says_nothing(self):
        assert detector_switches(None) == {}

    def test_an_older_backend_without_the_contract_method_says_nothing(self):
        class Bare:
            pass

        assert detector_switches(Bare()) == {}


class TestTheCoverageDoorPassesItOn:
    def test_the_coverage_block_hands_the_switches_to_the_composer(self, monkeypatch):
        """The reader is useless if the one shared door drops its answer."""
        from kiln import server

        seen: dict[str, object] = {}

        class _DI:
            @staticmethod
            def coverage_block(model, **kw):
                seen.update(kw)
                return {"headline": "x", "by_status": {}, "known": True}

        class _Pro:
            device_intelligence = _DI()

            @staticmethod
            def is_available(_name):
                return True

        monkeypatch.setattr(server, "_pro_bridge", lambda: _Pro())
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        monkeypatch.setattr(
            server, "_resolve_adapter", lambda name: _Adapter(_reading(enabled=False))
        )
        assert server._coverage_block_for("default") is not None
        assert seen.get("switches") == {"nozzle_clumping_detection": False}
