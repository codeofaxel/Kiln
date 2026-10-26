"""The real-export corpus, the doors it runs through, and how a reply is checked.

``tests/real_exports/`` holds files exported by the programs people actually
use (Blender, Autodesk Fusion, a CAD kernel's own STEP writer), each with its
truth measured in the program that made it.  Its README says how.

This module is the one reader of that directory, shared by public Kiln's door
sweep (``tests/test_real_export_corpus.py``) and by kiln-pro's, which loads
this file from public Kiln's shipping revision rather than keeping a copy.  It
holds four things:

- the corpus: every file and its truth;
- the doors: every registered tool whose signature takes a path to geometry,
  found by the same naming rule kiln-pro's mesh-input-doors gate uses, so a new
  door joins the sweep the day it is registered;
- how to call a door headless on a file, and on the caller mistakes that must
  be refused rather than crash;
- the checks: what a reply may claim about a file (pieces, sealed, edges, size,
  placement, verdict, score), read from whatever shape the reply takes, and the
  rules those claims must keep -- against the truth (A), against each other
  inside one reply (C), across two files holding the same object (D), and for
  a caller's mistake (E);
- the sentence scanner (F): a sentence a person reads never says HOW Kiln
  worked its answer out (``method_said``).  kiln-pro's output-leak gate calls
  the same scanner over the replies Kiln's servers send, so the two repos
  judge a sentence by one rule.

The claim readers are deliberately narrow: a value is read only under a key
that says unambiguously what it counts, and never from a branch describing
something other than the input (a repaired copy, a proposal, a colour zone).
A reply that makes no claim passes; a claim the authoring tool contradicts
fails, naming the door, the file, both values and, where the shape matches
one, the defect it is (``KNOWN_DEFECTS``).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORPUS_DIR = Path(__file__).resolve().parent / "real_exports"

# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


def load_corpus(root: Path = CORPUS_DIR) -> dict[str, dict[str, Any]]:
    """Every corpus file, keyed by its path relative to ``root``, with its truth."""
    truth: dict[str, dict[str, Any]] = {}
    for name in ("blender_truth.json", "step_truth.json"):
        truth.update(json.loads((root / name).read_text()))
    missing = [rel for rel in truth if not (root / rel).is_file()]
    if missing:
        raise FileNotFoundError(f"truth names files the corpus does not hold: {missing}")
    return truth


#: The files every door is run on: one of each way a real export differs from
#: a Kiln-made fixture.  The per-piece STLs of the multicolour set are inputs
#: to the compose door, not whole models, and ``enclosure_v2.step`` is the
#: second half of the revision pair, so they stay out of the per-file sweep.
SWEEP_FILES = (
    "donut_m.stl",            # Blender's default STL: metres, centred on the origin
    "donut_mm.stl",           # the same scene exported at Scale 1000
    "donut.glb",              # glTF: metres, Y up, vertices split at seams
    "donut.obj",              # Blender's default OBJ: metres, Y up, materials
    "knob_below_bed_mm.stl",  # one closed part where Blender adds it: half below the bed
    "knob_on_bed_mm.stl",     # the same part resting on the bed
    "enclosure_v1.step",      # Autodesk Fusion, one solid
    "assembly_3_bodies.step",  # three touching solids in one STEP
)

#: Two files that hold the SAME object and differ only in a way every slicer
#: ignores.  A door must say the same thing about both (``check_pair``).
SAME_OBJECT_PAIRS = (
    ("donut_m.stl", "donut_mm.stl", "unit"),
    ("knob_below_bed_mm.stl", "knob_on_bed_mm.stl", "placement"),
)

#: A door that takes two geometry files is handed these, so it compares a
#: real revision rather than a file with itself.
REVISION_PAIR = ("enclosure_v1.step", "enclosure_v2.step")

#: The multicolour sets: per-piece STLs, each piece's colour the material
#: Blender gave it.  The pieces overlap, as the pieces of every multicolour
#: model drawn in a modelling program do.
MULTICOLOUR_SETS = {
    "two_colours": ("donut_parts_mm/Donut.stl", "donut_parts_mm/Icing.stl"),
    "four_colours": tuple(
        f"donut_parts_mm/{n}.stl"
        for n in ("Donut", "Icing", "Sprinkle.00", "Sprinkle.01", "Sprinkle.02",
                  "Sprinkle.03", "Sprinkle.04", "Sprinkle.05")
    ),
}


def needs_cad_kernel(rel: str) -> bool:
    """Whether reading ``rel`` needs the optional CAD kernel."""
    return rel.lower().endswith((".step", ".stp"))


def stage(rel: str, dest: Path, root: Path = CORPUS_DIR) -> Path:
    """Copy a corpus file (and an OBJ's material file) into ``dest``.

    Every call works on a copy: many doors write their output beside the
    input, and the committed corpus must never change under a test run.
    """
    src = root / rel
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(rel).name
    shutil.copyfile(src, target)
    sidecar = src.with_suffix(".mtl")
    if src.suffix.lower() == ".obj" and sidecar.is_file():
        shutil.copyfile(sidecar, dest / sidecar.name)
    return target


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------

#: Parameter names that mean "a path to 3D geometry", and the rule for the
#: rest: the same rule as kiln-pro's ``scripts/audit_mesh_input_doors.py``
#: (``GEOMETRY_PARAMS`` / ``is_geometry_param``).  kiln-pro's corpus test fails
#: when the two disagree about any registered tool, so this is a copy with a
#: tripwire, never a second opinion.
GEOMETRY_PARAMS = frozenset({
    "file_path", "mesh_path", "model_path", "input_path",
    "stl_path", "obj_path", "mesh_a", "mesh_b",
    "host_mesh_path",
    "file_a", "file_b", "source_path", "part_paths",
})
_GEOMETRY_WORDS = frozenset({"mesh", "stl", "obj", "3mf", "model"})
_PATH_WORDS = frozenset({"path", "paths"})


def is_geometry_param(name: str) -> bool:
    if name in GEOMETRY_PARAMS:
        return True
    segments = set(name.lower().split("_"))
    return bool(segments & _GEOMETRY_WORDS) and bool(segments & _PATH_WORDS)


@dataclass(frozen=True)
class Door:
    name: str
    module: str
    geometry_params: tuple[str, ...]
    required: tuple[str, ...]
    optional: tuple[str, ...]


def discover_doors(mcp: Any, *, module_prefix: str) -> list[Door]:
    """Every registered tool whose signature takes a geometry path.

    ``module_prefix`` picks the repo: ``"kiln."`` for public Kiln's tools,
    ``"kiln_pro."`` for kiln-pro's.  Read from the live registry, so a tool
    registered by any route (a decorator, a computed name, a plugin) is seen.
    """
    doors = []
    for name, tool in sorted(getattr(mcp._tool_manager, "_tools", {}).items()):
        fn = getattr(tool, "fn", None)
        if fn is None or not getattr(fn, "__module__", "").startswith(module_prefix):
            continue
        params = inspect.signature(fn).parameters
        geo = tuple(p for p in params if is_geometry_param(p))
        if not geo:
            continue
        required = tuple(
            p for p, v in params.items()
            if v.default is inspect.Parameter.empty
            and v.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        )
        optional = tuple(p for p in params if p not in required)
        doors.append(Door(name, fn.__module__, geo, required, optional))
    return doors


def build_args(door: Door, files: dict[str, Path], work: Path,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Arguments for one headless call: the geometry, a writable output place,
    and whatever ``extra`` the door's own required inputs need.

    ``files`` maps each geometry parameter to the staged file it gets; a
    parameter that takes a list (``part_paths``) gets a one-item list.
    """
    args = dict(extra or {})
    for param, path in files.items():
        args[param] = [str(path)] if param.endswith("paths") else str(path)
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    first = next(iter(files.values()))
    for param in door.optional:
        if param == "output_dir":
            args.setdefault(param, str(out))
        elif param == "output_path":
            suffix = ".3mf" if "3mf" in door.name else (
                first.suffix if first.suffix.lower() in (".stl", ".obj") else ".stl")
            args.setdefault(param, str(out / f"{door.name}{suffix}"))
    return args


def call(mcp: Any, name: str, args: dict[str, Any], *, timeout_s: float = 240.0) -> dict[str, Any]:
    """Run a registered tool the way a host's ``tools/call`` does; one dict back.

    An exception that escapes the tool is part of the reply (``_raised``), not
    a test error: a door that throws at a caller is a finding like any other.
    """
    from kiln.mcp_compat import call_registered_tool

    async def _go():
        return await asyncio.wait_for(call_registered_tool(mcp, name, args), timeout_s)

    try:
        return as_reply(asyncio.run(_go()))
    except asyncio.TimeoutError:
        return {"_timed_out": timeout_s}
    except Exception as exc:  # noqa: BLE001 -- the escape IS the observation
        return {"_raised": f"{type(exc).__name__}: {str(exc)[:500]}"}


def as_reply(result: Any) -> dict[str, Any]:
    """A tool result as one dict, whatever shape the tool returned it in."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        return result
    blocks = getattr(result, "content", result)
    if isinstance(blocks, list):
        merged: dict[str, Any] = {}
        texts = []
        for block in blocks:
            text = getattr(block, "text", None)
            if text is None:
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                texts.append(text)
                continue
            if isinstance(parsed, dict):
                merged.update(parsed)
            else:
                texts.append(text)
        if texts:
            merged.setdefault("_text", "\n".join(texts))
        return merged
    if isinstance(result, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        return {"_text": result}
    return {"_text": repr(result)[:2000]}


# ---------------------------------------------------------------------------
# Reading a reply
# ---------------------------------------------------------------------------


def error_of(reply: dict[str, Any]) -> dict[str, Any]:
    err = reply.get("error")
    if isinstance(err, dict):
        return err
    if isinstance(err, str):
        return {"message": err, "code": reply.get("code") or reply.get("error_code"),
                "retryable": reply.get("retryable")}
    return {}


def walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every (dotted key path, value) in a reply, depth first."""
    if isinstance(node, dict):
        for k, v in node.items():
            sub = f"{path}.{k}" if path else str(k)
            yield sub, v
            yield from walk(v, sub)
    elif isinstance(node, list):
        for i, v in enumerate(node[:200]):
            yield f"{path}[{i}]", v
            yield from walk(v, f"{path}[{i}]")


def all_text(reply: dict[str, Any]) -> str:
    """Every string in a reply, joined: what a person or an agent could read."""
    return "\n".join(v for _p, v in walk(reply) if isinstance(v, str))


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[")[0].lower()


#: Branches that describe something OTHER than the input file as it arrived:
#: a repaired or rescaled copy, a proposal, an alternative, a colour zone, a
#: "what if it were placed" answer, a comparison's other side, and the
#: inspection a changing tool attaches to what it made.
_ELSEWHERE = re.compile(
    r"(^|\.)(after|repaired|repair_result|output|outputs|result_mesh|fixed|prepared|"
    r"proposed|candidates?|suggest\w*|recommend\w*|history|baseline|previous|"
    r"alternatives?|options|orientations?|zones?|segments?|regions?|preview\w*|"
    r"thumbnail|images?|renders?|remaining\w*|delta\w*|diff\w*|changes?|"
    r"placement|if_placed|.*_if_placed|plates?|parts\[\d+\]|copies|best|worst|"
    r"inspection_bundle|self_check)(\.|\[|$)",
    re.IGNORECASE,
)


def _about_input(path: str) -> bool:
    parent = path.rsplit(".", 1)[0] if "." in path else ""
    return not _ELSEWHERE.search(parent + ".")


#: A key whose number counts the separate pieces (connected bodies) a file
#: holds.  Listed, never matched loosely: "count" alone means anything.
PIECE_COUNT_KEYS = frozenset({
    "connected_components", "component_count", "num_components", "n_components",
    "body_count", "estimated_body_count", "num_bodies", "piece_count",
    "shell_count", "shells", "island_count",
})
#: A key whose boolean says the whole surface is sealed.  ``is_manifold`` is
#: the validation passes' name for it: every piece closed.
CLOSED_KEYS = frozenset({"is_watertight", "watertight", "all_closed", "is_closed", "is_manifold"})
#: A key that says some piece floats: in the air, joined to nothing, scrap
#: to remove.
FLOATING_KEYS = frozenset({"has_floating_fragments", "has_floating_parts", "floating_bodies"})
#: A key whose number counts edges that leave the surface open or pinched.
OPEN_EDGE_KEYS = frozenset({
    "non_manifold_edges", "non_manifold_edge_count", "boundary_edges",
    "boundary_edge_count", "open_edges", "open_edge_count", "pinched_edges",
    "pinch_edges", "hole_count",
})
#: A key whose number counts faces crossing another face of the same body.
SELF_INTERSECTION_KEYS = frozenset({"self_intersection_count", "self_intersecting_faces",
                                    "self_intersections"})
#: A key holding the overall size of the part.
SIZE_KEYS = frozenset({"dimensions", "dimensions_mm", "bounding_box", "bounding_box_mm",
                       "bbox", "bbox_mm", "extents_mm", "size_mm", "overall_size_mm"})
#: Printability's own score and grade, and the verdict key beside them.
SCORE_KEYS = frozenset({"score", "printability_score"})
GRADE_KEYS = frozenset({"grade", "printability_grade"})
PRINTABLE_KEYS = frozenset({"printable", "is_printable"})
VERDICT_KEYS = frozenset({"verdict"})


def _size_from(value: Any) -> list[float] | None:
    def num(v: Any) -> float | None:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    if isinstance(value, dict):
        for keys in (("x", "y", "z"), ("width_mm", "depth_mm", "height_mm"),
                     ("width", "depth", "height"), ("x_mm", "y_mm", "z_mm")):
            if all(k in value for k in keys):
                got = [num(value[k]) for k in keys]
                return got if None not in got else None  # type: ignore[return-value]
        if all(f"{a}_{e}" in value for a in "xyz" for e in ("min", "max")):
            got = [num(value[f"{a}_max"]) for a in "xyz"], [num(value[f"{a}_min"]) for a in "xyz"]
            if None in got[0] or None in got[1]:
                return None
            return [h - lo for h, lo in zip(*got, strict=True)]  # type: ignore[operator]
        lo, hi = value.get("min"), value.get("max")
        if isinstance(lo, list) and isinstance(hi, list) and len(lo) == len(hi) == 3:
            pairs = [(num(a), num(b)) for a, b in zip(lo, hi, strict=True)]
            if all(a is not None and b is not None for a, b in pairs):
                return [b - a for a, b in pairs]  # type: ignore[operator]
        return None
    if isinstance(value, list) and len(value) == 3:
        got = [num(v) for v in value]
        return got if None not in got else None  # type: ignore[return-value]
    return None


@dataclass
class Claims:
    """What one reply says about the input file, each with the path it sits at."""

    pieces: list[tuple[str, int]] = field(default_factory=list)
    closed: list[tuple[str, bool]] = field(default_factory=list)
    open_edges: list[tuple[str, int]] = field(default_factory=list)
    self_intersections: list[tuple[str, int]] = field(default_factory=list)
    sizes_mm: list[tuple[str, list[float]]] = field(default_factory=list)
    scores: list[tuple[str, float]] = field(default_factory=list)
    grades: list[tuple[str, str]] = field(default_factory=list)
    printable: list[tuple[str, bool]] = field(default_factory=list)
    verdicts: list[tuple[str, str]] = field(default_factory=list)
    floating: list[tuple[str, bool]] = field(default_factory=list)
    #: The structural analysis's counts: risks as ``(critical, warning)``
    #: at a risk report, and reinforcements recommended, by where they sit.
    structural: list[tuple[str, Any]] = field(default_factory=list)

    def by_path(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("pieces", "closed", "open_edges", "self_intersections",
                     "scores", "grades", "printable", "verdicts", "structural"):
            for path, value in getattr(self, name):
                out[f"{name}:{path}"] = value
        return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_claims(reply: dict[str, Any]) -> Claims:
    claims = Claims()
    for path, value in walk(reply):
        if not _about_input(path):
            continue
        leaf = _leaf(path)
        if leaf in PIECE_COUNT_KEYS and _int(value) is not None:
            claims.pieces.append((path, value))
        elif leaf in CLOSED_KEYS and isinstance(value, bool):
            claims.closed.append((path, value))
        elif leaf in OPEN_EDGE_KEYS and _int(value) is not None:
            claims.open_edges.append((path, value))
        elif leaf in SELF_INTERSECTION_KEYS and _int(value) is not None:
            claims.self_intersections.append((path, value))
        elif leaf in SIZE_KEYS and (size := _size_from(value)) is not None:
            claims.sizes_mm.append((path, size))
        elif (leaf in SCORE_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool)
              and 0 <= value <= 100):
            claims.scores.append((path, float(value)))
        elif leaf in GRADE_KEYS and isinstance(value, str) and re.fullmatch(r"[A-F][+-]?", value.strip()):
            claims.grades.append((path, value.strip()[0]))
        elif leaf in PRINTABLE_KEYS and isinstance(value, bool):
            claims.printable.append((path, value))
        elif leaf in VERDICT_KEYS and isinstance(value, str) and re.fullmatch(r"[a-z_]+", value):
            claims.verdicts.append((path, value))
        elif leaf in FLOATING_KEYS and (isinstance(value, bool) or _int(value) is not None):
            claims.floating.append((path, bool(value)))
        if isinstance(value, dict):
            claims.structural.extend(_structural_counts(path, value))
    claims.structural.extend(_structural_counts("", reply))
    return claims


#: The keys that mark a dict as the structural analysis's risk report: the
#: risk list, its count, or the structural score beside them.
_STRUCTURAL_MARKS = frozenset({"risks", "risk_count", "overall_structural_score", "structural_grade"})


def _structural_counts(path: str, block: dict[str, Any]) -> list[tuple[str, Any]]:
    """The structural counts *block* claims: risks as ``(critical, warning)``
    where it is a risk report, and how many reinforcements it recommends.
    A count that is ``None`` claims nothing (the analysis judged nothing)."""
    out: list[tuple[str, Any]] = []
    where = path or "<reply>"
    critical, warning = _int(block.get("critical_count")), _int(block.get("warning_count"))
    if _STRUCTURAL_MARKS & block.keys() and critical is not None and warning is not None:
        out.append((f"{where}.risk_counts", (critical, warning)))
    if "reinforcements" in block and _int(block.get("recommendation_count")) is not None:
        out.append((f"{where}.recommendation_count", block["recommendation_count"]))
    return out


# ---------------------------------------------------------------------------
# The defects these guards were written for (2026-10-23).  A finding whose
# shape matches one names it, so a red run reads as a merge checklist.
# ---------------------------------------------------------------------------

KNOWN_DEFECTS = {
    1: "'won't print' beside a passing score and grade",
    2: "a metres export read as millimetres with nothing said (1000x too small)",
    3: "pieces counted with a fixed millimetre weld: the count changes with the unit",
    4: "edges and self-intersections counted across pieces that only touch",
    5: "a part centred below the bed graded unprintable, though every slicer drops it onto the bed",
    6: "a caller's unknown printer id answered INTERNAL_ERROR, retryable",
    7: "glTF seam-split vertices read as open surfaces",
    8: "a multi-body STEP flattened into one body",
    9: "the compose message counts parts as colours",
    10: "a composed file of touching pieces the slicer Kiln drives refuses",
    11: "the embedded flush matrix the slicer Kiln drives cannot parse",
    13: "pieces that sit on the part, or a part with a second piece, called floating scrap to delete",
    14: "a printability score said in a sentence, where the verdict is the answer",
    15: "structural risks judged on a metres export as if it were the part: the counts change with the unit",
    16: "how Kiln works an answer out, said in a sentence: an internal name, a module or source path, "
        "a rule id, a method's weight or factor beside its number",
}


def _defect(n: int | None, *, same_class: bool = False) -> str:
    if n is None:
        return "[not on the 2026-10-23 list]"
    if same_class:
        return f"[same class as defect #{n} ({KNOWN_DEFECTS[n]}), not itself on the list]"
    return f"[defect #{n}: {KNOWN_DEFECTS[n]}]"


def _seam_defect(fmt: str) -> str:
    """glTF splits vertices at seams by design (defect #7); OBJ does the same
    wherever a face's texture coordinates change."""
    if fmt in (".glb", ".gltf"):
        return _defect(7)
    if fmt == ".obj":
        return _defect(7, same_class=True)
    return _defect(4)


def _grouped(findings: list[tuple[str, str, str]]) -> list[str]:
    """One line per distinct claim: the same wrong number at five paths of one
    reply is one finding, with its paths listed."""
    order: list[tuple[str, str]] = []
    paths: dict[tuple[str, str], list[str]] = {}
    for head, path, tail in findings:
        key = (head, tail)
        if key not in paths:
            order.append(key)
            paths[key] = []
        paths[key].append(path)
    out = []
    for head, tail in order:
        where = paths[(head, tail)]
        shown = ", ".join(where[:3]) + (f" (+{len(where) - 3} more)" if len(where) > 3 else "")
        out.append(f"{head}: {shown} {tail}")
    return out


# ---------------------------------------------------------------------------
# Guard A: every claim agrees with the truth measured in the authoring tool
# ---------------------------------------------------------------------------

#: Error codes whose own words say the failure is transient, so a retry can
#: help.  Anything else a door answers about a file it was handed will be the
#: same answer next time, and marking it retryable sends an agent round a loop.
_TRANSIENT_WORDS = re.compile(r"RATE_LIMIT|TIMEOUT|UNAVAILABLE|OFFLINE|BUSY|NETWORK|TRY_AGAIN", re.I)

#: What a refusal says when it calls the file itself broken.
_CALLS_IT_BROKEN = re.compile(
    r"not a valid (binary )?stl|could not parse|cannot parse|failed to parse|corrupt|"
    r"malformed|no layers were detected|repair your|has no volume|empty mesh|zero volume",
    re.IGNORECASE,
)
#: ...unless it is the honest STEP refusal, which the mesh-input-doors gate owns.
_NAMES_CAD = re.compile(r"\bSTEP\b|\bCAD\b", re.IGNORECASE)
#: A reply that read a metres file as millimetres must say something like this.
_NAMES_THE_UNIT = re.compile(
    r"\bmet(re|er)s?\b|\binch(es)?\b|1[,.]?000\s*(x|×|times)|thousand times|wrong (unit|scale)",
    re.IGNORECASE,
)


def _close(a: float, b: float, *, rel: float, absolute: float) -> bool:
    return abs(a - b) <= max(absolute, rel * max(abs(a), abs(b)))


def check_crash(door: str, rel: str, truth: dict[str, Any], reply: dict[str, Any]) -> list[str]:
    """A well-formed real export is never a crash, a hang, a retryable dead
    end, or called broken."""
    out = []
    made_by = truth.get("made_by", "a real tool")
    if "_raised" in reply:
        out.append(f"{door}({rel}): raised at the caller -- {reply['_raised']} {_defect(None)}")
    if "_timed_out" in reply:
        out.append(f"{door}({rel}): no answer in {reply['_timed_out']:.0f}s {_defect(None)}")
    err = error_of(reply)
    code = str(err.get("code") or "")
    if code.upper() == "INTERNAL_ERROR":
        out.append(f"{door}({rel}): INTERNAL_ERROR on a well-formed {made_by} export -- "
                   f"{str(err.get('message'))[:200]} {_defect(None)}")
    elif err.get("retryable") is True and not _TRANSIENT_WORDS.search(code):
        out.append(f"{door}({rel}): marked retryable ({code or 'no code'}) though the same file "
                   f"will get the same answer -- {str(err.get('message'))[:200]} {_defect(None)}")
    message = str(err.get("message") or "")
    if err and _CALLS_IT_BROKEN.search(message) and not _NAMES_CAD.search(message):
        defect = 2 if truth.get("unit") == "m" and "no layers" in message.lower() else None
        out.append(f"{door}({rel}): calls a well-formed {made_by} export broken -- "
                   f"{message[:200]} {_defect(defect)}")
    return out


def check_truth(door: str, rel: str, truth: dict[str, Any], reply: dict[str, Any]) -> list[str]:
    """Every claim the reply makes about the file agrees with its truth."""
    found: list[tuple[str, str, str]] = []
    head = f"{door}({rel})"
    claims = read_claims(reply)
    pieces = truth["piece_count"]
    unit = truth.get("unit", "mm")
    made_by = truth["made_by"]
    fmt = Path(rel).suffix.lower()
    cad = needs_cad_kernel(rel)
    for path, n in claims.pieces:
        if n == pieces:
            continue
        if cad:
            label = _defect(8) if n < pieces else _defect(None)
        elif fmt in (".glb", ".gltf", ".obj"):
            label = _seam_defect(fmt)
        else:
            label = _defect(3) if n > pieces else _defect(None)
        found.append((head, path, f"= {n} pieces; {made_by} holds {pieces} {label}"))
    if truth.get("all_closed"):
        for path, sealed in claims.closed:
            if not sealed:
                found.append((head, path, f"= False; every piece {made_by} wrote is closed {_seam_defect(fmt)}"))
        if truth.get("non_manifold_edges", 0) == 0:
            for path, n in claims.open_edges:
                if n:
                    found.append((head, path, f"= {n}; the file has 0 open or pinched edges {_seam_defect(fmt)}"))
    if truth.get("pieces_touch") or pieces == 1:
        for path, floats in claims.floating:
            if floats:
                found.append((head, path, f"says a piece floats; every piece {made_by} wrote touches "
                                          f"another {_defect(13)}"))
    expected = truth.get("self_intersecting_faces")
    for path, n in claims.self_intersections:
        if expected is not None and n > expected:
            found.append((head, path, f"= {n}; {expected} faces of any piece cross that piece -- pieces "
                                      f"overlapping each other are not a self-intersection {_defect(4)}"))
    want = sorted(truth["size_mm"])
    raw = sorted(v / truth.get("scale_to_mm", 1.0) for v in truth["size_mm"])
    unit_said = bool(_NAMES_THE_UNIT.search(all_text(reply)))
    for path, size in claims.sizes_mm:
        got = sorted(abs(v) for v in size)
        if all(_close(g, w, rel=0.02, absolute=0.6) for g, w in zip(got, want, strict=True)):
            continue
        # A reply rounds a sub-millimetre size to two or three places, so the
        # raw-unit match allows half a hundredth of a millimetre.
        if unit != "mm" and all(_close(g, w, rel=0.05, absolute=0.006) for g, w in zip(got, raw, strict=True)):
            if not unit_said:
                found.append((head, path, f"is {_fmt(size)} mm and nothing says the unit is wrong; the part is "
                                          f"{_fmt(truth['size_mm'])} mm, written in {unit} {_defect(2)}"))
            continue
        found.append((head, path, f"is {_fmt(size)}; the part is {_fmt(truth['size_mm'])} mm {_defect(None)}"))
    return _grouped(found)


def _fmt(size: list[float]) -> str:
    return "[" + ", ".join(f"{v:.4g}" for v in size) + "]"


# ---------------------------------------------------------------------------
# Guard C: one reply, one story
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ladder:
    """Printability's own grade ladder, read from the code that grades."""

    grade_of: Callable[[int], str]
    printable_line: float

    @classmethod
    def from_kiln(cls) -> Ladder:
        from kiln import printability

        grade_of = getattr(printability, "_score_to_grade", None)
        line = getattr(printability, "_PRINTABLE_SCORE_MIN", None)
        assert callable(grade_of) and line is not None, (
            "kiln.printability no longer exposes _score_to_grade / _PRINTABLE_SCORE_MIN; "
            "point the corpus story check at the ladder that now grades a part"
        )
        return cls(grade_of, float(line))

    @property
    def passing_grades(self) -> frozenset[str]:
        return frozenset(self.grade_of(s) for s in range(int(self.printable_line), 101))


def _dict_at(reply: dict[str, Any], path: str) -> dict[str, Any] | None:
    parent = path.rsplit(".", 1)[0] if "." in path else ""
    node: Any = reply
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", parent):
        if part.startswith("["):
            idx = int(part[1:-1])
            node = node[idx] if isinstance(node, list) and idx < len(node) else None
        else:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node if isinstance(node, dict) else None


_MESSAGE_KEYS = ("message", "summary", "verdict_text", "headline")

#: A sentence stating a printability score or grade: "Printability score
#: 50/100 (grade F)", "Printability: F (50/100)", "Not ready (printability
#: 45/100)".  The verdict is what a person is told; the score stays in the
#: report for comparing versions.
_SAYS_THE_SCORE = re.compile(
    r"printability\W+(score\W+)?\d{1,3}\s*/\s*100|printability\W+[A-F]\s*\(\d{1,3}\s*/\s*100|"
    r"\b\d{1,3}\s*/\s*100\s*\(grade\s+[A-F]",
    re.IGNORECASE,
)

#: The same for the structural risk analysis: "Score: 62/100 (D)" (any score
#: quoted with its grade), "Structural score 0/100 (F)", "Structural grade:
#: D", "Score improved 45 → 100 (F → A)".  A person is told the risks it
#: found, named; the score stays in the report for comparing versions.  The
#: grade is a capital letter, so "structural grade a part" is no hit.
_SAYS_THE_STRUCTURAL_SCORE = re.compile(
    r"\b\d{1,3}\s*/\s*100\s*\(\s*(?-i:[A-F])[+-]?\s*\)"
    r"|structural\W+(?:score|grade)\W+(?:\d|(?-i:[A-F])\b)"
    r"|\bscore\s+(?:improved|unchanged)\b",
    re.IGNORECASE,
)

#: Every other score or grade said as a number: "Score: 72.5/100.", "(85/100
#: vs 40/100)", "(score 87.5)", "Original score: 62", "improves score by
#: 12.3%", "Improvement: 41%", "Printer health score is 42%", "grade B", "41%
#: better".  A measurement keeps its number ("~70 min", "8 mm brim", "19.0%
#: overhang", "3 parts", "40% of 12 prints succeeded"): only a score, a grade
#: or a score's change leaves the sentence.  "food-grade", "upgrade" and
#: "min_printability_score=60" (a minimum the caller set) are no hit.
_SAYS_A_SCORE = re.compile(
    r"\b\d{1,3}(?:\.\d+)?\s*/\s*100\b"
    r"|\bscor(?:e|es|ed)\b\W{0,3}(?:(?:is|of|was|by|at|now)\s+)?\d"
    r"|(?<![-\w])grade[sd]?\W{0,3}(?:(?:is|of|was)\s+)?(?-i:[A-F])[+-]?(?![\w-])"
    r"|\bimprove(?:s|d|ment)\b\W{0,3}(?:by\s+)?\d+(?:\.\d+)?\s*%"
    r"|\d+(?:\.\d+)?\s*%\s+(?:better|worse)\b",
    re.IGNORECASE,
)

#: Where a reply says a sentence a person reads, at any depth: its message
#: and summary, a gate's or an alert's or an error's message, a reason, a
#: recommendation's reasoning, notes, warnings, next steps, and the list of
#: what a fix did.  Read for a score said in a sentence (guard C) and for how
#: Kiln worked an answer out (guard F).
_SENTENCE_KEYS = frozenset({
    *_MESSAGE_KEYS, "special_notes", "next_steps", "next_actions", "reasoning", "reason",
    "recommendation", "recommendations", "warnings", "notes", "note", "hint", "upgrade_hint",
    "explanation", "advice", "rationale", "fixed",
})

#: A sentence counting a part's structural risks, or calling it clear of them,
#: in the shapes the structural doors say it: "1 critical structural risk and
#: 20 warnings", "3 structural warnings", "No structural risks found.", "design
#: is structurally sound".
_SAYS_STRUCTURAL_COUNTS = re.compile(
    r"\b\d+ critical structural risks?\b|\b\d+ structural warnings?\b|\bno structural risks\b"
    r"|\bstructurally sound\b",
    re.IGNORECASE,
)


#: The words every units question says, whichever door says it.
_ASKS_UNITS = re.compile(r"\bnot a printable size\b", re.IGNORECASE)


def asks_units(reply: dict[str, Any]) -> bool:
    """Whether *reply* asks the units question: under ``units_question``
    (the reply's own, or its report's), or in a sentence of its own."""
    return any(
        isinstance(v, str) and v.strip() and (_leaf(p) == "units_question" or _ASKS_UNITS.search(v))
        for p, v in walk(reply)
    )


def _sentences(reply: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Every sentence a person reads in *reply*, with where it sits."""
    for path, value in walk(reply):
        if _leaf(path) not in _SENTENCE_KEYS:
            continue
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, list):
            for i, text in enumerate(value):
                if isinstance(text, str):
                    yield f"{path}[{i}]", text


def scores_said(reply: dict[str, Any]) -> list[tuple[str, str]]:
    """Each (where, what) in *reply* where a sentence says a score or grade
    as a number, printability's, the structural analysis's or any other."""
    out = []
    for where, text in _sentences(reply):
        said = (_SAYS_THE_SCORE.search(text) or _SAYS_THE_STRUCTURAL_SCORE.search(text)
                or _SAYS_A_SCORE.search(text))
        if said:
            out.append((where, said.group(0)))
    return out


def check_story(door: str, rel: str, reply: dict[str, Any], ladder: Ladder) -> list[str]:
    """The verdict, the score, the grade, the counts and the sentences of one
    reply tell one story, by Kiln's own ladder."""
    out = []
    claims = read_claims(reply)
    wont_print = any(v == "wont_print" for _p, v in claims.verdicts)
    ready = any(v == "ready" for _p, v in claims.verdicts)
    for path, score in claims.scores:
        block = _dict_at(reply, path) or {}
        grade = next((block[k] for k in GRADE_KEYS if isinstance(block.get(k), str)), None)
        if grade and re.fullmatch(r"[A-F][+-]?", grade) and grade[0] != ladder.grade_of(int(round(score))):
            out.append(f"{door}({rel}): {path} = {score:g} beside grade {grade}; Kiln's ladder grades "
                       f"{score:g} as {ladder.grade_of(int(round(score)))} {_defect(None)}")
        printable = block.get("printable")
        if printable is False and score >= ladder.printable_line:
            out.append(f"{door}({rel}): {path} = {score:g} (a printable score) beside printable=False "
                       f"{_defect(1)}")
        if printable is True and score < ladder.printable_line:
            out.append(f"{door}({rel}): {path} = {score:g} (under the printable line) beside "
                       f"printable=True {_defect(None)}")
    if wont_print:
        passing = [f"{p} = {s:g}" for p, s in claims.scores if s >= ladder.printable_line]
        passing += [f"{p} = {g}" for p, g in claims.grades if g in ladder.passing_grades]
        if passing:
            out.append(f"{door}({rel}): verdict wont_print beside {', '.join(passing)} {_defect(1)}")
        if any(v for _p, v in claims.printable):
            out.append(f"{door}({rel}): verdict wont_print beside printable=True {_defect(1)}")
    if ready and any(v is False for _p, v in claims.printable):
        out.append(f"{door}({rel}): verdict ready beside printable=False {_defect(None)}")
    # A sentence's numbers are checked against EVERY score and grade the reply
    # carries, under any name: a message may quote a readiness or structural
    # score, and only a number found nowhere is a disagreement.
    any_scores = [float(v) for p, v in walk(reply)
                  if _leaf(p).endswith("score") and isinstance(v, (int, float)) and not isinstance(v, bool)]
    any_grades = {v.strip()[0] for p, v in walk(reply)
                  if _leaf(p).endswith("grade") and isinstance(v, str) and re.fullmatch(r"\s*[A-F][+-]?\s*", v)}
    for key in _MESSAGE_KEYS:
        text = reply.get(key)
        if not isinstance(text, str):
            continue
        for match in re.finditer(r"(\d{1,3})\s*/\s*100", text):
            said = float(match.group(1))
            if any_scores and not any(abs(said - s) < 0.51 for s in any_scores):
                out.append(f"{door}({rel}): {key} says {match.group(0)}; no score in the reply is {said:g} "
                           f"{_defect(None)}")
        for match in re.finditer(r"\bgrade\s+([A-F])\b", text):
            if any_grades and match.group(1) not in any_grades:
                out.append(f"{door}({rel}): {key} says grade {match.group(1)}; the reply grades "
                           f"{sorted(any_grades)} {_defect(None)}")
        if re.match(r"\s*won'?t print", text, re.I) and claims.verdicts and not wont_print:
            out.append(f"{door}({rel}): {key} says it won't print; the verdict is "
                       f"{[v for _p, v in claims.verdicts]} {_defect(None)}")
    for where, text in _sentences(reply):
        if (said := _SAYS_THE_SCORE.search(text)):
            out.append(f"{door}({rel}): {where} says \"{said.group(0)}\" {_defect(14)}")
        elif (said := _SAYS_THE_STRUCTURAL_SCORE.search(text) or _SAYS_A_SCORE.search(text)):
            out.append(f"{door}({rel}): {where} says \"{said.group(0)}\" {_defect(14, same_class=True)}")
    # A reply that asks the units question judged a part nobody meant if it
    # also counts structural risks: at a thousandth of its size every risk
    # describes something else, so the question is the whole answer.
    if asks_units(reply):
        for path, counted in claims.structural:
            out.append(f"{door}({rel}): {path} = {counted!r} beside the units question {_defect(15)}")
        for where, text in _sentences(reply):
            if (said := _SAYS_STRUCTURAL_COUNTS.search(text)):
                out.append(f"{door}({rel}): {where} says \"{said.group(0)}\" beside the units question "
                           f"{_defect(15)}")
    return out


# ---------------------------------------------------------------------------
# Guard F: a sentence never says HOW Kiln worked its answer out (defect #16)
# ---------------------------------------------------------------------------
#
# A reply says what Kiln found and what to do about it.  How Kiln arrived
# there -- a name from inside its code, the module a rule lives in, a rule's
# id and version, the weight or factor a method multiplied by -- is the
# method, and a sentence carrying it hands the reader the recipe one clue at
# a time.  The leak gates refuse names and private text in SOURCE files; this
# reads what a door RETURNS, in the sentences ``_sentences`` reads for the
# score guard (#14), so the two guards agree about what a sentence is.
#
# It judges SHAPES only: an identifier, a path, a method's word beside a bare
# number, a ``key=number`` dump.  A method written out in plain English
# passes; no scanner can judge how much plain prose is too much, and the
# writer owns that.
#
# A sentence may say, and this never fires on: the person's numbers in their
# units (mm, g, minutes, °C, %, layers), a count, a tier named as a fact, and
# every name that is Kiln's INTERFACE -- a tool and its parameters, an
# environment setting Kiln reads, a key or value the same reply carries (a
# refusal code, a field to read), and the caller's own words (their file
# names, their printer's name).

#: A run holding a slash, or starting at a home folder: a path or a URL,
#: judged whole by the source-path rule and never word by word.
_PATH_RUN = re.compile(r"[^\s()\[\]{}<>\"'`,;]*[/\\][^\s()\[\]{}<>\"'`,;]*|~[^\s()\[\]{}<>\"'`,;]*")
#: Where a path or a bare file name points into Kiln's own code or data.
_KILN_SOURCE = re.compile(
    r"\.pyc?\b|\.pyi\b|(?:^|[/\\])kiln_pro[/\\]|(?:^|[/\\])src[/\\]kiln(?:[/\\]|$)|site-packages[/\\]kiln"
    r"|(?:^|[/\\])kiln[/\\](?:data|src|tests|scripts)[/\\]",
    re.IGNORECASE,
)
#: A bare file name: the person's file, or one Kiln made for them.  Masked
#: before identifiers are read, so ``donut_mm.stl`` is never a name.
_FILE_NAME = re.compile(
    r"\b[\w.-]+\.(?:stl|3mf|obj|glb|gltf|step|stp|amf|ply|off|scad|gcode|bgcode|png|jpe?g|webp|gif|svg|"
    r"dxf|pdf|json|ya?ml|toml|csv|txt|log|db|sqlite|cfg|ini|zip|cache)\b",
    re.IGNORECASE,
)
#: A bare Python file name: Kiln's source, wherever it is said.
_PY_FILE = re.compile(r"\b[\w.-]+\.pyc?\b")
#: The identifier shapes code has and a sentence does not.
_DOTTED_MODULE = re.compile(r"\bkiln(?:_pro)?(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_RULE_ID = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)*_v\d+\b")
_UNDERSCORE_NAME = re.compile(r"(?<![\w])_+[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*\b")
_CONSTANT_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SNAKE_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_URL = re.compile(r"\b(?:https?|file)://\S+", re.IGNORECASE)
#: An environment setting's name as Kiln's code spells it: a quoted
#: ``KILN_*`` literal.  A Python constant is never quoted, so a constant
#: named like a setting is still judged as a constant.
_SETTING_NAME = re.compile(r"""["'](KILN_[A-Z0-9_]+)["']""")
#: A name a tool's docstring hands the agent: ``like_this``.
_BACKTICKED = re.compile(r"``([^`\n]+)``")

#: The words a method's own numbers go by.  Each fires only beside a BARE
#: number: one with no physical unit after it (``weight 24 g`` is a part's
#: mass; ``weight 0.80`` is a method's).  The number follows the word
#: directly, after a colon, an equals sign or "is"/"of": a comma or a bracket
#: ends the word's phrase.
_METHOD_WORD = re.compile(
    r"(?<![-\w])(?P<word>weights?|coefficients?|factors?|multipliers?|contributions?|"
    r"penalt(?:y|ies)|deductions?|thresholds?|bias(?:es)?)\b"
    r"(?:\s*[:=]\s*|\s+(?:(?:is|of|was|at|by|set to)\s+)?)(?P<num>[-+~±]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
#: The figures named with a method's word that are the answer, not the
#: method: the safety factor a load check reports, a material's friction or
#: expansion coefficient, and the flow multiplier a person types into their
#: slicer.
_METHOD_WORD_IS_THE_ANSWER = re.compile(
    r"(?:safety\s+|friction\s+|expansion\s+|flow\s+|extrusion\s+)$", re.IGNORECASE)
#: A ``key=number`` dump: ``readiness=62``, ``min=75``.  The number is bare
#: (``overhangs=25.5%`` is a measurement), and the key is no parameter or
#: setting the caller can pass (``radius_mm=2``, ``KILN_X=1``: an
#: interface name shaped like one, with an underscore or in capitals) and no
#: single letter (``z=4`` is a place on the part, ``N=8`` a count of prints).
_KEY_EQUALS_NUMBER = re.compile(r"(?<![\w.])(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*=\s*(?P<num>[-+]?\d+(?:\.\d+)?)")
#: Points as a score's unit: ``+2.1 warp pts``, ``2.1-pt warp reduction``,
#: ``12 points closer``.  A typeface size (``12 pt text``) and a point on the
#: part (``its lowest point is 6 mm below``) are no hit.
_POINTS = re.compile(
    r"[-+]?\d+(?:\.\d+)?\s*(?:[A-Za-z]+\s+)?pts\b"
    r"|\d+(?:\.\d+)?-pt\b(?!\s+(?:font|text|type|letters?)\b)"
    r"|[-+]\d+(?:\.\d+)?\s*points?\b"
    r"|\d+(?:\.\d+)?\s*points?\s+(?:closer|further|farther|better|worse|higher|lower|more|less|up|down|"
    r"ahead|behind|above|below)\b",
    re.IGNORECASE,
)
#: What may follow a number for it to be a measurement in the person's units
#: or a count, never a method's own number.  A percentage beside a method's
#: word is still the method (``weight 30%``), so ``%`` counts only for a
#: ``key=number`` dump, where it is the part's (``overhangs=25.5%``).
_UNIT_AFTER = re.compile(
    r"\s?(?:mm(?:[23²³]|/s|/min)?|cm|µm|um|nm|m(?=\W|$)|km|inch(?:es)?|\"|ft|g(?=\W|$)|kg|mg|lb|oz|"
    r"s(?=\W|$)|secs?|seconds?|ms|mins?|minutes?|h(?=\W|$)|hrs?|hours?|days?|weeks?|°|º|deg(?:rees?)?|"
    r"[CFK](?=\W|$)|mpa|gpa|kpa|pa(?=\W|$)|psi|n(?=\W|$)|kn|n·m|j(?=\W|$)|kj|w(?=\W|$)|kw|kwh|v(?=\W|$)|"
    r"ma(?=\W|$)|hz|rpm|usd|layers?|parts?|pieces?|cop(?:y|ies)|prints?|walls?|perimeters?|lines?|"
    r"colou?rs?|slots?|objects?|steps?|edges?|faces?|triangles?|bod(?:y|ies)|printers?|spools?|"
    r"checks?|issues?|warnings?|risks?|times(?=\W|$))",
    re.IGNORECASE,
)
_PERCENT_AFTER = re.compile(r"\s?%")


def interface_words(mcp: Any = None, *roots: Path, module_prefixes: tuple[str, ...] = (),
                    manifests: tuple[Path, ...] = ()) -> frozenset[str]:
    """The names a sentence may say because they are Kiln's interface: every
    tool registered on *mcp* (from modules under *module_prefixes*, when
    given) and every tool a manifest in *manifests* lists (the paid tools a
    plain install names), each with its parameters and each name its
    description hands the agent in double backticks (a field to read, a value
    to pass); and every environment setting the code under *roots* reads (a
    quoted ``KILN_*``)."""
    words: set[str] = set()

    def tool(name: str, params: Any, doc: str) -> None:
        words.add(name)
        words.update(params)
        for quoted in _BACKTICKED.findall(doc or ""):
            words.update(_IDENT.findall(quoted))

    for name, entry in sorted(getattr(getattr(mcp, "_tool_manager", None), "_tools", {}).items()):
        fn = getattr(entry, "fn", None)
        if fn is None or (module_prefixes and not str(getattr(fn, "__module__", "")).startswith(module_prefixes)):
            continue
        params: Any = ()
        with contextlib.suppress(TypeError, ValueError):
            params = inspect.signature(fn).parameters
        tool(name, params, inspect.getdoc(fn) or "")
    for manifest in manifests:
        with contextlib.suppress(OSError, ValueError):
            for entry in json.loads(Path(manifest).read_text(encoding="utf-8")).get("tools", []):
                tool(entry["name"], (entry.get("parameters") or {}).get("properties", {}),
                     entry.get("description", ""))
    for root in roots:
        for path in Path(root).rglob("*.py"):
            with contextlib.suppress(OSError):
                words.update(_SETTING_NAME.findall(path.read_text(encoding="utf-8", errors="replace")))
    return frozenset(words)


def _words_in(node: Any) -> set[str]:
    """Every identifier-shaped word in *node*'s strings and keys."""
    out: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_IDENT.findall(str(key)))
            out.update(_words_in(value))
    elif isinstance(node, (list, tuple, set, frozenset)):
        for value in node:
            out.update(_words_in(value))
    elif isinstance(node, str):
        out.update(_IDENT.findall(node))
    return out


def _reply_names(reply: Any) -> set[str]:
    """The names a reply carries itself: every key, and every string value
    shaped like one name (a refusal code, an id, a check's name)."""
    out: set[str] = set()
    for path, value in walk(reply):
        out.add(_leaf(path))
        out.update(_IDENT.findall(path.rsplit(".", 1)[-1]))
        if isinstance(value, str) and _IDENT.fullmatch(value.strip()):
            out.add(value.strip())
    return out


def _masked(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _bare(text: str, end: int, *, percent_is_a_unit: bool) -> bool:
    """Whether the number ending at *end* stands with no unit after it."""
    rest = text[end:]
    if _UNIT_AFTER.match(rest):
        return False
    return not (percent_is_a_unit and _PERCENT_AFTER.match(rest))


def method_said_in(text: str, allowed: frozenset[str] | set[str] = frozenset(),
                   reply_names: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, str, str]]:
    """Each ``(kind, said, word)`` in one sentence where it says how Kiln
    worked its answer out.

    ``kind`` is ``internal_name`` (an identifier, a module or source path, a
    rule id) or ``method_number`` (a method's weight, factor, penalty or
    threshold beside its bare number, a ``key=number`` dump, points as a
    score's unit).  ``word`` is the stable part of what was said -- the name
    itself, or the method's word -- for a ledger to key on.

    *allowed* holds the names any sentence may say: Kiln's interface
    (:func:`interface_words`) and the caller's own words.  *reply_names* are
    the reply's own keys and name-shaped values: a sentence may point at a
    field or repeat a refusal code the reply carries, but a rule's id and
    version is the method even when the reply carries it as a value.
    """
    found: list[tuple[str, str, str]] = []
    spans: list[tuple[int, int]] = []
    for run in _PATH_RUN.finditer(text):
        path = run.group(0).rstrip(".:!?")
        if _KILN_SOURCE.search(path):
            found.append(("internal_name", path, path))
        spans.append(run.span())
    for name in _PY_FILE.finditer(text):
        if not any(s <= name.start() < e for s, e in spans):
            found.append(("internal_name", name.group(0), name.group(0)))
            spans.append(name.span())
    spans.extend(m.span() for m in _FILE_NAME.finditer(text))
    words = _masked(text, spans)
    nothing: frozenset[str] = frozenset()
    for rule, ok, also in ((_DOTTED_MODULE, nothing, nothing), (_RULE_ID, allowed, nothing),
                           (_UNDERSCORE_NAME, nothing, nothing), (_CONSTANT_NAME, allowed, reply_names),
                           (_SNAKE_NAME, allowed, reply_names)):
        hits = list(rule.finditer(words))
        found.extend(("internal_name", m.group(0), m.group(0)) for m in hits
                     if m.group(0) not in ok and m.group(0) not in also)
        words = _masked(words, [m.span() for m in hits])
    prose = _URL.sub(lambda m: " " * len(m.group(0)), text)
    taken: list[tuple[int, int]] = []

    def fresh(span: tuple[int, int]) -> bool:
        if any(span[0] < e and s < span[1] for s, e in taken):
            return False
        taken.append(span)
        return True

    for said in _METHOD_WORD.finditer(prose):
        if _METHOD_WORD_IS_THE_ANSWER.search(prose[:said.start()]) or re.match(
                r"\s*of\s+safety\b", prose[said.end("word"):], re.IGNORECASE):
            continue
        if _bare(prose, said.end("num"), percent_is_a_unit=False) and fresh(said.span()):
            found.append(("method_number", said.group(0), said.group("word").lower()))
    for said in _KEY_EQUALS_NUMBER.finditer(prose):
        key = said.group("key")
        if len(key) == 1 or (key in allowed and ("_" in key or key.isupper())):
            continue
        if _bare(prose, said.end("num"), percent_is_a_unit=True) and fresh(said.span()):
            found.append(("method_number", said.group(0), f"{key}="))
    for said in _POINTS.finditer(prose):
        if fresh(said.span()):
            found.append(("method_number", said.group(0).strip(), "points"))
    return found


def method_said(reply: Any, *, interface: frozenset[str] = frozenset(),
                callers_words: Any = None) -> list[tuple[str, str, str, str]]:
    """Each ``(where, kind, said, word)`` where a sentence of *reply* says how
    Kiln worked its answer out (see :func:`method_said_in`).

    *interface* is :func:`interface_words`; *callers_words* is anything the
    call was handed (its arguments), whose words a reply may repeat.
    """
    allowed = set(interface) | _words_in(callers_words)
    names = _reply_names(reply)
    out = []
    for where, text in _sentences(reply):
        for kind, said, word in method_said_in(text, allowed, names):
            out.append((where, kind, said, word))
    return out


# ---------------------------------------------------------------------------
# Guard D: the same object in two files gets the same answer
# ---------------------------------------------------------------------------

#: What must not move between the two files of a pair, by what differs.
#: Unit: geometry counts (a weld or a component tolerance in millimetres makes
#: them move).  Placement: everything about the part, since every slicer drops
#: a part onto the bed before it slices.
_PAIR_FIELDS = {
    "unit": ("pieces", "closed", "open_edges", "self_intersections", "structural"),
    "placement": ("pieces", "closed", "open_edges", "self_intersections",
                  "scores", "grades", "printable", "verdicts"),
}


def check_pair(door: str, kind: str, rel_a: str, reply_a: dict[str, Any],
               rel_b: str, reply_b: dict[str, Any]) -> list[str]:
    fields = _PAIR_FIELDS[kind]
    a, b = read_claims(reply_a).by_path(), read_claims(reply_b).by_path()
    defect = 3 if kind == "unit" else 5
    out = []
    for key in sorted(set(a) & set(b)):
        name, path = key.split(":", 1)
        if name in fields and a[key] != b[key]:
            out.append(f"{door}: {path} is {a[key]!r} for {rel_a} and {b[key]!r} for {rel_b}, the "
                       f"same object differing only in its {kind} "
                       f"{_defect(15 if name == 'structural' else defect)}")
    # Structural counts for one file and none for the other are the same
    # answer only when the other asks the units question instead.
    for key in sorted(set(a) ^ set(b)):
        name, path = key.split(":", 1)
        if name != "structural" or "structural" not in fields:
            continue
        rel_none, reply_none = (rel_b, reply_b) if key in a else (rel_a, reply_a)
        if not asks_units(reply_none):
            out.append(f"{door}: {path} is counted for {rel_a if key in a else rel_b} and not for "
                       f"{rel_none}, which does not say why (the units question), the same object "
                       f"differing only in its {kind} {_defect(15)}")
    ok_a, ok_b = reply_a.get("success"), reply_b.get("success")
    if kind == "placement" and ok_a is not None and ok_b is not None and ok_a != ok_b:
        out.append(f"{door}: success={ok_a} for {rel_a} and {ok_b} for {rel_b}, the same part "
                   f"differing only in where it sits {_defect(5)} -- "
                   f"{str(error_of(reply_a).get('message') or error_of(reply_b).get('message'))[:160]}")
    return out


# ---------------------------------------------------------------------------
# Guard E: a caller's mistake is a refusal, never a crash or a retry loop
# ---------------------------------------------------------------------------


def check_refusal(door: str, case: str, reply: dict[str, Any]) -> list[str]:
    out = []
    if "_raised" in reply:
        out.append(f"{door} [{case}]: raised at the caller -- {reply['_raised']} {_defect(None)}")
        return out
    if "_timed_out" in reply:
        out.append(f"{door} [{case}]: no answer in {reply['_timed_out']:.0f}s {_defect(None)}")
        return out
    err = error_of(reply)
    code = str(err.get("code") or "")
    defect = 6 if case.startswith("printer_id") else None
    if code.upper() == "INTERNAL_ERROR":
        out.append(f"{door} [{case}]: INTERNAL_ERROR for the caller's own mistake -- "
                   f"{str(err.get('message'))[:200]} {_defect(defect)}")
    elif err.get("retryable") is True and not _TRANSIENT_WORDS.search(code):
        out.append(f"{door} [{case}]: marked retryable ({code or 'no code'}) though the same input "
                   f"gets the same answer -- {str(err.get('message'))[:200]} {_defect(defect)}")
    return out


def caller_mistakes(door: Door, work: Path) -> dict[str, dict[str, Any]]:
    """The caller mistakes every door is handed, each as a full argument set
    (geometry filled with a file that does not open, or a printer that is not
    there).  Built per door, so a new door is asked the day it registers."""
    work.mkdir(parents=True, exist_ok=True)
    garbage = work / "not_a_model.stl"
    garbage.write_text("this is a shopping list, not a model\n")
    mistakes: dict[str, dict[str, Path]] = {
        "missing_file": {p: work / "does_not_exist.stl" for p in door.geometry_params},
        "not_a_model": {p: garbage for p in door.geometry_params},
    }
    cases: dict[str, dict[str, Any]] = {}
    for name, files in mistakes.items():
        cases[name] = {"files": files, "extra": {}}
    if "printer_id" in door.optional or "printer_id" in door.required:
        good = stage("knob_on_bed_mm.stl", work / "good")
        for pid in ("default", "no_such_printer_xyz"):
            cases[f"printer_id={pid!r}"] = {
                "files": {p: good for p in door.geometry_params},
                "extra": {"printer_id": pid},
            }
    if "output_path" in door.optional:
        good = stage("knob_on_bed_mm.stl", work / "good")
        cases["output_path_in_a_missing_folder"] = {
            "files": {p: good for p in door.geometry_params},
            "extra": {"output_path": str(work / "no" / "such" / "folder" / "out.stl")},
        }
    return cases


# ---------------------------------------------------------------------------
# Guard B: a composed print file is sliced by the slicer Kiln drives
# ---------------------------------------------------------------------------

#: The printer composed files are made for and sliced on: a four-slot AMS
#: machine, the multicolour printer Kiln drives most.
MULTICOLOUR_PRINTER = "bambu_a1"

#: What a reply says when it promises the emitted file slices.
SLICE_CLAIM = re.compile(r"slices (this file |the file |it )?(clean|as-is|as is)\b|verified in (prusa|orca)",
                         re.IGNORECASE)


def check_compose_and_slice(mcp: Any, set_name: str, corpus: dict[str, dict[str, Any]],
                            work: Path, *, posture: Callable[[], Any] = contextlib.nullcontext,
                            root: Path = CORPUS_DIR) -> list[str]:
    """Compose one multicolour set's touching pieces into a 3MF, then hand
    that file to Kiln's own slice door for the same printer.

    The compose reply's sentences must count colours, not parts, and a file
    Kiln makes to be printed must slice in the slicer Kiln picks for it --
    whatever the reply says, and doubly when it says so.  ``posture`` wraps
    both calls (public Kiln runs them as a plain install).
    """
    rels = MULTICOLOUR_SETS[set_name]
    colours = sorted({corpus[rel]["pieces"][0]["colour"] for rel in rels})
    parts = []
    for rel in rels:
        colour = corpus[rel]["pieces"][0]["colour"]
        parts.append({"stl_path": str(stage(rel, work / "parts", root)), "name": Path(rel).stem,
                      "material": colour, "extruder": colours.index(colour) + 1})
    out = work / f"{set_name}.3mf"
    with posture():
        composed = call(mcp, "compose_multicolor_3mf",
                        {"parts": parts, "output_path": str(out), "printer_id": MULTICOLOUR_PRINTER})
    if not composed.get("success"):
        return [f"compose_multicolor_3mf({set_name}): refused the set -- {error_of(composed) or composed} "
                f"{_defect(None)}"]
    findings = []
    message = str(composed.get("message") or "")
    for said in re.findall(r"(\d+)[- ]colou?r", message):
        if int(said) != len(colours):
            findings.append(f"compose_multicolor_3mf({set_name}): the message says {said} colours for "
                            f"{len(colours)} colours in {len(parts)} parts {_defect(9)} -- {message[:120]}")
    promised = bool(SLICE_CLAIM.search(all_text(composed)))
    with posture():
        sliced = call(mcp, "slice_model", {"input_path": str(out), "printer_id": MULTICOLOUR_PRINTER,
                                           "output_dir": str(work / "sliced"), "material": "PLA"})
    if not sliced.get("success"):
        why = str(error_of(sliced).get("message") or sliced)
        labels = [_defect(n) for n, cue in ((10, "conflict"), (11, "flush_volumes_matrix")) if cue in why]
        findings.append(
            f"slice_model({set_name}.3mf): Kiln's own slice refused the composed file"
            f"{' the compose door says slices' if promised else ''} "
            f"{' '.join(labels) or _defect(10, same_class=True)} -- {why[:700]}"
        )
    return findings


# ---------------------------------------------------------------------------
# Plain-install posture for public Kiln's sweep
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def without_kiln_pro() -> Iterator[None]:
    """Run as a customer's ``pip install kiln3d``: every ``kiln_pro`` import fails."""
    saved = {n: m for n, m in sys.modules.items() if n == "kiln_pro" or n.startswith("kiln_pro.")}
    for name in saved:
        del sys.modules[name]
    sys.modules["kiln_pro"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.modules.pop("kiln_pro", None)
        sys.modules.update(saved)
