"""Multi-part assembly tools plugin.

Provides MCP tools for creating assemblies, adding parts and mating
interfaces, validating clearances, and composing multi-part models
into a single output.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _AssemblyToolsPlugin:
    """Multi-part assembly tools (clearance checking, joint validation, composition).

    Tools:
        - create_assembly
        - add_assembly_part
        - add_assembly_interface
        - validate_assembly
        - check_assembly_clearances
        - compose_assembly_parts
        - get_joint_recommendation
    """

    @property
    def name(self) -> str:
        return "assembly_tools"

    @property
    def description(self) -> str:
        return "Multi-part assembly tools (clearance checking, joint validation, composition)"

    def register(self, mcp: Any) -> None:
        """Register assembly tools with the MCP server."""

        @mcp.tool()
        def create_assembly(name: str) -> dict:
            """Create a new empty assembly.

            Returns the assembly state as a JSON-serialisable dict that
            must be passed back to subsequent assembly tools.

            Args:
                name: Human-readable name for the assembly.
            """
            try:
                from kiln.assembly import create_assembly as _create

                assembly = _create(name)
                return {"success": True, "data": assembly.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in create_assembly")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def add_assembly_part(
            assembly_json: str,
            part_id: str,
            file_path: str,
            position_x: float = 0.0,
            position_y: float = 0.0,
            position_z: float = 0.0,
            material: str = "PLA",
            role: str = "structural",
        ) -> dict:
            """Add a part to an existing assembly.

            Parses the assembly from its JSON representation, appends
            the new part, and returns the updated assembly state.

            Args:
                assembly_json: JSON string of the current assembly state
                    (as returned by create_assembly or a previous tool call).
                part_id: Unique identifier for this part within the assembly.
                file_path: Path to the STL/OBJ mesh file for the part.
                position_x: X position offset in mm (default 0.0).
                position_y: Y position offset in mm (default 0.0).
                position_z: Z position offset in mm (default 0.0).
                material: Filament material for the part (default ``"PLA"``).
                role: Structural role of the part (default ``"structural"``).
            """
            try:
                from kiln.assembly import Assembly, AssemblyPart

                assembly = Assembly.from_dict(json.loads(assembly_json))
                part = AssemblyPart(
                    part_id=part_id,
                    file_path=file_path,
                    position_mm=(position_x, position_y, position_z),
                    material=material,
                    role=role,
                )
                # Route through add_part so the duplicate-ID check and the
                # free-tier part limit apply here too — appending to the
                # list directly would bypass both.
                assembly.add_part(part)
                return {"success": True, "data": assembly.to_dict()}
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:
                _logger.exception("Unexpected error in add_assembly_part")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def add_assembly_interface(
            assembly_json: str,
            part_a_id: str,
            part_b_id: str,
            joint_type: str = "clearance_fit",
            clearance_mm: float = 0.2,
            magnet_polarity_aligned: bool | None = None,
            fastener: dict[str, Any] | str | None = None,
        ) -> dict:
            """Add a mating interface between two parts in an assembly.

            Defines how two parts connect (joint type and clearance),
            which is used during validation and clearance checking.  For
            screw/anchor-based joints, ``fastener`` may provide an
            explicit hardware spec so downstream manuals and BOM tools do
            not have to guess from clearance alone.

            Args:
                assembly_json: JSON string of the current assembly state.
                part_a_id: ID of the first part in the interface.
                part_b_id: ID of the second part in the interface.
                joint_type: Type of joint (default ``"clearance_fit"``).
                clearance_mm: Clearance gap in mm (default 0.2).  For
                    ``"interference_fit"`` pass a NEGATIVE value (the
                    part is intentionally larger than its socket);
                    interference geometry is the entire point.
                magnet_polarity_aligned: Only meaningful when
                    ``joint_type == "magnetic"``.  ``True`` declares
                    the designer has confirmed which poles face each
                    other in each magnet pocket; ``None`` means
                    unknown.  Downstream tooling refuses to ship a
                    hand-wavy "make sure they pull together"
                    instruction when polarity is unknown.
                fastener: Optional FastenerSpec as a dict or JSON object
                    string.  Supported keys include ``size``, ``family``,
                    ``length_mm``, ``length_range_mm``, ``head_type``,
                    ``drive_type``, ``surface_type``,
                    ``quantity_per_interface``, and ``notes``.
            """
            try:
                from kiln.assembly import Assembly, FastenerSpec, MatingInterface

                assembly = Assembly.from_dict(json.loads(assembly_json))
                fastener_spec = _parse_fastener_spec_arg(fastener, FastenerSpec)
                interface = MatingInterface(
                    part_a_id=part_a_id,
                    part_b_id=part_b_id,
                    joint_type=joint_type,
                    clearance_mm=clearance_mm,
                    magnet_polarity_aligned=magnet_polarity_aligned,
                    fastener_spec=fastener_spec,
                )
                assembly.interfaces.append(interface)
                return {"success": True, "data": assembly.to_dict()}
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
            except Exception as exc:
                _logger.exception("Unexpected error in add_assembly_interface")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def validate_assembly(
            assembly_json: str,
            *,
            printer_id: str | None = None,
        ) -> dict:
            """Validate an assembly for correctness and printability.

            Runs clearance checks and joint validations on the assembly,
            returning the validated assembly state with results populated.

            Args:
                assembly_json: JSON string of the current assembly state.
                printer_id: Optional printer identifier (e.g.
                    ``"bambu_a1"``).  When supplied AND kiln-pro is
                    installed, each joint that drives a screw into a
                    printed part gains a ``screw_hole`` block with the
                    compensated hole diameter, thread engagement,
                    install-torque ceiling, and lead-in chamfer
                    (Bambu-calibrated on Bambu X1/P1/A1 + PLA/PETG; a
                    generic starting point otherwise).  Omit it to keep
                    the historic behaviour.
            """
            try:
                from kiln.assembly import Assembly
                from kiln.assembly import validate_assembly as _validate

                assembly = Assembly.from_dict(json.loads(assembly_json))
                validated = _validate(assembly, printer_id=printer_id)
                response = {"success": True, "data": validated.to_dict()}
                # Surface the heuristic-grade upgrade nudge when the assembly
                # carries a load-bearing signal: an engineering-grade material,
                # a structural joint (press-fit / threaded), or a screw driven
                # into a printed part.
                from kiln.load_bearing_detector import (
                    attach_load_bearing_nudge,
                    load_bearing_signal,
                )

                structural = any(
                    load_bearing_signal(material=p.material) for p in assembly.parts
                ) or any(
                    load_bearing_signal(joint_type=i.joint_type)
                    or (
                        i.fastener_spec is not None
                        and getattr(i.fastener_spec, "surface_type", None) == "printed"
                    )
                    for i in assembly.interfaces
                )
                if structural:
                    attach_load_bearing_nudge(response, force=True)
                return response
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
            except Exception as exc:
                _logger.exception("Unexpected error in validate_assembly")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def check_assembly_clearances(
            assembly_json: str,
            default_clearance_mm: float = 0.2,
        ) -> dict:
            """Check clearances between all mating parts in an assembly.

            Returns a list of clearance check results indicating whether
            each interface meets its clearance requirements.

            Args:
                assembly_json: JSON string of the current assembly state.
                default_clearance_mm: Default clearance gap in mm to use
                    when an interface does not specify one (default 0.2).
            """
            try:
                from kiln.assembly import Assembly, check_all_clearances

                assembly = Assembly.from_dict(json.loads(assembly_json))
                checks = check_all_clearances(
                    assembly,
                    default_clearance_mm=default_clearance_mm,
                )
                return {
                    "success": True,
                    "data": [c.to_dict() for c in checks],
                }
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
            except Exception as exc:
                _logger.exception("Unexpected error in check_assembly_clearances")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def compose_assembly_parts(
            assembly_json: str,
            output_path: str,
        ) -> dict:
            """Compose all assembly parts into a single output STL file.

            Merges the individual part meshes according to their
            positions and writes the combined model to ``output_path``.

            Args:
                assembly_json: JSON string of the current assembly state.
                output_path: File path where the composed STL will be written.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("generate"):
                return err

            try:
                from kiln.assembly import Assembly, compose_assembly

                assembly = Assembly.from_dict(json.loads(assembly_json))
                result = compose_assembly(assembly, output_path)
                response = {"success": True, "data": result}
                # Assemblies are legitimately multi-body, but parts that
                # TOUCH (or all but touch) are not assembled — they are
                # unfused pieces that will print as separate shells.
                from kiln.fusion_check import attach_fusion_report

                response = attach_fusion_report(response, output_path)
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        response, level="quick", source_path=output_path,
                    )
                except ImportError:
                    return response
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
            except Exception as exc:
                _logger.exception("Unexpected error in compose_assembly_parts")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def get_joint_recommendation(
            joint_type: str,
            material_a: str = "PLA",
            material_b: str = "PLA",
            *,
            printer_id: str | None = None,
            mating: str | None = None,
        ) -> dict:
            """Get recommended clearance settings for a joint type and material pairing.

            Returns clearance recommendations based on the joint type
            and the materials of the two mating parts.  When
            ``printer_id`` is supplied AND kiln-pro is installed, the
            response narrows the clearance range by the user's
            calibration tier (HIGH halves it, MEDIUM shaves ~10%, LOW
            and UNKNOWN leave it unchanged) and attaches a
            ``calibration_used`` block documenting the source.

            Args:
                joint_type: Type of joint (e.g. ``"clearance_fit"``,
                    ``"press_fit"``, ``"snap_fit"``).
                material_a: Material of the first part (default ``"PLA"``).
                material_b: Material of the second part (default ``"PLA"``).
                printer_id: Optional printer identifier (e.g.
                    ``"bambu_a1"``).  For a joint whose parts MOVE, omitting
                    it resolves the user's active printer rather than
                    answering generically — pass it only to ask about a
                    machine that is not the one they are set up on.  For
                    every other joint type, omitting it keeps the historic
                    flat-range behaviour.
                mating: Optional shape hint for joints whose parts MOVE
                    against each other (``"clearance_fit"``, ``"loose"``):
                    ``"pin_in_bore"`` for a shaft or pin turning in a hole,
                    ``"slot"`` for a tongue sliding in a groove,
                    ``"planar_face"`` for two flat faces sliding, or
                    ``"gear_flank"`` for meshing teeth.  Omit it and the
                    round-joint case is assumed, which is the erring-loose
                    reading — a bore is closed on from both sides and needs
                    about twice the allowance a flat gap does, so assuming
                    it can only give a joint too much room, never too
                    little.  Ignored for joints that do not move.
            """
            try:
                from kiln.assembly import get_clearance_recommendation

                result = get_clearance_recommendation(
                    joint_type,
                    material_a,
                    material_b,
                    printer_id=printer_id,
                    mating=mating,
                )
                response = {"success": True, "data": result}
                # Nudge toward engineering-grade math when the pairing carries
                # a load-bearing signal (engineering material or a structural
                # joint); stay silent on cosmetic PLA clearance / snap fits.
                from kiln.load_bearing_detector import (
                    attach_load_bearing_nudge,
                    is_engineering_material,
                )

                eng_mat = (
                    material_a if is_engineering_material(material_a) else material_b
                )
                return attach_load_bearing_nudge(
                    response, material=eng_mat, joint_type=joint_type,
                )
            except Exception as exc:
                _logger.exception("Unexpected error in get_joint_recommendation")
                return {"success": False, "error": str(exc)}

        _logger.debug("Registered assembly tools")


def _parse_fastener_spec_arg(fastener: Any, fastener_spec_cls: Any) -> Any | None:
    """Parse an optional MCP fastener argument into ``FastenerSpec``.

    MCP clients vary: some send nested JSON objects, others send JSON as
    a string because their schema layer only exposes primitive
    arguments.  Accept both shapes and fail clearly for anything else.
    """
    if fastener is None:
        return None
    if isinstance(fastener, str):
        if not fastener.strip():
            return None
        try:
            fastener = json.loads(fastener)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid fastener JSON: {exc}") from exc
    if not isinstance(fastener, dict):
        raise ValueError("fastener must be a JSON object or JSON object string")
    return fastener_spec_cls.from_dict(fastener)


plugin = _AssemblyToolsPlugin()
