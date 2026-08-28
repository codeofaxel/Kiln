"""Manifest of the data files bundled with Kiln, and helpers for scanning them.

The bundled catalogues grew one file at a time, and the consistency checks
over them grew the same way -- each test hard-coding the one file it was
written against.  This module gives the checks a single declaration to read
instead: every JSON under ``data/`` is listed here, classified either as a
printer-keyed catalogue or as reference data, with the reason recorded.
``discover_bundled_data_files`` lets a test fail when a new file ships
unclassified, so additions are a decision rather than a drift.

The helpers split any catalogue entry into prose passages and structured
figures.  Scanning by structure rather than by key name is deliberate: a
note added tomorrow under a key nobody predicted is still scanned.

Safety passages are load-bearing content.  A passage introduced by one of
the ``SAFETY_MARKERS`` carries hazard guidance -- ventilation, power draw,
machine damage -- and several checks treat such passages specially:
``redact_safety_passages`` splits them out, and the test suite requires
every printer to keep its safety notes populated.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

DATA_ROOT = Path(__file__).resolve().parent / "data"

#: Catalogues keyed by printer id at the top level.  These carry the
#: per-machine guidance -- including the free-text notes -- and are the
#: files the per-printer consistency checks walk.
PRINTER_KEYED_FILES: tuple[str, ...] = (
    "printer_intelligence.json",
    "slicer_profiles.json",
    "safety_profiles.json",
    "design_knowledge/printer_profiles.json",
    "design_knowledge/printer_material_compatibility.json",
)

#: Every other bundled data file, with what keys it.  This is the
#: fail-closed half of the manifest: adding a data file without
#: classifying it fails the manifest check.
REFERENCE_DATA_FILES: dict[str, str] = {
    "component_catalog.json": "COTS component dimensions, keyed by component",
    "design_templates.json": "parametric template definitions, keyed by template",
    "material_catalog.json": "material properties, keyed by material",
    "support_profiles.json": "support-generation presets, keyed by strategy",
    "tool_safety.json": "tool-call safety rules for the agent surface",
    "design_knowledge/design_templates.json": (
        "template design guidance, keyed by template"
    ),
    "design_knowledge/environment_compatibility.json": (
        "service-environment guidance, keyed by environment"
    ),
    "design_knowledge/functional_requirements.json": (
        "requirement vocabulary, keyed by requirement"
    ),
    "design_knowledge/load_tables.json": (
        "structural load tables, keyed by geometry and material"
    ),
    "design_knowledge/material_troubleshooting.json": (
        "print-defect guidance, keyed by symptom and material"
    ),
    "design_knowledge/materials.json": "material design data, keyed by material",
    "design_knowledge/multi_material_pairing.json": (
        "material-pair compatibility, keyed by material pair"
    ),
    "design_knowledge/post_processing.json": "finishing guidance, keyed by process",
    "design_knowledge/skin_contact.json": (
        "skin-contact and biocompatibility guidance, keyed by material"
    ),
}

#: Directories under ``data/`` that hold vendored third-party libraries
#: rather than Kiln data.  Their JSON is upstream tooling config.
VENDORED_DATA_DIRS: tuple[str, ...] = ("scad_libraries",)

BUNDLED_DATA_FILES: frozenset[str] = frozenset(PRINTER_KEYED_FILES) | frozenset(
    REFERENCE_DATA_FILES
)

#: Keys whose string values are machine configuration, not statements about
#: the machine.  Their numbers are settings a slicer consumes; reading them
#: as prose would classify every printer's travel speed as a claim.
PROSE_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {"start_gcode", "end_gcode", "settings", "bed_shape", "_sources", "_meta"}
)

#: Field names reserved out of the bundled catalogues.  The printer
#: catalogue's schema is fixed, and these markers must not appear anywhere
#: in the printer-keyed files -- the test suite sweeps for them.
RESERVED_FIELD_MARKERS: frozenset[str] = frozenset(
    {
        "ams_slots",
        "ams_type",
        "camera",
        "nozzle_options",
        "max_nozzle_temp",
        "max_speed_mm_s",
        "max_acceleration_mm_s2",
        "wifi",
        "capabilities",
    }
)

#: Schema identifiers reserved out of the bundled catalogues.
RESERVED_SCHEMA_MARKERS: frozenset[str] = frozenset(
    {
        "fdm_hardware_capabilities_v1",
        "fdm_operational_capabilities_v1",
    }
)

#: A hazard marker opens a passage that runs to the end of the note.  The
#: vocabulary is the convention the data already uses; extend it rather
#: than working around it.
#:
#: Deliberately narrow.  ``CRITICAL:`` and ``WARNING:`` are NOT markers:
#: the slicer catalogue uses ``CRITICAL:`` mid-note for correctness
#: warnings about relative-E gcode, and treating those as hazard passages
#: would misclassify several hundred characters of ordinary prose per
#: Bambu machine.  A marker has to mean "this is a hazard", not "this is
#: important".
SAFETY_MARKERS: tuple[str, ...] = (
    "safety note:",
    "ventilation note:",
    "power note:",
    "fire note:",
    "electrical note:",
    "hazard note:",
)

_MIN_PROSE_CHARS = 30
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


class ProseEntry(NamedTuple):
    """One free-text passage, and where in the catalogues it came from."""

    printer_id: str
    source: str
    key: str
    text: str

    def where(self) -> str:
        return f"{self.source}:{self.printer_id}.{self.key}"


def _root(root: Path | None) -> Path:
    return DATA_ROOT if root is None else Path(root)


def load(relative_path: str, root: Path | None = None) -> dict[str, Any]:
    """Parse one bundled data file."""
    return json.loads((_root(root) / relative_path).read_text(encoding="utf-8"))


def discover_bundled_data_files(root: Path | None = None) -> set[str]:
    """Every bundled data JSON on disk, vendored libraries excluded."""
    base = _root(root)
    found = set()
    for path in base.rglob("*.json"):
        relative = path.relative_to(base)
        if relative.parts[0] in VENDORED_DATA_DIRS:
            continue
        found.add(relative.as_posix())
    return found


def canonical_figure(value: Any) -> str:
    """One spelling per number, so 65, 65.0 and '65' compare equal."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else str(number)


def is_safety_marked(text: str) -> bool:
    """True when a passage carries hazard guidance."""
    lowered = text.lower()
    return any(marker in lowered for marker in SAFETY_MARKERS)


def redact_safety_passages(text: str) -> str:
    """Return the passage with its hazard section removed.

    The data's convention is that a hazard marker introduces a passage
    that runs to the end of the note, so everything from the first marker
    onward is dropped and the descriptive lead-in is returned.  Splitting
    per passage rather than per note matters: a note can mix description
    and hazard guidance, and a check that wants only one of the two must
    not be handed the other.
    """
    lowered = text.lower()
    starts = [lowered.find(marker) for marker in SAFETY_MARKERS]
    found = [start for start in starts if start >= 0]
    return text if not found else text[: min(found)]


def _walk(node: Any, key: str, scannable: bool, prose: list, figures: list) -> None:
    if isinstance(node, dict):
        for child_key, child in node.items():
            _walk(
                child,
                child_key,
                scannable and child_key not in PROSE_EXCLUDED_KEYS,
                prose,
                figures,
            )
    elif isinstance(node, list):
        for child in node:
            _walk(child, key, scannable, prose, figures)
    elif isinstance(node, str):
        if scannable and " " in node and len(node) >= _MIN_PROSE_CHARS:
            prose.append((key, node))
        else:
            figures.extend(_NUMBER.findall(node))
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        figures.append(canonical_figure(node))


def scan_entry(entry: Any) -> tuple[list[tuple[str, str]], set[str]]:
    """Split any JSON subtree into (prose passages, structured figures)."""
    prose: list[tuple[str, str]] = []
    figures: list[str] = []
    _walk(entry, "", True, prose, figures)
    return prose, set(figures)


def iter_printer_prose(
    files: tuple[str, ...] = PRINTER_KEYED_FILES,
    root: Path | None = None,
) -> Iterator[ProseEntry]:
    """Every free-text passage in the printer-keyed files, tagged by printer."""
    for relative_path in files:
        catalogue = load(relative_path, root)
        for printer_id, entry in catalogue.items():
            if printer_id.startswith("_"):
                continue
            prose, _ = scan_entry(entry)
            for key, text in prose:
                yield ProseEntry(printer_id, relative_path, key or "?", text)


def printer_published_figures(
    files: tuple[str, ...] = PRINTER_KEYED_FILES,
    root: Path | None = None,
) -> dict[str, set[str]]:
    """Per printer, the figures the catalogues publish as structured data.

    Prose is excluded on purpose: the question these figures answer is
    "do the catalogues state this as data", and a number that appears only
    inside a note is the thing under inspection, not evidence that it was
    published.

    ``max_feedrate`` is also reported in mm/s.  The safety catalogue
    publishes a feedrate ceiling in mm/min because the safety layer needs
    one, and checks comparing figures across unit systems need both
    spellings.
    """
    published: dict[str, set[str]] = {}
    for relative_path in files:
        catalogue = load(relative_path, root)
        for printer_id, entry in catalogue.items():
            if printer_id.startswith("_"):
                continue
            _, figures = scan_entry(entry)
            bucket = published.setdefault(printer_id, set())
            bucket.update(figures)
            feedrate = entry.get("max_feedrate") if isinstance(entry, dict) else None
            if isinstance(feedrate, (int, float)) and not isinstance(feedrate, bool):
                bucket.add(canonical_figure(feedrate / 60.0))
    return published


def published_figures(root: Path | None = None) -> set[str]:
    """Every figure the bundled data publishes as structured data.

    Deliberately wider than the printer files: a material's required bed
    temperature lives in the material catalogue, and a note that quotes it
    is quoting bundled data.
    """
    base = _root(root)
    universe: set[str] = set()
    for relative_path in sorted(discover_bundled_data_files(base)):
        _, figures = scan_entry(load(relative_path, base))
        universe.update(figures)
    return universe
