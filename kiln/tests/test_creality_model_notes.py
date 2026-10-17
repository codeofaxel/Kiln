"""The Creality adapter's per-model local-access notes."""

from __future__ import annotations

import pytest

from kiln.printers.creality import _model_local_access_notes


def _notes(model: str) -> str:
    return " ".join(_model_local_access_notes(model))


@pytest.mark.parametrize("model", ["k2", "k2_pro", "k2_plus", "K2 Plus"])
def test_k2_family_fluidd_port_is_official_not_community(model):
    notes = _notes(model)
    assert "4408" in notes
    assert "Official Creality Wiki" in notes
    assert "community" not in notes.lower()


def test_creality_hi_stays_community_confirmed():
    assert "community" in _notes("creality_hi").lower()


@pytest.mark.parametrize("model", ["k1c", "k1_max"])
def test_2025_k1c_and_k1_max_cannot_be_rooted(model):
    notes = _notes(model)
    assert "2025" in notes
    assert "root" in notes.lower()


def test_plain_k1_carries_no_2025_warning():
    assert "2025" not in _notes("k1")


def test_sparkx_i7_says_root_permission_must_be_on():
    notes = _notes("sparkx_i7")
    assert "root permission" in notes.lower()
    assert "1.1.2.4" in notes
