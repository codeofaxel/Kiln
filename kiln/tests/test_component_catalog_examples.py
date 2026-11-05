"""Every component catalog example compiles, and the thread entries build what
their own parameters say.

An entry's ``example_call`` is the line an agent copies: Kiln pastes it into
the prompt it writes OpenSCAD from ("Usage: ...") and returns it from
``match_design_components``.  A call that does not compile teaches every agent
the same broken line.  (2026-11-05: ``threaded_nut(od=13, ...)`` passed an
argument BOSL2's threaded_nut does not take and failed its own assertion, and
``screw("M5x20", ...)`` read the 20 as a 20 mm pitch and failed.  Both had been
listed since the catalog was added, 2026-03-20.)  A dropped argument is the
quiet form of the same fault: OpenSCAD warns, leaves it out and builds the part
anyway.

Each call is compiled through ``kiln.parametric.compile_scad_code`` (Kiln's own
compile path: its curve rule, the bundled libraries on OPENSCADPATH, a left-out
part refused) and checked through the validator for an argument OpenSCAD
ignored.  The thread entries are then measured off the mesh against the
numbers their ``key_params`` state.  Those halves skip where OpenSCAD is not
installed; the named-argument check reads the bundled library's source and
runs everywhere.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

import kiln

_DATA = Path(kiln.__file__).resolve().parent / "data"
_CATALOG = json.loads((_DATA / "component_catalog.json").read_text(encoding="utf-8"))
_ENTRIES = {cid: entry for cid, entry in _CATALOG.items() if not cid.startswith("_")}
_BOSL2 = _DATA / "scad_libraries" / "BOSL2"

# An argument the called module does not take, known and not yet decided.
# Each entry fails the test the day it is fixed (strict xfail): remove it then.
_KNOWN_DROPPED = {
    "rack": "height is not an argument of BOSL2's rack(); the rack is built at its own default height",
}


def _openscad_available() -> bool:
    try:
        from kiln.generation.openscad import _find_openscad

        _find_openscad()
        return True
    except Exception:  # noqa: BLE001 - any failure to find a runnable binary means no OpenSCAD
        return False


needs_openscad = pytest.mark.skipif(
    not _openscad_available(),
    reason="OpenSCAD is not installed: the catalog's calls are judged by compiling them",
)


def _known(cid: str):
    reason = _KNOWN_DROPPED.get(cid)
    return [pytest.mark.xfail(reason=reason, strict=True)] if reason else []


def _code(entry: dict) -> str:
    return entry["import_line"] + "\n" + entry["example_call"] + "\n"


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> dict[str, tuple[str | None, str | None]]:
    """Every example built once, through Kiln's compile path: id -> (stl, error)."""
    from kiln.parametric import compile_scad_code

    out = tmp_path_factory.mktemp("component_catalog")
    built: dict[str, tuple[str | None, str | None]] = {}
    for cid, entry in _ENTRIES.items():
        stl = out / f"{cid}.stl"
        try:
            compile_scad_code(_code(entry), output_path=str(stl), timeout=300)
            built[cid] = (str(stl), None)
        except Exception as exc:  # noqa: BLE001 - a refusal of any kind is this entry's finding
            built[cid] = (None, str(exc))
    return built


def _mesh(compiled, cid):
    import trimesh

    stl, error = compiled[cid]
    assert stl, f"{cid} did not compile:\n{error}"
    return trimesh.load(stl)


# --- every example ---------------------------------------------------------


@needs_openscad
@pytest.mark.parametrize("cid", sorted(_ENTRIES))
def test_the_example_compiles(compiled, cid):
    stl, error = compiled[cid]
    assert stl is not None, f"{cid}: {_ENTRIES[cid]['example_call']}\n{error}"
    assert Path(stl).stat().st_size > 0


@needs_openscad
@pytest.mark.parametrize("cid", [pytest.param(c, marks=_known(c)) for c in sorted(_ENTRIES)])
def test_openscad_drops_no_argument_of_the_example(cid):
    from kiln.generation.openscad import OpenSCADProvider

    verdict = OpenSCADProvider().validate_scad(_code(_ENTRIES[cid]))
    dropped = [w["message"] for w in verdict["warnings"] if "not specified as parameter" in w["message"]]
    assert not dropped, f"{cid}: {_ENTRIES[cid]['example_call']}\n" + "\n".join(dropped)


def _split_top_level(text: str) -> list[str]:
    """Split on commas outside brackets and strings."""
    parts, depth, quote, cur = [], 0, None, ""
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts]


def _inside_parens(text: str, start: int) -> str:
    """The text between the '(' at *start* and its matching ')'."""
    depth, quote = 0, None
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    raise AssertionError(f"unbalanced parentheses after offset {start}")


def _module_parameters(name: str) -> set[str]:
    """The parameter names of BOSL2's ``module name(...)``, read from the bundled source."""
    found = []
    for path in sorted(_BOSL2.glob("*.scad")):
        # Comments out first: an apostrophe or a bracket in one would unbalance the scan.
        source = re.sub(r"//[^\n]*|/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for match in re.finditer(rf"^module\s+{re.escape(name)}\s*\(", source, re.MULTILINE):
            found.append(_inside_parens(source, match.end() - 1))
    assert len(found) == 1, f"expected one definition of module {name}() in the bundled BOSL2, found {len(found)}"
    return {p.split("=")[0].strip() for p in _split_top_level(found[0])}


@pytest.mark.parametrize("cid", [pytest.param(c, marks=_known(c)) for c in sorted(_ENTRIES)])
def test_every_named_argument_is_one_the_module_takes(cid):
    call = _ENTRIES[cid]["example_call"]
    head = re.match(r"\s*(\w+)\s*\(", call)
    assert head, f"{cid}: the example call names no module: {call}"
    takes = _module_parameters(head.group(1))
    named = [
        p.split("=")[0].strip()
        for p in _split_top_level(_inside_parens(call, head.end() - 1))
        if re.match(r"^\$?\w+\s*=", p)
    ]
    # $-variables (such as $slop) reach every module through OpenSCAD's scoping.
    unknown = [n for n in named if not n.startswith("$") and n not in takes]
    assert not unknown, f"{cid}: {head.group(1)}() takes no argument {unknown}: {call}"


# --- the thread entries, measured ------------------------------------------


def _surface_radius(mesh, theta_deg: float, zs: np.ndarray, outer: bool) -> np.ndarray:
    """Radius of the part's outer (or inner) surface along one half-plane
    through the z axis, at each height in *zs* (nan where the plane misses)."""
    from trimesh.intersections import mesh_plane

    t = np.radians(theta_deg)
    u = np.array([np.cos(t), np.sin(t), 0.0])
    segs = mesh_plane(mesh, plane_normal=[-np.sin(t), np.cos(t), 0.0], plane_origin=[0.0, 0.0, 0.0])
    rho, z = segs @ u, segs[:, :, 2]
    keep = (rho > 1e-6).all(axis=1)
    rho, z = rho[keep], z[keep]
    lo, hi = z.min(axis=1), z.max(axis=1)
    out = np.full(len(zs), np.nan)
    for j, height in enumerate(zs):
        sel = (lo <= height) & (hi >= height)
        if not sel.any():
            continue
        z0, z1, r0, r1 = z[sel, 0], z[sel, 1], rho[sel, 0], rho[sel, 1]
        dz = z1 - z0
        flat = np.abs(dz) < 1e-12
        r = np.where(
            flat,
            np.maximum(r0, r1) if outer else np.minimum(r0, r1),
            r0 + (height - z0) / np.where(flat, 1.0, dz) * (r1 - r0),
        )
        out[j] = r.max() if outer else r.min()
    return out


def _thread(mesh, z0: float, z1: float, *, outer: bool) -> dict[str, float]:
    """Crest and root diameter and pitch of a thread on the z axis, read
    between heights *z0* and *z1* on eight half-planes."""
    zs = np.arange(z0, z1, 0.005)
    crests, roots, pitches = [], [], []
    for theta in range(0, 360, 45):
        r = _surface_radius(mesh, theta, zs, outer)
        ok = np.isfinite(r)
        r, z = r[ok], zs[ok]
        crest, root = (r.max(), r.min()) if outer else (r.min(), r.max())
        tooth = (r > (crest + root) / 2) if outer else (r < (crest + root) / 2)
        starts = np.flatnonzero(np.diff(tooth.astype(int)) == 1) + 1
        ends = np.flatnonzero(np.diff(tooth.astype(int)) == -1)
        centres = [z[(a + b) // 2] for a in starts for b in ends[ends > a][:1]]
        crests.append(crest)
        roots.append(root)
        if len(centres) >= 3:
            pitches.append(float(np.mean(np.diff(centres))))
    return {
        "crest_d": 2 * float(np.median(crests)),
        "root_d": 2 * float(np.median(roots)),
        "pitch": float(np.median(pitches)) if pitches else float("nan"),
    }


def _default(cid: str, name: str):
    return _ENTRIES[cid]["key_params"][name]["default"]


@needs_openscad
def test_threaded_rod_is_the_rod_its_parameters_state(compiled):
    mesh = _mesh(compiled, "threaded_rod")
    lo, hi = mesh.bounds
    d, pitch, length = (_default("threaded_rod", k) for k in ("d", "pitch", "l"))
    thread = _thread(mesh, lo[2] + 3, hi[2] - 3, outer=True)

    assert abs((hi[2] - lo[2]) - length) < 0.01
    assert abs(thread["crest_d"] - d) < 0.02, thread
    assert abs(thread["pitch"] - pitch) < 0.01, thread
    assert thread["crest_d"] - thread["root_d"] > 0.5 * pitch, f"no real tooth: {thread}"


@needs_openscad
def test_threaded_nut_is_the_nut_its_parameters_state(compiled):
    mesh = _mesh(compiled, "threaded_nut")
    lo, hi = mesh.bounds
    width, size, height, pitch, slop = (_default("threaded_nut", k) for k in ("nutwidth", "id", "h", "pitch", "$slop"))
    thread = _thread(mesh, lo[2] + 1.2, hi[2] - 1.2, outer=False)

    assert abs(min(hi[0] - lo[0], hi[1] - lo[1]) - width) < 0.01, "nutwidth is across the flats"
    assert abs((hi[2] - lo[2]) - height) < 0.01
    # BOSL2 measures id at the thread roots and widens them by 4 x $slop.
    assert abs(thread["root_d"] - (size + 4 * slop)) < 0.03, thread
    assert abs(thread["pitch"] - pitch) < 0.01, thread
    assert thread["root_d"] - thread["crest_d"] > 0.5 * pitch, f"a plain hole, not a thread: {thread}"


@needs_openscad
def test_screw_is_the_screw_its_spec_states_and_stands_on_its_head(compiled):
    mesh = _mesh(compiled, "screw")
    lo, hi = mesh.bounds
    size, length = _default("screw", "spec").removeprefix("M").split(",")
    size, length = float(size), float(length)
    # Where the part narrows from head to shaft: the head is at the bottom.
    zs = np.arange(lo[2] + 0.05, hi[2], 0.05)
    reach = _surface_radius(mesh, 0.0, zs, outer=True)
    narrow = zs[reach < 0.6 * np.nanmax(reach)]
    assert narrow.size and narrow.min() > lo[2] + 1, "the head is not on the bed"
    head_top = narrow.min()
    thread = _thread(mesh, head_top + 3, hi[2] - 3, outer=True)

    assert abs((hi[2] - head_top) - length) < 0.1, "the spec's length is measured under the head"
    # The standard coarse pitch for M5 (ISO 261), and a crest inside the
    # standard screw tolerance band, a little under nominal.
    assert size == 5 and abs(thread["pitch"] - 0.8) < 0.01, thread
    assert size - 0.2 < thread["crest_d"] < size, thread
    # drive="hex": the key recess opens in the face on the bed.
    section = mesh.section(plane_origin=[0, 0, lo[2] + 1.0], plane_normal=[0, 0, 1])
    assert section is not None and len(section.discrete) == 2, "no key recess in the head"
