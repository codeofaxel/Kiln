"""Single source of truth for print-failure vocabulary.

Used across the learning loop so the DB column values, the classifier
mappings, the design-constraint mitigations, and the auto-classify
threshold all live in one place instead of drifting across modules.

Consumers:

* :mod:`kiln.plugins.learning_tools` — ``record_print_outcome``
  validation + auto-classify mapping.
* :mod:`kiln.generation_feedback` — failure-mode → design-constraint
  translation inside ``enhance_prompt_with_design_intelligence``.

The canonical vocabulary is the DB schema's ``print_outcomes.failure_mode``
column.  The failure classifier (:mod:`kiln.failure_recovery`
``FailureType``) uses a slightly different vocabulary; use
:func:`to_canonical` to translate it in.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical vocabulary
# ---------------------------------------------------------------------------

# Canonical failure modes — the set of valid values for the
# ``failure_mode`` column in the ``print_outcomes`` table.  Anything
# outside this set is rejected at record time.
VALID_FAILURE_MODES: frozenset[str] = frozenset({
    "spaghetti",
    "layer_shift",
    "warping",
    "adhesion",
    "stringing",
    "under_extrusion",
    "over_extrusion",
    "clog",
    "thermal_runaway",
    "power_loss",
    "filament_runout",
    "mechanical",
    "other",
})


# ---------------------------------------------------------------------------
# Classifier → canonical translation
# ---------------------------------------------------------------------------

# Mapping from :class:`kiln.failure_recovery.FailureType` values to the
# canonical DB vocabulary.  Values absent from this map are assumed to
# already be canonical (e.g. ``warping`` matches in both directions).
CLASSIFIER_TO_CANONICAL: dict[str, str] = {
    "adhesion_loss": "adhesion",
    "nozzle_clog": "clog",
    "unknown": "other",
}


def to_canonical(mode: str | None) -> str | None:
    """Return the canonical DB failure_mode for *mode*, or ``None`` when
    no valid canonical form can be produced.

    Handles the classifier→DB translation and validates the result
    against :data:`VALID_FAILURE_MODES` in one step, so callers do not
    need to remember to check both.
    """
    if not mode:
        return None
    lower = mode.lower()
    canonical = CLASSIFIER_TO_CANONICAL.get(lower, lower)
    return canonical if canonical in VALID_FAILURE_MODES else None


# ---------------------------------------------------------------------------
# Design-constraint mitigations
# ---------------------------------------------------------------------------

# One mitigation string per canonical failure_mode that has a
# design-level remedy.  Hardware/consumable failures (thermal_runaway,
# power_loss, mechanical, filament_runout) have no design mitigation —
# their fixes live outside the generated-mesh domain.
#
# ``elephant_foot`` is retained as a key despite not being in
# ``VALID_FAILURE_MODES`` — the pattern shows up in some analysis
# reports and its mitigation is purely design-level (chamfer the
# bottom), so the generation layer should still act on it when it
# arrives via a different path.
MITIGATIONS: dict[str, str] = {
    "adhesion": "extra-wide base for bed adhesion, consider brim",
    "stringing": "minimize travel moves, avoid thin isolated features",
    "warping": "chamfered corners, avoid large flat surfaces",
    "layer_shift": "low center of gravity, avoid tall narrow geometry",
    "spaghetti": "no unsupported overhangs, solid geometry",
    "under_extrusion": "minimum wall thickness 1.2mm",
    "over_extrusion": "generous tolerances, avoid tight press-fits",
    "clog": "avoid rapid retraction zones",
    "elephant_foot": "slight chamfer on bottom edges",
}


def mitigation_for(mode: str | None) -> str | None:
    """Return the design-constraint mitigation for a failure_mode, or
    ``None`` if no design-level mitigation is known.

    Accepts either canonical or classifier-vocabulary strings — both
    resolve to the same canonical key before lookup.  Returns ``None``
    for hardware/consumable failures that have no design-level fix.
    """
    if not mode:
        return None
    lower = mode.lower()
    canonical = CLASSIFIER_TO_CANONICAL.get(lower, lower)
    # Canonical key wins; fall through to the raw lowercase for keys
    # like ``elephant_foot`` that aren't in the canonical set but are
    # still in MITIGATIONS.
    return MITIGATIONS.get(canonical) or MITIGATIONS.get(lower)


# ---------------------------------------------------------------------------
# Auto-classify threshold
# ---------------------------------------------------------------------------

# Minimum confidence from the failure classifier for auto-stored
# ``failure_mode``.  Below this, the classification is echoed back to
# the caller but never written to the outcome row — prevents silent
# data poisoning from low-confidence guesses.
AUTO_CLASSIFY_MIN_CONFIDENCE: float = 0.75
