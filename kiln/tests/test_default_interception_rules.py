"""Tests for the default bed-safety interception rule set.

These rules are the last-line-of-defense against off-bed G-code moves.
The tests verify:
  1. The rule set for a known printer (bambu_a1, 256x256x256 bed)
     contains the six expected reject rules.
  2. An unknown printer returns an empty list (we don't fabricate a
     guessed bed size).
  3. A tiny inline regex matcher, fed realistic G-code lines, accepts
     safe moves and rejects the dangerous ones — i.e. the patterns
     actually DO what their ``reason`` text says they do.

If any of these fail, the adapter-boundary backstop is broken and
Incident #0 (2026-04-15 Bambu A1 purge-tool crash) could recur.
"""
from __future__ import annotations

import re

import pytest

from kiln.safety.default_interception_rules import get_default_bed_safety_rules


# ---------------------------------------------------------------------------
# Tiny inline matcher used by the behavioural tests below.
# ---------------------------------------------------------------------------


def _evaluate(rules: list[dict], line: str) -> tuple[str, str | None]:
    """Run a G-code line through a list of rule dicts.

    Returns ``(action, reason)`` where action is the first matching
    rule's action, or ``"allow"`` if no rule matches.  Deliberately
    simple — mirrors what the eventual adapter layer will do and keeps
    the test self-contained (no dependency on the production
    interceptor).
    """
    for rule in rules:
        if re.search(rule["pattern"], line):
            return rule["action"], rule["reason"]
    return "allow", None


def _rule_by_reason_contains(rules: list[dict], substr: str) -> dict:
    """Pluck the first rule whose reason contains ``substr``."""
    for r in rules:
        if substr in r["reason"]:
            return r
    raise AssertionError(
        f"No rule reason contained {substr!r}.  Reasons were: "
        + "; ".join(r["reason"][:60] for r in rules)
    )


# ---------------------------------------------------------------------------
# Structural tests — known printer
# ---------------------------------------------------------------------------


class TestBambuA1RuleSet:
    """The Bambu A1 has a 256x256x256 build volume.  The rule set
    must contain six reject rules — one per bound."""

    @pytest.fixture
    def rules(self) -> list[dict]:
        return get_default_bed_safety_rules("bambu_a1")

    def test_six_rules_emitted(self, rules: list[dict]) -> None:
        assert len(rules) == 6

    def test_all_rules_are_reject(self, rules: list[dict]) -> None:
        assert all(r["action"] == "reject" for r in rules)

    def test_all_rules_are_bed_fit_category(self, rules: list[dict]) -> None:
        assert all(r["category"] == "bed_fit" for r in rules)

    def test_every_rule_has_all_four_keys(self, rules: list[dict]) -> None:
        for r in rules:
            assert set(r.keys()) == {"pattern", "action", "reason", "category"}
            assert isinstance(r["pattern"], str) and r["pattern"]
            assert isinstance(r["reason"], str) and r["reason"]

    def test_every_pattern_compiles(self, rules: list[dict]) -> None:
        for r in rules:
            re.compile(r["pattern"])  # raises on invalid regex

    def test_reason_mentions_negative_x(self, rules: list[dict]) -> None:
        _rule_by_reason_contains(rules, "negative X")

    def test_reason_mentions_negative_y(self, rules: list[dict]) -> None:
        _rule_by_reason_contains(rules, "negative Y")

    def test_reason_mentions_z_floor(self, rules: list[dict]) -> None:
        _rule_by_reason_contains(rules, "Z <")

    def test_reason_mentions_256_for_x_upper(self, rules: list[dict]) -> None:
        # The X-above-bed reason should cite the 256mm bed width.
        for r in rules:
            if "X > 256" in r["reason"]:
                return
        raise AssertionError("No rule cited 'X > 256'")

    def test_reason_mentions_256_for_y_upper(self, rules: list[dict]) -> None:
        for r in rules:
            if "Y > 256" in r["reason"]:
                return
        raise AssertionError("No rule cited 'Y > 256'")

    def test_reason_mentions_256_for_z_upper(self, rules: list[dict]) -> None:
        for r in rules:
            if "Z > 256" in r["reason"]:
                return
        raise AssertionError("No rule cited 'Z > 256'")


# ---------------------------------------------------------------------------
# Unknown printer
# ---------------------------------------------------------------------------


class TestUnknownPrinter:
    def test_unknown_printer_returns_empty_list(self) -> None:
        assert get_default_bed_safety_rules("no_such_printer_42") == []

    def test_empty_string_returns_empty_list(self) -> None:
        assert get_default_bed_safety_rules("") == []


# ---------------------------------------------------------------------------
# Behavioural tests — rules actually catch / don't catch the right lines
# ---------------------------------------------------------------------------


class TestBambuA1Behaviour:
    """Feed realistic G-code lines through the rule set and confirm
    the actions match our expectations."""

    @pytest.fixture
    def rules(self) -> list[dict]:
        return get_default_bed_safety_rules("bambu_a1")

    # -------- Dangerous lines that MUST be rejected ------------------------

    @pytest.mark.parametrize(
        "line",
        [
            # Incident #0: a Ø25mm disc centered on origin.
            "G1 X-12.5 Y-12.5 Z0.2 E0.5 F1500",
            "G1 X-5 Y10 E0.3",
            "G0 X-1 Y10",
            "G1 X10 Y-5",
            "G0 Y-1.5",
            "G1 X10 Y-0.7 E0.2",
            # Exceeding bed width.
            "G1 X257 Y10 E0.5",
            "G1 X300 Y128",
            "G1 X256.6 Y10 E0.5",
            "G1 X128 Y257 E0.5",
            "G0 X999 Y999",
            # Nozzle diving into bed.
            "G1 Z-1.0 F300",
            "G0 Z-12",
            "G1 X10 Y10 Z-0.6 E0.1",
            # Gantry above build height.
            "G0 Z257",
            "G1 Z500",
        ],
    )
    def test_dangerous_line_rejected(
        self, rules: list[dict], line: str,
    ) -> None:
        action, reason = _evaluate(rules, line)
        assert action == "reject", (
            f"Expected reject for {line!r}, got {action}. "
            f"Reason: {reason}"
        )

    # -------- Safe lines that MUST NOT be flagged --------------------------

    @pytest.mark.parametrize(
        "line",
        [
            # Dead-center move — the canonical "obviously safe" case.
            "G1 X128 Y128 E0.5",
            "G1 X128 Y128 Z0.2 E0.5 F3000",
            # Corner moves that are still on-bed.
            "G1 X0 Y0 Z0.2",
            "G1 X256 Y256 Z0.2",
            # Floating-point noise near the edge — absorbed by epsilon.
            "G1 X256.0000001 Y128",
            "G1 X256.4 Y10",
            "G1 X128 Y256.5 E0.3",
            # Small negative Z inside epsilon (probe baby-step range).
            "G1 Z-0.4 F300",
            "G1 Z-0.49",
            # Retraction / extruder-only moves.
            "G1 E-0.8 F1800",
            "G1 E5.2 F1200",
            # Non-motion commands — our rules only watch G0/G1.
            "M104 S210",
            "M140 S60",
            "G28",
            "G92 E0",
            # Comment-only lines.
            "; LAYER_CHANGE",
            "",
            # Similar-looking but non-G01 commands must not match.
            # (G10/G11 are retract/unretract on some firmwares.)
            "G10",
            "G11",
            # M-codes with negative params must not match our G0/G1 rules.
            "M201 X-1000 Y-1000",
        ],
    )
    def test_safe_line_allowed(
        self, rules: list[dict], line: str,
    ) -> None:
        action, reason = _evaluate(rules, line)
        assert action == "allow", (
            f"Expected allow for {line!r}, got {action}. "
            f"Reason: {reason}"
        )

    # -------- Unknown printer — nothing gets blocked -----------------------

    def test_unknown_printer_does_not_block_anything(self) -> None:
        rules = get_default_bed_safety_rules("obscure_printer_xyz")
        # All dangerous lines go straight through.
        for line in ["G1 X-12.5 Y10", "G1 Z-5", "G1 X9999 Y9999"]:
            action, _ = _evaluate(rules, line)
            assert action == "allow"
