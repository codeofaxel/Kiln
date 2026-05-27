"""Coverage for the recommend_material → nozzle-summary wire.

This is the sibling wire to the abrasive-escalation overlay
(commit 8d1d523).  It appends a one-sentence nozzle-context line to
``MaterialRecommendation.reasoning`` whenever the caller supplies a
``printer_id`` and the bridge resolves a nozzle state.  Free-tier
callers (no kiln-pro / no resolved state) see an unchanged reasoning
string.

The two wires compose: when the top pick is abrasive AND the active
nozzle is brass, both the prepended NOZZLE ADVISORY and the appended
nozzle context appear in the same reasoning string.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiln import _pro_nozzle_bridge
from kiln.material_routing import (
    _format_nozzle_context_line,
    recommend_material,
)


# ---------------------------------------------------------------------------
# Pure formatter
# ---------------------------------------------------------------------------


class TestFormatNozzleContextLine:
    def test_none_returns_empty(self) -> None:
        assert _format_nozzle_context_line(None) == ""

    def test_empty_dict_returns_empty(self) -> None:
        assert _format_nozzle_context_line({}) == ""

    def test_missing_material_returns_empty(self) -> None:
        assert _format_nozzle_context_line({"diameter_mm": 0.4}) == ""

    def test_brass_with_full_summary(self) -> None:
        line = _format_nozzle_context_line(
            {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "bambu_mqtt",
                "grams_through": 120.0,
                "trusted_for_verdicts": True,
            }
        )
        assert "brass" in line
        assert "0.4mm" in line
        assert "bambu_mqtt" in line
        assert "~120g through" in line
        assert line.startswith("Nozzle context:")
        assert line.endswith("Settings tuned accordingly.")
        # Caller's one-sentence + length contract.
        assert len(line) <= 200

    def test_zero_grams_omitted(self) -> None:
        line = _format_nozzle_context_line(
            {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "factory_default",
                "grams_through": 0,
                "trusted_for_verdicts": True,
            }
        )
        # Don't write "~0g through" — it's noise on a fresh nozzle.
        assert "~0g" not in line
        assert "brass" in line
        assert "factory_default" in line

    def test_kilograms_when_large(self) -> None:
        line = _format_nozzle_context_line(
            {
                "material": "hardened_steel",
                "diameter_mm": 0.6,
                "provenance": "user_set",
                "grams_through": 4250.0,
                "trusted_for_verdicts": True,
            }
        )
        assert "hardened_steel" in line
        assert "~4.2kg through" in line

    def test_low_confidence_hedge(self) -> None:
        line = _format_nozzle_context_line(
            {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "factory_default",
                "grams_through": 0,
                "trusted_for_verdicts": False,
            }
        )
        assert "low confidence" in line
        # Don't lecture the user twice — drop the "Settings tuned"
        # phrase when we're already hedging.
        assert "Settings tuned" not in line


# ---------------------------------------------------------------------------
# Wire — recommend_material reasoning mutation
# ---------------------------------------------------------------------------


class TestRecommendMaterialNozzleSummaryWire:
    """End-to-end behaviour with the bridge mocked at the import path."""

    def test_no_printer_id_zero_behavior_change(self) -> None:
        """Existing callers that don't pass printer_id are untouched."""
        rec = recommend_material("strong")
        assert "Nozzle context" not in rec.reasoning
        assert "NOZZLE ADVISORY" not in rec.reasoning

    def test_free_tier_no_state_resolves_zero_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bridge returns None on free tier — reasoning unchanged."""
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation",
            lambda **_: None,
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary",
            lambda _printer_id: None,
        )
        baseline = recommend_material("strong")
        wired = recommend_material("strong", printer_id="bambu_a1")
        assert wired.reasoning == baseline.reasoning

    def test_pro_with_summary_appends_context_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pro+ install with a resolved state appends one nozzle line."""
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation",
            lambda **_: None,  # not abrasive — only the summary wire fires
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary",
            lambda _printer_id: {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "bambu_mqtt",
                "grams_through": 120.0,
                "trusted_for_verdicts": True,
            },
        )
        rec = recommend_material("strong", printer_id="bambu_a1")
        assert "Nozzle context:" in rec.reasoning
        assert "brass" in rec.reasoning
        assert "0.4mm" in rec.reasoning
        assert "bambu_mqtt" in rec.reasoning
        assert "~120g" in rec.reasoning
        # The original verdict reasoning should still be there.
        assert "scores highest" in rec.reasoning
        # No abrasive prepend when consult_abrasive_escalation returned None.
        assert "NOZZLE ADVISORY" not in rec.reasoning

    def test_summary_with_zero_grams_omits_grams(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation",
            lambda **_: None,
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary",
            lambda _printer_id: {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "factory_default",
                "grams_through": 0,
                "trusted_for_verdicts": True,
            },
        )
        rec = recommend_material("strong", printer_id="bambu_a1")
        assert "Nozzle context:" in rec.reasoning
        assert "~0g" not in rec.reasoning

    def test_summary_bridge_exception_silent_degrade(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bridge raising must not bubble — reasoning stays unchanged."""
        def _boom(*_a: Any, **_kw: Any) -> dict[str, Any] | None:
            raise RuntimeError("simulated bridge fault")

        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation", _boom,
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary", _boom,
        )
        baseline = recommend_material("strong")
        wired = recommend_material("strong", printer_id="bambu_a1")
        assert wired.reasoning == baseline.reasoning

    def test_both_wires_compose_when_abrasive_brass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gap #8 + Gap #9 both fire when the top pick is abrasive on brass."""
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation",
            lambda **_: {
                "escalation_reason": "abrasive_brass",
                "user_warning": (
                    "PETG-CF on brass burns through ~360 g before "
                    "catastrophic tip wear."
                ),
            },
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary",
            lambda _printer_id: {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "bambu_mqtt",
                "grams_through": 120.0,
                "trusted_for_verdicts": True,
            },
        )
        # An intent that the scorer ranks any material for — both wires
        # operate independently of the top pick's identity.
        rec = recommend_material("strong", printer_id="bambu_a1")
        # Gap #8 prepend.
        assert rec.reasoning.startswith("NOZZLE ADVISORY:")
        assert "PETG-CF on brass" in rec.reasoning
        # Gap #9 append.
        assert "Nozzle context:" in rec.reasoning
        assert rec.reasoning.endswith("Settings tuned accordingly.")
        # And the original verdict reasoning sits between them.
        assert "scores highest" in rec.reasoning

    def test_low_confidence_summary_renders_hedge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_abrasive_escalation",
            lambda **_: None,
        )
        monkeypatch.setattr(
            _pro_nozzle_bridge, "consult_nozzle_summary",
            lambda _printer_id: {
                "material": "brass",
                "diameter_mm": 0.4,
                "provenance": "factory_default",
                "grams_through": 0,
                "trusted_for_verdicts": False,
            },
        )
        rec = recommend_material("strong", printer_id="bambu_a1")
        assert "low confidence" in rec.reasoning
