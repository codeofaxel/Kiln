"""Material substitution API for finding alternative FDM filaments.

Helps agents and users find compatible substitute filaments when their
preferred material is unavailable, too expensive, or otherwise unsuitable.

Covers FDM filament types (PLA, PETG, ABS, ASA, TPU, Nylon, composites)
with built-in substitution rules, compatibility scoring, and trade-off
descriptions.

Usage::

    from kiln.material_substitution import (
        find_substitutes,
        get_best_substitute,
        get_substitution_matrix,
    )

    subs = find_substitutes("pla", "fdm")
    best = get_best_substitute("abs", "fdm")
    matrix = get_substitution_matrix()
    ok = matrix.is_compatible("pla", "petg", "fdm")
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SubstitutionReason(enum.Enum):
    """Reason for seeking a material substitution."""

    UNAVAILABLE = "unavailable"
    COST = "cost"
    LEAD_TIME = "lead_time"
    STRENGTH = "strength"
    FINISH_QUALITY = "finish_quality"
    HEAT_RESISTANCE = "heat_resistance"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MaterialSubstitution:
    """A single material substitution recommendation.

    :param original_material: The filament being replaced.
    :param substitute_material: The recommended alternative.
    :param device_type: Device class (``"fdm"``).
    :param compatibility_score: How well the substitute matches (0.0--1.0).
    :param reasons: Scenarios where this substitution is appropriate.
    :param trade_offs: Human-readable description of what changes.
    :param cost_delta_pct: Cost difference as a percentage (positive = more
        expensive).
    """

    original_material: str
    substitute_material: str
    device_type: str
    compatibility_score: float
    reasons: List[SubstitutionReason] = field(default_factory=list)
    trade_offs: str = ""
    cost_delta_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "original_material": self.original_material,
            "substitute_material": self.substitute_material,
            "device_type": self.device_type,
            "compatibility_score": self.compatibility_score,
            "reasons": [r.value for r in self.reasons],
            "trade_offs": self.trade_offs,
            "cost_delta_pct": self.cost_delta_pct,
        }


# ---------------------------------------------------------------------------
# Internal substitution rule type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SubRule:
    """Internal representation of a single directional substitution rule."""

    substitute: str
    score: float
    reasons: List[SubstitutionReason]
    trade_offs: str
    cost_delta_pct: float


# ---------------------------------------------------------------------------
# Substitution matrix
# ---------------------------------------------------------------------------

class SubstitutionMatrix:
    """Knowledge base of FDM filament substitution rules.

    Maintains a dict-of-dicts structure keyed by ``(device_type, original)``
    mapping to a list of :class:`_SubRule` entries.  Provides lookup,
    filtering, and compatibility queries.
    """

    def __init__(self) -> None:
        # _rules[device_type][original_material] -> list[_SubRule]
        self._rules: Dict[str, Dict[str, List[_SubRule]]] = {}
        self._build_builtin_rules()

    # -------------------------------------------------------------------
    # Built-in rules
    # -------------------------------------------------------------------

    def _add_rule(
        self,
        device_type: str,
        original: str,
        substitute: str,
        score: float,
        reasons: List[SubstitutionReason],
        trade_offs: str,
        cost_delta_pct: float,
    ) -> None:
        """Register a single directional substitution rule."""
        dt = device_type.lower()
        if dt not in self._rules:
            self._rules[dt] = {}
        if original not in self._rules[dt]:
            self._rules[dt][original] = []
        self._rules[dt][original].append(_SubRule(
            substitute=substitute,
            score=score,
            reasons=reasons,
            trade_offs=trade_offs,
            cost_delta_pct=cost_delta_pct,
        ))

    def _add_bidirectional(
        self,
        device_type: str,
        mat_a: str,
        mat_b: str,
        score: float,
        reasons: List[SubstitutionReason],
        trade_offs_a_to_b: str,
        trade_offs_b_to_a: str,
        cost_delta_pct_a_to_b: float,
    ) -> None:
        """Register a bidirectional substitution (A <-> B)."""
        self._add_rule(
            device_type, mat_a, mat_b, score, reasons,
            trade_offs_a_to_b, cost_delta_pct_a_to_b,
        )
        self._add_rule(
            device_type, mat_b, mat_a, score, reasons,
            trade_offs_b_to_a, -cost_delta_pct_a_to_b,
        )

    def _build_builtin_rules(self) -> None:
        """Populate the matrix with built-in FDM filament substitution knowledge."""

        # ---------------------------------------------------------------
        # PLA family
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "pla", "pla_plus",
            score=0.95,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
            ],
            trade_offs_a_to_b=(
                "PLA+ is tougher with better impact resistance and "
                "slightly higher layer adhesion, at a similar price point"
            ),
            trade_offs_b_to_a=(
                "Standard PLA is cheaper, more widely available, and "
                "easier to find in specialty colors, but more brittle"
            ),
            cost_delta_pct_a_to_b=10.0,
        )

        self._add_bidirectional(
            "fdm", "pla", "silk_pla",
            score=0.90,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.FINISH_QUALITY,
            ],
            trade_offs_a_to_b=(
                "Silk PLA produces a glossy, metallic surface finish "
                "but is slightly weaker and more brittle than standard PLA"
            ),
            trade_offs_b_to_a=(
                "Standard PLA is stronger and more predictable to print, "
                "but lacks the decorative sheen of Silk PLA"
            ),
            cost_delta_pct_a_to_b=15.0,
        )

        self._add_bidirectional(
            "fdm", "wood_pla", "pla",
            score=0.85,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.FINISH_QUALITY,
            ],
            trade_offs_a_to_b=(
                "Standard PLA is stronger and easier to print, but "
                "lacks the wood-grain texture and matte aesthetic"
            ),
            trade_offs_b_to_a=(
                "Wood PLA adds a natural wood-like texture and appearance, "
                "but is slightly weaker and can clog small nozzles"
            ),
            cost_delta_pct_a_to_b=-10.0,
        )

        # ---------------------------------------------------------------
        # PLA <-> PETG (cross-family, moderate compatibility)
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "pla", "petg",
            score=0.75,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
                SubstitutionReason.HEAT_RESISTANCE,
            ],
            trade_offs_a_to_b=(
                "PETG is stronger, more heat-resistant, and less brittle, "
                "but is slightly harder to print (stringing), requires "
                "higher temps, and has a glossier finish"
            ),
            trade_offs_b_to_a=(
                "PLA is easier to print with sharper detail and more "
                "color options, but is brittle and softens at ~60C"
            ),
            cost_delta_pct_a_to_b=15.0,
        )

        # ---------------------------------------------------------------
        # PLA <-> ABS (cross-family, low compatibility)
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "pla", "abs",
            score=0.55,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
                SubstitutionReason.HEAT_RESISTANCE,
            ],
            trade_offs_a_to_b=(
                "ABS is much more heat-resistant and impact-tough, but "
                "requires an enclosure, higher nozzle/bed temps, and "
                "produces fumes; prone to warping without proper setup"
            ),
            trade_offs_b_to_a=(
                "PLA prints easily without an enclosure and with minimal "
                "warping, but is brittle and has poor heat resistance"
            ),
            cost_delta_pct_a_to_b=5.0,
        )

        # ---------------------------------------------------------------
        # ABS <-> ASA
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "abs", "asa",
            score=0.90,
            reasons=[
                SubstitutionReason.UNAVAILABLE,
                SubstitutionReason.HEAT_RESISTANCE,
                SubstitutionReason.FINISH_QUALITY,
            ],
            trade_offs_a_to_b=(
                "ASA has better UV resistance and weather durability, "
                "making it ideal for outdoor parts; similar print "
                "requirements to ABS"
            ),
            trade_offs_b_to_a=(
                "ABS is cheaper and more widely available with a larger "
                "color selection, but yellows and degrades in sunlight"
            ),
            cost_delta_pct_a_to_b=20.0,
        )

        # ---------------------------------------------------------------
        # PETG <-> ABS
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "petg", "abs",
            score=0.70,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
                SubstitutionReason.HEAT_RESISTANCE,
            ],
            trade_offs_a_to_b=(
                "ABS has higher heat resistance and can be vapor-smoothed "
                "with acetone, but requires an enclosure and produces "
                "fumes; more prone to warping"
            ),
            trade_offs_b_to_a=(
                "PETG is easier to print without an enclosure, has good "
                "chemical resistance and flexibility, but has lower heat "
                "resistance and is prone to stringing"
            ),
            cost_delta_pct_a_to_b=-5.0,
        )

        # ---------------------------------------------------------------
        # PETG <-> Nylon
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "petg", "nylon",
            score=0.60,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
            ],
            trade_offs_a_to_b=(
                "Nylon is significantly stronger with excellent wear "
                "resistance and flexibility, but is hygroscopic (absorbs "
                "moisture), harder to print, and requires a dry box"
            ),
            trade_offs_b_to_a=(
                "PETG is much easier to print and store, with good "
                "all-round properties, but has lower tensile strength "
                "and wear resistance than Nylon"
            ),
            cost_delta_pct_a_to_b=40.0,
        )

        # ---------------------------------------------------------------
        # Flex materials: TPU <-> TPE
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "tpu", "tpe",
            score=0.85,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
                SubstitutionReason.FINISH_QUALITY,
            ],
            trade_offs_a_to_b=(
                "TPE is softer and more elastic but harder to print "
                "reliably; better for very flexible parts like phone "
                "cases or gaskets"
            ),
            trade_offs_b_to_a=(
                "TPU is stiffer (higher shore hardness), easier to "
                "print on most printers, and more abrasion-resistant; "
                "better for functional flexible parts"
            ),
            cost_delta_pct_a_to_b=-5.0,
        )

        # ---------------------------------------------------------------
        # Carbon fiber composites
        # ---------------------------------------------------------------
        self._add_bidirectional(
            "fdm", "cf_pla", "cf_petg",
            score=0.80,
            reasons=[
                SubstitutionReason.UNAVAILABLE, SubstitutionReason.STRENGTH,
                SubstitutionReason.HEAT_RESISTANCE,
            ],
            trade_offs_a_to_b=(
                "CF-PETG has better heat resistance and toughness than "
                "CF-PLA, but is harder to print and slightly more "
                "expensive; both require a hardened nozzle"
            ),
            trade_offs_b_to_a=(
                "CF-PLA is easier to print with better dimensional "
                "accuracy, but is more brittle and has lower heat "
                "resistance; both require a hardened nozzle"
            ),
            cost_delta_pct_a_to_b=15.0,
        )

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def find_substitutes(
        self,
        material: str,
        device_type: str,
        *,
        reason: Optional[SubstitutionReason] = None,
        min_score: float = 0.5,
    ) -> List[MaterialSubstitution]:
        """Find substitute filaments for a given original.

        :param material: Original material identifier.
        :param device_type: Device class (``"fdm"``).
        :param reason: If provided, only return substitutions that address
            this specific reason.
        :param min_score: Minimum compatibility score threshold (0.0--1.0).
        :returns: List of :class:`MaterialSubstitution` sorted by
            descending compatibility score.
        """
        dt = device_type.lower().strip()
        mat = material.lower().strip()

        dt_rules = self._rules.get(dt, {})
        rules = dt_rules.get(mat, [])

        results: List[MaterialSubstitution] = []
        for rule in rules:
            if rule.score < min_score:
                continue
            if reason is not None and reason not in rule.reasons:
                continue
            results.append(MaterialSubstitution(
                original_material=mat,
                substitute_material=rule.substitute,
                device_type=dt,
                compatibility_score=rule.score,
                reasons=list(rule.reasons),
                trade_offs=rule.trade_offs,
                cost_delta_pct=rule.cost_delta_pct,
            ))

        results.sort(key=lambda s: s.compatibility_score, reverse=True)
        return results

    def is_compatible(
        self,
        original: str,
        substitute: str,
        device_type: str,
    ) -> bool:
        """Check whether two filaments are registered as substitutes.

        :param original: Original material identifier.
        :param substitute: Proposed substitute identifier.
        :param device_type: Device class.
        :returns: ``True`` if a substitution rule exists.
        """
        dt = device_type.lower().strip()
        orig = original.lower().strip()
        sub = substitute.lower().strip()

        dt_rules = self._rules.get(dt, {})
        rules = dt_rules.get(orig, [])
        return any(r.substitute == sub for r in rules)

    def get_best_substitute(
        self,
        material: str,
        device_type: str,
    ) -> Optional[MaterialSubstitution]:
        """Return the highest-scored substitute for a filament.

        :param material: Original material identifier.
        :param device_type: Device class.
        :returns: The best :class:`MaterialSubstitution`, or ``None`` if
            no substitutions are available.
        """
        subs = self.find_substitutes(material, device_type, min_score=0.0)
        return subs[0] if subs else None


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_matrix_instance: Optional[SubstitutionMatrix] = None


def get_substitution_matrix() -> SubstitutionMatrix:
    """Return the shared :class:`SubstitutionMatrix` singleton.

    :returns: The lazily-initialised matrix instance.
    """
    global _matrix_instance
    if _matrix_instance is None:
        _matrix_instance = SubstitutionMatrix()
    return _matrix_instance


def _reset_singleton() -> None:
    """Reset the singleton instance (for testing)."""
    global _matrix_instance
    _matrix_instance = None


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def find_substitutes(
    material: str,
    device_type: str,
    *,
    reason: Optional[SubstitutionReason] = None,
    min_score: float = 0.5,
) -> List[MaterialSubstitution]:
    """Find substitute filaments (delegates to the singleton matrix).

    :param material: Original material identifier.
    :param device_type: Device class (``"fdm"``).
    :param reason: If provided, only return substitutions matching this reason.
    :param min_score: Minimum compatibility score threshold.
    :returns: List of :class:`MaterialSubstitution` sorted by score.
    """
    return get_substitution_matrix().find_substitutes(
        material, device_type, reason=reason, min_score=min_score,
    )


def get_best_substitute(
    material: str,
    device_type: str,
) -> Optional[MaterialSubstitution]:
    """Return the best substitute for a filament (delegates to singleton).

    :param material: Original material identifier.
    :param device_type: Device class.
    :returns: Best :class:`MaterialSubstitution`, or ``None``.
    """
    return get_substitution_matrix().get_best_substitute(material, device_type)
