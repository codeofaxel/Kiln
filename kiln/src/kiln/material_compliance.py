"""Material compliance flags for regulatory and safety checks.

Provides a pre-populated database of compliance properties for common 3D
printing materials (food safety, REACH, RoHS, flame retardancy, etc.) and
tools to verify that a material meets a given set of requirements before
starting a print job.

Usage::

    from kiln.material_compliance import MaterialComplianceDatabase

    db = MaterialComplianceDatabase()
    info = db.get_compliance("PLA")
    print(info.food_safe)         # True (with caveats)
    print(info.warnings)          # ["Nozzle must be food-safe ..."]

    result = db.check_job_compliance("ABS", requirements=["food_safe"])
    print(result.compliant)       # False
    print(result.failures)        # ["ABS is not food_safe"]
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class MaterialCompliance:
    """Compliance and safety properties for a single material type.

    :param material_type: Canonical name (e.g. ``"PLA"``, ``"ABS"``).
    :param food_safe: Whether the material itself is food-contact safe
        (nozzle/printer may add caveats).
    :param reach_compliant: EU REACH regulation compliance.
    :param rohs_compliant: EU RoHS directive compliance.
    :param uv_resistant: Whether the material resists UV degradation.
    :param flame_retardant: Whether the material is inherently flame
        retardant or self-extinguishing.
    :param biocompatible: Whether the material is suitable for
        biocompatible applications (e.g. medical implants).
    :param max_continuous_temp_c: Maximum continuous service temperature
        in degrees Celsius.
    :param warnings: Safety or handling warnings.
    """

    material_type: str
    food_safe: bool = False
    reach_compliant: bool = False
    rohs_compliant: bool = False
    uv_resistant: bool = False
    flame_retardant: bool = False
    biocompatible: bool = False
    max_continuous_temp_c: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class ComplianceCheckResult:
    """Result of checking a material against a set of requirements.

    :param compliant: ``True`` if the material meets *all* requirements.
    :param failures: List of requirements that were **not** met.
    :param warnings: Relevant safety/handling warnings for the material.
    """

    compliant: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Checkable boolean properties (must match MaterialCompliance field names)
# ---------------------------------------------------------------------------

_CHECKABLE_PROPERTIES = frozenset(
    {
        "food_safe",
        "reach_compliant",
        "rohs_compliant",
        "uv_resistant",
        "flame_retardant",
        "biocompatible",
    }
)


# ---------------------------------------------------------------------------
# Pre-populated material database
# ---------------------------------------------------------------------------


def _build_default_materials() -> dict[str, MaterialCompliance]:
    """Return the built-in compliance database for common materials."""
    materials: list[MaterialCompliance] = [
        MaterialCompliance(
            material_type="PLA",
            food_safe=True,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=50.0,
            warnings=[
                "Nozzle and printer must also be food-safe for food contact",
                "Degrades under prolonged UV exposure",
                "Low heat resistance — not suitable for hot liquids",
            ],
        ),
        MaterialCompliance(
            material_type="ABS",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=98.0,
            warnings=[
                "Emits styrene fumes — requires ventilation",
                "Not food-safe",
                "Prone to warping without heated enclosure",
            ],
        ),
        MaterialCompliance(
            material_type="PETG",
            food_safe=True,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=73.0,
            warnings=[
                "Nozzle and printer must also be food-safe for food contact",
                "Strings easily — tune retraction settings",
            ],
        ),
        MaterialCompliance(
            material_type="TPU",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=True,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=80.0,
            warnings=[
                "Flexible — direct drive extruder recommended",
                "Not food-safe by default",
            ],
        ),
        MaterialCompliance(
            material_type="NYLON",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=80.0,
            warnings=[
                "Absorbs moisture rapidly — store in dry box",
                "Requires heated enclosure for reliable prints",
                "Emits fumes — requires ventilation",
            ],
        ),
        MaterialCompliance(
            material_type="ASA",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=True,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=98.0,
            warnings=[
                "Emits styrene fumes — requires ventilation",
                "Requires heated enclosure",
                "Not food-safe",
            ],
        ),
        MaterialCompliance(
            material_type="PC",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=True,
            flame_retardant=True,
            biocompatible=False,
            max_continuous_temp_c=130.0,
            warnings=[
                "Requires very high print temperatures (260-310 C)",
                "Absorbs moisture — store in dry box",
                "Emits BPA at elevated temperatures — not food-safe",
                "Requires heated enclosure",
            ],
        ),
        MaterialCompliance(
            material_type="PVA",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=50.0,
            warnings=[
                "Water-soluble support material only",
                "Absorbs moisture extremely fast — store sealed with desiccant",
                "Not for structural parts",
            ],
        ),
        MaterialCompliance(
            material_type="HIPS",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=80.0,
            warnings=[
                "Dissolves in limonene — use as support material for ABS",
                "Emits styrene fumes — requires ventilation",
                "Not food-safe",
            ],
        ),
        MaterialCompliance(
            material_type="PP",
            food_safe=True,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=100.0,
            warnings=[
                "Very poor bed adhesion — use PP-specific build surface",
                "High warpage tendency",
                "Nozzle and printer must also be food-safe for food contact",
            ],
        ),
        MaterialCompliance(
            material_type="RESIN_STANDARD",
            food_safe=False,
            reach_compliant=False,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=50.0,
            warnings=[
                "Uncured resin is a skin sensitizer — wear nitrile gloves",
                "Suspected carcinogen — use PPE and ventilation",
                "Requires UV post-curing",
                "Dispose of uncured resin as hazardous waste",
            ],
        ),
        MaterialCompliance(
            material_type="RESIN_TOUGH",
            food_safe=False,
            reach_compliant=False,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=False,
            max_continuous_temp_c=70.0,
            warnings=[
                "Uncured resin is a skin sensitizer — wear nitrile gloves",
                "Suspected carcinogen — use PPE and ventilation",
                "Requires UV post-curing",
                "Dispose of uncured resin as hazardous waste",
            ],
        ),
        MaterialCompliance(
            material_type="RESIN_DENTAL",
            food_safe=False,
            reach_compliant=True,
            rohs_compliant=True,
            uv_resistant=False,
            flame_retardant=False,
            biocompatible=True,
            max_continuous_temp_c=60.0,
            warnings=[
                "Biocompatible only after full UV post-cure per manufacturer spec",
                "Uncured resin is a skin sensitizer — wear nitrile gloves",
                "Requires UV post-curing",
            ],
        ),
    ]
    return {m.material_type.upper(): m for m in materials}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class MaterialComplianceDatabase:
    """Pre-populated database of material compliance properties.

    The database ships with compliance data for common FDM filaments and
    SLA resins.  Custom materials can be registered at runtime.
    """

    def __init__(self) -> None:
        self._materials: dict[str, MaterialCompliance] = _build_default_materials()

    def get_compliance(self, material_type: str) -> MaterialCompliance | None:
        """Look up compliance data for a material.

        :param material_type: Material name (case-insensitive).
        :returns: The :class:`MaterialCompliance` record, or ``None`` if
            the material is not in the database.
        """
        return self._materials.get(material_type.upper())

    def check_job_compliance(
        self,
        material: str,
        *,
        requirements: list[str],
    ) -> ComplianceCheckResult:
        """Check whether a material meets all specified requirements.

        :param material: Material name (case-insensitive).
        :param requirements: List of property names to check (e.g.
            ``["food_safe", "reach_compliant"]``).
        :returns: A :class:`ComplianceCheckResult`.
        """
        info = self._materials.get(material.upper())
        if info is None:
            return ComplianceCheckResult(
                compliant=False,
                failures=[f"Unknown material '{material}'"],
                warnings=[],
            )

        failures: list[str] = []
        for req in requirements:
            if req not in _CHECKABLE_PROPERTIES:
                failures.append(f"Unknown requirement '{req}'")
                continue
            value = getattr(info, req, False)
            if not value:
                failures.append(f"{material.upper()} is not {req}")

        return ComplianceCheckResult(
            compliant=len(failures) == 0,
            failures=failures,
            warnings=list(info.warnings),
        )

    def get_warnings(self, material: str) -> list[str]:
        """Return safety/handling warnings for a material.

        :param material: Material name (case-insensitive).
        :returns: List of warning strings, or empty list if unknown.
        """
        info = self._materials.get(material.upper())
        if info is None:
            return []
        return list(info.warnings)
