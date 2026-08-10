"""Moving joints get a gap derived for the machine, not a table midpoint.

A joint whose parts have to move against each other is the one case where
the historic static band is actively misleading: it answers 0.65 mm for
every printer and every material, so PLA on a calibrated machine and ABS
on an unknown one were told the same thing.  When Kiln Pro is present AND
the caller is entitled to it, the gap is derived instead.

Everything here also pins the shape of the FREE path, because that is the
one most likely to break silently: without kiln-pro, without a printer,
or for a caller whose tier does not include it, the answers must be
exactly what public Kiln has always given.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from kiln import assembly as A


@pytest.fixture()
def parts():
    return [
        A.AssemblyPart("a", "a.stl", material="PETG"),
        A.AssemblyPart("b", "b.stl", material="PETG"),
    ]


@pytest.fixture()
def entitled():
    """A caller whose tier includes the derivation."""
    with patch.object(A, "_has_pro_license", lambda: True):
        yield


@pytest.fixture()
def free():
    with patch.object(A, "_has_pro_license", lambda: False):
        yield


class TestFreePathUnchanged:
    """The historic answers, exactly."""

    def test_without_a_printer_the_answer_is_the_static_midpoint(self, entitled):
        r = A.get_clearance_recommendation("clearance_fit", "PETG", "PETG")
        low, high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        assert r["recommended_clearance_mm"] == pytest.approx((low + high) / 2)
        assert r["running_clearance"] == {}

    def test_a_free_caller_gets_the_static_answer(self, free):
        r = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
        )
        low, high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        assert r["recommended_clearance_mm"] == pytest.approx((low + high) / 2)
        assert r["running_clearance"] == {}

    def test_the_response_always_carries_the_key(self, free):
        """Downstream consumers rely on presence, not truthiness."""
        r = A.get_clearance_recommendation("press_fit", "PLA", "PLA")
        assert "running_clearance" in r

    def test_entitlement_is_the_caller_not_the_process(self, parts):
        """On the hosted server the process holds a licence for everyone,
        so 'is kiln-pro importable' would hand the derivation to every
        free caller.  The gate must be the one that resolves the CALLER."""
        seen = []

        def _gate():
            seen.append(True)
            return False

        with patch.object(A, "_has_pro_license", _gate):
            A.get_clearance_recommendation(
                "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
            )
        assert seen, "the tier gate was never consulted"

    def test_works_with_kiln_pro_absent(self, entitled, monkeypatch):
        """The free-tier regression that matters: no kiln-pro installed."""
        monkeypatch.setitem(
            sys.modules, "kiln_pro.engineering.running_clearance", None
        )
        r = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
        )
        assert r["running_clearance"] == {}
        low, high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        assert r["recommended_clearance_mm"] == pytest.approx((low + high) / 2)


class TestDerivedForMovingJoints:
    def test_the_gap_depends_on_the_material(self, entitled):
        """The whole point: one number for every material was the bug."""
        pla = A.get_clearance_recommendation(
            "clearance_fit", "PLA", "PLA", printer_id="bambu_a1"
        )["recommended_clearance_mm"]
        abs_ = A.get_clearance_recommendation(
            "clearance_fit", "ABS", "ABS", printer_id="bambu_a1"
        )["recommended_clearance_mm"]
        assert pla < abs_, "ABS moves more than PLA and must get more room"

    def test_it_stays_inside_the_historic_band(self, entitled):
        """Never a surprise: the derived number sits where a user of the
        old table would already have been told to look."""
        low, high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        for material in ("PLA", "PETG", "ABS", "ASA", "Nylon"):
            got = A.get_clearance_recommendation(
                "clearance_fit", material, material, printer_id="bambu_a1"
            )["recommended_clearance_mm"]
            assert low <= got <= high, f"{material} landed outside {low}-{high}"

    def test_the_provenance_rides_along(self, entitled):
        r = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
        )
        rc = r["running_clearance"]
        assert rc["recommended_mm"] == r["recommended_clearance_mm"]
        assert rc["expected_as_printed_mm"] > 0
        assert rc["confidence"]
        assert "move against each other" in r["rationale"]

    def test_a_static_joint_is_left_alone(self, entitled):
        """A press fit is not a running fit and must not be widened."""
        for joint in ("press_fit", "snap_fit", "threaded", "glued"):
            r = A.get_clearance_recommendation(
                joint, "PETG", "PETG", printer_id="bambu_a1"
            )
            assert r["running_clearance"] == {}, f"{joint} was treated as moving"

    def test_an_unspecified_shape_assumes_the_demanding_one(self, entitled):
        """A bore is closed on from both sides and needs about twice what
        a flat gap does, so assuming it can only give a joint too much
        room, never too little."""
        assumed = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
        )
        flat = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1",
            mating="planar_face",
        )
        assert assumed["running_clearance"]["mating_assumed"] is True
        assert flat["running_clearance"]["mating_assumed"] is False
        assert assumed["recommended_clearance_mm"] > flat["recommended_clearance_mm"]
        assert "not specified" in assumed["rationale"]

    def test_a_nonsense_shape_falls_back_rather_than_raising(self, entitled):
        r = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1",
            mating="magnets",
        )
        assert r["running_clearance"] == {}
        assert r["recommended_clearance_mm"] > 0


class TestValidationAgrees:
    """The recommendation and the check must not disagree.

    If validation keeps the static minimum while the recommendation is
    machine-aware, a design passes a check it will fail on the plate.
    """

    def test_a_gap_this_printer_would_close_is_flagged(self, entitled, parts):
        low, _high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        # Legal by the static table, too tight for PETG on a real machine.
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=low + 0.1)
        result = A.validate_joint(iface, parts, printer_id="bambu_a1")
        assert result.issues
        assert "fused" in result.issues[0]

    def test_the_recommended_gap_passes_its_own_check(self, entitled, parts):
        """The two sides must be consistent by construction."""
        rec = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
        )["recommended_clearance_mm"]
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=rec)
        assert not A.validate_joint(iface, parts, printer_id="bambu_a1").issues

    def test_free_and_printerless_validation_is_unchanged(self, free, parts):
        low, _high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=low + 0.1)
        assert not A.validate_joint(iface, parts, printer_id="bambu_a1").issues
        assert not A.validate_joint(iface, parts).issues

    def test_the_static_minimum_still_bites_without_a_printer(self, entitled, parts):
        low, _high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=low - 0.1)
        result = A.validate_joint(iface, parts)
        assert result.issues
        assert "below minimum" in result.issues[0]
