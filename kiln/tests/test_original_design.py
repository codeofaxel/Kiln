"""Tests for kiln.original_design -- end-to-end original design auditing."""

from __future__ import annotations

import os
import struct
import tempfile
from unittest.mock import patch

import pytest

from kiln.generation.base import GenerationError, GenerationJob, GenerationResult, GenerationStatus
from kiln.original_design import audit_original_design, generate_original_design


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    header = b"\x00" * 80
    count = struct.pack("<I", len(triangles))
    body = b""
    for v1, v2, v3 in triangles:
        normal = struct.pack("<3f", 0.0, 0.0, 0.0)
        verts = struct.pack("<9f", *v1, *v2, *v3)
        body += normal + verts + struct.pack("<H", 0)
    return header + count + body


def _cube_triangles(size: float = 10.0) -> list[tuple]:
    s = size
    verts = [
        (0, 0, 0),
        (s, 0, 0),
        (s, s, 0),
        (0, s, 0),
        (0, 0, s),
        (s, 0, s),
        (s, s, s),
        (0, s, s),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (2, 6, 7),
        (2, 7, 3),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _thin_wall_triangles(wall_thickness: float = 0.3) -> list[tuple]:
    t = wall_thickness
    verts = [
        (0, 0, 0),
        (20, 0, 0),
        (20, 20, 0),
        (0, 20, 0),
        (0, 0, t),
        (20, 0, t),
        (20, 20, t),
        (0, 20, t),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (2, 6, 7),
        (2, 7, 3),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


def _write_stl(tmpdir: str, triangles: list[tuple], name: str) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as fh:
        fh.write(_make_binary_stl(triangles))
    return path


class _StubGenerationProvider:
    name = "gemini"
    display_name = "Gemini Deep Think"

    def __init__(self, paths: list[str]) -> None:
        self._paths = paths
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, format: str = "stl", style=None, **kwargs) -> GenerationJob:
        self.prompts.append(prompt)
        index = len(self.prompts)
        return GenerationJob(
            id=f"job-{index}",
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.SUCCEEDED,
            progress=100,
            created_at=0.0,
            format=format,
            style=style,
        )

    def get_job_status(self, job_id: str) -> GenerationJob:
        raise AssertionError("Synchronous stub should not be polled")

    def download_result(self, job_id: str, output_dir: str | None = None) -> GenerationResult:
        index = int(job_id.split("-")[-1]) - 1
        path = self._paths[index]
        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=path,
            format="stl",
            file_size_bytes=os.path.getsize(path),
            prompt=self.prompts[index],
        )


@pytest.fixture(autouse=True)
def _reset_kb():
    from kiln.design_intelligence import _reset_knowledge_base

    _reset_knowledge_base()
    yield
    _reset_knowledge_base()


class TestAuditOriginalDesign:
    def test_clean_part_can_score_as_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        assert audit.ready_for_print is True
        assert audit.readiness_score >= 90
        assert audit.readiness_grade == "A"
        assert audit.blockers == []
        assert audit.orientation is not None
        assert audit.enhanced_prompt["improved_prompt"] != "simple coaster"

    def test_thin_functional_part_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _thin_wall_triangles(0.3), "thin.stl")
            audit = audit_original_design(
                path,
                "wall shelf bracket that holds 10 lbs",
                printer_model="bambu_a1",
            )

        assert audit.ready_for_print is False
        assert audit.readiness_score < 75
        assert len(audit.feedback) > 0
        assert any("thin" in action.lower() or "wall thickness" in action.lower() for action in audit.next_actions)


class TestGenerateOriginalDesign:
    def test_generation_loop_can_recover_on_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            thin_path = _write_stl(tmpdir, _thin_wall_triangles(0.3), "thin.stl")
            cube_path = _write_stl(tmpdir, _cube_triangles(12.0), "cube.stl")
            provider = _StubGenerationProvider([thin_path, cube_path])

            with patch(
                "kiln.original_design._resolve_original_design_provider",
                return_value=(
                    "gemini",
                    provider,
                    "Gemini is preferred for original printable designs.",
                ),
            ):
                session = generate_original_design(
                    "wall shelf bracket that holds 10 lbs",
                    provider="auto",
                    printer_model="bambu_a1",
                    max_attempts=2,
                )

        assert session.ready_for_print is True
        assert session.best_attempt_number == 2
        assert session.attempts_made == 2
        assert session.best_readiness_score >= 90
        assert session.attempts[0].readiness_score < session.attempts[1].readiness_score
        assert provider.prompts[0] != provider.prompts[1]
        assert session.attempts[0].next_prompt_suggestion == provider.prompts[1]

    def test_openscad_is_rejected_for_natural_language_original_design(self):
        with pytest.raises(GenerationError, match="compile-only backend"):
            generate_original_design(
                "phone stand with cable slot",
                provider="openscad",
            )
