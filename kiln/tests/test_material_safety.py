"""Tests for kiln.material_safety: the multi-material check and the
abrasive-filament floor.

The check is Kiln's (kiln-pro, https://kiln3d.com), asked through
``kiln._pro_material_pairing_bridge``.  These pin, on an install without kiln-pro:
the served check is relayed as it is; with no answer the check is basic --
the pairs that must never share a print are still refused, word for word,
every other pair is a caution with the conservative purge, and one sentence
says why; the flush matrix reads each pair's purge from the check.
"""

from __future__ import annotations

import socket
import urllib.error
from typing import Any

import pytest

from kiln import material_safety as ms
from kiln.material_safety import (
    abrasive_nozzle_floor,
    build_bambu_flush_matrix,
    check_material_compatibility,
    is_abrasive_filament,
)

#: The pairs that must never share a print, and the refusal each gives.
_REFUSED = {
    ("PLA", "ABS"): "⛔ INCOMPATIBLE MATERIALS: PLA + ABS. Do not print — see warnings for details.",
    ("PLA", "ASA"): "⛔ INCOMPATIBLE MATERIALS: PLA + ASA. Do not print — see warnings for details.",
    ("PLA", "NYLON"): "⛔ INCOMPATIBLE MATERIALS: PLA + NYLON. Do not print — see warnings for details.",
    ("PLA", "PC"): "⛔ INCOMPATIBLE MATERIALS: PLA + PC. Do not print — see warnings for details.",
    ("ABS", "TPU"): "⛔ INCOMPATIBLE MATERIALS: ABS + TPU. Do not print — see warnings for details.",
}
_SIGNED_OUT = {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in.", "why": "signed_out"}


def _served(monkeypatch, reply: Any) -> list[dict[str, Any]]:
    import kiln.server as srv

    asks: list[dict[str, Any]] = []

    def call(tool_name, _timeout=None, _asked_by_user=None, **kwargs):
        asks.append({"tool": tool_name, "asked_by_user": _asked_by_user, **kwargs})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(srv, "_pro_api_call", call)
    return asks


#: Kiln's answer for PLA and PETG: each pair's entry by index, PETG on its own
#: not in the table, and PETG needing an enclosure.
_ENTRIES = {
    "success": True,
    "names": ["PLA", "PETG"],
    "pairs": [
        [0, 0, {"known": True, "level": "ok", "warning": None, "mitigations": [], "purge_volume_mm3": 30}],
        [0, 1, {"known": True, "level": "conditional", "warning": "w", "mitigations": ["m"], "purge_volume_mm3": 70}],
        [1, 1, {"known": False, "level": "caution", "purge_volume_mm3": 80}],
    ],
    "hardware": [{"hardened_nozzle": False, "enclosure": False}, {"hardened_nozzle": False, "enclosure": True}],
}


def _check() -> dict[str, Any]:
    """The check the composer builds from :data:`_ENTRIES`."""
    return {"pairs": [{"material_a": "PLA", "material_b": "PETG", "purge_volume_mm3": 70}]}


class TestTheServedCheck:
    def test_the_check_is_built_from_kilns_answer_for_each_pair(self, monkeypatch):
        asks = _served(monkeypatch, _ENTRIES)
        check = check_material_compatibility([" pla ", "PETG", "", None, "PETG"])  # type: ignore[list-item]
        assert asks == [{"tool": "material_mix_check", "asked_by_user": False, "materials": ["PLA", "PETG"]}]
        assert check_material_compatibility(["", None])["message"] == "No materials specified."  # type: ignore[list-item]
        assert len(asks) == 1, "a check with no materials asks nothing"
        assert check == {
            "safe": True,
            "level": "conditional",
            "pairs": [
                {"material_a": "PLA", "material_b": "PETG", "level": "conditional", "warning": "w",
                 "mitigations": ["m"], "purge_volume_mm3": 70},
                {"material_a": "PETG", "material_b": "PETG", "level": "caution",
                 "warning": "Compatibility between PETG and PETG is unknown. Verify temperature ranges are "
                            "compatible before printing.",
                 "mitigations": ["Check nozzle temp range for both PETG and PETG.",
                                 "Test with a small print before committing to a full job."],
                 "purge_volume_mm3": 80},
            ],
            "hardware_warnings": ["PETG requires an enclosure to prevent warping and fume issues."],
            "purge_matrix": [[0.0, 70.0, 70.0], [70.0, 0.0, 80.0], [70.0, 80.0, 0.0]],
            "message": "\u26a0\ufe0f  Conditional compatibility. Review mitigations before printing: w; "
                       "Compatibility between PETG and PETG is unknown. Verify temperature ranges are "
                       "compatible before printing.",
        }

    def test_an_answer_missing_a_pair_is_a_basic_check(self, monkeypatch):
        _served(monkeypatch, {**_ENTRIES, "pairs": _ENTRIES["pairs"][:1]})
        check = check_material_compatibility(["PLA", "PETG"])
        assert check["basic"] is True and check["why"] == "unanswered"


class TestABasicCheck:
    @pytest.mark.parametrize("pair,message", sorted(_REFUSED.items()))
    def test_a_pair_that_must_never_share_a_print_is_refused_offline(self, monkeypatch, pair, message):
        _served(monkeypatch, _SIGNED_OUT)
        result = check_material_compatibility(list(pair))
        assert result["safe"] is False and result["level"] == "incompatible"
        assert result["message"] == message
        assert result["pairs"][0]["level"] == "incompatible"
        assert result["pairs"][0]["warning"].startswith("INCOMPATIBLE:")
        assert result["basic"] is True

    @pytest.mark.parametrize(
        "reply,cause,fix",
        [
            (_SIGNED_OUT, "signed_out", "sign in and compose again"),
            (urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")), "offline",
             "reconnect to the internet"),
            ({"status": "error", "code": "SERVER_UNREACHABLE", "error": "timed out"}, "unanswered", "wait a minute"),
            ({"success": True, "check": {"safe": True}}, "unanswered", "wait a minute"),
        ],
    )
    def test_every_other_pair_is_a_caution_with_the_conservative_purge(self, monkeypatch, reply, cause, fix):
        _served(monkeypatch, reply)
        result = check_material_compatibility(["PLA", "PETG", "TPU"])
        assert result["safe"] is True and result["level"] == "caution" and result["basic"] is True
        assert result["why"] == cause and fix in result["caution"]
        assert "Kiln can't check how these materials pair right now because" in result["caution"]
        assert {p["level"] for p in result["pairs"]} == {"caution"}
        assert {p["purge_volume_mm3"] for p in result["pairs"]} == {800}
        assert all(p["mitigations"] == [] for p in result["pairs"])
        assert result["purge_matrix"] == [[0.0, 800.0, 800.0], [800.0, 0.0, 800.0], [800.0, 800.0, 0.0]]

    def test_a_basic_pair_says_only_what_the_nozzle_ranges_show(self, monkeypatch):
        _served(monkeypatch, _SIGNED_OUT)
        pairs = {(p["material_a"], p["material_b"]): p["warning"]
                 for p in check_material_compatibility(["TPU", "PC", "PLA GREY"])["pairs"]}
        assert pairs[("TPU", "PC")].startswith("These two can't share one nozzle temperature")
        assert pairs[("TPU", "PLA GREY")].startswith("Kiln has no nozzle temperature range for PLA GREY")

    def test_the_filled_filament_floor_still_warns(self, monkeypatch):
        _served(monkeypatch, _SIGNED_OUT)
        result = check_material_compatibility(["PLA", "PETG-CF"])
        assert result["hardware_warnings"] == [
            "PETG-CF requires a hardened/wear-resistant nozzle (not standard brass)."
        ]

    @pytest.mark.parametrize(
        "materials,warned",
        [
            (["ABS", "ASA"], ["ABS", "ASA"]),
            (["PC", "NYLON", "PA"], ["PC", "NYLON", "PA"]),
            (["ABS"], ["ABS"]),
            (["PLA", "PETG", "TPU"], []),
        ],
    )
    def test_the_enclosure_and_fume_caution_holds_offline(self, monkeypatch, materials, warned):
        """A material the catalog says needs ventilation keeps its enclosure
        line when Kiln's check does not answer, in the served check's words."""
        _served(monkeypatch, _SIGNED_OUT)
        result = check_material_compatibility(materials)
        assert result["basic"] is True
        assert result["hardware_warnings"] == [
            f"{m} requires an enclosure to prevent warping and fume issues." for m in warned
        ]

    def test_the_offline_enclosure_line_is_the_served_one(self, monkeypatch):
        served = {"success": True, "names": ["ABS"],
                  "pairs": [[0, 0, {"known": True, "level": "ok", "warning": None, "mitigations": [],
                                    "purge_volume_mm3": 40}]],
                  "hardware": [{"hardened_nozzle": False, "enclosure": True}]}
        _served(monkeypatch, served)
        online = check_material_compatibility(["ABS"])["hardware_warnings"]
        _served(monkeypatch, _SIGNED_OUT)
        assert check_material_compatibility(["ABS"])["hardware_warnings"] == online

    @pytest.mark.parametrize("materials", [["ABS"], ["PLA", "PLA", "PLA"]])
    def test_one_material_says_nothing_about_pairs(self, monkeypatch, materials):
        """With one material loaded there is no pair to refuse or purge
        between, so the line saying why the check is missing speaks of the
        material's hardware."""
        _served(monkeypatch, _SIGNED_OUT)
        result = check_material_compatibility(materials)
        assert result["basic"] is True and result["why"] == "signed_out"
        assert "Kiln can't check what hardware this material needs right now because" in result["caution"]
        assert "pair" not in result["caution"]
        assert ("conservative purge between its colours" in result["caution"]) is (len(materials) > 1)
        assert result["caution"].endswith("Sign in and compose again.")


class TestTheFlushMatrix:
    def test_each_pair_purges_what_the_check_says(self):
        assert build_bambu_flush_matrix(["PLA", "PETG"], n_slots=2, check=_check()) == "0 70 70 0"

    def test_an_empty_slot_or_an_unanswered_pair_gets_the_conservative_purge(self):
        assert build_bambu_flush_matrix(["PLA", "PETG", "", "TPU"], n_slots=4, check=_check()).split() == [
            "0", "70", "800", "800", "70", "0", "800", "800", "800", "800", "0", "800", "800", "800", "800", "0",
        ]

    def test_without_an_answer_every_pair_gets_the_conservative_purge(self, monkeypatch):
        _served(monkeypatch, _SIGNED_OUT)
        assert build_bambu_flush_matrix(["PLA", "PETG", "PLA"], n_slots=3) == "0 800 800 800 0 800 800 800 0"


def test_the_pair_and_process_tables_are_not_in_this_package():
    for name in ("MATERIAL_PROCESS", "_COMPAT", "_lookup_compat", "get_process_settings"):
        assert not hasattr(ms, name), name
    assert set(ms._REFUSED_PAIRS) == {frozenset(pair) for pair in _REFUSED}


# ---------------------------------------------------------------------------
# is_abrasive_filament / abrasive_nozzle_floor
# ---------------------------------------------------------------------------


class TestAbrasiveFilament:
    """Filled-filament detection and the free nozzle-wear floor.

    Coverage: the name rule's hits for filled grades, colourway names that
    must not trip it, and the floor's honesty about what it cannot establish.
    """

    @pytest.mark.parametrize(
        "material",
        [
            "PLA-CF",
            "PETG-CF",
            "PA-CF",
            "PAHT-CF",
            "PPS-CF",
            "PLA-GF",
            "ASA-GF",
            "PA6-CF15",     # grade number rides along
            "pla-cf",       # case-insensitive
            "PLA Wood",
            "Copperfill",
            "woodfill",
            "Metal Filled PLA",
            "PLA Carbon Fiber",
            "Glass Fiber Nylon",
            "glass-fibre PA",
        ],
    )
    def test_filled_filament_is_abrasive(self, material):
        assert is_abrasive_filament(material) is True

    @pytest.mark.parametrize(
        "material",
        [
            "PLA", "PETG", "ABS", "ASA", "TPU", "TPE", "PVA", "HIPS",
            "PA", "PC", "PLA+", "PLA-HF", "NYLON",
            "Polycarbonate",   # contains "carbon" — must not read as filled
            "Copper PLA",      # a colourway, not a filler
            "Bronze PETG",
            "Sea Glass PLA",
            "Redwood PLA",
            "", "   ",
        ],
    )
    def test_unfilled_filament_is_not_abrasive(self, material):
        assert is_abrasive_filament(material) is False

    def test_floor_names_the_material_and_the_fix(self):
        note = abrasive_nozzle_floor("PETG-CF")
        assert "PETG-CF" in note
        assert "hardened steel" in note.lower()

    def test_floor_does_not_claim_to_know_the_nozzle(self):
        # Public Kiln holds no nozzle state.  Claiming otherwise would be
        # the false-safe direction: the reader would trust a reading that
        # was never taken.
        note = abrasive_nozzle_floor("PLA-CF").lower()
        assert "cannot see which nozzle" in note
        assert "caution and not a measurement" in note

    def test_floor_is_silent_for_unfilled_filament(self):
        assert abrasive_nozzle_floor("PLA") == ""
        assert abrasive_nozzle_floor("") == ""
