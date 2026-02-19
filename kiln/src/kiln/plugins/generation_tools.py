"""Model generation tools plugin.

Extracts text-to-3D model generation MCP tools from server.py into a focused
plugin module.  Provides tools for listing providers, submitting generation
jobs, polling status, downloading results, and running full generate-to-print
pipelines.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any

_logger = logging.getLogger(__name__)


class _GenerationToolsPlugin:
    """Text-to-3D model generation tools.

    Tools:
        - list_generation_providers
        - generate_model
        - generation_status
        - download_generated_model
        - await_generation
        - generate_and_print
        - validate_generated_mesh
    """

    @property
    def name(self) -> str:
        return "generation_tools"

    @property
    def description(self) -> str:
        return "Text-to-3D model generation tools (Meshy, OpenSCAD)"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register generation tools with the MCP server."""

        @mcp.tool()
        def list_generation_providers() -> dict:
            """List available text-to-3D generation providers.

            Returns details about each provider: name, description,
            available styles, and whether it requires an API key.
            Use this to discover providers before calling ``generate_model``.
            """
            import kiln.server as _srv

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
            ]
            return {
                "success": True,
                "providers": providers,
            }

        @mcp.tool()
        def generate_model(
            prompt: str,
            provider: str = "meshy",
            format: str = "stl",
            style: str | None = None,
        ) -> dict:
            """Generate a 3D model from a text description.

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
            import kiln.server as _srv
            from kiln.generation import GenerationAuthError, GenerationError

            if err := _srv._check_auth("generate"):
                return err
            try:
                gen = _srv._get_generation_provider(provider)
                job = gen.generate(prompt, format=format, style=style)
                return {
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
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to generate model (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(
                    f"Failed to generate model: {exc}", code=exc.code or "GENERATION_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in generate_model")
                return _srv._error_dict(
                    f"Unexpected error in generate_model: {exc}", code="INTERNAL_ERROR"
                )

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
            import kiln.server as _srv
            from kiln.generation import GenerationAuthError, GenerationError

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
                return _srv._error_dict(
                    f"Unexpected error in generation_status: {exc}", code="INTERNAL_ERROR"
                )

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
            import kiln.server as _srv
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

                # Auto-convert OBJ to STL for maximum slicer compatibility.
                if result.format == "obj":
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
                        _logger.info("Auto-converted OBJ to STL: %s", stl_path)
                    except Exception as exc:
                        _logger.warning("OBJ→STL conversion failed, keeping OBJ: %s", exc)

                # Validate the mesh if it's an STL or OBJ.
                validation = None
                dimensions = None
                if result.format in ("stl", "obj"):
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

                return {
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
                return _srv._error_dict(
                    f"Unexpected error in download_generated_model: {exc}", code="INTERNAL_ERROR"
                )

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
            import kiln.server as _srv
            from kiln.generation import GenerationAuthError, GenerationError, GenerationStatus

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
                return _srv._error_dict(
                    f"Failed to await generation: {exc}", code=exc.code or "GENERATION_ERROR"
                )
            except Exception as exc:
                _logger.exception("Unexpected error in await_generation")
                return _srv._error_dict(
                    f"Unexpected error in await_generation: {exc}", code="INTERNAL_ERROR"
                )

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
            import kiln.server as _srv
            from kiln.generation import (
                GenerationAuthError,
                GenerationError,
                GenerationResult,
                GenerationStatus,
                convert_to_stl,
                validate_mesh,
            )
            from kiln.printers import PrinterError, PrinterNotFoundError

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

                # Step 3.5: Auto-convert OBJ → STL
                if result.format == "obj":
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
                        _logger.warning("OBJ→STL conversion failed, keeping OBJ: %s", exc)

                # Step 4: Validate
                if result.format in ("stl", "obj"):
                    val = validate_mesh(result.local_path)
                    if not val.valid:
                        return _srv._error_dict(
                            f"Generated mesh failed validation: {'; '.join(val.errors)}",
                            code="VALIDATION_FAILED",
                        )

                # Step 5: Slice
                from kiln.slicer import slice_file

                effective_printer_id, effective_profile = (
                    _srv._resolve_slice_profile_context(
                        profile=profile,
                        printer_id=printer_id,
                    )
                )
                slice_result = slice_file(
                    result.local_path,
                    profile=effective_profile,
                )

                # Step 6: Upload (but do NOT auto-start — require explicit start_print)
                if printer_name:
                    adapter = _srv._registry.get(printer_name)
                else:
                    adapter = _srv._get_adapter()

                upload = adapter.upload_file(slice_result.output_path)
                file_name = upload.file_name or os.path.basename(slice_result.output_path)

                # Compute dimensions for review
                gen_validation = None
                gen_dimensions = None
                if result.format in ("stl", "obj"):
                    val_result = validate_mesh(result.local_path)
                    gen_validation = val_result.to_dict()
                    if val_result.bounding_box:
                        bb = val_result.bounding_box
                        w = bb.get("x_max", 0) - bb.get("x_min", 0)
                        d = bb.get("y_max", 0) - bb.get("y_min", 0)
                        h = bb.get("z_max", 0) - bb.get("z_min", 0)
                        gen_dimensions = {
                            "width_mm": round(w, 2),
                            "depth_mm": round(d, 2),
                            "height_mm": round(h, 2),
                            "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
                        }

                # Auto-print only if the user has opted in via KILN_AUTO_PRINT_GENERATED.
                print_data = None
                auto_printed = False
                if _srv._AUTO_PRINT_GENERATED:
                    # Mandatory pre-flight safety gate before starting print.
                    pf = _srv.preflight_check()
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
                    print_result = adapter.start_print(file_name)
                    _srv._heater_watchdog.notify_print_started()
                    print_data = print_result.to_dict()
                    auto_printed = True

                resp: dict[str, Any] = {
                    "success": True,
                    "generation": result.to_dict(),
                    "slice": slice_result.to_dict(),
                    "upload": upload.to_dict(),
                    "file_name": file_name,
                    "printer_id": effective_printer_id,
                    "profile_path": effective_profile,
                    "validation": gen_validation,
                    "dimensions": gen_dimensions,
                    "experimental": True,
                    "auto_print_enabled": _srv._AUTO_PRINT_GENERATED,
                }

                if auto_printed:
                    resp["print"] = print_data
                    resp["safety_notice"] = (
                        "WARNING: Auto-print for generated models is enabled "
                        "(KILN_AUTO_PRINT_GENERATED=true). AI-generated models "
                        "are experimental and may damage printer hardware. "
                        "Disable this setting unless you accept the risk."
                    )
                    resp["message"] = (
                        f"Generated '{prompt[:80]}' via {gen.display_name}, sliced, and started printing (auto-print ON)."
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

                return resp
            except GenerationAuthError as exc:
                return _srv._error_dict(
                    f"Failed to generate and print (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
                    code="AUTH_ERROR",
                )
            except GenerationError as exc:
                return _srv._error_dict(
                    f"Failed to generate and print: {exc}", code=exc.code or "GENERATION_ERROR"
                )
            except PrinterNotFoundError:
                return _srv._error_dict(
                    f"Printer {printer_name!r} not found.", code="NOT_FOUND"
                )
            except (PrinterError, RuntimeError) as exc:
                return _srv._error_dict(
                    f"Failed to generate and print: {exc}. Check printer connection and slicer availability."
                )
            except Exception as exc:
                _logger.exception("Unexpected error in generate_and_print")
                return _srv._error_dict(
                    f"Unexpected error in generate_and_print: {exc}", code="INTERNAL_ERROR"
                )

        @mcp.tool()
        def validate_generated_mesh(file_path: str) -> dict:
            """Validate a 3D mesh file for printing readiness.

            Checks that the file is a valid STL or OBJ, has reasonable
            dimensions, an acceptable polygon count, and is manifold
            (watertight).

            Args:
                file_path: Path to an STL or OBJ file.
            """
            import kiln.server as _srv
            from kiln.generation import validate_mesh

            try:
                result = validate_mesh(file_path)
                return {
                    "success": True,
                    "validation": result.to_dict(),
                    "message": (
                        "Mesh is valid."
                        if result.valid
                        else f"Mesh has issues: {'; '.join(result.errors)}"
                    ),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in validate_generated_mesh")
                return _srv._error_dict(
                    f"Unexpected error in validate_generated_mesh: {exc}", code="INTERNAL_ERROR"
                )

        _logger.debug("Registered generation tools")


plugin = _GenerationToolsPlugin()
