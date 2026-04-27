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
                assembly.parts.append(part)
                return {"success": True, "data": assembly.to_dict()}
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
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
        ) -> dict:
            """Add a mating interface between two parts in an assembly.

            Defines how two parts connect (joint type and clearance),
            which is used during validation and clearance checking.

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
            """
            try:
                from kiln.assembly import Assembly, MatingInterface

                assembly = Assembly.from_dict(json.loads(assembly_json))
                interface = MatingInterface(
                    part_a_id=part_a_id,
                    part_b_id=part_b_id,
                    joint_type=joint_type,
                    clearance_mm=clearance_mm,
                    magnet_polarity_aligned=magnet_polarity_aligned,
                )
                assembly.interfaces.append(interface)
                return {"success": True, "data": assembly.to_dict()}
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"Invalid assembly JSON: {exc}"}
            except Exception as exc:
                _logger.exception("Unexpected error in add_assembly_interface")
                return {"success": False, "error": str(exc)}

        @mcp.tool()
        def validate_assembly(assembly_json: str) -> dict:
            """Validate an assembly for correctness and printability.

            Runs clearance checks and joint validations on the assembly,
            returning the validated assembly state with results populated.

            Args:
                assembly_json: JSON string of the current assembly state.
            """
            try:
                from kiln.assembly import Assembly
                from kiln.assembly import validate_assembly as _validate

                assembly = Assembly.from_dict(json.loads(assembly_json))
                validated = _validate(assembly)
                return {"success": True, "data": validated.to_dict()}
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
            try:
                from kiln.assembly import Assembly, compose_assembly

                assembly = Assembly.from_dict(json.loads(assembly_json))
                result = compose_assembly(assembly, output_path)
                return {"success": True, "data": result}
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
        ) -> dict:
            """Get recommended clearance settings for a joint type and material pairing.

            Returns clearance recommendations based on the joint type
            and the materials of the two mating parts.

            Args:
                joint_type: Type of joint (e.g. ``"clearance_fit"``,
                    ``"press_fit"``, ``"snap_fit"``).
                material_a: Material of the first part (default ``"PLA"``).
                material_b: Material of the second part (default ``"PLA"``).
            """
            try:
                from kiln.assembly import get_clearance_recommendation

                result = get_clearance_recommendation(
                    joint_type,
                    material_a,
                    material_b,
                )
                return {"success": True, "data": result}
            except Exception as exc:
                _logger.exception("Unexpected error in get_joint_recommendation")
                return {"success": False, "error": str(exc)}

        _logger.debug("Registered assembly tools")


plugin = _AssemblyToolsPlugin()
