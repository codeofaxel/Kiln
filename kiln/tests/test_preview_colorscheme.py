"""Guard: every OpenSCAD preview render MUST use Kiln's colorscheme.

Kiln's preview system renders through OpenSCAD with
``--colorscheme=DeepOcean`` (a dark background + high-contrast grey
model) — the look every product preview shares. A render that omits
``--colorscheme`` falls back to OpenSCAD's default "Cornfield" theme
(a yellow model on cream) — the off-brand output an agent scratchpad
script leaked in 2026-07.

Lives in PUBLIC Kiln's own suite (not just kiln-pro's) deliberately:
public CI runs this repo's tests with kiln-pro NOT installed (see
ci.yml), so a kiln-pro-only copy of this guard would never protect a
public-only contributor who edits ``model_visualizer.py`` or
``decoration_helpers.py`` — the actual render sites, which all live
here. This is the copy that runs on every PR to this repo, no private
package required. It also scans an installed ``kiln_pro`` when present
(a full dev checkout), so it's a strict superset, never a narrower
check.

Structural scan of SOURCE — does not render — so it's fast and has no
OpenSCAD dependency.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_ALLOWLIST = {
    "test_preview_colorscheme.py": "this guard",
}

_OPENSCAD = re.compile(r"openscad", re.IGNORECASE)
_PNG_EXPORT = re.compile(r"imgsize|-o[\"'\s,]+[^\"']*\.png", re.IGNORECASE)
_COLORSCHEME = re.compile(r"colorscheme", re.IGNORECASE)


def _package_src_dir(mod_name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(mod_name)
    except ValueError:
        # A prior import in the same test session can leave a stub module in
        # sys.modules with no __spec__ (e.g. kiln.server's free-tier
        # pro-tool-stub registration for "kiln_pro") — find_spec() raises
        # ValueError instead of returning None for that case.  Absent-or-
        # stubbed both mean "nothing real to scan here", consistent with
        # this file's own design intent below.
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def _iter_python_sources():
    for mod in ("kiln", "kiln_pro"):
        root = _package_src_dir(mod)
        if root is None:
            continue  # kiln_pro absent in public CI by design — fine
        for py in root.rglob("*.py"):
            yield py


def test_every_openscad_png_render_names_a_colorscheme():
    offenders: list[str] = []
    scanned = 0
    for py in _iter_python_sources():
        if _ALLOWLIST.get(py.name):
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not (_OPENSCAD.search(text) and _PNG_EXPORT.search(text)):
            continue
        scanned += 1
        if not _COLORSCHEME.search(text):
            offenders.append(str(py))

    # Sanity: public Kiln alone has 3+ known render sites (model_visualizer,
    # decoration_helpers, multicolor_3mf).  Zero scanned means the detector
    # or package path resolution has drifted and this guard is vacuous.
    assert scanned >= 1, (
        "preview-colorscheme guard scanned zero OpenSCAD-PNG render sites "
        "in public Kiln — the detector or package path has drifted"
    )
    assert not offenders, (
        "OpenSCAD preview render(s) missing --colorscheme (would emit the "
        "off-brand Cornfield theme instead of DeepOcean):\n  "
        + "\n  ".join(offenders)
        + "\nRender previews through visualize_model, or add "
        "--colorscheme=DeepOcean to the OpenSCAD command."
    )


def _is_offender(text: str) -> bool:
    return bool(
        _OPENSCAD.search(text)
        and _PNG_EXPORT.search(text)
        and not _COLORSCHEME.search(text)
    )


def test_detector_flags_a_bad_render_and_clears_a_good_one():
    bad = 'cmd = [openscad, "-o", png_path, "--imgsize=800,600", "--render", scad]'
    good = bad.replace("--render", "--colorscheme=DeepOcean")
    unrelated = 'subprocess.run(["openscad", "-o", out_stl, scad_path])'
    assert _is_offender(bad)
    assert not _is_offender(good)
    assert not _is_offender(unrelated)
