"""The colour-difference module against the formula's published test data.

``SHARMA_PAIRS`` is the supplementary test set of Sharma, Wu and Dalal (2005),
read from the authors' page
(https://hajim.rochester.edu/ece/sites/gsharma/ciede2000/dataNprograms/ciede2000testdata.txt,
2026-10-01): two CIELAB colours and the expected CIEDE2000 difference, to four
decimals.  Every pair is checked in both orders, because the formula's hue
handling is where implementations go wrong and order is where it shows.
"""

from __future__ import annotations

import math

import pytest

from kiln import ams_routing
from kiln.colour_distance import (
    LOOK_BANDS,
    LOOK_NEAR_MAX,
    LOOK_VISIBLE_MAX,
    delta_e_76,
    delta_e_2000,
    hex_to_lab,
    look_band,
    look_words,
    normalize_hex,
)

SHARMA_PAIRS: tuple[tuple[tuple[float, float, float], tuple[float, float, float], float], ...] = (
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
)


@pytest.mark.parametrize(("lab1", "lab2", "expected"), SHARMA_PAIRS)
def test_ciede2000_matches_the_published_pairs(lab1, lab2, expected):
    assert delta_e_2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)
    assert delta_e_2000(lab2, lab1) == pytest.approx(expected, abs=1e-4)


def test_the_published_set_is_whole():
    assert len(SHARMA_PAIRS) == 34


def test_identical_colours_are_zero_apart():
    lab = hex_to_lab("C12E1F")
    assert delta_e_2000(lab, lab) == 0.0
    assert delta_e_76(lab, lab) == 0.0


def test_ciede2000_is_kinder_than_cie76_on_a_saturated_pair():
    # A saturated green against a nearby green: CIE76 reads the gap several
    # times larger than people see it, which is why the look bands are not
    # written on the CIE76 scale.
    a, b = hex_to_lab("00AE42"), hex_to_lab("00A553")
    assert delta_e_2000(a, b) < delta_e_76(a, b) / 2


@pytest.mark.parametrize("code", ["00AE42", "FF6A13", "C12E1F", "000000", "FFFFFF", "7F7E83"])
def test_the_ams_planner_converts_through_the_same_function(code):
    # One conversion for both questions: what the planner measures and what
    # the look bands grade must be the same Lab triple for the same code.
    assert ams_routing._lab(code) == hex_to_lab(code)


def test_white_is_lightness_100_and_black_is_0():
    assert hex_to_lab("FFFFFF") == pytest.approx((100.0, 0.0, 0.0), abs=0.01)
    assert hex_to_lab("000000") == pytest.approx((0.0, 0.0, 0.0), abs=0.01)


@pytest.mark.parametrize(
    ("delta", "band"),
    [
        (0.0, "near"),
        (LOOK_NEAR_MAX, "near"),
        (math.nextafter(LOOK_NEAR_MAX, math.inf), "visible"),
        (LOOK_VISIBLE_MAX, "visible"),
        (math.nextafter(LOOK_VISIBLE_MAX, math.inf), "different"),
        (40.0, "different"),
    ],
)
def test_look_band_edges(delta, band):
    assert look_band(delta) == band


def test_every_band_has_words_and_the_last_is_open():
    assert [b for b, _u, _w in LOOK_BANDS] == ["near", "visible", "different"]
    assert LOOK_BANDS[-1][1] == math.inf
    for band, _upper, words in LOOK_BANDS:
        assert look_words(band) == words
        assert words
    with pytest.raises(ValueError):
        look_words("exact")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("#c12e1f", "C12E1F"), ("C12E1FFF", "C12E1F"), (" 00ae42 ", "00AE42"), ("0x1234", None), ("red", None), (None, None)],
)
def test_normalize_hex(raw, expected):
    assert normalize_hex(raw) == expected
