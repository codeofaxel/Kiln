"""Print intelligence tools plugin — DNA, community, material routing.

Registers MCP tools for model fingerprinting, print DNA recording and
prediction, community print registry, and smart material recommendation.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

_logger = logging.getLogger(__name__)


class _IntelligenceToolsPlugin:
    """Print intelligence tools — DNA, community registry, material routing.

    Tools:
        - fingerprint_model
        - record_print_dna
        - predict_print_settings
        - find_similar_prints
        - get_model_print_history
        - contribute_community_print
        - get_community_insight
        - community_stats
        - recommend_material
        - list_available_materials
    """

    @property
    def name(self) -> str:
        return "intelligence_tools"

    @property
    def description(self) -> str:
        return "Print DNA, community registry, and material routing tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register intelligence tools with the MCP server."""

        @mcp.tool()
        def fingerprint_model(file_path: str) -> dict:
            """Compute a geometric fingerprint for a 3D model file.

            Reads the STL file and produces a fingerprint containing: SHA-256
            file hash, triangle/vertex counts, bounding box, surface area,
            volume, overhang ratio, complexity score, and a geometric
            signature for similarity matching.

            Args:
                file_path: Path to the STL file to fingerprint.
            """
            import kiln.server as _srv

            try:
                from kiln.print_dna import fingerprint_model as _fingerprint

                fp = _fingerprint(file_path)
                return {"success": True, "fingerprint": fp.to_dict()}
            except FileNotFoundError:
                return _srv._error_dict(f"File not found: {file_path}", code="NOT_FOUND")
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in fingerprint_model")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def record_print_dna(
            file_hash: str,
            geometric_signature: str,
            triangle_count: int,
            surface_area_mm2: float,
            volume_mm3: float,
            overhang_ratio: float,
            complexity_score: float,
            printer_model: str,
            material: str,
            settings: dict,
            outcome: str,
            quality_grade: str = "B",
            failure_mode: str | None = None,
            print_time_seconds: int = 0,
        ) -> dict:
            """Record a print outcome with full model DNA.

            Saves the model fingerprint alongside print settings and outcome
            for cross-user learning.  Use ``fingerprint_model`` first to
            compute the fingerprint fields.

            Args:
                file_hash: SHA-256 hash of the model file.
                geometric_signature: Geometric signature from fingerprinting.
                triangle_count: Number of triangles in the model.
                surface_area_mm2: Total surface area in mm^2.
                volume_mm3: Model volume in mm^3.
                overhang_ratio: Ratio of overhanging triangles (0.0-1.0).
                complexity_score: Model complexity (0.0-1.0).
                printer_model: Printer model name.
                material: Material used (e.g. ``"PLA"``).
                settings: Print settings dict.
                outcome: ``"success"``, ``"failed"``, or ``"partial"``.
                quality_grade: Grade from ``"A"`` to ``"F"`` (default ``"B"``).
                failure_mode: Optional failure description.
                print_time_seconds: Print duration in seconds.
            """
            import kiln.server as _srv

            try:
                from kiln.print_dna import ModelFingerprint
                from kiln.print_dna import record_print_dna as _record

                fp = ModelFingerprint(
                    file_hash=file_hash,
                    triangle_count=triangle_count,
                    vertex_count=0,
                    bounding_box={},
                    surface_area_mm2=surface_area_mm2,
                    volume_mm3=volume_mm3,
                    overhang_ratio=overhang_ratio,
                    complexity_score=complexity_score,
                    geometric_signature=geometric_signature,
                )

                _record(
                    fp,
                    printer_model,
                    material,
                    settings,
                    outcome,
                    quality_grade=quality_grade,
                    failure_mode=failure_mode,
                    print_time_seconds=print_time_seconds,
                )

                return {
                    "success": True,
                    "file_hash": file_hash,
                    "outcome": outcome,
                    "printer_model": printer_model,
                    "material": material,
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in record_print_dna")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def predict_print_settings(
            file_hash: str,
            geometric_signature: str,
            surface_area_mm2: float,
            volume_mm3: float,
            complexity_score: float,
            printer_model: str,
            material: str,
        ) -> dict:
            """Predict optimal print settings from historical DNA data.

            Searches for exact file hash matches first, then falls back to
            geometrically similar models, and finally to material defaults.

            Args:
                file_hash: SHA-256 hash of the model file.
                geometric_signature: Geometric signature from fingerprinting.
                surface_area_mm2: Surface area in mm^2.
                volume_mm3: Model volume in mm^3.
                complexity_score: Model complexity (0.0-1.0).
                printer_model: Target printer model.
                material: Target material.
            """
            import kiln.server as _srv

            try:
                from kiln.print_dna import (
                    ModelFingerprint,
                    predict_settings,
                )

                fp = ModelFingerprint(
                    file_hash=file_hash,
                    triangle_count=0,
                    vertex_count=0,
                    bounding_box={},
                    surface_area_mm2=surface_area_mm2,
                    volume_mm3=volume_mm3,
                    overhang_ratio=0.0,
                    complexity_score=complexity_score,
                    geometric_signature=geometric_signature,
                )

                prediction = predict_settings(fp, printer_model, material)
                return {"success": True, "prediction": prediction.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in predict_print_settings")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def find_similar_prints(
            file_hash: str,
            geometric_signature: str,
            surface_area_mm2: float = 0.0,
            volume_mm3: float = 0.0,
            complexity_score: float = 0.0,
            limit: int = 10,
            threshold: float = 0.8,
        ) -> dict:
            """Find similar models in the print DNA knowledge base.

            Uses geometric signature matching and surface area / volume
            similarity to locate models with similar geometry.

            Args:
                file_hash: SHA-256 hash of the model file.
                geometric_signature: Geometric signature from fingerprinting.
                surface_area_mm2: Surface area in mm^2 (for fuzzy matching).
                volume_mm3: Volume in mm^3 (for fuzzy matching).
                complexity_score: Complexity (for fuzzy matching).
                limit: Maximum results (default 10).
                threshold: Similarity threshold 0.0-1.0 (default 0.8).
            """
            import kiln.server as _srv

            try:
                from kiln.print_dna import (
                    ModelFingerprint,
                    find_similar_models,
                )

                fp = ModelFingerprint(
                    file_hash=file_hash,
                    triangle_count=0,
                    vertex_count=0,
                    bounding_box={},
                    surface_area_mm2=surface_area_mm2,
                    volume_mm3=volume_mm3,
                    overhang_ratio=0.0,
                    complexity_score=complexity_score,
                    geometric_signature=geometric_signature,
                )

                records = find_similar_models(fp, limit=limit, threshold=threshold)
                return {
                    "success": True,
                    "similar_models": [r.to_dict() for r in records],
                    "count": len(records),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in find_similar_prints")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_model_print_history(file_hash: str) -> dict:
            """Get all print attempts for a model identified by file hash.

            Returns the complete history of print outcomes, settings, and
            quality grades for a specific model.

            Args:
                file_hash: SHA-256 hash of the model file.
            """
            import kiln.server as _srv

            try:
                from kiln.print_dna import get_model_history, get_success_rate

                records = get_model_history(file_hash)
                rate = get_success_rate(file_hash)

                return {
                    "success": True,
                    "file_hash": file_hash,
                    "history": [r.to_dict() for r in records],
                    "total_prints": rate["total_prints"],
                    "success_rate": rate["success_rate"],
                    "outcomes": rate["outcomes"],
                    "grade_distribution": rate["grade_distribution"],
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_model_print_history")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def contribute_community_print(
            geometric_signature: str,
            printer_model: str,
            material: str,
            settings: dict,
            outcome: str,
            quality_grade: str = "B",
            failure_mode: str | None = None,
            print_time_seconds: int = 0,
        ) -> dict:
            """Contribute a print outcome to the community registry.

            Adds an anonymous print record for community aggregation.
            Only geometric signatures and settings are stored — never
            file contents, user IDs, or file paths.

            Args:
                geometric_signature: Geometric signature from fingerprinting.
                printer_model: Printer model name.
                material: Material used.
                settings: Print settings dict.
                outcome: ``"success"``, ``"failed"``, or ``"partial"``.
                quality_grade: Grade from ``"A"`` to ``"F"`` (default ``"B"``).
                failure_mode: Optional failure description.
                print_time_seconds: Print duration in seconds.
            """
            import kiln.server as _srv

            try:
                from kiln.community_registry import (
                    CommunityPrintRecord,
                    contribute_print,
                )

                settings_hash = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]

                record = CommunityPrintRecord(
                    geometric_signature=geometric_signature,
                    printer_model=printer_model,
                    material=material,
                    settings_hash=settings_hash,
                    settings=settings,
                    outcome=outcome,
                    quality_grade=quality_grade,
                    failure_mode=failure_mode,
                    print_time_seconds=print_time_seconds,
                    region="anonymous",
                    timestamp=time.time(),
                )

                contribute_print(record)

                return {
                    "success": True,
                    "geometric_signature": geometric_signature,
                    "outcome": outcome,
                }
            except ValueError as exc:
                return _srv._error_dict(str(exc), code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in contribute_community_print")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_community_insight(geometric_signature: str) -> dict:
            """Get community aggregated data for a model geometry.

            Returns success rates, top printer models, top materials,
            recommended settings, and common failure modes from the
            community registry.

            Args:
                geometric_signature: Geometric signature to look up.
            """
            import kiln.server as _srv

            try:
                from kiln.community_registry import (
                    get_community_insight as _get_insight,
                )

                insight = _get_insight(geometric_signature)
                if insight is None:
                    return {
                        "success": True,
                        "has_data": False,
                        "message": "No community data found for this geometry.",
                    }

                return {
                    "success": True,
                    "has_data": True,
                    "insight": insight.to_dict(),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_community_insight")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def community_stats() -> dict:
            """Get overall community registry statistics.

            Returns total records, unique models, printers, materials,
            and overall success rate.
            """
            import kiln.server as _srv

            try:
                from kiln.community_registry import get_community_stats

                stats = get_community_stats()
                return {"success": True, "stats": stats.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in community_stats")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def recommend_material(
            intent: str,
            has_enclosure: bool = False,
            has_heated_bed: bool = True,
            budget_usd: float | None = None,
        ) -> dict:
            """Recommend a 3D printing material based on user intent.

            Translates natural language intent (e.g. ``"make it strong"``,
            ``"make it pretty"``, ``"make it cheap"``) into an optimal
            material recommendation with settings.

            Args:
                intent: User intent text (e.g. ``"strong"``, ``"pretty"``).
                has_enclosure: Whether the printer has an enclosure.
                has_heated_bed: Whether the printer has a heated bed.
                budget_usd: Optional maximum budget per kg in USD.
            """
            import kiln.server as _srv

            try:
                from kiln.material_routing import (
                    recommend_material as _recommend,
                )

                caps = {
                    "has_enclosure": has_enclosure,
                    "has_heated_bed": has_heated_bed,
                }

                rec = _recommend(
                    intent,
                    printer_capabilities=caps,
                    budget_usd=budget_usd,
                )

                return {"success": True, "recommendation": rec.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in recommend_material")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def list_available_materials() -> dict:
            """List all available 3D printing materials with properties.

            Returns details for every material in the database including
            strength, flexibility, heat resistance, surface quality,
            ease of print, cost, and temperature requirements.
            """
            import kiln.server as _srv

            try:
                from kiln.material_routing import list_materials

                materials = list_materials()
                return {
                    "success": True,
                    "materials": [m.to_dict() for m in materials],
                    "count": len(materials),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in list_available_materials")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered print intelligence tools")


plugin = _IntelligenceToolsPlugin()
