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

_PRICING_URL = "https://kiln3d.com/pricing"


def _community_layer(community_insight: dict[str, Any] | None) -> dict[str, Any]:
    """Describe the community layer's state in one honest block.

    Present whether or not community data arrived, so a caller never has
    to guess between "nobody has printed this" and "this plan doesn't
    include it" — and so the absence reads as an invitation rather than
    an error.
    """
    if community_insight is not None:
        return {
            "available": True,
            "sample_size": community_insight.get("total_prints", 0),
        }
    return {
        "available": False,
        "note": (
            "How this shape printed for everyone else comes with Kiln Pro."
        ),
        "learn_more": _PRICING_URL,
    }


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
            volume, overhang ratio, complexity score, and TWO geometric
            signatures for similarity matching.

            ``geometric_signature_v2`` is the one to carry into
            ``record_print_dna`` / ``predict_print_settings`` /
            ``find_similar_prints`` / ``contribute_community_print``: it
            tells this design apart from a different one that happens to
            share the older ``geometric_signature``.  Pass both — the
            older key is what joins this print to history recorded before
            v2 existed.

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
            geometric_signature_v2: str = "",
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
                geometric_signature_v2: ``fingerprint_model``'s
                    ``geometric_signature_v2``.  Pass it: it is what lets
                    this print be told apart from a different design that
                    happens to share the older signature.  Omitted, the row
                    is stored with the older key only and can never be
                    separated from that design later.
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
                    geometric_signature_v2=geometric_signature_v2,
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
            geometric_signature_v2: str = "",
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
                geometric_signature_v2: ``fingerprint_model``'s
                    ``geometric_signature_v2``.  Pass it: without it the
                    prediction can be averaged over prints of a DIFFERENT
                    design that shares the older signature.
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
                    geometric_signature_v2=geometric_signature_v2,
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
            geometric_signature_v2: str = "",
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
                geometric_signature_v2: ``fingerprint_model``'s
                    ``geometric_signature_v2``.  Pass it: without it,
                    models that merely share the older signature are
                    reported as the same geometry.
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
                    geometric_signature_v2=geometric_signature_v2,
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
        def get_model_print_history(
            file_hash: str = "",
            material: str = "",
            model_path: str = "",
            geometric_signature: str = "",
            geometric_signature_v2: str = "",
        ) -> dict:
            """Get all print attempts for a model.

            Returns the complete history of print outcomes, settings, and
            quality grades.  The success-rate metrics cover every material
            by default; pass ``material`` to scope them to one filament
            ("how does this do in PETG?").

            PASS ``model_path`` WHENEVER YOU HAVE THE FILE.  History is a
            question about the SHAPE, and ``model_path`` lets Kiln identify
            it directly.  A file hash alone identifies BYTES: re-export an
            unchanged part from CAD and the hash changes, so a hash-only
            lookup reports a part with real history as never printed.

            ``identified_by`` in the response says which it used —
            ``"shape"`` is the precise answer, ``"file"`` means the history
            may be incomplete.

            Args:
                file_hash: SHA-256 hash of the model file, when known.
                material: Optional material filter for the success-rate
                    metrics.  Empty = all materials.
                model_path: Path to the model file — the best input.  Kiln
                    fingerprints it and identifies the design itself.
                geometric_signature: v1 shape signature, if you already have
                    one from ``fingerprint_model``.
                geometric_signature_v2: v2 shape signature, likewise.
            """
            import kiln.server as _srv

            try:
                from kiln.print_dna import get_model_history, get_success_rate

                # The file is the richest input: derive every identity from
                # it rather than making the caller carry hashes around.  A
                # file we cannot parse is not fatal — fall through to
                # whatever identity the caller did supply.
                unread_file_reason = ""
                if model_path:
                    try:
                        from kiln.print_dna import fingerprint_model as _fp

                        fp = _fp(model_path)
                        file_hash = file_hash or fp.file_hash
                        geometric_signature = (
                            geometric_signature or fp.geometric_signature
                        )
                        geometric_signature_v2 = (
                            geometric_signature_v2 or fp.geometric_signature_v2
                        )
                    except Exception as exc:
                        unread_file_reason = str(exc)
                        _logger.debug(
                            "could not fingerprint %s; using supplied identity",
                            model_path,
                            exc_info=True,
                        )

                if not (file_hash or geometric_signature or geometric_signature_v2):
                    # Say what happened to the FILE.  Asking a caller to
                    # "pass model_path" when they just did sent CAD users in
                    # a circle, and the fingerprint_model they were pointed
                    # at fails on the same file for the same reason.
                    if unread_file_reason:
                        return _srv._error_dict(
                            f"Could not read a model from {model_path}: "
                            f"{unread_file_reason}",
                            code="UNREADABLE_INPUT",
                        )
                    return _srv._error_dict(
                        "Name the model: pass model_path (best), or a "
                        "file_hash / geometric_signature from fingerprint_model.",
                        code="VALIDATION_ERROR",
                    )

                records = get_model_history(
                    file_hash,
                    geometric_signature=geometric_signature,
                    geometric_signature_v2=geometric_signature_v2,
                )
                rate = get_success_rate(
                    file_hash,
                    material=material or None,
                    geometric_signature=geometric_signature,
                    geometric_signature_v2=geometric_signature_v2,
                )

                # Defaulted, not indexed: an older kiln3d (or a caller that
                # substitutes this engine) returns a rate dict without the
                # key, and a history answer must not become an exception
                # over a provenance label.
                identified = rate.get("identified_by", "file")
                result = {
                    "success": True,
                    "file_hash": file_hash,
                    "identified_by": identified,
                    "history": [r.to_dict() for r in records],
                    "total_prints": rate["total_prints"],
                    "success_rate": rate["success_rate"],
                    "outcomes": rate["outcomes"],
                    "grade_distribution": rate["grade_distribution"],
                }
                if identified == "file":
                    result["note"] = (
                        "Matched on the file only, so this covers this exact "
                        "file and not the design. Pass model_path for the "
                        "part's full history."
                    )
                return result
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
            job_id: str | None = None,
            geometric_signature_v2: str = "",
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
                job_id: The print's job id when known — it anchors the
                    federation dedupe key, so a print that was also
                    watched (or recorded via ``record_print_outcome``)
                    ships to the community pool once, not twice.
                geometric_signature_v2: ``fingerprint_model``'s
                    ``geometric_signature_v2``.  Pass it: it is what keeps
                    this contribution from being averaged into a different
                    design that shares the older signature.
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
                    geometric_signature_v2=geometric_signature_v2,
                )

                contribute_print(record)

                # Durable federation through the ONE contribution door —
                # canonical dedupe key + one vocabulary translation — so a
                # print that was also watched by a monitor or recorded via
                # record_print_outcome ships once.  (The old key here was
                # timestamp-minted, so it could never dedupe against
                # anything, including a replay of itself.)
                try:
                    from kiln import community_outbox
                    community_outbox.contribute_print_outcome(
                        outcome=outcome,
                        geometric_signature=geometric_signature,
                        geometric_signature_v2=geometric_signature_v2 or None,
                        job_id=job_id,
                        printer_model=printer_model,
                        material=material,
                        print_time_seconds=print_time_seconds,
                        extra=record.to_dict(),
                    )
                except Exception:
                    pass  # Never let sync failure affect local contribution

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
        def get_community_insight(
            geometric_signature: str, geometric_signature_v2: str = ""
        ) -> dict:
            """Get aggregated print data for a model geometry.

            Two layers, and the tool always returns whatever it can get:

            * ``insight`` — what THIS install has printed of this
              geometry: success rate, printers, materials, settings,
              failure modes.  Always available, no account needed.
            * ``community`` — the same picture across everyone who has
              printed this shape, so you can start from what already
              worked.  Community insights come with Kiln Pro
              (https://kiln3d.com/pricing); without them ``community``
              reports that it isn't available and the local layer is
              unaffected.

            Never fails because of the network or the plan: no
            connection, no account, or a plan without community
            insights all still return the local answer.

            Args:
                geometric_signature: Geometric signature to look up
                    (``fingerprint_model``'s ``geometric_signature``).
                geometric_signature_v2: The same mesh's
                    ``geometric_signature_v2``, when known.  Supplying it
                    narrows the answer to THIS design: without it, prints
                    of a different design that happens to share the older
                    signature can be counted into the result.
            """
            import kiln.server as _srv

            try:
                from kiln.community_registry import (
                    get_community_insight as _get_insight,
                )

                local = _get_insight(
                    geometric_signature,
                    geometric_signature_v2=geometric_signature_v2,
                )
                local_dict = local.to_dict() if local is not None else None

                community_dict = None
                try:
                    from kiln.community_sync import (
                        fetch_community_insight_for_signature,
                    )

                    community_dict = fetch_community_insight_for_signature(
                        geometric_signature,
                        geometric_signature_v2=geometric_signature_v2,
                    )
                except Exception:
                    # A community read never costs the caller their local
                    # answer — this is enrichment, not the substance.
                    _logger.debug(
                        "Community insight unavailable", exc_info=True,
                    )

                result: dict = {
                    "success": True,
                    "has_data": bool(local_dict or community_dict),
                    "community": _community_layer(community_dict),
                }
                if local_dict is not None:
                    result["insight"] = local_dict
                if community_dict is not None:
                    result["community_insight"] = community_dict
                if not result["has_data"]:
                    result["message"] = (
                        "No print data found for this geometry yet."
                    )
                return result
            except Exception as exc:
                _logger.exception("Unexpected error in get_community_insight")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def community_stats() -> dict:
            """Get overall print-registry statistics.

            ``stats`` covers this install: total records, unique models,
            printers, materials, and overall success rate.  ``community``
            adds the size of the shared pool everyone contributes to —
            counts only, available to anyone signed in.

            Works offline: the local numbers are always returned.
            """
            import kiln.server as _srv

            try:
                from kiln.community_registry import get_community_stats

                result: dict = {
                    "success": True,
                    "stats": get_community_stats().to_dict(),
                }
                try:
                    from kiln.community_sync import fetch_community_corpus_stats

                    corpus = fetch_community_corpus_stats()
                except Exception:
                    corpus = None
                    _logger.debug("Community stats unavailable", exc_info=True)
                if corpus is not None:
                    result["community"] = {"available": True, **corpus}
                else:
                    from kiln.tiers_and_terms import signin_hint_fields

                    result["community"] = {
                        "available": False,
                        # ``None`` covers two different worlds and this copy
                        # must not pick one for the user: a signed-out or
                        # offline machine, OR a reachable pool that simply
                        # has nothing in it yet.  Telling a signed-in user
                        # with an empty pool to "sign in" reports the
                        # product's own quiet as the user's fault.
                        "note": (
                            "Community totals aren't available right now — "
                            "either this machine isn't signed in to Kiln, "
                            "or the shared pool has no prints to report "
                            "yet. Signing in is free and takes a few "
                            "seconds; if you're already signed in, there's "
                            "nothing to fix on your side."
                        ),
                        **signin_hint_fields(),
                    }
                return result
            except Exception as exc:
                _logger.exception("Unexpected error in community_stats")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def recommend_material(
            intent: str,
            has_enclosure: bool = False,
            has_heated_bed: bool = True,
            budget_usd: float | None = None,
            printer_id: str = "",
            on_hand_only: bool = False,
        ) -> dict:
            """Recommend material from intent + printer capabilities (considers enclosure, bed, budget).

            Uses printer DNA + historical data to translate natural language
            intent (e.g. ``"make it strong"``, ``"make it pretty"``,
            ``"make it cheap"``) into an optimal material recommendation
            with settings.

            Pass ``printer_id`` to answer for a SPECIFIC machine — essential
            on a mixed fleet, where "what should I run this in" depends on
            which printer will run it.  The recommendation is then computed
            against that machine's nozzle state (abrasive materials on a
            brass nozzle get an explicit wear warning) and the response
            names the machine it answered for.

            Pass ``on_hand_only=True`` to recommend only from materials you
            physically have — recorded spools (``add_spool``) plus what's
            loaded on your machines (AMS/CFS sync).  Scoped to one printer
            this works on every tier; sweeping a multi-machine fleet in one
            call is a Kiln Business feature (https://kiln3d.com/pricing).  The recommendation's
            ``availability`` block then says WHERE the material is: which
            machine has it loaded, or that it's on the shelf and needs a
            spool swap first.  With ``printer_id`` the loaded half is
            scoped to that one machine (shelf spools always count — they
            can be swapped in); without it, every machine's load counts.
            When nothing on hand suits the request, the response returns
            the best catalog pick clearly labeled needs-purchase — it
            never silently widens to the catalog.  And when what you have
            works but a material you DON'T own fits the job materially
            better, the answer names that too, so "best of what you have"
            is never mistaken for "right for the job".

            **Which material tool to use:**

            - Quick intent-based pick for your own printer? → ``recommend_material`` (this tool)
            - Only from spools I actually own? → ``recommend_material(on_hand_only=True)``
            - Designing a part and need engineering specs? → ``recommend_design_material``
            - Ordering a print from a service? → ``suggest_material_for_order``
            - Which of MY printers has a material loaded? → ``find_printers_with_material``
            - What's loaded across the fleet right now? → ``get_fleet_material_summary``

            Args:
                intent: User intent text (e.g. ``"strong"``, ``"pretty"``).
                has_enclosure: Whether the printer has an enclosure.
                has_heated_bed: Whether the printer has a heated bed.
                budget_usd: Optional maximum budget per kg in USD.
                printer_id: Optional registered printer to answer for.
                    Empty = printer-agnostic recommendation.
                on_hand_only: Restrict candidates to recorded inventory
                    (loaded materials + shelf spools).  Default False =
                    full catalog.
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

                on_hand: list[dict[str, Any]] | None = None
                if on_hand_only:
                    from kiln.material_inventory import (
                        fleet_scope_verdict,
                        get_on_hand_materials,
                    )
                    from kiln.persistence import get_db

                    # Scoped to one machine, this is single-machine
                    # awareness and free on every tier.  Unscoped, it
                    # sweeps the whole fleet — the same cross-machine
                    # answer the fleet inventory tools sell, so it goes
                    # through the same shared gate.
                    if not printer_id and (
                        blocked := fleet_scope_verdict(
                            "Recommending across every machine you own"
                        )
                    ):
                        blocked["upgrade_hint"] = (
                            "Pass printer_id to answer for one machine on "
                            "your current plan, or see "
                            "https://kiln3d.com/pricing for fleet-wide answers."
                        )
                        return blocked

                    inventory = get_on_hand_materials(
                        get_db(), printer_name=printer_id or None
                    )
                    on_hand = [m.to_dict() for m in inventory]

                rec = _recommend(
                    intent,
                    printer_capabilities=caps,
                    budget_usd=budget_usd,
                    printer_id=printer_id,
                    on_hand=on_hand,
                )

                response = {"success": True, "recommendation": rec.to_dict()}
                # Say which machine the answer was computed for — an
                # unnamed printer context is an invisible assumption.
                if printer_id:
                    response["answered_for_printer"] = printer_id
                if on_hand_only:
                    response["on_hand_scope"] = (
                        f"printer:{printer_id}" if printer_id else "fleet"
                    )
                return response
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
