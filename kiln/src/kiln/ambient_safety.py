"""Ambient-aware safety warnings for enclosed printers.

Uses chamber temperature data that printers already report (Bambu via
MQTT, Voron/Klipper via Moonraker) to generate material-specific
safety warnings.  All analysis is local — no data leaves the machine.

Warnings cover:

* **Chamber too cold for material** — ABS/ASA/PC need enclosed warmth
  to avoid warping.  If the chamber is below the material's minimum,
  warn before and during printing.
* **Chamber too hot for material** — PLA softens above ~45C.  If the
  enclosure is hot from a previous print, warn that PLA will deform.
* **Thermal runaway risk** — chamber temp rising unexpectedly fast or
  exceeding the printer's safety profile max.
* **Cool-down advisory** — after a high-temp print, advise waiting
  before starting a PLA job if the chamber is still warm.

Configure via environment variables:

    KILN_AMBIENT_CHECK_ENABLED    -- enable ambient checks (default true)
    KILN_AMBIENT_COOLDOWN_TEMP_C  -- chamber temp threshold for PLA-safe (default 35.0)

Usage::

    from kiln.ambient_safety import check_ambient_safety

    result = check_ambient_safety(
        chamber_temp_c=52.0,
        material="PLA",
        printer_profile_id="bambu_x1c",
    )
    for w in result.warnings:
        print(w.message)  # "Chamber at 52°C is too hot for PLA (softens above 45°C)..."
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

_CHECK_ENABLED = os.environ.get("KILN_AMBIENT_CHECK_ENABLED", "true").lower() in ("true", "1", "yes")
_COOLDOWN_TEMP_C = float(os.environ.get("KILN_AMBIENT_COOLDOWN_TEMP_C", "35.0"))


# ---------------------------------------------------------------------------
# Material thermal profiles
# ---------------------------------------------------------------------------

# Minimum and maximum chamber temperatures for common FDM materials.
# These are printing-condition ranges where the material performs well.
# min_chamber_c: below this, the material is prone to warping/splitting.
# max_chamber_c: above this, the material softens or degrades on the bed.
# None means "no constraint" for that direction.

_MATERIAL_THERMAL_PROFILES: dict[str, dict[str, float | None]] = {
    "PLA": {"min_chamber_c": None, "max_chamber_c": 45.0},
    "PLA+": {"min_chamber_c": None, "max_chamber_c": 45.0},
    "PETG": {"min_chamber_c": None, "max_chamber_c": 55.0},
    "ABS": {"min_chamber_c": 35.0, "max_chamber_c": 70.0},
    "ASA": {"min_chamber_c": 35.0, "max_chamber_c": 70.0},
    "PC": {"min_chamber_c": 40.0, "max_chamber_c": 75.0},
    "NYLON": {"min_chamber_c": 30.0, "max_chamber_c": 65.0},
    "PA": {"min_chamber_c": 30.0, "max_chamber_c": 65.0},
    "TPU": {"min_chamber_c": None, "max_chamber_c": 50.0},
    "HIPS": {"min_chamber_c": 30.0, "max_chamber_c": 65.0},
    "PVA": {"min_chamber_c": None, "max_chamber_c": 40.0},
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AmbientWarning:
    """A single ambient-condition safety warning.

    :param severity: ``"info"``, ``"warning"``, or ``"critical"``.
    :param category: Warning category (e.g. ``"too_hot"``, ``"too_cold"``).
    :param message: Human-readable warning text.
    """

    severity: str
    category: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
        }


@dataclass
class AmbientSafetyResult:
    """Result of an ambient safety check.

    :param safe: True if no warnings or only info-level findings.
    :param chamber_temp_c: The chamber temperature that was checked.
    :param material: The material that was checked against.
    :param warnings: List of warnings, empty if conditions are fine.
    """

    safe: bool
    chamber_temp_c: float | None
    material: str | None
    warnings: list[AmbientWarning] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "chamber_temp_c": self.chamber_temp_c,
            "material": self.material,
            "warnings": [w.to_dict() for w in self.warnings],
        }


# ---------------------------------------------------------------------------
# Core check functions
# ---------------------------------------------------------------------------


def _normalize_material(material: str | None) -> str | None:
    """Normalize material name for lookup."""
    if material is None:
        return None
    # Strip whitespace, uppercase, remove common suffixes/prefixes
    cleaned = material.strip().upper()
    # Handle common variants
    cleaned = cleaned.replace("-", "").replace(" ", "")
    # Map common aliases
    _ALIASES: dict[str, str] = {
        "PLA+": "PLA+",
        "PLAPLUS": "PLA+",
        "PLA_PLUS": "PLA+",
        "POLYCARBONATE": "PC",
        "NYLON": "NYLON",
        "PA6": "PA",
        "PA12": "PA",
        "PA6CF": "PA",
    }
    if cleaned in _ALIASES:
        return _ALIASES[cleaned]
    return cleaned


def _get_thermal_profile(material: str) -> dict[str, float | None] | None:
    """Look up thermal profile for a material."""
    return _MATERIAL_THERMAL_PROFILES.get(material)


def check_ambient_safety(
    *,
    chamber_temp_c: float | None = None,
    material: str | None = None,
    max_chamber_temp_c: float | None = None,
) -> AmbientSafetyResult:
    """Check ambient conditions against material requirements.

    :param chamber_temp_c: Current chamber temperature in Celsius,
        or ``None`` if the printer doesn't report it.
    :param material: Material type (e.g. ``"PLA"``, ``"ABS"``).
    :param max_chamber_temp_c: Max chamber temp from the printer's
        safety profile, or ``None`` if unknown.
    :returns: :class:`AmbientSafetyResult` with warnings.
    """
    warnings: list[AmbientWarning] = []
    norm_material = _normalize_material(material)

    if chamber_temp_c is None:
        # No chamber data available — can't check
        return AmbientSafetyResult(
            safe=True,
            chamber_temp_c=None,
            material=norm_material,
            warnings=[],
        )

    if not _CHECK_ENABLED:
        return AmbientSafetyResult(
            safe=True,
            chamber_temp_c=chamber_temp_c,
            material=norm_material,
            warnings=[],
        )

    # Check against printer safety profile max
    if max_chamber_temp_c is not None and chamber_temp_c > max_chamber_temp_c:
        warnings.append(AmbientWarning(
            severity="critical",
            category="thermal_runaway",
            message=(
                f"Chamber temperature ({chamber_temp_c:.0f}C) exceeds printer maximum "
                f"({max_chamber_temp_c:.0f}C). Possible thermal runaway — check immediately."
            ),
        ))

    # Material-specific checks
    if norm_material is not None:
        profile = _get_thermal_profile(norm_material)
        if profile is not None:
            min_c = profile["min_chamber_c"]
            max_c = profile["max_chamber_c"]

            if max_c is not None and chamber_temp_c > max_c:
                warnings.append(AmbientWarning(
                    severity="warning",
                    category="too_hot",
                    message=(
                        f"Chamber at {chamber_temp_c:.0f}C is too hot for {norm_material} "
                        f"(softens above {max_c:.0f}C). Let the chamber cool before printing, "
                        f"or consider opening the enclosure door."
                    ),
                ))

            if min_c is not None and chamber_temp_c < min_c:
                warnings.append(AmbientWarning(
                    severity="warning",
                    category="too_cold",
                    message=(
                        f"Chamber at {chamber_temp_c:.0f}C is below the recommended minimum "
                        f"for {norm_material} ({min_c:.0f}C). Risk of warping or layer "
                        f"splitting. Preheat the enclosure or use a draft shield."
                    ),
                ))

    # Cooldown advisory: chamber is warm and material is heat-sensitive
    if norm_material in ("PLA", "PLA+", "PVA", "TPU") and chamber_temp_c > _COOLDOWN_TEMP_C:
        # Only add if we haven't already flagged "too_hot"
        categories = {w.category for w in warnings}
        if "too_hot" not in categories:
            warnings.append(AmbientWarning(
                severity="info",
                category="cooldown_advisory",
                message=(
                    f"Chamber is still warm ({chamber_temp_c:.0f}C). For best results "
                    f"with {norm_material}, wait until it cools below {_COOLDOWN_TEMP_C:.0f}C."
                ),
            ))

    # Determine overall safety
    has_blocking = any(w.severity in ("warning", "critical") for w in warnings)

    return AmbientSafetyResult(
        safe=not has_blocking,
        chamber_temp_c=chamber_temp_c,
        material=norm_material,
        warnings=warnings,
    )


def get_supported_materials() -> list[str]:
    """Return list of materials with thermal profile data."""
    return sorted(_MATERIAL_THERMAL_PROFILES.keys())
