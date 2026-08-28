"""AI generation pipeline tools plugin.

Extracts AI-generation MCP tools from server.py into a focused plugin
module.  Covers text-to-3D, image-to-3D, generation job management,
and full generate-to-print pipelines.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any

from kiln.print_start_verdict import resolve_print_start

_logger = logging.getLogger(__name__)


class _GenerationAIToolsPlugin:
    """AI generation pipeline tools.

    Tools:
        - list_generation_providers
        - generate_model
        - generate_model_from_image
        - generation_status
        - download_generated_model
        - await_generation
        - generate_and_print
        - smart_generate_from_template
        - generate_template_variations
    """

    @property
    def name(self) -> str:
        return "generation_ai_tools"

    @property
    def description(self) -> str:
        return (
            "AI generation pipeline tools — text-to-3D, image-to-3D, "
            "generation job management, and generate-to-print pipelines"
        )

    def register(self, mcp: Any) -> None:  # noqa: C901, PLR0915
        """Register generation AI tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # list_generation_providers
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_generation_providers() -> dict:
            """List available text-to-3D generation providers.

            Returns details about each provider: name, description,
            available styles, and whether it requires an API key.
            Use this to discover providers before calling ``generate_model``.
            """
            providers = [
                {
                    "name": "meshy",
                    "display_name": "Meshy",
                    "description": (
                        "Cloud AI text-to-3D.  Generates 3D models from natural "
                        "language descriptions.  Requires KILN_MESHY_API_KEY."
                    ),
                    "requires_api_key": True,
                    "api_key_env": "KILN_MESHY_API_KEY",
                    "api_key_set": bool(_srv._MESHY_API_KEY),
                    "styles": ["realistic", "sculpture"],
                    "async": True,
                    "typical_time_seconds": 60,
                },
                {
                    "name": "openscad",
                    "display_name": "OpenSCAD",
                    "description": (
                        "Local parametric generation.  Prompt must be valid "
                        "OpenSCAD code.  Completes synchronously, no API key needed."
                    ),
                    "requires_api_key": False,
                    "styles": [],
                    "async": False,
                    "typical_time_seconds": 5,
                },
                {
                    "name": "gemini",
                    "display_name": "Gemini Deep Think",
                    "description": (
                        "AI-reasoned text-to-3D via Google Gemini.  Gemini deeply "
                        "reasons about geometry and produces OpenSCAD code, compiled "
                        "locally to STL.  Supports natural language and napkin-sketch "
                        "descriptions.  Requires KILN_GEMINI_API_KEY."
                    ),
                    "requires_api_key": True,
                    "api_key_env": "KILN_GEMINI_API_KEY",
                    "api_key_set": bool(_srv._GEMINI_API_KEY),
                    "styles": ["organic", "mechanical", "decorative"],
                    "async": False,
                    "typical_time_seconds": 30,
                },
                {
                    "name": "tripo3d",
                    "display_name": "Tripo3D",
                    "description": (
                        "Cloud text-to-3D generation via Tripo3D. Produces high-detail "
                        "meshes with async job polling. Requires KILN_TRIPO3D_API_KEY."
                    ),
                    "requires_api_key": True,
                    "api_key_env": "KILN_TRIPO3D_API_KEY",
                    "api_key_set": bool(os.environ.get("KILN_TRIPO3D_API_KEY", "").strip()),
                    "styles": [],
                    "async": True,
                    "typical_time_seconds": 90,
                },
                {
                    "name": "stability",
                    "display_name": "Stability AI",
                    "description": (
                        "Synchronous text-to-3D generation via Stability AI. Returns "
                        "GLB output directly. Requires KILN_STABILITY_API_KEY."
                    ),
                    "requires_api_key": True,
                    "api_key_env": "KILN_STABILITY_API_KEY",
                    "api_key_set": bool(os.environ.get("KILN_STABILITY_API_KEY", "").strip()),
                    "styles": [],
                    "async": False,
                    "typical_time_seconds": 60,
                },
            ]
            return {
                "success": True,
                "providers": providers,
            }

        # ------------------------------------------------------------------
        # generate_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def generate_model(
            prompt: str,
            provider: str = "meshy",
            format: str = "stl",
            style: str | None = None,
            material: str = "",
        ) -> dict:
            """Generate a 3D model from a text prompt via external AI API (Meshy/etc).

            Pass ``material`` when the user has named one ("print this in
            TPU") — it steers the design-intelligence prompt enrichment
            toward that material's constraints.  It is a design hint, not a
            slicing setting; leave it empty when the material is undecided.

            Start here if user has no template/image — just a text description.
            For image-based generation, use ``generate_model_from_image``.
            For parametric templates (local, no AI API needed), use ``generate_from_template``.
            To also slice + upload in one step, use ``generate_and_print``.

            **EXPERIMENTAL:** AI-generated 3D models are experimental and may not
            be suitable for printing without manual review.  Generated geometry
            can have thin walls, non-manifold faces, floating islands, or
            dimensions that exceed printer build volume.  3D printers are delicate
            hardware — always validate the generated mesh before printing.

            **When possible, prefer downloading proven community models from
            marketplaces** (Thingiverse, MyMiniFactory) over generating new ones.
            Use generation for custom/unique objects only.

            Submits a generation job to the specified provider and returns a
            job ID for status tracking.  Use ``generation_status`` to poll for
            completion, then ``download_generated_model`` to retrieve the file.

            **Prompt tips for Meshy (text-to-3D AI):**
            - Describe the physical object clearly: shape, size, purpose.
            - Include material cues: "wooden", "metallic", "smooth plastic".
            - Specify printability: "solid base", "no overhangs", "flat bottom".
            - Keep prompts under 200 words for best results (max 600 chars).
            - Good example: "A phone stand with a curved cradle, flat rectangular
              base, and angled back support. Smooth plastic surface."
            - Bad example: "make me something cool" (too vague).

            **For OpenSCAD**, the prompt must be valid OpenSCAD code.  The job
            completes synchronously and the result is immediately available.

            Args:
                prompt: Text description (or OpenSCAD code for ``openscad``).
                provider: Generation backend — ``"meshy"`` (cloud AI) or
                    ``"openscad"`` (local parametric).  Default: ``"meshy"``.
                format: Desired output format (``"stl"``).  Default: ``"stl"``.
                style: Optional style hint (``"realistic"`` or ``"sculpture"``
                    for Meshy).  Ignored by OpenSCAD.
            """
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
            )

            if err := _srv._check_auth("generate"):
                return err
            try:
                gen = _srv._get_generation_provider(provider)

                # Auto-enrich prompts with design intelligence (skip for OpenSCAD
                # which takes raw code, not natural language).
                enrichment_info: dict[str, Any] | None = None
                if provider != "openscad":
                    try:
                        from kiln.generation_feedback import (
                            enhance_prompt_with_design_intelligence,
                            get_provider_prompt_limit,
                        )

                        max_len = get_provider_prompt_limit(provider)
                        improved = enhance_prompt_with_design_intelligence(
                            prompt,
                            material=material or None,
                            printer_model=_srv._PRINTER_MODEL or None,
                            max_length=max_len,
                        )
                        if improved.constraints_added:
                            enrichment_info = {
                                "constraints_applied": len(improved.constraints_added),
                                "constraints": improved.constraints_added,
                            }
                            prompt = improved.improved_prompt
                    except Exception:
                        _logger.debug("Design intelligence enrichment unavailable", exc_info=True)

                job = gen.generate(prompt, format=format, style=style)
                result_dict: dict[str, Any] = {
                    "success": True,
                    "job": job.to_dict(),
                    "experimental": True,
                    "safety_notice": (
                        "AI-generated models are experimental. Always validate "
                        "the mesh with validate_generated_mesh and review "
                        "dimensions before printing. Generated models may require "
                        "manual refinement."
                    ),
                    "message": f"Generation job submitted to {gen.display_name}.",
                }
                if enrichment_info:
                    result_dict["design_intelligence"] = enrichment_info

                # Telemetry: count generation
                try:
                    from kiln.daily_stats import record_event
                    record_event("generations")
                except Exception:
                    pass

                # No autofire bundle here — generate_model submits an
                # ASYNC job and the response carries only the job
                # descriptor, not a mesh path.  The mesh becomes
                # available downstream via ``download_generated_model``,
                # which IS autofire-wired so the bundle attaches at the
                # point where the mesh actually exists.
                return result_dict
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to generate model (auth): {exc}. Check your provider API key is set (KILN_MESHY_API_KEY, KILN_GEMINI_API_KEY).",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(f"Failed to generate model: {exc}", code=exc.code or "GENERATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in generate_model")
                return _srv._error_dict(f"Unexpected error in generate_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # generate_model_from_image
        # ------------------------------------------------------------------

        @mcp.tool()
        def generate_model_from_image(
            image_url: str,
            provider: str = "meshy",
            style: str | None = None,
        ) -> dict:
            """Make a 3D model from a reference image.

            DEFAULT (keyless): you (the agent) can SEE the image — study it
            and write the OpenSCAD yourself, then compile_scad. You do NOT
            need an image-to-3D provider, and with no key configured this
            tool hands the job back to your vision instead of erroring.

            OPT-IN cloud path: when the user has set their OWN
            KILN_MESHY_API_KEY, this submits the image to Meshy for an AI mesh
            reconstruction. The image should show the object clearly against a
            clean background for best results.

            **EXPERIMENTAL:** AI-generated models are experimental.  Always
            validate the mesh before printing.

            **Image tips:**
            - Use a clear, well-lit photo of the object.
            - Plain/solid backgrounds produce better results.
            - Show the full object — avoid cropped or partial views.
            - Multiple angles are not supported; use the best single view.

            Args:
                image_url: URL to the reference image (PNG, JPG).  Must be
                    publicly accessible.
                provider: Generation provider.  Currently only ``"meshy"``
                    supports image-to-3D.
                style: Optional style hint (``"realistic"`` or ``"sculpture"``).
            """
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
            )

            if err := _srv._check_auth("generate"):
                return err
            # Keyless default: you (the agent) can SEE the image — study it
            # and write the OpenSCAD to match.  Cloud image-to-3D needs the
            # user's OWN key and is opt-in only; without a key we hand the job
            # back to your vision rather than nagging for a provider.
            import os

            if not os.environ.get("KILN_MESHY_API_KEY", "").strip():
                return {
                    "success": True,
                    "needs_agent_vision": True,
                    "image_url": image_url,
                    "message": (
                        "No image-to-3D provider is configured — and you don't "
                        "need one. You can SEE this image: study it and write "
                        "the OpenSCAD to match the object, then compile it with "
                        "compile_scad (free, no key). Show the user a preview "
                        "and iterate. Cloud image-to-3D (Meshy) is an OPT-IN "
                        "that needs the user's own KILN_MESHY_API_KEY — never "
                        "required."
                    ),
                }
            if provider != "meshy":
                return _srv._error_dict(
                    f"Image-to-3D is only supported by the 'meshy' provider, got {provider!r}.",
                    code="UNSUPPORTED_PROVIDER",
                )
            try:
                gen = _srv._get_generation_provider(provider)
                job = gen.generate("", format="stl", style=style, image_url=image_url)

                # Telemetry: count generation
                try:
                    from kiln.daily_stats import record_event
                    record_event("generations")
                except Exception:
                    pass

                response = {
                    "success": True,
                    "job": job.to_dict(),
                    "experimental": True,
                    "safety_notice": (
                        "AI-generated models from images are experimental. "
                        "Dimensional accuracy is not guaranteed — always validate "
                        "the mesh and check dimensions before printing."
                    ),
                    "message": f"Image-to-3D job submitted to {gen.display_name}.",
                }
                # No autofire bundle here — generate_model_from_image
                # submits an ASYNC job and the response carries only
                # the job descriptor, not a mesh path.  The mesh
                # becomes available downstream via
                # ``download_generated_model``, which IS autofire-wired.
                return response
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Image-to-3D provider auth failed: {exc}. You don't need a "
                    "provider — study the image and write the OpenSCAD yourself, "
                    "then compile_scad. Cloud Meshy is opt-in via KILN_MESHY_API_KEY.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(f"Failed to generate from image: {exc}", code=exc.code or "GENERATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in generate_model_from_image")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # generation_status
        # ------------------------------------------------------------------

        @mcp.tool()
        def generation_status(
            job_id: str,
            provider: str = "meshy",
        ) -> dict:
            """Check the status of a model generation job.

            Args:
                job_id: Job ID returned by ``generate_model``.
                provider: Provider that owns the job (``"meshy"`` or ``"openscad"``).
            """
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
            )

            if err := _srv._check_auth("generate"):
                return err
            try:
                gen = _srv._get_generation_provider(provider)
                job = gen.get_job_status(job_id)
                return {
                    "success": True,
                    "job": job.to_dict(),
                }
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to check generation status (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(
                    f"Failed to check generation status: {exc}", code=exc.code or "GENERATION_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in generation_status")
                return _srv._error_dict(f"Unexpected error in generation_status: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # download_generated_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def download_generated_model(
            job_id: str,
            provider: str = "meshy",
            output_path: str | None = None,
        ) -> dict:
            """Download a completed generated model and optionally validate it.

            Args:
                job_id: Job ID of a completed generation job.
                provider: Provider that owns the job (``"meshy"`` or ``"openscad"``).
                output_path: Directory to save the file.  Defaults to
                    the system temp directory.
            """
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
                GenerationResult,
                convert_to_stl,
                validate_mesh,
            )

            if err := _srv._check_auth("generate"):
                return err
            output_dir = output_path or os.path.join(tempfile.gettempdir(), "kiln_generated")
            if disk_err := _srv._check_disk_space(output_dir):
                return disk_err
            try:
                gen = _srv._get_generation_provider(provider)
                result = gen.download_result(job_id, output_dir=output_dir)

                # Auto-convert OBJ/GLB to STL for maximum slicer compatibility.
                if result.format in ("obj", "glb"):
                    try:
                        stl_path = convert_to_stl(result.local_path)
                        result = GenerationResult(
                            job_id=result.job_id,
                            provider=result.provider,
                            local_path=stl_path,
                            format="stl",
                            file_size_bytes=os.path.getsize(stl_path),
                            prompt=result.prompt,
                        )
                        _logger.info("Auto-converted %s to STL: %s", result.format.upper(), stl_path)
                    except Exception as exc:
                        _logger.warning("%s→STL conversion failed, keeping original: %s", result.format.upper(), exc)

                # Validate the mesh if it's a supported format.
                validation = None
                dimensions = None
                if result.format in ("stl", "obj", "glb"):
                    val = validate_mesh(result.local_path)
                    validation = val.to_dict()
                    if val.bounding_box:
                        bb = val.bounding_box
                        w = bb.get("x_max", 0) - bb.get("x_min", 0)
                        d = bb.get("y_max", 0) - bb.get("y_min", 0)
                        h = bb.get("z_max", 0) - bb.get("z_min", 0)
                        dimensions = {
                            "width_mm": round(w, 2),
                            "depth_mm": round(d, 2),
                            "height_mm": round(h, 2),
                            "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
                        }

                response = {
                    "success": True,
                    "result": result.to_dict(),
                    "validation": validation,
                    "dimensions": dimensions,
                    "experimental": True,
                    "safety_notice": (
                        "AI-generated model. Inspect validation results and "
                        "dimensions carefully before printing. Generated geometry "
                        "may have thin walls, overhangs, or non-manifold faces "
                        "that can fail during printing or damage hardware."
                    ),
                    "message": f"Model downloaded to {result.local_path}.",
                }
                # Autofire bundle: this is the SYNC mesh-producing step
                # in the AI-generation flow (``generate_model`` submits
                # an async job; this tool downloads + converts the
                # completed result).  The mesh path is
                # ``result.local_path`` — explicit source_path since
                # the response nests the path under "result".
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response,
                        source_path=result.local_path,
                        level="quick",
                    )
                except ImportError:
                    return response
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to download generated model (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(
                    f"Failed to download generated model: {exc}", code=exc.code or "GENERATION_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in download_generated_model")
                return _srv._error_dict(f"Unexpected error in download_generated_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # await_generation
        # ------------------------------------------------------------------

        @mcp.tool()
        def await_generation(
            job_id: str,
            provider: str = "meshy",
            timeout: int = 600,
            poll_interval: int = 10,
        ) -> dict:
            """Wait for a generation job to complete and return the final status.

            Polls the provider until the job reaches a terminal state or the
            timeout is exceeded.  Useful for agents that want to block until
            a model is ready.

            Args:
                job_id: Job ID from ``generate_model``.
                provider: Provider that owns the job.
                timeout: Max seconds to wait for generation (default 600 = 10 min).
                poll_interval: Seconds between polls (default 10).
            """
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
                GenerationStatus,
            )

            if err := _srv._check_auth("generate"):
                return err
            try:
                gen = _srv._get_generation_provider(provider)
                start = time.time()
                progress_log: list[dict] = []

                while True:
                    elapsed = time.time() - start
                    if elapsed >= timeout:
                        return {
                            "success": True,
                            "outcome": "timeout",
                            "elapsed_seconds": round(elapsed, 1),
                            "message": f"Timed out after {timeout}s waiting for generation.",
                            "progress_log": progress_log[-20:],
                        }

                    job = gen.get_job_status(job_id)

                    progress_log.append(
                        {
                            "time": round(elapsed, 1),
                            "status": job.status.value,
                            "progress": job.progress,
                        }
                    )

                    if job.status == GenerationStatus.SUCCEEDED:
                        return {
                            "success": True,
                            "outcome": "completed",
                            "job": job.to_dict(),
                            "elapsed_seconds": round(elapsed, 1),
                            "progress_log": progress_log[-20:],
                        }
                    if job.status == GenerationStatus.FAILED:
                        return {
                            "success": True,
                            "outcome": "failed",
                            "job": job.to_dict(),
                            "error": job.error,
                            "elapsed_seconds": round(elapsed, 1),
                            "progress_log": progress_log[-20:],
                        }
                    if job.status == GenerationStatus.CANCELLED:
                        return {
                            "success": True,
                            "outcome": "cancelled",
                            "job": job.to_dict(),
                            "elapsed_seconds": round(elapsed, 1),
                            "progress_log": progress_log[-20:],
                        }

                    time.sleep(poll_interval)

            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to await generation (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(f"Failed to await generation: {exc}", code=exc.code or "GENERATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in await_generation")
                return _srv._error_dict(f"Unexpected error in await_generation: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # generate_and_print
        # ------------------------------------------------------------------

        @mcp.tool()
        def generate_and_print(
            prompt: str,
            provider: str = "meshy",
            style: str | None = None,
            printer_name: str | None = None,
            profile: str | None = None,
            printer_id: str | None = None,
            timeout: int = 600,
        ) -> dict:
            """Full pipeline: generate a model, validate, slice, and upload (preview).

            **EXPERIMENTAL:** This generates a 3D model, validates it, slices it,
            and uploads it to the printer — but does NOT start printing.  3D
            printers are delicate hardware and AI-generated models are not
            guaranteed to be safe or printable.  You MUST call ``start_print``
            separately after reviewing the preview results.

            When possible, prefer downloading proven models from marketplaces
            (Thingiverse, MyMiniFactory) instead of generating new ones.

            Args:
                prompt: Text description of the 3D model to generate.
                provider: Generation provider (``"meshy"`` or ``"openscad"``).
                style: Optional style hint for cloud providers.
                printer_name: Target printer.  Omit for the default printer.
                profile: Slicer profile path.
                printer_id: Optional printer model ID for bundled profile
                    auto-selection (e.g. ``"prusa_mini"``).
                timeout: Max seconds to wait for generation (default 600).
            """
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
                GenerationResult,
                GenerationStatus,
                convert_to_stl,
            )
            from kiln.printers.base import PrinterError
            from kiln.registry import PrinterNotFoundError

            if err := _srv._check_auth("print"):
                return err
            try:
                gen = _srv._get_generation_provider(provider)

                # Step 1: Generate
                job = gen.generate(prompt, format="stl", style=style)
                _logger.info("Generation job %s submitted to %s", job.id, gen.display_name)

                # Step 2: Wait for completion (skip polling for synchronous providers)
                if job.status != GenerationStatus.SUCCEEDED:
                    start = time.time()
                    while True:
                        elapsed = time.time() - start
                        if elapsed >= timeout:
                            return _srv._error_dict(
                                f"Generation timed out after {timeout}s.",
                                code="GENERATION_TIMEOUT",
                            )
                        job = gen.get_job_status(job.id)
                        if job.status == GenerationStatus.SUCCEEDED:
                            break
                        if job.status in (GenerationStatus.FAILED, GenerationStatus.CANCELLED):
                            return _srv._error_dict(
                                f"Generation {job.status.value}: {job.error or 'unknown error'}",
                                code="GENERATION_FAILED",
                            )
                        time.sleep(10)

                # Step 3: Download
                result = gen.download_result(job.id)

                # Step 3.5: Auto-convert OBJ/GLB -> STL
                if result.format in ("obj", "glb"):
                    try:
                        stl_path = convert_to_stl(result.local_path)
                        result = GenerationResult(
                            job_id=result.job_id,
                            provider=result.provider,
                            local_path=stl_path,
                            format="stl",
                            file_size_bytes=os.path.getsize(stl_path),
                            prompt=result.prompt,
                        )
                    except Exception as exc:
                        _logger.warning("%s->STL conversion failed: %s", result.format.upper(), exc)

                # Step 4: Comprehensive validation pipeline.
                #
                # AI-generated meshes go through Kiln's full pre-print
                # validation gate -- format check, mesh analysis, auto-
                # scale (catches the AI-models-in-meters bug), watertight
                # check, auto-repair, printability scoring, support
                # assessment, structural check, bed-fit, and material
                # check.  Same gate the direct slice_and_print path uses,
                # so AI users get the same engineering review as user-
                # supplied STLs -- not a weaker subset.
                pipeline_result = None
                _build_vol = None
                bed_size_source = None
                bed_size_model_id = None
                if result.format in ("stl", "obj", "glb"):
                    from kiln.plugins.validation_pipeline_tools import (
                        run_full_validation_pipeline,
                    )
                    from kiln.printers.bed_fit import resolve_build_volume

                    # Resolve build volume from printer if available
                    # (used for the bed_dims_mm field in the response;
                    # the validation pipeline itself does its own bed-fit
                    # check via printer_id).
                    try:
                        _adapter = _srv._resolve_adapter(printer_name)
                        _printer_info = _adapter.get_printer_info()
                        bv = getattr(_printer_info, "build_volume", None)
                        if isinstance(bv, dict) and bv:
                            x = bv.get("x")
                            y = bv.get("y")
                            z = bv.get("z")
                            if x is not None and y is not None and z is not None:
                                _build_vol = (float(x), float(y), float(z))
                                bed_size_source = "adapter"
                    except Exception:
                        _logger.debug("Could not resolve build volume from printer", exc_info=True)
                    if _build_vol is None and (printer_id or printer_name):
                        resolved = resolve_build_volume(printer_id or printer_name)
                        if resolved:
                            bed_size_model_id, _build_vol = resolved
                            bed_size_source = "printer_intelligence"
                        elif printer_id:
                            return _srv._error_dict(
                                f"Unknown printer_id {printer_id!r}; use a supported "
                                "printer model id or omit printer_id and pass an "
                                "adapter with build-volume metadata.",
                                code="UNKNOWN_PRINTER_MODEL",
                            )

                    try:
                        pipeline_result = run_full_validation_pipeline(
                            result.local_path,
                            printer_id=printer_id or "",
                            material="PLA",
                        )
                    except Exception as exc:
                        _logger.error("Validation pipeline crashed: %s", exc, exc_info=True)
                        return _srv._error_dict(
                            f"Validation pipeline error: {exc}",
                            code="VALIDATION_ERROR",
                        )

                    if not pipeline_result.get("ready_to_print", False):
                        score = pipeline_result.get("printability_score", 0)
                        summary = pipeline_result.get(
                            "summary", "Generated mesh failed validation",
                        )
                        err_resp = _srv._error_dict(
                            f"Generated mesh failed pre-print validation "
                            f"(score {score}/100): {summary}",
                            code="VALIDATION_FAILED",
                        )
                        err_resp["validation"] = pipeline_result
                        return err_resp

                    # Use the (possibly repaired/scaled) validated path
                    # going forward.
                    validated_path = (
                        pipeline_result.get("validated_path")
                        or result.local_path
                    )
                    result = GenerationResult(
                        job_id=result.job_id,
                        provider=result.provider,
                        local_path=validated_path,
                        format="stl",
                        file_size_bytes=os.path.getsize(validated_path),
                        prompt=result.prompt,
                    )

                # Step 5: Slice
                from kiln.slicer import slice_file

                effective_printer_id, effective_profile = _srv._resolve_slice_profile_context(
                    profile=profile,
                    printer_id=printer_id,
                )

                # Printer's own start routine (kiln-pro handoff) — same seam
                # as slice_and_print, so a generated model warms up exactly
                # like an uploaded one.
                start_handoff: str | None = None
                try:
                    _sg_adapter = _srv._resolve_adapter(printer_name)
                except Exception:
                    _sg_adapter = None
                if _sg_adapter is not None and effective_printer_id:
                    from kiln.slicer_profiles import (
                        resolve_slicer_profile,
                        start_gcode_override_from_printer,
                    )

                    _sg_patch, _sg_reason = start_gcode_override_from_printer(
                        _sg_adapter, effective_printer_id, None
                    )
                    if _sg_patch:
                        try:
                            effective_profile = resolve_slicer_profile(
                                effective_printer_id, overrides=_sg_patch
                            )
                            start_handoff = _sg_reason.removeprefix("handoff:")
                        except Exception:
                            _logger.debug("handoff re-resolve failed", exc_info=True)

                slice_result = slice_file(
                    result.local_path,
                    profile=effective_profile,
                )

                # Step 6: Upload (but do NOT auto-start — require explicit start_print)
                # Same door as the control verbs: config.yaml fallback included.
                adapter = _srv._resolve_adapter(printer_name)

                upload = adapter.upload_file(slice_result.output_path)
                file_name = upload.file_name or os.path.basename(slice_result.output_path)

                # Use pipeline results for response (already computed above)
                gen_validation = pipeline_result if pipeline_result else None
                gen_dimensions = None
                if pipeline_result:
                    _dims = (
                        pipeline_result.get("model_info", {}).get("dimensions_mm")
                        or {}
                    )
                    # The new pipeline reports dims as x/y/z (or
                    # width_mm/depth_mm/height_mm — handle both shapes).
                    w = float(_dims.get("x", _dims.get("width_mm", 0)) or 0)
                    d = float(_dims.get("y", _dims.get("depth_mm", 0)) or 0)
                    h = float(_dims.get("z", _dims.get("height_mm", 0)) or 0)
                    if w or d or h:
                        gen_dimensions = {
                            "width_mm": round(w, 2),
                            "depth_mm": round(d, 2),
                            "height_mm": round(h, 2),
                            "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
                        }

                # Auto-print only if the user has opted in via KILN_AUTO_PRINT_GENERATED.
                print_data = None
                print_verdict = None
                auto_printed = False
                if _srv._AUTO_PRINT_GENERATED:
                    safety_printer = _srv._resolve_effective_printer_name(printer_name)
                    if block := _srv._emergency_latch_error("generate_and_print", safety_printer):
                        return block
                    # Mandatory pre-flight safety gate before starting print.
                    pf = _srv.preflight_check(printer_name=printer_name)
                    if not pf.get("ready", False):
                        _srv._audit(
                            "generate_and_print",
                            "preflight_failed",
                            details={
                                "file": file_name,
                                "summary": pf.get("summary", ""),
                            },
                        )
                        return _srv._error_dict(
                            pf.get("summary", "Pre-flight checks failed"),
                            code="PREFLIGHT_FAILED",
                        )
                    # Captured before the command so the verdict can tell a
                    # reading about THIS job from the printer's last word
                    # about the previous one.
                    # No preview gate here, deliberately: the object does
                    # not exist until this call runs, so no token could have
                    # been issued for it.  The standing opt-in
                    # (KILN_AUTO_PRINT_GENERATED, off by default) IS the
                    # consent; without it this tool uploads and makes the
                    # caller go through start_print, which does gate.
                    _srv._audit(
                        "generate_and_print",
                        "auto_printed_without_preview",
                        details={
                            "file": file_name,
                            "consent": "KILN_AUTO_PRINT_GENERATED",
                        },
                    )
                    sent_at = time.monotonic()
                    print_result = adapter.start_print(file_name)
                    _srv._note_print_started(adapter)
                    print_verdict = resolve_print_start(
                        adapter, print_result, sent_at=sent_at,
                        file_name=file_name,
                    )
                    print_data = print_verdict.to_dict()
                    auto_printed = True

                resp: dict[str, Any] = {
                    "success": print_verdict.ok if auto_printed else True,
                    "generation": result.to_dict(),
                    "slice": slice_result.to_dict(),
                    "upload": upload.to_dict(),
                    "file_name": file_name,
                    **(
                        {"start_gcode_source": f"{start_handoff} — the printer's own start routine"}
                        if start_handoff
                        else {}
                    ),
                    "printer_id": effective_printer_id,
                    "profile_path": effective_profile,
                    "validation": gen_validation,
                    "dimensions": gen_dimensions,
                    "experimental": True,
                    "auto_print_enabled": _srv._AUTO_PRINT_GENERATED,
                }
                if _build_vol is not None:
                    resp["bed_dims_mm"] = list(_build_vol)
                    resp["bed_size_source"] = bed_size_source
                    if bed_size_model_id:
                        resp["bed_size_model_id"] = bed_size_model_id

                if auto_printed:
                    resp["print"] = print_data
                    resp["print_start"] = print_verdict.state
                    resp["safety_notice"] = (
                        "WARNING: Auto-print for generated models is enabled "
                        "(KILN_AUTO_PRINT_GENERATED=true). AI-generated models "
                        "are experimental and may damage printer hardware. "
                        "Disable this setting unless you accept the risk."
                    )
                    if print_verdict.confirmed:
                        _tail = "and started printing (auto-print ON)."
                    elif print_verdict.ok:
                        _tail = (
                            "and sent the print command (auto-print ON). The "
                            "printer has not confirmed it is running yet — "
                            "call printer_status() to watch it start."
                        )
                    else:
                        _tail = (
                            f"but the printer did not start it. "
                            f"{print_verdict.message}"
                        )
                    resp["message"] = (
                        f"Generated '{prompt[:80]}' via {gen.display_name}, "
                        f"sliced, {_tail}"
                    )
                else:
                    resp["ready_to_print"] = True
                    resp["safety_notice"] = (
                        "Model generated, sliced, and uploaded but NOT started. "
                        "AI-generated models are experimental — review the "
                        "dimensions and validation results above. Call "
                        "start_print to begin printing after review. "
                        "Set KILN_AUTO_PRINT_GENERATED=true to enable auto-print."
                    )
                    resp["message"] = (
                        f"Generated '{prompt[:80]}' via {gen.display_name}, "
                        f"sliced, and uploaded. Call start_print('{file_name}') "
                        f"to begin printing after review."
                    )

                try:
                    from kiln_pro.plugins.git_render_tools import attach_inspect_bundle
                    return attach_inspect_bundle(
                        resp, source_path=result.local_path, level="quick",
                    )
                except ImportError:
                    return resp
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to generate and print (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(f"Failed to generate and print: {exc}", code=exc.code or "GENERATION_ERROR")
            except PrinterNotFoundError:
                return _srv._error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to generate and print: {exc}. Check printer connection and slicer availability."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in generate_and_print")
                return _srv._error_dict(f"Unexpected error in generate_and_print: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # smart_generate_from_template
        # ------------------------------------------------------------------

        @mcp.tool()
        def smart_generate_from_template(
            template_id: str,
            parameters: dict | None = None,
            material: str = "PLA",
            auto_reinforce: bool = False,
        ) -> dict:
            """Generate from template + structural analysis + print settings (recommended for functional parts).

            Higher-level than ``generate_from_template`` — adds structural risk analysis
            and auto-reinforcement. This is the **one-step design-to-print-ready** pipeline:

            1. Generates STL from a parametric template (like ``generate_from_template``)
            2. Runs structural risk analysis (thin necks, cantilevers, sharp corners)
            3. Optionally auto-applies reinforcements (fillets, wall thickening, etc.)
            4. Infers optimal slicer settings tuned to the design's structural profile
            5. Returns the STL path + recommended settings ready for slicing

            The agent can take the output and directly call ``reslice_with_overrides``
            or ``run_reslice_and_print`` with the recommended settings.

            :param template_id: Template ID from ``list_design_templates``.
            :param parameters: Parameter overrides (e.g., ``{"phone_width": 80}``).
            :param material: Filament type for settings inference (PLA, PETG, ABS, etc.).
            :param auto_reinforce: If True, auto-apply structural reinforcements.
            :returns: Dict with STL path, structural grade, reinforcements, and print settings.
            """
            if err := _srv._check_auth("generate"):
                return err
            try:
                # Step 1: Generate from template
                gen_result = _srv.generate_from_template(template_id, parameters)
                if not gen_result.get("success"):
                    return gen_result  # Pass through error

                stl_path = gen_result.get("result", {}).get("local_path", "")
                if not stl_path:
                    return _srv._error_dict("Template generated but no STL path returned")

                # Step 2: Structural analysis
                from kiln.design_reasoning import generate_improvement_plan
                from kiln.design_reasoning import infer_print_settings as _infer_settings

                plan = generate_improvement_plan(stl_path)

                # Step 3: Optional auto-reinforcement
                reinforcement_result = None
                if auto_reinforce and plan.reinforcements:
                    from kiln.design_reasoning import apply_reinforcements

                    reinf = apply_reinforcements(stl_path)
                    stl_path = reinf.output_path
                    reinforcement_result = reinf.to_dict()

                # Step 4: Infer slicer settings
                settings = _infer_settings(stl_path, material=material)

                response = {
                    "success": True,
                    "template": template_id,
                    "parameters_used": gen_result.get("parameters_used", {}),
                    "stl_path": stl_path,
                    "dimensions": gen_result.get("dimensions"),
                    "structural_analysis": {
                        "score": plan.overall_structural_score,
                        "grade": plan.structural_grade,
                        "risk_count": len(plan.risks),
                        "critical_count": plan.critical_count,
                        "warning_count": plan.warning_count,
                        "summary": plan.summary,
                    },
                    "reinforcement": reinforcement_result,
                    "recommended_print_settings": settings.to_dict(),
                    # This tool builds a fresh envelope rather than
                    # returning generate_from_template's, so anything
                    # that tool attaches has to be carried across by
                    # hand or it is silently dropped at this door.
                    **(
                        {"fastener_advice": gen_result["fastener_advice"]}
                        if gen_result.get("fastener_advice")
                        else {}
                    ),
                    "next_steps": (
                        f"STL ready at {stl_path}. "
                        f"Structural grade: {plan.structural_grade}. "
                        f"Recommended: {settings.perimeters} perimeters, "
                        f"{settings.infill_percent}% {settings.infill_pattern} infill, "
                        f"{settings.layer_height_mm}mm layers"
                        + (", supports enabled" if settings.support_enabled else "")
                        + (", brim enabled" if settings.brim_enabled else "")
                        + ". Use reslice_with_overrides() or run_reslice_and_print() "
                        "with these settings."
                    ),
                }
                try:
                    from kiln_pro.plugins.git_render_tools import attach_inspect_bundle
                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response
            except Exception as exc:
                return _srv._error_dict(f"Smart template generation failed: {exc}")

        # ------------------------------------------------------------------
        # generate_template_variations
        # ------------------------------------------------------------------

        @mcp.tool()
        def generate_template_variations(
            template_id: str,
            variation_count: int = 3,
            parameter_ranges: dict[str, list[float]] | None = None,
        ) -> dict:
            """Generate multiple variations of a parametric template.

            Creates N variations by sampling parameter values across their
            valid ranges.  Useful for exploring design space or offering
            choices to the user.

            :param template_id: Template ID (e.g. ``"phone_stand"``).
            :param variation_count: Number of variations (1-10, default 3).
            :param parameter_ranges: Optional overrides ``{param: [min, max]}``.
            :returns: Dict with list of generated variations and their files.
            """
            from kiln.generation import GenerationError

            if err := _srv._check_auth("generate"):
                return err
            import json
            from string import Template

            variation_count = max(1, min(10, variation_count))

            try:
                tpl_path = os.path.join(os.path.dirname(_srv.__file__), "data", "design_templates.json")
                with open(tpl_path) as fh:
                    templates = json.load(fh)

                tpl = templates.get(template_id)
                if not tpl:
                    available = [k for k in templates if not k.startswith("_")]
                    return _srv._error_dict(
                        f"Unknown template '{template_id}'. Available: {available}",
                        code="UNKNOWN_TEMPLATE",
                    )

                gen = _srv._get_generation_provider("openscad")
                variations: list[dict[str, Any]] = []

                for i in range(variation_count):
                    params: dict[str, Any] = {}
                    for pname, pdef in tpl.get("parameters", {}).items():
                        if pdef.get("type") == "string":
                            params[pname] = pdef["default"]
                            continue

                        # Use custom range or template defaults
                        if parameter_ranges and pname in parameter_ranges:
                            pmin, pmax = parameter_ranges[pname]
                        else:
                            pmin = pdef.get("min", pdef["default"] * 0.5)
                            pmax = pdef.get("max", pdef["default"] * 1.5)

                        # Evenly space across range for deterministic exploration
                        if variation_count == 1:
                            val = pdef["default"]
                        else:
                            t = i / (variation_count - 1)
                            val = pmin + t * (pmax - pmin)
                        params[pname] = round(val, 1)

                    scad_code = Template(tpl["scad_template"]).safe_substitute(params)
                    job = gen.generate(scad_code, format="stl")

                    var_entry: dict[str, Any] = {
                        "variation": i + 1,
                        "parameters": params,
                        "status": job.status.value,
                    }

                    if job.status.value == "succeeded":
                        dl = gen.download_result(job.id)
                        var_entry["file_path"] = dl.local_path
                        var_entry["file_size_bytes"] = dl.file_size_bytes

                    variations.append(var_entry)

                # One count per call, not per variant: the user asked
                # for this template once and got N renders of it.
                # Counting each variant would let a single call outweigh
                # a week of real builds.  This tool loads and renders
                # the template itself rather than going through
                # generate_from_template, so it needs its own entry --
                # a second door that would otherwise report nothing.
                if variations:
                    try:
                        from kiln.daily_stats import record_template_use

                        record_template_use(template_id)
                    except Exception:  # noqa: BLE001
                        pass  # telemetry never breaks a generation

                response = {
                    "success": True,
                    "template": template_id,
                    "variation_count": len(variations),
                    "variations": variations,
                }
                # Pick the first variation with a file_path for preview.
                first_path = next(
                    (v.get("file_path") for v in variations if v.get("file_path")),
                    None,
                )
                try:
                    from kiln_pro.plugins.git_render_tools import attach_inspect_bundle
                    return attach_inspect_bundle(
                        response, source_path=first_path, level="quick",
                    )
                except ImportError:
                    return response
            except GenerationError as exc:
                return _srv._error_dict(f"Variation generation failed: {exc}", code=exc.code or "GENERATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in generate_template_variations")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered generation AI tools")


plugin = _GenerationAIToolsPlugin()
