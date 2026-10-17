"""A make from a person's picture never comes back silent about the picture.

On an install without kiln-pro nothing can read a saved goal or draw a
comparison, so the picture doors say so in ``reference_check`` (status
``not_compared``) instead of leaving the block out -- a result with no block
reads the same as one nobody asked to match a picture.  These tests walk the
doors as a plain install: kiln-pro removed from the import system.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import kiln.server as srv
from kiln.front_door import join_goal
from kiln.generation.base import GenerationJob, GenerationResult, GenerationStatus
from kiln.reference_compare import (
    COMPARISON_FAILED,
    NOT_COMPARED,
    NOT_COMPARED_WITH_GOAL,
    NOT_COMPARED_WITH_PICTURE,
    PICTURE_NOT_COMPARED,
    compare_with_goal,
)


@pytest.fixture
def plain_install(monkeypatch):
    """No kiln-pro: every ``from kiln_pro... import`` raises ImportError --
    including a submodule an earlier test already imported, which the
    import system would otherwise hand back from its cache."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _tools_of(plugin) -> dict:
    captured: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    plugin.register(_MCP())
    return captured


_ASCII_CUBE = """solid cube
""" + "".join(
    f"facet normal 0 0 0\nouter loop\nvertex {a}\nvertex {b}\nvertex {c}\nendloop\nendfacet\n"
    for a, b, c in [
        ("0 0 0", "10 10 0", "10 0 0"), ("0 0 0", "0 10 0", "10 10 0"),
        ("0 0 10", "10 0 10", "10 10 10"), ("0 0 10", "10 10 10", "0 10 10"),
        ("0 0 0", "10 0 0", "10 0 10"), ("0 0 0", "10 0 10", "0 0 10"),
        ("0 10 0", "10 10 10", "10 10 0"), ("0 10 0", "0 10 10", "10 10 10"),
        ("0 0 0", "0 0 10", "0 10 10"), ("0 0 0", "0 10 10", "0 10 0"),
        ("10 0 0", "10 10 0", "10 10 10"), ("10 0 0", "10 10 10", "10 0 10"),
    ]
) + "endsolid cube\n"


@pytest.fixture
def made(tmp_path) -> str:
    path = tmp_path / "made.stl"
    path.write_text(_ASCII_CUBE)
    return str(path)


class _Provider:
    name = "fake"
    display_name = "Fake"

    def __init__(self, made: str) -> None:
        self.made = made
        self.source_image = ""

    def generate(self, prompt, **_kw):
        return GenerationJob(
            id="job1", provider=self.name, prompt=prompt,
            status=GenerationStatus.SUCCEEDED, progress=100, created_at=0.0,
        )

    def download_result(self, job_id, output_dir=None):
        return GenerationResult(
            job_id=job_id, provider=self.name, local_path=self.made, format="stl",
            file_size_bytes=Path(self.made).stat().st_size, prompt="",
            source_image=self.source_image,
        )


@pytest.fixture
def provider(made, monkeypatch):
    p = _Provider(made)
    monkeypatch.setattr(srv, "_get_generation_provider", lambda name="meshy": p)
    return p


def _envelope(resp):
    if isinstance(resp, list):
        return next(x for x in resp if isinstance(x, dict))
    return resp


# ---------------------------------------------------------------------------
# The helper every make door calls
# ---------------------------------------------------------------------------


def test_a_make_that_names_a_goal_says_it_was_not_compared(plain_install, made):
    result = join_goal("goal-1", {"success": True, "output_path": made}, made)
    assert result["reference_check"] == {
        "status": NOT_COMPARED,
        "references": [],
        "reason": NOT_COMPARED_WITH_GOAL,
    }


def test_a_make_that_names_no_goal_has_no_picture_to_mention(plain_install, made):
    result = join_goal("", {"success": True, "output_path": made}, made)
    assert "reference_check" not in result


def test_a_failed_make_is_not_told_about_pictures(plain_install):
    result = join_goal("goal-1", {"success": False, "error": "no"}, None)
    assert "reference_check" not in result


def test_a_comparison_that_fails_is_said(monkeypatch, made):
    import types

    def broken(*_a, **_k):
        raise RuntimeError("renderer fell over")

    fake = types.ModuleType("kiln_pro.design_brief.explicit_attach")
    fake.compare_with_goal_reference = broken
    monkeypatch.setitem(sys.modules, "kiln_pro.design_brief.explicit_attach", fake)
    result: dict = {"success": True}
    compare_with_goal("goal-1", result, made)
    assert result["reference_check"]["status"] == NOT_COMPARED
    assert result["reference_check"]["reason"] == COMPARISON_FAILED


# ---------------------------------------------------------------------------
# The doors, as FastMCP calls them
# ---------------------------------------------------------------------------


def test_iterate_design_says_its_make_was_not_compared(plain_install, provider):
    from kiln.plugins.design_reasoning_tools import plugin

    out = _envelope(
        _tools_of(plugin)["iterate_design"](
            prompt="cube(10);", provider="openscad", max_iterations=1, brief_id="goal-1",
        )
    )
    assert out["success"] is True, out
    assert out["reference_check"]["status"] == NOT_COMPARED
    assert out["reference_check"]["reason"] == NOT_COMPARED_WITH_GOAL


def test_a_model_made_from_a_picture_says_it_was_not_compared(plain_install, provider):
    from kiln.plugins.generation_ai_tools import plugin

    provider.source_image = "https://example.com/bull.png"
    out = _envelope(_tools_of(plugin)["download_generated_model"](job_id="job1"))
    assert out["success"] is True, out
    assert out["reference_check"] == {
        "status": NOT_COMPARED,
        "references": ["https://example.com/bull.png"],
        "reason": NOT_COMPARED_WITH_PICTURE,
    }


def test_the_photo_door_says_nothing_here_will_compare(plain_install, monkeypatch):
    from kiln.plugins.generation_ai_tools import plugin

    monkeypatch.delenv("KILN_MESHY_API_KEY", raising=False)
    out = _tools_of(plugin)["generate_model_from_image"](image_url="https://example.com/bull.png")
    assert out["reference_picture"] == {
        "picture": "https://example.com/bull.png",
        "compared": False,
        "note": PICTURE_NOT_COMPARED,
    }


# ---------------------------------------------------------------------------
# The picture an image-to-3D job was made from reaches its download
# ---------------------------------------------------------------------------


def test_a_meshy_image_job_downloads_with_the_picture_it_was_made_from(tmp_path):
    responses = pytest.importorskip("responses")
    from kiln.generation.meshy import _BASE_URL, MeshyProvider

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, f"{_BASE_URL}/image-to-3d", json={"result": "task-img"})
        rsps.add(
            responses.GET, f"{_BASE_URL}/image-to-3d/task-img",
            json={"status": "SUCCEEDED", "progress": 100,
                  "model_urls": {"obj": "https://cdn.example/model.obj"}},
        )
        rsps.add(responses.GET, "https://cdn.example/model.obj", body=b"v 0 0 0\n")
        rsps.add(responses.POST, f"{_BASE_URL}/text-to-3d", json={"result": "task-txt"})
        rsps.add(
            responses.GET, f"{_BASE_URL}/text-to-3d/task-txt",
            json={"status": "SUCCEEDED", "progress": 100,
                  "model_urls": {"obj": "https://cdn.example/words.obj"}},
        )
        rsps.add(responses.GET, "https://cdn.example/words.obj", body=b"v 0 0 0\n")

        p = MeshyProvider(api_key="test-key")
        p.generate("", image_url="https://example.com/bull.png")
        p.get_job_status("task-img")
        from_picture = p.download_result("task-img", output_dir=str(tmp_path))
        p.generate("a bull")
        p.get_job_status("task-txt")
        from_words = p.download_result("task-txt", output_dir=str(tmp_path))

    assert from_picture.source_image == "https://example.com/bull.png"
    assert from_words.source_image == ""
