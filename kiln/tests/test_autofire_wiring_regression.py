"""Regression pins for two non-obvious autofire wirings.

Both wirings are inside large MCP tool functions where the
``attach_inspect_bundle`` call is easy to lose during an unrelated
refactor.  The file-level autofire-coverage meta-test in kiln-pro
catches "no autofire call in this file at all" but cannot tell the
difference between an import at the top and a real call inside the
right function.  These tests pin the call site at the function body
level.

Both wirings have non-default ``source_path`` plumbing that would
silently degrade to a no-op if the plumbing fell out — so the
regression test is meaningful, not cosmetic.
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _function_body(source: str, def_signature: str) -> str:
    """Return the slice from ``def_signature`` through the next
    ``@mcp.tool()`` decorator (or end of file)."""

    start = source.find(def_signature)
    assert start != -1, f"{def_signature!r} not found in source"
    next_tool = source.find("@mcp.tool()", start + 1)
    return source[start: next_tool if next_tool != -1 else len(source)]


def test_download_generated_model_autofires_on_local_path() -> None:
    """``download_generated_model`` is the SYNC mesh-producing tool in
    the AI-generation flow.  Its autofire MUST pass
    ``source_path=result.local_path`` explicitly, because the path is
    nested under ``result`` rather than living at a top-level key the
    helper's default stl_keys lookup would find.

    Pins:
      * the autofire call exists inside ``download_generated_model``,
      * it passes ``result.local_path`` (or equivalent) explicitly
        as ``source_path``.
    """

    path = _REPO_ROOT / "kiln/src/kiln/plugins/generation_ai_tools.py"
    body = _function_body(path.read_text(), "def download_generated_model(")

    assert "attach_inspect_bundle" in body, (
        "download_generated_model lost its autofire wiring — "
        "regression on G1 from autofire-gap-polish."
    )
    assert "source_path=result.local_path" in body, (
        "download_generated_model autofire dropped explicit "
        "source_path=result.local_path.  Default stl_keys lookup "
        "won't find local_path (it's nested under result), so the "
        "bundle would silently no-op without this kwarg."
    )


def test_generate_model_intentionally_unwired() -> None:
    """``generate_model`` and ``generate_model_from_image`` return
    async job descriptors with no mesh path on the response.  An
    autofire call here would be a misleading no-op — the helper
    finds no source_path and silently returns the plain dict, while
    the wiring suggests to readers that a bundle should arrive.

    Pin: these two tools MUST NOT call attach_inspect_bundle.  The
    real wiring lives downstream on ``download_generated_model``.
    """

    path = _REPO_ROOT / "kiln/src/kiln/plugins/generation_ai_tools.py"
    src = path.read_text()

    for sig in ("def generate_model(", "def generate_model_from_image("):
        body = _function_body(src, sig)
        assert "attach_inspect_bundle" not in body, (
            f"{sig!r} regained an autofire call.  This tool returns an "
            f"async job descriptor with no mesh path; autofire would be "
            f"a silent no-op.  Wire it on download_generated_model instead."
        )


def test_split_mesh_by_component_renders_largest_output() -> None:
    """``split_mesh_by_component`` returns ``{"file_paths": [comp1,
    comp2, ...]}``.  The wrong autofire (the original wiring) passed
    the INPUT mesh as source_path, so the bundle showed the user the
    pre-split mesh — wrong picture for a tool whose purpose is to
    produce separate components.

    Pin: the autofire source_path resolves to the LARGEST file from
    file_paths (by os.path.getsize), with the input file_path as a
    defensive fallback.
    """

    path = _REPO_ROOT / "kiln/src/kiln/plugins/mesh_tools.py"
    body = _function_body(path.read_text(), "def split_mesh_by_component(")

    assert "attach_inspect_bundle" in body, (
        "split_mesh_by_component lost its autofire wiring — "
        "regression on G2 from autofire-gap-polish."
    )
    assert "max(" in body and "os.path.getsize" in body, (
        "split_mesh_by_component autofire dropped the largest-output "
        "selection.  Without max(...key=os.path.getsize), the bundle "
        "would either show the input mesh (wrong: pre-split) or only "
        "the first component (arbitrary)."
    )
