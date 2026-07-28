"""STEP file import tools plugin.

Provides MCP tools for importing STEP/STP CAD files, checking backend
availability, and extracting file metadata without full conversion.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

# Eager, unlike the conversion imports below, which stay lazy for plugin
# load time.  An exception class referenced in an `except` clause has to be
# bound before the clause is evaluated: import it inside the `try` and a
# failing import would surface as a NameError from the handler instead of
# the real error.  kiln.step_import is stdlib-only at module level, so this
# costs nothing.
from kiln.step_import import NoBackendError

_logger = logging.getLogger(__name__)


class _StepToolsPlugin:
    """STEP file import and inspection tools.

    Tools:
        - import_step_file
        - check_step_support
        - step_file_info
    """

    @property
    def name(self) -> str:
        return "step_tools"

    @property
    def description(self) -> str:
        return "STEP/STP CAD file import, conversion to STL, and metadata extraction"

    def register(self, mcp: Any) -> None:
        """Register STEP import tools with the MCP server."""

        @mcp.tool()
        def import_step_file(
            file_path: str,
            output_format: str = "stl",
            merge_bodies: bool = True,
            output_dir: str | None = None,
        ) -> dict:
            """Import a STEP (.step/.stp) CAD file and convert it to STL for Kiln's mesh pipeline.

            Converts STEP files using available backends (FreeCAD, Gmsh, or
            CadQuery).  Multi-body STEP files can be merged into a single STL
            or split into separate files per body.

            Use ``check_step_support`` first to verify that a conversion
            backend is installed.  After conversion, use ``diagnose_mesh``
            or ``analyze_mesh_geometry`` to validate the output.

            If this returns ``code="NO_BACKEND"``, read the ``remedy`` field
            rather than guessing: when ``remedy.actionable_by_caller`` is
            True, tell the user to run ``remedy.command`` (``kiln
            install-step-backend``) — one command and it works.  When it is
            False the caller is on a hosted server and has nothing to
            install; say so plainly and suggest ``report_issue``.  Never
            hand a hosted user an install instruction.

            Args:
                file_path: Path to the STEP/STP file.
                output_format: Output format (currently only ``"stl"`` supported).
                merge_bodies: If True, merge all bodies into one STL.
                    If False, export each body as a separate file.
                output_dir: Directory for output files.  Defaults to
                    the STEP file's parent directory.
            """
            if output_format != "stl":
                return {
                    "error": f"Unsupported output format: {output_format!r}. Only 'stl' is currently supported.",
                    "code": "UNSUPPORTED_FORMAT",
                }

            try:
                from kiln.step_import import convert_step_to_stl

                result = convert_step_to_stl(
                    file_path,
                    output_dir=output_dir,
                    merge_bodies=merge_bodies,
                )

                response = {
                    "status": "ok",
                    "output_path": result.output_path,
                    "output_paths": result.output_paths,
                    "body_count": result.body_count,
                    "file_size_bytes": result.file_size_bytes,
                    "conversion_time_s": result.conversion_time_s,
                    "warnings": result.warnings,
                    "next_steps": [
                        "Run diagnose_mesh on the output to check for defects.",
                        "Run analyze_mesh_geometry for printability scoring.",
                        "Run slice_model to prepare for printing.",
                    ],
                }
                try:
                    from kiln_pro.plugins.git_render_tools import attach_inspect_bundle
                    return attach_inspect_bundle(response, level="quick")
                except ImportError:
                    return response

            except FileNotFoundError as exc:
                return {"error": str(exc), "code": "FILE_NOT_FOUND"}
            except ValueError as exc:
                return {"error": str(exc), "code": "INVALID_INPUT"}
            except NoBackendError as exc:
                # NOT a conversion error — the file was never opened.  Its own
                # code, so an agent can tell "your STEP is bad" (which the user
                # must fix) from "this install has no converter" (which is a
                # one-command fix, or ours to fix if they're on hosted).
                return {
                    "error": str(exc),
                    "code": "NO_BACKEND",
                    "remedy": exc.remedy,
                }
            except Exception as exc:
                _logger.error("STEP import failed: %s", exc, exc_info=True)
                return {"error": str(exc), "code": "CONVERSION_ERROR"}

        @mcp.tool()
        def check_step_support() -> dict:
            """Check which STEP import backends are available on this system.

            Returns a dict listing each backend (FreeCAD, Gmsh, CadQuery)
            with its availability status and priority.  If none is found,
            includes ``install_help`` (prose) and ``remedy`` (structured) —
            prefer ``remedy``: its ``actionable_by_caller`` flag tells you
            whether the user can fix this (``kiln install-step-backend``) or
            whether they're on a hosted server where they cannot.

            Call this before ``import_step_file`` to verify the system is
            ready for STEP conversion.
            """
            from kiln.step_import import check_step_support as _check

            info = _check()
            return {
                "status": "ok" if info["any_available"] else "no_backend",
                **info,
            }

        @mcp.tool()
        def step_file_info(file_path: str) -> dict:
            """Extract metadata from a STEP file without converting it.

            Parses the STEP ASCII header to extract product names, estimated
            body count, file schema, and other metadata.  This is fast and
            requires no external backend.

            Use this to inspect a STEP file before deciding whether to
            import it.

            Args:
                file_path: Path to the STEP/STP file.
            """
            try:
                from kiln.step_import import get_step_metadata

                metadata = get_step_metadata(file_path)
                return {"status": "ok", **metadata}

            except FileNotFoundError as exc:
                return {"error": str(exc), "code": "FILE_NOT_FOUND"}
            except ValueError as exc:
                return {"error": str(exc), "code": "INVALID_INPUT"}
            except Exception as exc:
                _logger.error("STEP metadata extraction failed: %s", exc)
                return {"error": str(exc), "code": "PARSE_ERROR"}


plugin = _StepToolsPlugin()
