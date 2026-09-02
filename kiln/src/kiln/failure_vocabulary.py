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

# Mapping from every known engine vocabulary to the canonical DB
# vocabulary.  Each entry's key is a value that some engine actually
# emits (lowercased); the value is the canonical form for storage,
# rerouter safety checks, and design-constraint lookup.
#
# Source vocabularies covered:
#   * :class:`kiln.failure_recovery.FailureType`
#       — adhesion_loss, nozzle_clog, unknown
#   * :class:`kiln.print_recovery.FailureType`
#       — adhesion_failure, blob_detected, communication_loss
#   * :class:`kiln.recovery.FailureType`
#       — bed_adhesion_failure, first_layer_failure, network_disconnect,
#         printer_error, software_crash, timeout, user_cancelled
#
# Values not in this map and not in :data:`VALID_FAILURE_MODES` are
# rejected (return ``None``) — better to surface "unrecognized" than to
# silently lose data.
CLASSIFIER_TO_CANONICAL: dict[str, str] = {
    # kiln.failure_recovery.FailureType
    "adhesion_loss": "adhesion",
    "nozzle_clog": "clog",
    "unknown": "other",
    # kiln.print_recovery.FailureType
    "adhesion_failure": "adhesion",
    "blob_detected": "spaghetti",
    "communication_loss": "other",
    # kiln.recovery.FailureType
    "bed_adhesion_failure": "adhesion",
    "first_layer_failure": "adhesion",
    "network_disconnect": "other",
    "printer_error": "mechanical",
    "software_crash": "other",
    "timeout": "other",
    "user_cancelled": "other",
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


def normalize_failure_type(value: str | None) -> str | None:
    """Normalize ANY engine's failure-type string to canonical form.

    Sister to :func:`to_canonical` with two practical extensions:

    * **Whitespace-tolerant.**  Leading/trailing whitespace is stripped
      before lookup, so ``" Layer Shift "`` resolves to ``"layer_shift"``
      cleanly.
    * **Designed as the public boundary translator.**  Every consumer
      that reads failure-type strings from another engine should call
      this — outcome storage (so two spellings of "adhesion" don't
      become two strategies in the recovery_outcomes.json),
      rerouter safety checks (so ``adhesion_loss`` is treated as
      safety-critical exactly like ``adhesion_failure``), and any
      future downstream that compares failure types across engines.

    Returns ``None`` for empty input, all-whitespace input, or strings
    that don't resolve to any known canonical mode.  Never raises.

    Examples::

        >>> normalize_failure_type("ADHESION_FAILURE")
        'adhesion'
        >>> normalize_failure_type("bed_adhesion_failure")
        'adhesion'
        >>> normalize_failure_type("Layer Shift")  # spaces tolerated
        None        # space is preserved in lookup; spelling must match
        >>> normalize_failure_type("layer_shift")
        'layer_shift'
        >>> normalize_failure_type(" thermal_runaway ")
        'thermal_runaway'
        >>> normalize_failure_type("nope")
        None
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return to_canonical(stripped)


# Severity ladder normalization — the parallel three-vocabulary problem.
# kiln.print_recovery uses free-form strings like "low/medium/high/critical".
# kiln.print_health_monitor uses an enum: "ok/warning/critical".
# kiln_pro.recovery.predictive uses "info/amber/red" plus a "clear"
# whole-assessment severity.
#
# Canonical ladder (lowercase, ordered):
#   "ok" < "info" < "low" < "medium" < "high" < "critical"
#
# This dict translates each engine's spelling into the canonical
# spelling.  Values not listed here pass through verbatim (lowercased)
# and are rejected by :func:`normalize_severity` if not in the
# canonical set.
_SEVERITY_TO_CANONICAL: dict[str, str] = {
    # kiln.print_health_monitor.HealthSeverity
    "ok": "ok",
    "warning": "medium",
    # kiln_pro.recovery.predictive RiskSignal severity
    "info": "info",
    "amber": "medium",
    "red": "high",
    "clear": "ok",
    # kiln.print_recovery.FailureReport.severity (free-form strings)
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
}

CANONICAL_SEVERITIES: frozenset[str] = frozenset({
    "ok",
    "info",
    "low",
    "medium",
    "high",
    "critical",
})


def normalize_severity(value: str | None) -> str | None:
    """Normalize ANY engine's severity string to the canonical ladder.

    Canonical ladder (low → high): ``ok`` < ``info`` < ``low``
    < ``medium`` < ``high`` < ``critical``.

    The mapping merges three engine vocabularies onto this ladder:
    ``warning`` and ``amber`` both become ``medium``; ``red`` becomes
    ``high``; ``clear`` becomes ``ok``.  Case- and whitespace-
    tolerant.  Returns ``None`` for unrecognized input.

    Pair this with :func:`severity_at_least` when you want a
    threshold check (e.g. "is this signal at least medium severity?")
    that works regardless of which engine emitted the string.
    """
    if value is None:
        return None
    stripped = value.strip().lower()
    if not stripped:
        return None
    canonical = _SEVERITY_TO_CANONICAL.get(stripped, stripped)
    return canonical if canonical in CANONICAL_SEVERITIES else None


_SEVERITY_RANK: dict[str, int] = {
    "ok": 0,
    "info": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "critical": 5,
}


def severity_at_least(value: str | None, threshold: str) -> bool:
    """Return True when *value* normalizes to at least *threshold*.

    Both arguments are normalized via :func:`normalize_severity` so
    callers can pass any engine's spelling.  Unrecognized *value*
    returns False (defensive — unknown severity is not "above" any
    known threshold).  Unrecognized *threshold* raises ValueError —
    that one's a programming error, not data.
    """
    norm_threshold = normalize_severity(threshold)
    if norm_threshold is None:
        raise ValueError(
            f"threshold must normalize to a canonical severity, got {threshold!r}"
        )
    norm_value = normalize_severity(value)
    if norm_value is None:
        return False
    return _SEVERITY_RANK[norm_value] >= _SEVERITY_RANK[norm_threshold]


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
# Negative-constraint anti-patterns
# ---------------------------------------------------------------------------

# Symmetric counterpart to MITIGATIONS: short clauses describing what
# the design must *not* contain, given a recurring failure mode.  These
# emit as "Avoid: ..." clauses in the proactive prompt enhancement so
# the generative model has explicit exclusion guidance in addition to
# the positive mitigations above.  Each entry is a single
# noun-phrase fragment so multiple can be joined with "; " inside the
# Avoid clause.
ANTI_PATTERNS: dict[str, str] = {
    "adhesion": "tall narrow bases or small bed-contact footprints",
    "stringing": "long unsupported travel moves between disconnected geometry",
    "warping": "large flat surfaces and sharp 90-degree corners on the build plate",
    "layer_shift": "tall thin towers and aggressive overhanging cantilevers",
    "spaghetti": "unsupported overhangs greater than 45 degrees",
    "under_extrusion": "walls thinner than 1.2mm",
    "over_extrusion": "tight press-fits requiring sub-tenth-mm tolerances",
    "clog": "rapid retraction zones and tight infill on small features",
    "elephant_foot": "sharp 90-degree bottom edges",
}


def anti_pattern_for(mode: str | None) -> str | None:
    """Return the anti-pattern clause for a failure_mode, or ``None``.

    Symmetric to :func:`mitigation_for` — accepts either canonical or
    classifier-vocabulary strings and returns ``None`` for hardware
    failures that have no design-level anti-pattern.
    """
    if not mode:
        return None
    lower = mode.lower()
    canonical = CLASSIFIER_TO_CANONICAL.get(lower, lower)
    return ANTI_PATTERNS.get(canonical) or ANTI_PATTERNS.get(lower)


# ---------------------------------------------------------------------------
# Auto-classify threshold
# ---------------------------------------------------------------------------

# Minimum confidence from the failure classifier for auto-stored
# ``failure_mode``.  Below this, the classification is echoed back to
# the caller but never written to the outcome row — prevents silent
# data poisoning from low-confidence guesses.
AUTO_CLASSIFY_MIN_CONFIDENCE: float = 0.75
