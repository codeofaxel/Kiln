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
        assert audit.readiness_score >= 80  # thermal stress heuristics lower score
        assert audit.readiness_grade in ("A", "B")  # thermal stress may lower grade
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
        assert session.best_readiness_score >= 80  # thermal stress heuristics lower score
        assert session.attempts[0].readiness_score < session.attempts[1].readiness_score
        assert provider.prompts[0] != provider.prompts[1]
        assert session.attempts[0].next_prompt_suggestion == provider.prompts[1]

    def test_openscad_is_rejected_for_natural_language_original_design(self):
        with pytest.raises(GenerationError, match="compile-only backend"):
            generate_original_design(
                "phone stand with cable slot",
                provider="openscad",
            )


# ---------------------------------------------------------------------------
# Optional kiln-pro intent-verification bridge hook
# ---------------------------------------------------------------------------


class _FakeIntentGate:
    """Minimal IntentGate shape used by the bridge call."""

    def __init__(
        self,
        name: str,
        passed: bool,
        severity: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        self.name = name
        self.passed = passed
        self.severity = severity
        self.message = message
        self.details = details or {}


def _install_fake_kiln_pro(
    monkeypatch,
    *,
    gates: list[_FakeIntentGate],
    feedback_items: list | None = None,
):
    """Inject a fake ``kiln_pro.bridge`` exposing ``intent_verification``.

    Pre-imports the real ``kiln_pro`` modules so that monkeypatch's
    teardown restores them with their loaded state intact.  Without
    this, the cleanup would simply remove the entries from sys.modules
    and the next test's ``from kiln_pro.bridge import pro_features``
    would trigger a fresh ``_load_features()`` cycle whose plugin-load
    side effects can poison later test scoring.
    """
    import sys
    import types

    try:
        import kiln_pro  # noqa: F401
        import kiln_pro.bridge  # noqa: F401
        import kiln_pro.intent_verification  # noqa: F401
    except ImportError:
        pass

    iv_module = types.ModuleType("kiln_pro.intent_verification")

    def _verify_intent_from_sidecar(_path):
        return list(gates)

    def _gates_to_feedback(_failed_gates, *, original_prompt: str = ""):
        return list(feedback_items or [])

    iv_module.verify_intent_from_sidecar = _verify_intent_from_sidecar
    iv_module.gates_to_feedback = _gates_to_feedback

    class _FakeProFeatures:
        intent_verification = iv_module

        def is_available(self, feature: str) -> bool:
            return feature == "intent_verification"

    bridge_module = types.ModuleType("kiln_pro.bridge")
    bridge_module.pro_features = _FakeProFeatures()

    pkg_module = types.ModuleType("kiln_pro")
    pkg_module.bridge = bridge_module

    monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.intent_verification", iv_module)


class TestAuditOriginalDesignIntentVerification:
    """The optional kiln-pro intent_verification bridge call."""

    def test_no_kiln_pro_installed_returns_baseline_audit(self, monkeypatch):
        import builtins as _builtins
        real_import = _builtins.__import__

        def _no_kiln_pro(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("kiln_pro"):
                raise ImportError("simulated: kiln-pro not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(_builtins, "__import__", _no_kiln_pro)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            baseline = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        # Baseline audit has none of the intent gates.
        assert all(
            not g.name.startswith("intent_") for g in baseline.gates
        )
        # Sanity: baseline still produces the well-known gates.
        gate_names = {g.name for g in baseline.gates}
        assert "mesh_validation" in gate_names
        assert "printability" in gate_names

    def test_failing_intent_gates_drop_score_and_feed_retry_loop(self, monkeypatch):
        failing_gate = _FakeIntentGate(
            name="intent_bbox_x",
            passed=False,
            severity="critical",
            message="Declared bbox_x=20.00mm, observed bbox_x=10.00mm.",
            details={
                "measurement": "bbox_x",
                "expected": {"value": 20.0, "tolerance": 1.0},
                "observed": 10.0,
                "category": "size_and_shape",
            },
        )
        passing_gate = _FakeIntentGate(
            name="intent_bbox_z",
            passed=True,
            severity="info",
            message="Declared bbox_z matches observed.",
            details={"measurement": "bbox_z"},
        )

        fake_pf = {
            "original_prompt": "wall plaque 20mm wide",
            "feedback_type": "intent",
            "issues": ["Declared bbox_x=20mm, observed 10mm"],
            "constraints": [
                "declared bbox_x 20.00 mm, observed 10.00 mm — "
                "fix to match declared intent"
            ],
            "severity": "critical",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")

            # Baseline: no kiln-pro present at all.
            import builtins as _builtins
            real_import = _builtins.__import__

            def _no_kiln_pro(name, globals=None, locals=None, fromlist=(), level=0):
                if name.startswith("kiln_pro"):
                    raise ImportError("simulated: kiln-pro not installed")
                return real_import(name, globals, locals, fromlist, level)

            with monkeypatch.context() as m_baseline:
                m_baseline.setattr(_builtins, "__import__", _no_kiln_pro)
                baseline = audit_original_design(
                    path,
                    "wall plaque 20mm wide",
                    printer_model="bambu_a1",
                )

            # With kiln-pro: failing critical intent gate present.
            with monkeypatch.context() as m_pro:
                _install_fake_kiln_pro(
                    m_pro,
                    gates=[failing_gate, passing_gate],
                    feedback_items=[fake_pf],
                )
                audit = audit_original_design(
                    path,
                    "wall plaque 20mm wide",
                    printer_model="bambu_a1",
                )

        intent_gate_names = {
            g.name for g in audit.gates if g.name.startswith("intent_")
        }
        assert intent_gate_names == {"intent_bbox_x", "intent_bbox_z"}

        assert audit.readiness_score < baseline.readiness_score
        assert any(
            "bbox_x" in blocker for blocker in audit.blockers
        )

        assert any(
            item.get("feedback_type") == "intent"
            for item in audit.feedback
        )

    def test_overlay_failure_never_breaks_audit(self, monkeypatch):
        import sys
        import types

        try:
            import kiln_pro  # noqa: F401
            import kiln_pro.bridge  # noqa: F401
            import kiln_pro.intent_verification  # noqa: F401
        except ImportError:
            pass

        iv_module = types.ModuleType("kiln_pro.intent_verification")

        def _broken(_path):
            raise RuntimeError("simulated overlay failure")

        iv_module.verify_intent_from_sidecar = _broken
        iv_module.gates_to_feedback = lambda *a, **k: []

        class _FakeProFeatures:
            intent_verification = iv_module

            def is_available(self, feature: str) -> bool:
                return feature == "intent_verification"

        bridge_module = types.ModuleType("kiln_pro.bridge")
        bridge_module.pro_features = _FakeProFeatures()
        pkg_module = types.ModuleType("kiln_pro")
        pkg_module.bridge = bridge_module

        monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.intent_verification", iv_module)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        assert audit.readiness_score >= 0
        # No intent gates added because the verify call raised.
        assert not any(
            g.name.startswith("intent_") for g in audit.gates
        )
