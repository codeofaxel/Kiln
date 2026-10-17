"""Colour difference: how far apart two colours are, and how that looks.

Kiln asks two different questions about a pair of colours, and both are
answered from this module so a colour means the same thing to each:

* **Is this loaded spool the one for this colour?**  The AMS planner
  (:mod:`kiln.ams_routing`) maps a print's colours onto the spools a printer
  reports.  It measures with :func:`delta_e_76` and uses a spool within
  ``kiln.ams_routing.MATCH_DELTA_E``.  That edge is deliberately forgiving:
  a "red" spool is the one meant for a red part even when the two codes
  differ by a shade.
* **How close will it look?**  Graded with :func:`delta_e_2000` into three
  bands (:func:`look_band`).  CIE76 is plain distance in CIELAB, which
  overstates differences between saturated colours several times over;
  CIEDE2000 corrects for that, so the bands are written on its scale.

The questions differ, so their answers can differ for one pair: a spool the
planner uses can still be graded "visibly different".  Both are true.  What
must never differ is the conversion underneath, so both go through
:func:`hex_to_lab`.

The bands are reasoned defaults, not a standard.  Measured visibility
thresholds for CIEDE2000 sit around 1 to 2.5 for side-by-side viewing in a
booth; a print on a desk under room light is a looser condition.  Below
:data:`LOOK_NEAR_MAX` two colours are hard to tell apart, up to
:data:`LOOK_VISIBLE_MAX` they are close but a side-by-side look shows the
difference, and beyond that they read as different colours.

All conversions assume sRGB with the D65 white point, the space hex colour
codes are written in.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = [
    "LOOK_BANDS",
    "LOOK_NEAR_MAX",
    "LOOK_VISIBLE_MAX",
    "delta_e_76",
    "delta_e_2000",
    "hex_to_lab",
    "look_band",
    "look_words",
    "normalize_hex",
]

#: Upper edge (CIEDE2000) of "hard to tell apart".
LOOK_NEAR_MAX = 2.0
#: Upper edge (CIEDE2000) of "close, but visible side by side".
LOOK_VISIBLE_MAX = 5.0

#: ``(band, upper edge, words)`` in ascending order.  The last band is open.
LOOK_BANDS: tuple[tuple[str, float, str], ...] = (
    ("near", LOOK_NEAR_MAX, "hard to tell apart"),
    ("visible", LOOK_VISIBLE_MAX, "close, but you'd see the difference side by side"),
    ("different", math.inf, "a visibly different colour"),
)

_HEX6 = re.compile(r"^[0-9A-F]{6}$")

# D65 reference white, normalised so Y = 1.
_WHITE_X = 0.95047
_WHITE_Z = 1.08883


def normalize_hex(value: Any) -> str | None:
    """``RRGGBB`` upper-hex, alpha dropped; ``None`` when it is not a colour.

    Accepts ``#RRGGBB``, ``RRGGBB`` and the ``RRGGBBAA`` a Bambu tray reports.
    """
    if not isinstance(value, str):
        return None
    h = value.strip().lstrip("#").upper()
    if len(h) >= 6:
        h = h[:6]
    if not _HEX6.fullmatch(h):
        return None
    return h


def _srgb_to_linear(channel: int) -> float:
    v = channel / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def hex_to_lab(hex6: str) -> tuple[float, float, float]:
    """CIELAB (D65, 2 degree observer) of a normalised ``RRGGBB`` code."""
    r = _srgb_to_linear(int(hex6[0:2], 16))
    g = _srgb_to_linear(int(hex6[2:4], 16))
    b = _srgb_to_linear(int(hex6[4:6], 16))
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / _WHITE_X
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / _WHITE_Z

    def pivot(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e_76(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """CIE76: straight-line distance in CIELAB.

    Spelled out rather than ``math.dist``, which rounds differently in the
    last bit: the AMS planner's edge is judged on this number, and moving
    its maths here must not move a single decision.
    """
    return (
        (lab1[0] - lab2[0]) ** 2 + (lab1[1] - lab2[1]) ** 2 + (lab1[2] - lab2[2]) ** 2
    ) ** 0.5


def _hue_degrees(b: float, a_prime: float) -> float:
    if a_prime == 0 and b == 0:
        return 0.0
    return math.degrees(math.atan2(b, a_prime)) % 360.0


def delta_e_2000(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float]
) -> float:
    """CIEDE2000 colour difference, weights k_L = k_C = k_H = 1.

    Written to Sharma, Wu and Dalal's 2005 notes on the formula; the 34
    test pairs they published pin it in the tests.
    """
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2.0
    c_bar7 = c_bar**7
    g = 0.5 * (1.0 - math.sqrt(c_bar7 / (c_bar7 + 25.0**7)))
    a1p, a2p = (1.0 + g) * a1, (1.0 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p, h2p = _hue_degrees(b1, a1p), _hue_degrees(b2, a2p)

    dl = l2 - l1
    dc = c2p - c1p
    if c1p * c2p == 0:
        dh_angle = 0.0
    else:
        dh_angle = h2p - h1p
        if dh_angle > 180.0:
            dh_angle -= 360.0
        elif dh_angle < -180.0:
            dh_angle += 360.0
    dh = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh_angle) / 2.0)

    l_bar = (l1 + l2) / 2.0
    c_bar_p = (c1p + c2p) / 2.0
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        h_bar = (h1p + h2p) / 2.0
    elif h1p + h2p < 360.0:
        h_bar = (h1p + h2p + 360.0) / 2.0
    else:
        h_bar = (h1p + h2p - 360.0) / 2.0

    t = (
        1.0
        - 0.17 * math.cos(math.radians(h_bar - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * h_bar))
        + 0.32 * math.cos(math.radians(3.0 * h_bar + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * h_bar - 63.0))
    )
    d_theta = 30.0 * math.exp(-(((h_bar - 275.0) / 25.0) ** 2))
    c_bar_p7 = c_bar_p**7
    r_c = 2.0 * math.sqrt(c_bar_p7 / (c_bar_p7 + 25.0**7))
    l_offset = (l_bar - 50.0) ** 2
    s_l = 1.0 + 0.015 * l_offset / math.sqrt(20.0 + l_offset)
    s_c = 1.0 + 0.045 * c_bar_p
    s_h = 1.0 + 0.015 * c_bar_p * t
    r_t = -math.sin(math.radians(2.0 * d_theta)) * r_c

    term_l = dl / s_l
    term_c = dc / s_c
    term_h = dh / s_h
    return math.sqrt(term_l**2 + term_c**2 + term_h**2 + r_t * term_c * term_h)


def look_band(delta_e: float) -> str:
    """``"near"``, ``"visible"`` or ``"different"`` for a CIEDE2000 value."""
    for band, upper, _words in LOOK_BANDS:
        if delta_e <= upper:
            return band
    return LOOK_BANDS[-1][0]


def look_words(band: str) -> str:
    """The plain-English words for *band*."""
    for name, _upper, words in LOOK_BANDS:
        if name == band:
            return words
    raise ValueError(f"unknown look band {band!r}")
