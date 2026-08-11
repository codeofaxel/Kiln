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
            output_format: str = "auto",
            merge_bodies: bool = True,
            output_dir: str | None = None,
        ) -> dict:
            """Import a STEP (.step/.stp) CAD file and convert it for Kiln's mesh pipeline.

            Converts STEP files using whichever backend is available (the
            OCCT kernel that ``kiln install-step-backend`` sets up, or an
            existing FreeCAD / Gmsh / CadQuery install).

            **The output format follows what the file carries.**  A plain
            single-solid STEP becomes an STL.  A STEP with part colours or
            multiple bodies becomes ONE 3MF that keeps each part's colour,
            name, and position — so a coloured CAD assembly arrives ready
            for a multi-material print instead of flattened grey.  Pass
            ``output_format="stl"`` or ``"3mf"`` to force either.

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
                output_format: ``"auto"`` (default — 3MF when colour or
                    multiple bodies are present, else STL), ``"stl"``, or
                    ``"3mf"``.
                merge_bodies: STL output only: if True, merge all bodies
                    into one STL; if False, export each body separately.
                    (3MF keeps bodies as separate named objects either way.)
                output_dir: Directory for output files.  Defaults to
                    the STEP file's parent directory.
            """
            if output_format not in ("auto", "stl", "3mf"):
                return {
                    "error": (
                        f"Unsupported output format: {output_format!r}. "
                        "Use 'auto', 'stl', or '3mf'."
                    ),
                    "code": "UNSUPPORTED_FORMAT",
                }

            try:
                from kiln.step_import import convert_step

                result = convert_step(
                    file_path,
                    output_dir=output_dir,
                    merge_bodies=merge_bodies,
                    output_format=output_format,
                )

                response = {
                    "status": "ok",
                    "output_path": result.output_path,
                    "output_paths": result.output_paths,
                    "output_format": result.output_format,
                    "body_count": result.body_count,
                    "part_names": result.part_names,
                    "part_colors": result.part_colors,
                    "file_size_bytes": result.file_size_bytes,
                    "conversion_time_s": result.conversion_time_s,
                    "warnings": result.warnings,
                    "next_steps": [
                        "Run diagnose_mesh on the output to check for defects.",
                        "Run analyze_mesh_geometry for printability scoring.",
                        "Run slice_model to prepare for printing.",
                    ],
                }
                # The analytic truth behind the triangles just made: a stage
                # that gets it labels the model as CAD ("1 solid, 4 true
                # cylinders, r=45.000 exact") instead of passing the
                # tessellation off as the geometry.  The census engine lives
                # in kiln-pro, so this is optional in exactly the way the
                # inspect bundle below is: present when kiln-pro is, absent
                # otherwise, and never a cost to the conversion either way.
                # Without it the import still succeeds and the stage renders
                # the mesh as it always has — it just says nothing about the
                # B-rep, which is the honest answer when nothing measured it.
                try:
                    from kiln_pro.step_facts import read_step_facts

                    response["cad_facts"] = read_step_facts(file_path)
                except Exception:  # noqa: BLE001 — display material only
                    _logger.debug("step facts skipped", exc_info=True)
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

            Returns a dict listing each backend (FreeCAD, Gmsh, the OCCT
            kernel, CadQuery) with its availability status and priority.
            If none is found,
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
