"""Free-tier load-bearing detector — surfaces the engineering-tier upgrade nudge.

When a user asks Kiln to do something that LOOKS like a load-bearing
print (wall mount, shelf bracket, hinge, gear, etc.), the existing
free-tier estimators (``estimate_structural_load``,
``get_joint_recommendation``, ``validate_assembly``) currently return
a lookup-table answer with a fixed 3× safety factor and no shape /
buckling / fatigue / creep awareness.  That's an active footgun: the
user gets a confident-looking number that's wrong for any real
load-bearing application.

This detector trips on five dimensions (per
``RESEARCH_normie_vocabulary.md`` §3 in the engineering-tolerances
research dossier):

  (a) **Action verbs** — "holds", "supports", "mounts", "hangs", etc.
  (b) **Object nouns** — "bracket", "mount", "shelf", "hinge", "gear", etc.
  (c) **Adjectives** — "structural", "load-bearing", "heavy duty", etc.
  (d) **Mass/force numerics** — ≥ 22 N (≈ 5 lbs) is the practical
      "will it hold?" threshold DIYers use
  (e) **Engineering-grade material strings** — Nylon, PA-CF, PEEK, etc.

Plus a **geometric-tell** pass for phrases that imply load-bearing
without using §a/b vocabulary (wall mount, bookshelf, garage hook,
VESA, drone arm, etc.) and a **decoy** pass that downgrades benign
patterns (phone stand, drink coaster, decorative bracket).

When the score crosses the trip threshold (50, designed so any single
high-confidence keyword fires on its own), the detector emits a
``LoadBearingVerdict`` carrying:

  - ``trip_score`` and ``trip_reasons`` (transparent — the user sees
    exactly which signals fired)
  - ``confidence`` (low / medium / high)
    - ``upgrade_recommendation`` — structured guidance that free-tier tools
    attach to their response when deeper engineering analysis is warranted

The recommendation is factual and specific: it names what the heuristic
doesn't account for (cross-section shape, buckling, fatigue, creep, FDM
anisotropy) and points the user toward a deeper analysis path when a simple
lookup-table answer would be misleading.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Trigger lexicons (sourced from RESEARCH_normie_vocabulary.md §1)
# ---------------------------------------------------------------------------

# §1.1 — verbs (high frequency = solid signal; medium = secondary)
_HIGH_FREQ_VERBS: frozenset[str] = frozenset({
    "holds", "supports", "hangs", "mounts", "attaches", "clamps",
    "latches", "hinges", "pivots",
})

_MED_FREQ_VERBS: frozenset[str] = frozenset({
    "bolts", "fastens", "clips", "secures", "grips",
    "carries", "anchors", "straps", "transmits", "bears",
    "swings", "levers", "resists",
})

# §1.2 — nouns (HIGH conf = trips on its own; MED conf = needs a friend)
_HIGH_CONF_NOUNS: frozenset[str] = frozenset({
    "bracket", "mount", "hanger", "hook", "shelf", "rack",
    "hinge", "gear", "arm", "frame", "brace", "chassis",
    "support", "handle",
})

_MED_CONF_NOUNS: frozenset[str] = frozenset({
    "clip", "latch", "knob", "jig", "fixture", "lever",
    "pulley", "bearing", "bushing", "tripod", "stand",
    "clamp", "vise", "trolley", "pull", "lid", "tool",
    "wrench", "strut",
})

# §1.3 — adjectives
_LOAD_BEARING_ADJECTIVES: frozenset[str] = frozenset({
    "strong", "heavy duty", "heavy-duty", "sturdy", "rigid",
    "tough", "won't break", "wont break", "won't sag",
    "wont sag", "won't snap", "wont snap", "robust",
    "permanent", "long-term", "long term", "high-strength",
    "high strength",
})

_DURABILITY_ADJECTIVES: frozenset[str] = frozenset({
    "outdoor", "weatherproof", "uv-resistant", "uv resistant",
    "chemical-resistant", "chemical resistant",
})

_NEAR_CERTAIN_ADJECTIVES: frozenset[str] = frozenset({
    "load-bearing", "load bearing", "structural",
    "load-rated", "load rated", "rated for",
})

# §1.5 — engineering-grade material strings (any mention shifts prior)
_ENGINEERING_MATERIALS: frozenset[str] = frozenset({
    "nylon", "pa6", "pa12", "pa-cf", "pa6-cf", "pa12-cf",
    "polycarbonate", "pc-cf", "pet-cf", "petg-cf",
    "peek", "pps", "carbon fiber", "carbon-fiber", "carbon fibre",
    "glass fiber", "glass-fiber", "glass fibre",
    "polyamide",
})

# §3.2 — geometric tells (object/use-case patterns implying load-bearing)
# Tuples of (regex pattern, score, confidence label).  Negative scores
# are decoys (e.g. "phone stand" → 0, "decorative bracket" → -20).
_GEOMETRIC_TELLS: list[tuple[re.Pattern[str], int, str]] = [
    # Very high confidence — life safety / overhead
    (re.compile(r"\bceiling\s*(mount|hook|rack|hanger)\b", re.I), 40, "very_high"),
    (re.compile(r"\b(suspend|suspended)\b", re.I), 35, "very_high"),
    (re.compile(r"\b(bookshelf|book\s+shelf)\b", re.I), 40, "very_high"),
    (re.compile(r"\b(ladder\s+hook|kayak\s+(hanger|mount))\b", re.I), 40, "very_high"),
    (re.compile(r"\b(monitor\s+(mount|arm)|VESA(\s+mount)?|tv\s+mount)\b", re.I), 40, "very_high"),

    # High confidence
    (re.compile(r"\bwall\s*(mount|bracket|hook|hanger|rack)\b", re.I), 35, "high"),
    (re.compile(r"\bgarage\s*(hook|rack|shelf|mount|hanger)\b", re.I), 35, "high"),
    (re.compile(r"\bdrone\s*(arm|frame|chassis)\b", re.I), 35, "high"),
    (re.compile(r"\bbike\s*(storage|rack|hanger|mount)\b", re.I), 35, "high"),
    (re.compile(r"\bstud\s*(mount|bracket)\b", re.I), 30, "high"),
    (re.compile(r"\b(rc|r/c)\s*(arm|chassis|suspension|frame)\b", re.I), 30, "high"),
    (re.compile(r"\bguitar\s*(hanger|mount|hook|stand)\b", re.I), 30, "high"),

    # Medium confidence
    (re.compile(r"\b(stroller|purse|bag)\s+hook\b", re.I), 20, "medium"),
    (re.compile(r"\btap\s+(handle|mount)\b", re.I), 25, "medium"),
    (re.compile(r"\blaptop\s+stand\b", re.I), 15, "medium"),
    (re.compile(r"\btablet\s+stand\b", re.I), 10, "low_medium"),

    # Decoys / anti-patterns — score 0 (don't trip) or negative (downgrade)
    (re.compile(r"\bphone\s+stand\b", re.I), 0, "decoy"),
    (re.compile(r"\bdrink\s+coaster\b", re.I), 0, "decoy"),
    (re.compile(r"\bcoaster\b", re.I), 0, "decoy"),
    (re.compile(r"\bpicture\s+frame\b", re.I), 0, "decoy"),
    (re.compile(r"\bfridge\s+magnet\b", re.I), 0, "decoy"),
    (re.compile(r"\blithophane\b", re.I), 0, "decoy"),
    (re.compile(r"\bheadphone\s+stand\b", re.I), 5, "low"),
    (re.compile(r"\bdecorative\s+bracket\b", re.I), -20, "decoy"),
    (re.compile(r"\bdecorative\b", re.I), -10, "decoy"),
    (re.compile(r"\bcosmetic\b", re.I), -10, "decoy"),
    (re.compile(r"\bornament\b", re.I), -15, "decoy"),
    (re.compile(r"\bfigurine\b", re.I), -15, "decoy"),
]

# §3 — scoring weights
_WEIGHT_HIGH_VERB = 30
_WEIGHT_MED_VERB = 20
_WEIGHT_HIGH_NOUN = 50
_WEIGHT_MED_NOUN = 30
_WEIGHT_NEAR_CERTAIN_ADJ = 60
_WEIGHT_LOAD_BEARING_ADJ = 25
_WEIGHT_DURABILITY_ADJ = 20
_WEIGHT_LOAD_THRESHOLD_HIGH = 50  # ≥ 22 N
_WEIGHT_LOAD_THRESHOLD_LOW = 15   # 5..22 N
_WEIGHT_ENGINEERING_MATERIAL = 30

_TRIP_THRESHOLD = 50         # designed so any single high-confidence keyword fires
_HIGH_LOAD_THRESHOLD_N = 22.0   # ~5 lb / ~2.3 kg — practical "will it hold?" line
_LOW_LOAD_THRESHOLD_N = 5.0     # 0.5 kg / ~1 lb — borderline

_HIGH_CONF_SCORE = 100
_MED_CONF_SCORE = 70


# ---------------------------------------------------------------------------
# Mass / force numeric extractor
# ---------------------------------------------------------------------------

# Match patterns like "5 lbs", "10 lb", "2 kg", "50 N", "20 newtons",
# "10 grams", "8 oz", "100 g", with various spacings.
_LOAD_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    # (pattern, conversion factor to newtons; assumes "weight" load on Earth)
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:lbs?|pounds?)\b", re.I), 4.448),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:kgs?|kilograms?|kilogrammes?)\b", re.I), 9.81),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:gs?|grams?|grammes?)\b", re.I), 0.00981),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:ozs?|ounces?)\b", re.I), 0.278),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:newtons?)\b", re.I), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*N\b"), 1.0),
]


def extract_load_in_newtons(brief: str) -> float | None:
    """Parse the largest mass/force mention in a brief and return Newtons.

    Examples:
        >>> extract_load_in_newtons("holds 8 lbs")
        35.584
        >>> extract_load_in_newtons("must support 2 kg")
        19.62
        >>> extract_load_in_newtons("a small ornament")
        None
    """
    max_n: float | None = None
    for pattern, factor in _LOAD_PATTERNS:
        for match in pattern.finditer(brief):
            try:
                value = float(match.group(1)) * factor
            except (ValueError, TypeError):
                continue
            if max_n is None or value > max_n:
                max_n = value
    return max_n


# ---------------------------------------------------------------------------
# Verdict dataclass
# ---------------------------------------------------------------------------


@dataclass
class LoadBearingVerdict:
    """Result of running the load-bearing detector on a request.

    :param is_load_bearing: True when ``trip_score >= 50`` (the trip
        threshold designed so any single high-confidence keyword fires).
    :param trip_score: Sum of additive weights from all signals that
        fired.  Higher = more confident the request is load-bearing.
    :param confidence: ``"high"`` (>=100), ``"medium"`` (>=70), or
        ``"low"`` (>=50, just above threshold).
    :param trip_reasons: Human-readable list of which signals fired,
        e.g. ``["noun 'bracket' (+50)", "verb 'holds' (+30)",
        "load 35.6 N >= 22 N (+50)"]``.  The free-tier nudge surfaces
        these so the user understands why we think this is load-bearing.
    :param load_n_extracted: Parsed load magnitude in newtons, or None.
    :param applies_engineering_material: True when the brief or the
        explicit ``material`` argument names an engineering-grade
        filament (Nylon / PC / PA-CF / etc.).
    :param upgrade_recommendation: The Kiln Pro upgrade-nudge dict
        that free-tier tools attach to their response.  Contains a
        ``code``, ``warning``, ``pro_upgrade.upgrade_url``, and a
        ``pro_upgrade.what_youd_get`` list.  Empty when the detector
        didn't trip.
    """

    is_load_bearing: bool
    trip_score: int
    confidence: str
    trip_reasons: list[str] = field(default_factory=list)
    load_n_extracted: float | None = None
    applies_engineering_material: bool = False
    upgrade_recommendation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def detect_load_bearing(
    brief: str,
    *,
    material: str | None = None,
    applied_load_n: float | None = None,
) -> LoadBearingVerdict:
    """Run the load-bearing detector on a free-text brief.

    The detector is a free-tier gate.  When it trips, free-tier tools
    (``estimate_structural_load``, ``get_joint_recommendation``,
    ``validate_assembly``) attach an ``upgrade_recommendation`` to
    their response so the user sees the path to engineering-grade
    math (real beam mechanics, ISO 286 fits, tolerance stacking,
    fatigue + creep derating, calibrated tolerances) — Kiln Pro.

    :param brief: Free-text user request, e.g. "wall mount that holds
        my guitar" or "snap-fit lid for my arduino enclosure".
    :param material: Optional explicit material name.  Engineering-
        grade materials (Nylon, PC, CF composites) shift the prior.
    :param applied_load_n: Optional explicit load in newtons (caller
        already knows the load — no need to parse from text).
    :returns: :class:`LoadBearingVerdict`.

    Examples:
        >>> v = detect_load_bearing("wall mount that holds my guitar")
        >>> v.is_load_bearing
        True
        >>> "noun 'mount' (+50)" in v.trip_reasons
        True

        >>> v = detect_load_bearing("phone stand for my desk")
        >>> v.is_load_bearing
        False

        >>> v = detect_load_bearing("structural drone arm", material="PA6-CF")
        >>> v.confidence
        'high'
    """
    brief_lower = brief.lower()
    score = 0
    reasons: list[str] = []

    # (a) Verbs
    for verb in _HIGH_FREQ_VERBS:
        if re.search(rf"\b{re.escape(verb)}\b", brief_lower):
            score += _WEIGHT_HIGH_VERB
            reasons.append(f"verb '{verb}' (+{_WEIGHT_HIGH_VERB})")
            break  # one verb signal is enough; don't double-count
    else:
        for verb in _MED_FREQ_VERBS:
            if re.search(rf"\b{re.escape(verb)}\b", brief_lower):
                score += _WEIGHT_MED_VERB
                reasons.append(f"verb '{verb}' (+{_WEIGHT_MED_VERB})")
                break

    # (b) Nouns
    for noun in _HIGH_CONF_NOUNS:
        if re.search(rf"\b{re.escape(noun)}s?\b", brief_lower):
            score += _WEIGHT_HIGH_NOUN
            reasons.append(f"noun '{noun}' (+{_WEIGHT_HIGH_NOUN})")
            break
    else:
        for noun in _MED_CONF_NOUNS:
            if re.search(rf"\b{re.escape(noun)}s?\b", brief_lower):
                score += _WEIGHT_MED_NOUN
                reasons.append(f"noun '{noun}' (+{_WEIGHT_MED_NOUN})")
                break

    # (c) Adjectives — near-certain trip first
    near_certain_hit = False
    for adj in _NEAR_CERTAIN_ADJECTIVES:
        if adj in brief_lower:
            score += _WEIGHT_NEAR_CERTAIN_ADJ
            reasons.append(f"phrase '{adj}' (+{_WEIGHT_NEAR_CERTAIN_ADJ})")
            near_certain_hit = True
            break
    if not near_certain_hit:
        for adj in _LOAD_BEARING_ADJECTIVES:
            if adj in brief_lower:
                score += _WEIGHT_LOAD_BEARING_ADJ
                reasons.append(f"phrase '{adj}' (+{_WEIGHT_LOAD_BEARING_ADJ})")
                break
        else:
            for adj in _DURABILITY_ADJECTIVES:
                if adj in brief_lower:
                    score += _WEIGHT_DURABILITY_ADJ
                    reasons.append(f"phrase '{adj}' (+{_WEIGHT_DURABILITY_ADJ})")
                    break

    # (d) Numeric load — caller-provided wins, else parse from text
    load_n = applied_load_n if applied_load_n is not None else extract_load_in_newtons(brief)
    if load_n is not None:
        if load_n >= _HIGH_LOAD_THRESHOLD_N:
            score += _WEIGHT_LOAD_THRESHOLD_HIGH
            reasons.append(
                f"load {load_n:.1f} N >= {_HIGH_LOAD_THRESHOLD_N:.0f} N "
                f"(~5 lb) (+{_WEIGHT_LOAD_THRESHOLD_HIGH})"
            )
        elif load_n >= _LOW_LOAD_THRESHOLD_N:
            score += _WEIGHT_LOAD_THRESHOLD_LOW
            reasons.append(
                f"load {load_n:.1f} N (borderline) (+{_WEIGHT_LOAD_THRESHOLD_LOW})"
            )

    # (e) Engineering material
    eng_mat = False
    material_text = (material.lower() if material else "") + " " + brief_lower
    for mat_kw in _ENGINEERING_MATERIALS:
        if re.search(rf"\b{re.escape(mat_kw)}\b", material_text):
            score += _WEIGHT_ENGINEERING_MATERIAL
            reasons.append(f"engineering-grade material '{mat_kw}' (+{_WEIGHT_ENGINEERING_MATERIAL})")
            eng_mat = True
            break

    # (f) Geometric tells
    for pattern, points, conf_label in _GEOMETRIC_TELLS:
        if pattern.search(brief):
            score += points
            sign = "+" if points >= 0 else ""
            reasons.append(
                f"geometric tell ({conf_label}) {sign}{points}: "
                f"matched '{pattern.pattern}'"
            )

    # Confidence + verdict
    is_lb = score >= _TRIP_THRESHOLD
    if score >= _HIGH_CONF_SCORE:
        confidence = "high"
    elif score >= _MED_CONF_SCORE:
        confidence = "medium"
    elif is_lb:
        confidence = "low"
    else:
        confidence = "below_threshold"

    upgrade = _build_upgrade_recommendation(reasons) if is_lb else {}

    return LoadBearingVerdict(
        is_load_bearing=is_lb,
        trip_score=score,
        confidence=confidence,
        trip_reasons=reasons,
        load_n_extracted=load_n,
        applies_engineering_material=eng_mat,
        upgrade_recommendation=upgrade,
    )


# ---------------------------------------------------------------------------
# Upgrade-nudge builder — the funnel-allowed Pro mention
# ---------------------------------------------------------------------------

# Per CLAUDE.md "Trademark + cross-repo discipline" — naming kiln-pro
# and linking kiln3d.com from public Kiln is a funnel, not a leak.
_PRO_UPGRADE_URL = "https://kiln3d.com/pricing"


def _build_upgrade_recommendation(trip_reasons: list[str]) -> dict[str, Any]:
    """Build the upgrade-nudge dict free-tier tools attach to their response.

    Wording is factual and specific: names what the heuristic does
    NOT account for so the user can judge if it matters for their
    case.  Not fear-mongering, not vague.
    """
    return {
        "code": "LOAD_BEARING_DETECTED",
        "tier": "free",
        "engineering_grade": "heuristic",
        "warning": (
            "This part looks load-bearing.  The estimate above is the "
            "quick answer.  Kiln Pro shows the receipts — engineering "
            "safety factor, fatigue + creep checks, your printer's "
            "calibrated tolerances — when it matters.  → "
            "https://kiln3d.com/pricing"
        ),
        "trip_reasons": trip_reasons,
        "pro_upgrade": {
            "available": True,
            "tool": "design_for_load",
            "upgrade_url": _PRO_UPGRADE_URL,
            "what_youd_get": [
                "Real beam mechanics (sigma = Mc/I) with cross-section shape awareness",
                "Application-class factor of safety (1.5 prototype to 4-10 life-safety)",
                "Fatigue derating for cyclic snap-fits, hinges, and gears",
                "Creep derating for sustained loads (PLA shelves are NOT safe long-term)",
                "ISO 286 H7/h6-style fits with FDM-achievability gating",
                "Per-printer dimensional calibration (your printer's offsets, not generic)",
                "Tolerance stack analysis (worst-case + RSS + Monte Carlo) for assemblies",
                "Plain-English answer plus optional engineer-mode formula trace",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Structured-input nudge wiring
# ---------------------------------------------------------------------------
# Free-tier tools that take structured arguments (not free text) can't run
# the prose heuristic, so they decide via an explicit signal and attach the
# same upgrade nudge.  A press-fit or threaded joint carries mechanical load;
# clearance / snap / glued / magnetic / loose joints do not on their own.
_STRUCTURAL_JOINT_TYPES: frozenset[str] = frozenset({"press_fit", "threaded"})


def is_engineering_material(material: str | None) -> bool:
    """True when ``material`` names an engineering-grade filament."""
    if not material:
        return False
    text = material.lower()
    return any(
        re.search(rf"\b{re.escape(kw)}\b", text) for kw in _ENGINEERING_MATERIALS
    )


def load_bearing_signal(
    *,
    material: str | None = None,
    joint_type: str | None = None,
    applied_load_n: float | None = None,
) -> bool:
    """True when a structured request carries a load-bearing signal.

    For tools that take structured inputs instead of free text: an
    engineering-grade material, a structural joint (press-fit / threaded),
    or an explicit applied load at/above the low threshold.
    """
    if applied_load_n is not None and applied_load_n >= _LOW_LOAD_THRESHOLD_N:
        return True
    if is_engineering_material(material):
        return True
    if joint_type and joint_type.strip().lower() in _STRUCTURAL_JOINT_TYPES:
        return True
    return False


def attach_load_bearing_nudge(
    response: dict[str, Any],
    *,
    force: bool = False,
    material: str | None = None,
    joint_type: str | None = None,
    applied_load_n: float | None = None,
) -> dict[str, Any]:
    """Attach the heuristic-grade upgrade nudge to a free-tier tool response.

    Adds an ``upgrade_recommendation`` block plus a one-line
    ``load_bearing_note`` string when the part is load-bearing — always when
    ``force`` is set (the tool is definitionally structural, e.g.
    ``estimate_structural_load``), otherwise when :func:`load_bearing_signal`
    fires.  No-op on error responses and when no signal is present, so
    cosmetic calls stay clean.  Mutates and returns ``response``.
    """
    if not isinstance(response, dict) or response.get("success") is False:
        return response
    if not (
        force
        or load_bearing_signal(
            material=material,
            joint_type=joint_type,
            applied_load_n=applied_load_n,
        )
    ):
        return response
    reasons: list[str] = []
    if force:
        reasons.append("load-bearing context")
    if is_engineering_material(material):
        reasons.append(f"engineering-grade material '{material}'")
    if joint_type and joint_type.strip().lower() in _STRUCTURAL_JOINT_TYPES:
        reasons.append(f"structural joint '{joint_type}'")
    if applied_load_n is not None and applied_load_n >= _LOW_LOAD_THRESHOLD_N:
        reasons.append(f"applied load {applied_load_n:.1f} N")
    nudge = _build_upgrade_recommendation(reasons)
    response["upgrade_recommendation"] = nudge
    response["load_bearing_note"] = nudge["warning"]
    return response
