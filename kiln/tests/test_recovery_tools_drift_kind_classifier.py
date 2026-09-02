"""Coverage for the symptom-keyword classifier read by the kiln-pro hooks.

``_classify_drift_kind_from_failure`` maps a free-form failure context
(error message + classified analysis) to one of three ``drift_kind``
values, which it passes on:

- ``"dimensional"`` — first-layer thickness drift, dimensional drift,
  fine-detail loss.
- ``"flow"`` — under-extrusion, layer-adhesion failures, clog /
  starved extruder.
- ``"unknown"`` — neither vocabulary matched.

Flow keywords take precedence over dimensional keywords because flow
symptoms are typically more specific signals (under-extrusion
implicates the bore directly), while dimensional symptoms can also
stem from belt tension or frame settling.

The classifier sits at module scope so the two hook sites
(``analyze_print_failure_smart`` and ``plan_failure_recovery``) share
the same canonical vocabulary — adding a synonym in either set
immediately benefits both hooks.
"""

from __future__ import annotations

import pytest

from kiln.plugins.recovery_tools import _classify_drift_kind_from_failure


class TestEmptyInputsReturnUnknown:
    """No signal in → no claim about which component matters."""

    def test_both_none_returns_unknown(self):
        assert _classify_drift_kind_from_failure(None, None) == "unknown"

    def test_empty_strings_return_unknown(self):
        assert _classify_drift_kind_from_failure("", "") == "unknown"

    def test_blank_message_returns_unknown(self):
        assert _classify_drift_kind_from_failure("   ", None) == "unknown"


class TestFlowKeywordsClassify:
    """Flow vocabulary maps to ``"flow"`` so per-component routing
    weights bore wear."""

    @pytest.mark.parametrize(
        "msg",
        [
            "Under-extrusion at layer 42",
            "Under extrusion mid-print",
            "Underextrusion warning",
            "Layer adhesion failure",
            "Layer-adhesion regression",
            "Filament clog detected",
            "Clogged nozzle",
            "Hot end appears starved",
            "No extrusion in last 10 layers",
        ],
    )
    def test_flow_keyword_in_message(self, msg):
        assert _classify_drift_kind_from_failure(msg) == "flow"

    def test_flow_keyword_in_analysis(self):
        """The classifier scans both the message and the analysis text."""
        assert (
            _classify_drift_kind_from_failure(
                None, "clog suspected in classification",
            )
            == "flow"
        )

    def test_case_insensitive(self):
        assert _classify_drift_kind_from_failure("CLOG") == "flow"
        assert _classify_drift_kind_from_failure("Layer Adhesion") == "flow"


class TestDimensionalKeywordsClassify:
    """Dimensional vocabulary maps to ``"dimensional"`` so per-component
    routing weights tip wear."""

    @pytest.mark.parametrize(
        "msg",
        [
            "First layer thickness off by 0.1mm",
            "First-layer thickness drift",
            "Dimension out of tolerance",
            "Dimensional inaccuracy",
            "Geometry distorted",
            "Geometric drift on x-axis",
            "Small text illegible",
            "Fine detail lost",
            "Fine-detail loss in corners",
        ],
    )
    def test_dimensional_keyword_in_message(self, msg):
        assert _classify_drift_kind_from_failure(msg) == "dimensional"


class TestFlowTakesPrecedenceOverDimensional:
    """When both vocabularies match, flow wins — flow symptoms are
    typically more specific signals that implicate the bore directly,
    while dimensional symptoms can also stem from belt tension or
    frame settling."""

    def test_flow_wins_when_both_keywords_present(self):
        msg = "Dimensional drift accompanied by under-extrusion"
        assert _classify_drift_kind_from_failure(msg) == "flow"

    def test_flow_wins_across_message_and_analysis(self):
        assert (
            _classify_drift_kind_from_failure(
                "dimensional error", "clog suspected",
            )
            == "flow"
        )


class TestNeutralFailureMessagesReturnUnknown:
    """Failure messages that don't carry a tip or bore cue resolve to
    ``"unknown"`` — the hypothesis falls back to single-scalar wear."""

    @pytest.mark.parametrize(
        "msg",
        [
            "Thermal runaway detected",
            "Bed adhesion failure",  # adhesion alone (not "layer adhesion")
            "Z-offset too high",
            "Layer shift on x-axis",
            "Warping at edge",
            "Power loss during print",
        ],
    )
    def test_neutral_message_returns_unknown(self, msg):
        assert _classify_drift_kind_from_failure(msg) == "unknown"
