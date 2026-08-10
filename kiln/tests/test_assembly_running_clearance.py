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


class TestResponseIsSelfConsistent:
    """The three numbers in the payload have to agree with each other.

    They did not.  Every material reported a tolerance that took the
    recommendation BELOW its own floor, so a caller reading
    `recommended +/- tolerance` designed the gap the recommendation
    exists to rule out; and TPU came back recommending 1.65 mm against a
    range of [1.1, 1.1], because the flexible-material bump raises the
    recommendation after the range is fixed.
    """

    JOINTS = ("clearance_fit", "loose", "press_fit", "snap_fit",
              "threaded", "glued", "magnetic")
    MATERIALS = ("PLA", "PETG", "ABS", "ASA", "Nylon", "TPU", "TPE",
                 "unobtainium")

    @pytest.mark.parametrize("entitled_caller", [True, False])
    def test_recommendation_lies_inside_its_own_range(self, entitled_caller):
        with patch.object(A, "_has_pro_license", lambda: entitled_caller):
            for joint in self.JOINTS:
                for material in self.MATERIALS:
                    for printer in ("bambu_a1", None):
                        r = A.get_clearance_recommendation(
                            joint, material, material, printer_id=printer
                        )
                        rec = r["recommended_clearance_mm"]
                        low, high = r["clearance_range_mm"]
                        assert low <= high, f"{joint}/{material}: range inverted"
                        assert low - 1e-9 <= rec <= high + 1e-9, (
                            f"{joint}/{material}/printer={printer}: "
                            f"recommended {rec} outside range [{low}, {high}]"
                        )

    def test_a_running_fit_never_offers_give_it_does_not_have(self, entitled):
        """Below the floor the parts weld, so the minus side of the
        tolerance may never cross it."""
        for material in self.MATERIALS:
            r = A.get_clearance_recommendation(
                "clearance_fit", material, material, printer_id="bambu_a1"
            )
            if not r["running_clearance"]:
                continue
            rec = r["recommended_clearance_mm"]
            floor = r["clearance_range_mm"][0]
            assert rec - r["tolerance_mm"] >= floor - 1e-9, (
                f"{material}: recommended-minus-tolerance "
                f"{rec - r['tolerance_mm']:.3f} is under the {floor:.3f} floor"
            )

    def test_the_flexible_bump_cannot_escape_the_range(self, entitled):
        """A threaded TPU joint has always reported 0.30 mm recommended
        against a 0.15-0.25 mm range on main.  Widening the range to
        contain its own recommendation changes no advice."""
        r = A.get_clearance_recommendation(
            "threaded", "TPU", "TPU", printer_id="bambu_a1"
        )
        low, high = r["clearance_range_mm"]
        assert low <= r["recommended_clearance_mm"] <= high

    def test_the_minimum_is_named_as_a_minimum(self, entitled):
        r = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="bambu_a1"
        )
        assert "minimum rather than a target" in r["rationale"]


class TestCalibrationIsResolvedOncePerSweep:
    """Validating an assembly asks one unchanging question per interface.

    Resolving calibration scans the filesystem for slicer profiles, so a
    twenty-interface assembly paid for the same answer twenty times.
    """

    def test_the_sweep_resolves_calibration_once(self, entitled, parts):
        calls: list = []
        real = A._running_clearance_view

        def _counting(joint_type, **kw):
            if kw.get("cache") is None or (
                joint_type, kw.get("material"), kw.get("printer_id"),
                kw.get("mating"),
            ) not in kw["cache"]:
                calls.append(joint_type)
            return real(joint_type, **kw)

        ifaces = [
            A.MatingInterface("a", "b", "clearance_fit", clearance_mm=0.9)
            for _ in range(8)
        ]
        asm = A.Assembly(
            assembly_id="x", name="x", parts=parts, interfaces=ifaces
        )
        with patch.object(A, "_running_clearance_view", _counting), \
                patch.object(A, "check_all_clearances", lambda a: None):
            A.validate_assembly(asm, printer_id="bambu_a1")
        assert len(calls) == 1, f"resolved calibration {len(calls)} times for 8 joints"

    def test_the_cache_does_not_outlive_the_call(self, entitled, parts):
        """A cache that survived would answer a later call from slicer
        profiles the user has since edited."""
        seen: list = []
        real = A.validate_joint

        def _spy(interface, parts_, **kw):
            seen.append(kw.get("_clearance_cache"))
            return real(interface, parts_, **kw)

        ifaces = [A.MatingInterface("a", "b", "clearance_fit", clearance_mm=0.9)]
        asm = A.Assembly(assembly_id="x", name="x", parts=parts, interfaces=ifaces)
        with patch.object(A, "validate_joint", _spy), \
                patch.object(A, "check_all_clearances", lambda a: None):
            A.validate_assembly(asm, printer_id="bambu_a1")
            A.validate_assembly(asm, printer_id="bambu_a1")
        assert len(seen) == 2
        assert seen[0] is not seen[1], "the same cache spanned two calls"

    def test_caching_does_not_change_the_verdict(self, entitled, parts):
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=0.4)
        uncached = A.validate_joint(iface, parts, printer_id="bambu_a1")
        cache: dict = {}
        first = A.validate_joint(
            iface, parts, printer_id="bambu_a1", _clearance_cache=cache
        )
        second = A.validate_joint(
            iface, parts, printer_id="bambu_a1", _clearance_cache=cache
        )
        assert uncached.issues == first.issues == second.issues


class TestReachesCallersThatNeverAskedForIt:
    """The wiring that decides whether any of this actually lands.

    A machine-aware clearance that only fires when a caller passes
    printer_id reaches the callers who already knew to ask, and leaves
    every existing assembly path on the static table forever.  Not
    passing a printer was never a request for a generic answer — it is a
    call site that predates the parameter.
    """

    @pytest.fixture()
    def configured(self):
        import kiln.printer_model_resolver as R
        with patch.object(R, "resolve_printer_model", lambda: "bambu_a1"):
            yield

    @pytest.fixture()
    def unconfigured(self):
        import kiln.printer_model_resolver as R
        with patch.object(R, "resolve_printer_model", lambda: None):
            yield

    def test_the_recommendation_uses_the_active_printer(self, entitled, configured):
        r = A.get_clearance_recommendation("clearance_fit", "PETG", "PETG")
        assert r["running_clearance"], "never reached the derivation"
        assert r["running_clearance"]["printer_resolved"] is True
        assert r["running_clearance"]["printer_id"] == "bambu_a1"

    def test_validation_uses_the_active_printer(self, entitled, configured, parts):
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=0.40)
        assert A.validate_joint(iface, parts).issues, (
            "a gap this printer closes passed a check that never asked "
            "which printer"
        )

    def test_the_whole_assembly_sweep_inherits_it(self, entitled, configured, parts):
        iface = A.MatingInterface("a", "b", "clearance_fit", clearance_mm=0.40)
        asm = A.Assembly(assembly_id="x", name="x", parts=parts, interfaces=[iface])
        with patch.object(A, "check_all_clearances", lambda a: None):
            A.validate_assembly(asm)
        assert asm.joint_validations[0].issues

    def test_an_explicit_printer_still_wins(self, entitled, configured):
        r = A.get_clearance_recommendation(
            "clearance_fit", "PETG", "PETG", printer_id="prusa_mk4"
        )
        assert r["running_clearance"]["printer_id"] == "prusa_mk4"
        assert r["running_clearance"]["printer_resolved"] is False

    def test_no_configured_printer_keeps_the_static_answer(
        self, entitled, unconfigured
    ):
        """Bare library call, or the hosted server with its empty registry."""
        r = A.get_clearance_recommendation("clearance_fit", "PETG", "PETG")
        assert r["running_clearance"] == {}
        low, high = A._DEFAULT_JOINT_CLEARANCES["clearance_fit"]
        assert r["recommended_clearance_mm"] == pytest.approx((low + high) / 2)

    def test_a_free_caller_is_not_swept_in(self, free, configured):
        """Resolving the printer must not become a way past the tier gate."""
        r = A.get_clearance_recommendation("clearance_fit", "PETG", "PETG")
        assert r["running_clearance"] == {}

    def test_an_unreadable_config_degrades_quietly(self, entitled, monkeypatch):
        import kiln.printer_model_resolver as R

        def _boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(R, "resolve_printer_model", _boom)
        r = A.get_clearance_recommendation("clearance_fit", "PETG", "PETG")
        assert r["running_clearance"] == {}
        assert r["recommended_clearance_mm"] > 0
