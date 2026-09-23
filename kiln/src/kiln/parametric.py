"""Parametric OpenSCAD utilities -- parse, update, and validate parameters.

Supports the parametric-first generation workflow where AI-generated
OpenSCAD code includes adjustable dimension variables at the top of
the file. Users can tweak parameters without re-prompting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

# Regex for a top-level OpenSCAD variable assignment:
#   variable_name = <number>;  // optional comment
_VAR_RE = re.compile(
    r"^(?P<name>[a-zA-Z_]\w*)\s*=\s*(?P<value>-?\d+(?:\.\d+)?)\s*;"
    r"(?:\s*//\s*(?P<comment>.*))?$"
)

# Any top-level assignment, literal or not:
#   variable_name = <expression>;  // optional comment
# A line matching this but not _VAR_RE is a derived-parameter candidate.
_ASSIGN_RE = re.compile(
    r"^(?P<name>[a-zA-Z_]\w*)\s*=\s*(?P<expr>[^;]+?)\s*;"
    r"(?:\s*//\s*(?P<comment>.*))?$"
)

# Identifiers inside an expression, once string literals are removed.
_IDENT_RE = re.compile(r"[a-zA-Z_]\w*")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')

# Lines that signal "end of parameter block" — actual geometry code begins.
_STOP_KEYWORDS = frozenset(
    {"module", "function", "use", "include", "difference", "union",
     "intersection", "translate", "rotate", "scale", "cube", "sphere",
     "cylinder", "polyhedron", "linear_extrude", "rotate_extrude",
     "import", "surface", "hull", "minkowski", "color", "render",
     "for", "if", "let", "echo"}
)

# Extract (min: X, max: Y) or (min: X) or (max: Y) from comment text.
_MINMAX_RE = re.compile(
    r"\(\s*(?:min:\s*(?P<min>-?\d+(?:\.\d+)?))?"
    r"(?:\s*,\s*)?"
    r"(?:max:\s*(?P<max>-?\d+(?:\.\d+)?))?\s*\)"
)

# Fallback range pattern: (X - Y)
_RANGE_RE = re.compile(
    r"\(\s*(?P<lo>-?\d+(?:\.\d+)?)\s*-\s*(?P<hi>-?\d+(?:\.\d+)?)\s*\)"
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ParameterDef:
    """A single parsed OpenSCAD parameter definition.

    A literal parameter (``wall = 2;``) carries its ``value``.  A derived
    parameter (``inner = outer - 2*wall;``) has ``derived=True``, no
    ``value``, the source ``expression`` and the parameter names it
    ``depends_on``; it moves when any of those change and cannot be set
    directly.
    """

    name: str
    value: float | None
    unit: str = "mm"
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None
    derived: bool = False
    expression: str = ""
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "description": self.description,
            "derived": self.derived,
        }
        if self.min_value is not None:
            d["min_value"] = self.min_value
        if self.max_value is not None:
            d["max_value"] = self.max_value
        if self.derived:
            d["expression"] = self.expression
            d["depends_on"] = list(self.depends_on)
        return d


class DerivedParameterError(ValueError):
    """Raised when a caller tries to set a derived parameter directly."""


@dataclass
class ParameterWarning:
    """A warning about a parameter value that violates a design limit."""

    parameter_name: str
    current_value: float
    limit_value: float
    limit_type: str  # "min" or "max"
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_name": self.parameter_name,
            "current_value": self.current_value,
            "limit_value": self.limit_value,
            "limit_type": self.limit_type,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _humanize(name: str) -> str:
    """Convert ``snake_case`` variable name to a readable description."""
    return name.replace("_", " ")


def _parse_comment(
    name: str, comment: str,
) -> tuple[str, str, float | None, float | None]:
    """Split a trailing ``// comment`` into (unit, description, min, max)."""
    unit = "mm"
    description = _humanize(name)
    if comment:
        parts = comment.split(None, 1)
        if parts:
            candidate = parts[0].rstrip(",;:()")
            if candidate.isalpha() or candidate in ("mm", "cm", "m", "in"):
                unit = candidate
                description = parts[1].strip() if len(parts) > 1 else _humanize(name)
            else:
                description = comment

    min_val: float | None = None
    max_val: float | None = None

    mm = _MINMAX_RE.search(comment)
    if mm:
        if mm.group("min"):
            min_val = float(mm.group("min"))
        if mm.group("max"):
            max_val = float(mm.group("max"))
        # Strip the range from description
        desc_clean = _MINMAX_RE.sub("", description).strip().rstrip(",;: ")
        if desc_clean:
            description = desc_clean
    else:
        rm = _RANGE_RE.search(comment)
        if rm:
            min_val = float(rm.group("lo"))
            max_val = float(rm.group("hi"))
            desc_clean = _RANGE_RE.sub("", description).strip().rstrip(",;: ")
            if desc_clean:
                description = desc_clean

    return unit, description, min_val, max_val


def _referenced_params(expression: str, known: set[str]) -> list[str]:
    """Known parameter names an expression references, in order of first use."""
    seen: list[str] = []
    for ident in _IDENT_RE.findall(_STRING_RE.sub("", expression)):
        if ident in known and ident not in seen:
            seen.append(ident)
    return seen


def _block_lines(scad_code: str) -> list[str]:
    """Stripped, non-blank, non-comment lines up to the first geometry line."""
    lines: list[str] = []
    for raw_line in scad_code.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        first_token = line.split("(")[0].split("{")[0].split(" ")[0]
        if first_token.rstrip(";") in _STOP_KEYWORDS:
            break
        lines.append(line)
    return lines


def _parameter_names(lines: list[str]) -> set[str]:
    """Every name the block assigns from a literal or from other parameters.

    OpenSCAD top-level assignments are order-independent, so a line may
    reference a name declared below it.  Literal-numeric names seed the
    set; a line whose expression references a name already in the set is
    a derived parameter and adds its own name, until no line adds one.
    A line that references nothing in the set (a vector, a string, a
    function of constants) never joins it.
    """
    known = {m.group("name") for m in map(_VAR_RE.match, lines) if m}
    pending = [
        a for a in map(_ASSIGN_RE.match, lines)
        if a and a.group("name") not in known
    ]
    grew = True
    while grew and pending:
        grew = False
        for a in list(pending):
            if _referenced_params(a.group("expr"), known):
                known.add(a.group("name"))
                pending.remove(a)
                grew = True
    return known


def parse_openscad_parameters(scad_code: str) -> list[ParameterDef]:
    """Parse OpenSCAD variable declarations at the top of a file.

    The parameter block runs from the top of the file to the first line
    of geometry code; blank lines and ``//`` comments inside it are
    skipped.  From each variable line the unit, description, and optional
    min/max range are extracted from the trailing ``//`` comment.

    A line assigning an expression rather than a literal number, such as
    ``inner = outer - 2*wall;``, is reported as a derived parameter
    (``derived=True``, ``expression``, ``depends_on``, no value or range)
    when the expression references at least one other parameter in the
    block.  OpenSCAD assignments are order-independent, so the referenced
    parameter may be declared above or below the line, and may itself be
    derived; ``depends_on`` lists the names the line references directly.
    A line that references no parameter at all (a vector, a string, a
    function of constants) ends the block, as any non-literal line always
    has.

    :param scad_code: Full OpenSCAD source text.
    :returns: List of :class:`ParameterDef` found in the parameter block.
    """
    lines = _block_lines(scad_code)
    known = _parameter_names(lines)
    params: list[ParameterDef] = []

    for line in lines:
        m = _VAR_RE.match(line)
        if m:
            name = m.group("name")
            comment = (m.group("comment") or "").strip()
            unit, description, min_val, max_val = _parse_comment(name, comment)
            params.append(
                ParameterDef(
                    name=name,
                    value=float(m.group("value")),
                    unit=unit,
                    description=description,
                    min_value=min_val,
                    max_value=max_val,
                )
            )
            continue

        a = _ASSIGN_RE.match(line)
        depends_on = (
            _referenced_params(a.group("expr"), known) if a else []
        )
        if not depends_on:
            # Non-variable, non-comment, non-blank → end of param block
            break

        name = a.group("name")
        comment = (a.group("comment") or "").strip()
        unit, description, _, _ = _parse_comment(name, comment)
        params.append(
            ParameterDef(
                name=name,
                value=None,
                unit=unit,
                description=description,
                derived=True,
                expression=a.group("expr").strip(),
                depends_on=depends_on,
            )
        )

    return params


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def update_openscad_parameter(
    scad_code: str,
    param_name: str,
    new_value: float,
) -> str:
    """Replace the value of a named parameter in OpenSCAD source.

    Finds the line ``param_name = <number>;`` and substitutes *new_value*
    while preserving any trailing comment.

    :param scad_code: Full OpenSCAD source text.
    :param param_name: Variable name to update.
    :param new_value: New numeric value.
    :returns: Modified source text.
    :raises DerivedParameterError: If the parameter is derived from others;
        the message names its formula and the parameters to change instead.
    :raises ValueError: If the parameter is not found.
    """
    for p in parse_openscad_parameters(scad_code):
        if p.name == param_name and p.derived:
            raise DerivedParameterError(
                f"Parameter '{param_name}' is derived "
                f"({param_name} = {p.expression}) and cannot be set directly; "
                f"change {', '.join(p.depends_on)} instead"
            )

    # Build a pattern that matches this specific variable assignment
    pat = re.compile(
        r"^(?P<pre>" + re.escape(param_name) + r"\s*=\s*)"
        r"-?\d+(?:\.\d+)?"
        r"(?P<post>\s*;.*)$",
        re.MULTILINE,
    )

    # Format new value — use int representation when there's no fractional part
    if new_value == int(new_value):
        val_str = str(int(new_value))
    else:
        val_str = f"{new_value:g}"

    result, count = pat.subn(rf"\g<pre>{val_str}\g<post>", scad_code, count=1)
    if count == 0:
        raise ValueError(f"Parameter '{param_name}' not found in OpenSCAD code")
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_openscad_parameters(
    scad_code: str,
    material: str | None = None,
) -> list[ParameterWarning]:
    """Validate parameter values against comment ranges and material limits.

    Checks each parameter against:
    1. The min/max range declared in its ``// comment``.
    2. Material-specific design limits loaded from the design intelligence
       knowledge base (when *material* is provided).

    Heuristic name matching maps parameters to the appropriate design
    limit based on keywords in the variable name.  Derived parameters
    carry no value of their own and are skipped.

    :param scad_code: Full OpenSCAD source text.
    :param material: Optional material ID (e.g. ``"pla"``) for limit lookup.
    :returns: List of :class:`ParameterWarning` for any violations.
    """
    params = parse_openscad_parameters(scad_code)
    if not params:
        return []

    warnings: list[ParameterWarning] = []

    # Load material design limits if available
    mat_limits: dict[str, Any] = {}
    if material:
        try:
            from kiln.design_intelligence import get_material_profile

            profile = get_material_profile(material)
            if profile and profile.design_limits:
                mat_limits = profile.design_limits
        except Exception:
            _logger.debug(
                "Could not load material profile for validation",
                exc_info=True,
            )

    for p in params:
        if p.derived or p.value is None:
            continue

        # Check comment-declared min/max
        if p.min_value is not None and p.value < p.min_value:
            warnings.append(
                ParameterWarning(
                    parameter_name=p.name,
                    current_value=p.value,
                    limit_value=p.min_value,
                    limit_type="min",
                    message=(
                        f"'{p.name}' value {p.value} is below declared "
                        f"minimum {p.min_value}"
                    ),
                )
            )
        if p.max_value is not None and p.value > p.max_value:
            warnings.append(
                ParameterWarning(
                    parameter_name=p.name,
                    current_value=p.value,
                    limit_value=p.max_value,
                    limit_type="max",
                    message=(
                        f"'{p.name}' value {p.value} exceeds declared "
                        f"maximum {p.max_value}"
                    ),
                )
            )

        if not mat_limits:
            continue

        name_lower = p.name.lower()

        # Wall / thickness checks
        if "wall" in name_lower or "thickness" in name_lower:
            min_wall = mat_limits.get("min_wall_thickness_mm")
            if min_wall is not None and p.value < min_wall:
                warnings.append(
                    ParameterWarning(
                        parameter_name=p.name,
                        current_value=p.value,
                        limit_value=min_wall,
                        limit_type="min",
                        message=(
                            f"'{p.name}' ({p.value}mm) is below material "
                            f"minimum wall thickness ({min_wall}mm)"
                        ),
                    )
                )
            rec_wall = mat_limits.get("recommended_wall_thickness_mm")
            if (
                rec_wall is not None
                and p.value < rec_wall
                and (min_wall is None or p.value >= min_wall)
            ):
                warnings.append(
                    ParameterWarning(
                        parameter_name=p.name,
                        current_value=p.value,
                        limit_value=rec_wall,
                        limit_type="min",
                        message=(
                            f"'{p.name}' ({p.value}mm) is below recommended "
                            f"wall thickness ({rec_wall}mm)"
                        ),
                    )
                )

        # Hole / diameter checks
        if "hole" in name_lower or "diameter" in name_lower:
            min_hole = mat_limits.get("min_hole_diameter_mm")
            if min_hole is not None and p.value < min_hole:
                warnings.append(
                    ParameterWarning(
                        parameter_name=p.name,
                        current_value=p.value,
                        limit_value=min_hole,
                        limit_type="min",
                        message=(
                            f"'{p.name}' ({p.value}mm) is below material "
                            f"minimum hole diameter ({min_hole}mm)"
                        ),
                    )
                )

        # Pin checks
        if "pin" in name_lower:
            min_pin = mat_limits.get("min_pin_diameter_mm")
            if min_pin is not None and p.value < min_pin:
                warnings.append(
                    ParameterWarning(
                        parameter_name=p.name,
                        current_value=p.value,
                        limit_value=min_pin,
                        limit_type="min",
                        message=(
                            f"'{p.name}' ({p.value}mm) is below material "
                            f"minimum pin diameter ({min_pin}mm)"
                        ),
                    )
                )

        # Bridge / span checks
        if "bridge" in name_lower or "span" in name_lower:
            max_bridge = mat_limits.get("max_bridge_length_mm")
            if max_bridge is not None and p.value > max_bridge:
                warnings.append(
                    ParameterWarning(
                        parameter_name=p.name,
                        current_value=p.value,
                        limit_value=max_bridge,
                        limit_type="max",
                        message=(
                            f"'{p.name}' ({p.value}mm) exceeds material "
                            f"max bridge length ({max_bridge}mm)"
                        ),
                    )
                )

        # Overhang / angle checks
        if "overhang" in name_lower or "angle" in name_lower:
            max_overhang = mat_limits.get("max_unsupported_overhang_deg")
            if max_overhang is not None and p.value > max_overhang:
                warnings.append(
                    ParameterWarning(
                        parameter_name=p.name,
                        current_value=p.value,
                        limit_value=max_overhang,
                        limit_type="max",
                        message=(
                            f"'{p.name}' ({p.value}\u00b0) exceeds material "
                            f"max unsupported overhang ({max_overhang}\u00b0)"
                        ),
                    )
                )

    return warnings


# ---------------------------------------------------------------------------
# OpenSCAD code structure analysis
# ---------------------------------------------------------------------------


@dataclass
class ScadModule:
    """A parsed OpenSCAD module definition."""

    name: str
    line_start: int
    line_end: int
    code: str
    description: str  # from comment above module if present

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScadCodeStructure:
    """Parsed structure of an OpenSCAD file."""

    parameters: list[ParameterDef]
    modules: list[ScadModule]
    total_lines: int
    has_library_imports: bool
    libraries_used: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameters": [p.to_dict() for p in self.parameters],
            "modules": [m.to_dict() for m in self.modules],
            "total_lines": self.total_lines,
            "has_library_imports": self.has_library_imports,
            "libraries_used": self.libraries_used,
        }


def analyze_scad_structure(scad_code: str) -> ScadCodeStructure:
    """Analyze the structure of OpenSCAD code.

    Parses modules, parameters, and library imports to help agents
    understand the code structure for targeted modifications.

    :param scad_code: OpenSCAD source code.
    :returns: Parsed structure with modules, parameters, and metadata.
    """
    parameters = parse_openscad_parameters(scad_code)

    # Parse modules
    modules: list[ScadModule] = []
    lines = scad_code.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Look for module declarations
        match = re.match(r"^module\s+(\w+)\s*\(", line)
        if match:
            module_name = match.group(1)
            # Look for description in preceding comment
            description = ""
            if i > 0 and lines[i - 1].strip().startswith("//"):
                description = lines[i - 1].strip().lstrip("/ ").strip()

            # Find the matching closing brace
            brace_count = 0
            start = i
            for j in range(i, len(lines)):
                brace_count += lines[j].count("{") - lines[j].count("}")
                if brace_count <= 0 and j > i:
                    modules.append(
                        ScadModule(
                            name=module_name,
                            line_start=start + 1,  # 1-indexed
                            line_end=j + 1,
                            code="\n".join(lines[start : j + 1]),
                            description=description,
                        )
                    )
                    i = j
                    break
            else:
                # Unclosed module -- take rest of file
                modules.append(
                    ScadModule(
                        name=module_name,
                        line_start=start + 1,
                        line_end=len(lines),
                        code="\n".join(lines[start:]),
                        description=description,
                    )
                )
        i += 1

    # Parse library imports
    libraries_used: list[str] = []
    has_imports = False
    for line in lines:
        stripped = line.strip()
        inc_match = re.match(r"(?:include|use)\s*<(\w+)/", stripped)
        if inc_match:
            has_imports = True
            lib_name = inc_match.group(1)
            if lib_name not in libraries_used:
                libraries_used.append(lib_name)

    return ScadCodeStructure(
        parameters=parameters,
        modules=modules,
        total_lines=len(lines),
        has_library_imports=has_imports,
        libraries_used=libraries_used,
    )


def modify_scad_module(
    scad_code: str,
    module_name: str,
    new_module_code: str,
) -> str:
    """Replace a module's implementation in OpenSCAD code.

    Finds the named module and replaces its entire body with new code.
    Use this for targeted modifications like "add ventilation holes
    to the top_panel module."

    :param scad_code: Original OpenSCAD source.
    :param module_name: Name of the module to replace.
    :param new_module_code: Complete new module definition (including
        the ``module name() { ... }`` wrapper).
    :returns: Modified source code.
    :raises ValueError: If module not found.
    """
    structure = analyze_scad_structure(scad_code)
    target = None
    for mod in structure.modules:
        if mod.name == module_name:
            target = mod
            break

    if target is None:
        available = [m.name for m in structure.modules]
        raise ValueError(
            f"Module {module_name!r} not found. "
            f"Available modules: {', '.join(available) or 'none'}"
        )

    lines = scad_code.split("\n")
    # Replace lines from start to end (0-indexed: line_start-1 to line_end-1)
    before = lines[: target.line_start - 1]
    after = lines[target.line_end :]

    return "\n".join(before + [new_module_code] + after)


def insert_into_scad_module(
    scad_code: str,
    module_name: str,
    code_to_insert: str,
    position: str = "end",
) -> str:
    """Insert code into an existing OpenSCAD module.

    Adds geometry or operations inside a module without replacing it.
    Use for modifications like "add screw holes to the base" -- inserts
    the screw hole code inside the base module.

    :param scad_code: Original OpenSCAD source.
    :param module_name: Name of the module to modify.
    :param code_to_insert: OpenSCAD code to insert (e.g. a difference()
        block or additional geometry).
    :param position: Where to insert -- "end" (before closing brace) or
        "start" (after opening brace).
    :returns: Modified source code.
    :raises ValueError: If module not found.
    """
    structure = analyze_scad_structure(scad_code)
    target = None
    for mod in structure.modules:
        if mod.name == module_name:
            target = mod
            break

    if target is None:
        available = [m.name for m in structure.modules]
        raise ValueError(
            f"Module {module_name!r} not found. "
            f"Available modules: {', '.join(available) or 'none'}"
        )

    lines = scad_code.split("\n")

    if position == "start":
        # Find the first { after module declaration
        for idx in range(target.line_start - 1, min(target.line_end, len(lines))):
            if "{" in lines[idx]:
                # Insert after this line
                indent = "    "
                insert_lines = [indent + ln for ln in code_to_insert.split("\n")]
                lines = lines[: idx + 1] + insert_lines + lines[idx + 1 :]
                break
    else:  # end
        # Find the last } in the module
        for idx in range(target.line_end - 1, target.line_start - 2, -1):
            if "}" in lines[idx]:
                indent = "    "
                insert_lines = [indent + ln for ln in code_to_insert.split("\n")]
                lines = lines[:idx] + insert_lines + lines[idx:]
                break

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Compile and tweak workflows
# ---------------------------------------------------------------------------


def render_template_scad(template: dict[str, Any], params: dict[str, Any]) -> str:
    """The OpenSCAD a parametric part from ``data/design_templates.json`` builds.

    Every door that builds geometry from a template renders it here, so
    each one gets the same substitution and the same curve resolution:
    :data:`kiln.curve_resolution.SCAD_TRAILER` is appended, and a template
    need never type a facet count of its own.  The result is
    self-contained -- OpenSCAD run on it by hand builds the same part
    Kiln does.

    :param template: One template record (its ``scad_template`` is used).
    :param params: Parameter values, substituted into ``${name}`` slots.
    :returns: OpenSCAD source ready to compile.
    """
    from string import Template

    from kiln.curve_resolution import SCAD_TRAILER

    return Template(template["scad_template"]).safe_substitute(params) + SCAD_TRAILER


def compile_scad_code(
    scad_code: str,
    *,
    output_path: str | None = None,
    timeout: int = 300,
    as_written: bool = False,
) -> str:
    """Compile OpenSCAD code to an STL file.

    Uses Kiln's OpenSCAD provider with bundled library support.
    Returns the path to the generated STL file.

    :param scad_code: Valid OpenSCAD source code.
    :param output_path: Optional output STL path. Auto-generated if None.
    :param timeout: Maximum compilation time in seconds.
    :param as_written: Skip Kiln's curve rule and compile the source
        exactly as given -- for a design's kept source, whose rebuild must
        make the part it made (see :func:`kiln.curve_resolution.apply_rule`).
    :returns: Absolute path to the generated STL file.
    :raises ValueError: If compilation fails.
    """
    import os
    import shutil

    from kiln.generation.openscad import OpenSCADProvider

    provider = OpenSCADProvider(timeout=timeout)
    kwargs: dict[str, Any] = {}
    if output_path:
        kwargs["output_dir"] = os.path.dirname(output_path) or "."
    job = provider.generate(scad_code, as_written=as_written, **kwargs)

    if job.status.value == "failed":
        raise ValueError(f"OpenSCAD compilation failed: {job.error}")

    result = provider.download_result(job.id)
    if not result or not result.local_path:
        raise ValueError("OpenSCAD produced no output")

    if output_path and result.local_path != output_path:
        shutil.move(result.local_path, output_path)
        return output_path

    return result.local_path


def tweak_and_compile(
    scad_code: str,
    parameter_name: str,
    new_value: float,
    *,
    material: str | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Update a parameter and recompile in one step.

    The complete parametric tweaking workflow: validate the new value
    against material limits, update the code, compile to STL.

    :param scad_code: OpenSCAD source code.
    :param parameter_name: Parameter to update.
    :param new_value: New value.
    :param material: Optional material for limit validation.
    :param output_path: Optional output STL path.
    :returns: Dict with updated_code, stl_path, warnings.
    """
    # 1. Update the parameter
    updated_code = update_openscad_parameter(scad_code, parameter_name, new_value)

    # 2. Validate against material limits
    warnings = validate_openscad_parameters(updated_code, material=material)

    # 3. Compile
    stl_path = compile_scad_code(updated_code, output_path=output_path)

    return {
        "updated_code": updated_code,
        "stl_path": stl_path,
        "warnings": [w.to_dict() for w in warnings],
        "parameter_name": parameter_name,
        "new_value": new_value,
    }
