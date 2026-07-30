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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


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

        # None of the pro verdict gates leak in...
        intent_gates = [g for g in baseline.gates if g.name.startswith("intent_")]
        assert [g.name for g in intent_gates] == ["intent_verification"]
        # ...but the audit says the check did not run, rather than
        # omitting it and reading as a clean bill of health.
        not_checked = intent_gates[0]
        assert not_checked.checked is False
        assert not_checked.passed is False
        assert not_checked.severity == "info"
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
        # The verify call raised, so no verdict gates — but a check that
        # blew up is a check that did not run, and the audit has to say
        # so.  Going quiet here is what let a design "pass" a gate that
        # never executed.
        intent_gates = [g for g in audit.gates if g.name.startswith("intent_")]
        assert [g.name for g in intent_gates] == ["intent_verification"]
        assert intent_gates[0].checked is False


# ---------------------------------------------------------------------------
# Optional kiln-pro saved-design summary
# ---------------------------------------------------------------------------


def _install_fake_kiln_pro_with_brief(
    monkeypatch,
    *,
    intent_gates: list[_FakeIntentGate],
    summary_dict: dict | None,
    intent_generator: str = "design_brief:abc123",
    has_design_brief: bool = True,
):
    """Inject a fake ``kiln_pro.bridge`` exposing intent_verification AND design_brief.

    The audit hook only fires the saved-design summary when both
    modules report available.  The fake's ``design_brief.check_brief_honor``
    returns ``summary_dict`` verbatim so the test pins the wrap-as-AuditGate
    path independently from the actual rollup logic (which lives in
    kiln-pro and has its own tests).
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
        return list(intent_gates)

    def _gates_to_feedback(_failed_gates, *, original_prompt: str = ""):
        return []

    class _FakeIntentObject:
        generator = intent_generator

    def _load_intent_sidecar(_path):
        return _FakeIntentObject()

    iv_module.verify_intent_from_sidecar = _verify_intent_from_sidecar
    iv_module.gates_to_feedback = _gates_to_feedback
    iv_module.load_intent_sidecar = _load_intent_sidecar

    db_module = types.ModuleType("kiln_pro.design_brief")

    def _check_brief_honor(*, intent_gates, intent_generator):
        return summary_dict

    db_module.check_brief_honor = _check_brief_honor

    class _FakeProFeatures:
        intent_verification = iv_module
        design_brief = db_module if has_design_brief else None

        def is_available(self, feature: str) -> bool:
            if feature == "intent_verification":
                return True
            if feature == "design_brief":
                return has_design_brief
            return False

    bridge_module = types.ModuleType("kiln_pro.bridge")
    bridge_module.pro_features = _FakeProFeatures()

    pkg_module = types.ModuleType("kiln_pro")
    pkg_module.bridge = bridge_module

    monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.intent_verification", iv_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.design_brief", db_module)


class TestAuditOriginalDesignBriefHonor:
    """Saved-design summary gate (kiln-pro design_brief integration).

    The audit appends ONE extra gate (named ``brief_honor``) when the
    mesh's intent sidecar was derived from a saved design AND the
    kiln-pro design_brief module is available.  In every other case
    the audit looks identical to before.
    """

    def test_no_kiln_pro_installed_omits_brief_honor_gate(self, monkeypatch):
        import builtins as _builtins
        real_import = _builtins.__import__

        def _no_kiln_pro(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("kiln_pro"):
                raise ImportError("simulated: kiln-pro not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(_builtins, "__import__", _no_kiln_pro)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        assert not any(g.name == "brief_honor" for g in audit.gates)

    def test_design_brief_module_absent_omits_brief_honor_gate(self, monkeypatch):
        """``intent_verification`` available, ``design_brief`` is not.

        The intent gates still fire; the saved-design summary doesn't.
        """
        passing_gate = _FakeIntentGate(
            name="intent.structure.walls",
            passed=True,
            severity="info",
            message="walls — observed thickness matches expected.",
            details={"category": "structure"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            _install_fake_kiln_pro_with_brief(
                monkeypatch,
                intent_gates=[passing_gate],
                summary_dict=None,  # would-be summary
                has_design_brief=False,
            )
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        assert not any(g.name == "brief_honor" for g in audit.gates)

    def test_brief_honor_returns_none_omits_gate(self, monkeypatch):
        """Generator string doesn't reference a saved design.

        ``check_brief_honor`` returns ``None``; the audit appends nothing.
        """
        passing_gate = _FakeIntentGate(
            name="intent.structure.walls",
            passed=True,
            severity="info",
            message="walls — observed thickness matches expected.",
            details={"category": "structure"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            _install_fake_kiln_pro_with_brief(
                monkeypatch,
                intent_gates=[passing_gate],
                summary_dict=None,  # helper signals "no saved design tied"
                intent_generator="template:phone_stand",
            )
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        assert not any(g.name == "brief_honor" for g in audit.gates)

    def test_all_pass_summary_appears_with_normie_message(self, monkeypatch):
        passing_gate = _FakeIntentGate(
            name="intent.structure.walls",
            passed=True,
            severity="info",
            message="walls — observed thickness matches expected.",
            details={"category": "structure"},
        )

        summary = {
            "name": "brief_honor",
            "passed": True,
            "severity": "info",
            "message": "Your design matches what you asked for (held the load, used a safe material).",
            "details": {"brief_id": "abc123", "passed_categories": ["structure", "safety"]},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            _install_fake_kiln_pro_with_brief(
                monkeypatch,
                intent_gates=[passing_gate],
                summary_dict=summary,
            )
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        brief_honor_gates = [g for g in audit.gates if g.name == "brief_honor"]
        assert len(brief_honor_gates) == 1
        gate = brief_honor_gates[0]
        assert gate.passed is True
        assert gate.severity == "info"
        assert "matches what you asked for" in gate.message
        # Engineering vocabulary stays out of user-facing strings.
        lower_msg = gate.message.lower()
        for forbidden in ("brief", "intent", "assertion", "verifier", "honored"):
            assert forbidden not in lower_msg, (
                f"engineering vocabulary {forbidden!r} leaked into audit gate message: {gate.message!r}"
            )

    def test_failure_summary_appears_naming_failing_categories(self, monkeypatch):
        failing_gate = _FakeIntentGate(
            name="intent.structure.walls",
            passed=False,
            severity="critical",
            message="walls thick enough to hold the load — observed too thin.",
            details={"category": "structure"},
        )

        summary = {
            "name": "brief_honor",
            "passed": False,
            "severity": "critical",
            "message": (
                "Your design doesn't fully match what you asked for. "
                "Specifically: walls thick enough to hold the load."
            ),
            "details": {
                "brief_id": "abc123",
                "failed_critical_count": 1,
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            _install_fake_kiln_pro_with_brief(
                monkeypatch,
                intent_gates=[failing_gate],
                summary_dict=summary,
            )
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        brief_honor_gates = [g for g in audit.gates if g.name == "brief_honor"]
        assert len(brief_honor_gates) == 1
        gate = brief_honor_gates[0]
        assert gate.passed is False
        assert gate.severity == "critical"
        assert "walls thick enough to hold the load" in gate.message
        assert "doesn't fully match" in gate.message

    def test_brief_honor_failure_never_breaks_audit(self, monkeypatch):
        """A raise in ``check_brief_honor`` is swallowed; audit still ships."""
        import sys
        import types

        try:
            import kiln_pro  # noqa: F401
            import kiln_pro.bridge  # noqa: F401
        except ImportError:
            pass

        iv_module = types.ModuleType("kiln_pro.intent_verification")
        iv_module.verify_intent_from_sidecar = lambda _path: []
        iv_module.gates_to_feedback = lambda *a, **k: []

        class _FakeIntentObject:
            generator = "design_brief:abc123"

        iv_module.load_intent_sidecar = lambda _path: _FakeIntentObject()

        db_module = types.ModuleType("kiln_pro.design_brief")

        def _broken(**_kwargs):
            raise RuntimeError("simulated brief-honor failure")

        db_module.check_brief_honor = _broken

        class _FakeProFeatures:
            intent_verification = iv_module
            design_brief = db_module

            def is_available(self, feature: str) -> bool:
                return feature in ("intent_verification", "design_brief")

        bridge_module = types.ModuleType("kiln_pro.bridge")
        bridge_module.pro_features = _FakeProFeatures()
        pkg_module = types.ModuleType("kiln_pro")
        pkg_module.bridge = bridge_module

        monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.intent_verification", iv_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.design_brief", db_module)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
            )

        assert audit.readiness_score >= 0
        assert not any(g.name == "brief_honor" for g in audit.gates)


# ---------------------------------------------------------------------------
# TestAuditInspectionBundleConsumer
#
# Consumer-proof for bundle-as-lingua-franca.  When a pre-built
# inspection bundle is provided, the audit reads printability findings
# from it instead of re-running analyze_printability.  Same answer,
# half the cost.
# ---------------------------------------------------------------------------


def _audit_inspection_bundle(*, score: int = 60, grade: str = "D") -> dict:
    """Synthetic inspection bundle shaped like attach_inspect_bundle's
    output, populated only with the printability channel."""
    return {
        "schema_version": "1.0",
        "channels": {
            "printability": {
                "name": "printability",
                "tier": "pro",
                "status": "ok",
                "images": [],
                "findings": {
                    "score": score,
                    "grade": grade,
                    "printable": score >= 50,
                    "recommendations": [
                        "increase wall count",
                        "raise nozzle temperature",
                    ],
                },
                "summary": f"grade {grade}",
                "error": None,
                "elapsed_ms": 0,
            },
        },
        "channels_emitted": ["printability"],
    }


class TestAuditInspectionBundleConsumer:
    """When ``inspection_bundle`` is provided, ``audit_original_design``
    reads printability from it instead of re-running analyze_printability."""

    def test_bundle_skips_analyze_printability(self):
        """analyze_printability MUST NOT be called when the bundle has
        the printability channel — that's the whole point of the
        consumer-proof refactor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            with patch(
                "kiln.original_design.analyze_printability"
            ) as mock_printability:
                bundle = _audit_inspection_bundle(score=44, grade="F")
                audit = audit_original_design(
                    path,
                    "test object",
                    inspection_bundle=bundle,
                )
                mock_printability.assert_not_called()
        # The printability gate read the BUNDLE's score, not the
        # (mocked-and-never-called) analyze_printability output.
        assert audit.printability.get("score") == 44
        assert audit.printability.get("grade") == "F"

    def test_bundle_rides_along_on_audit_result(self):
        """The bundle dict appears on the audit result so downstream
        consumers can read other channels (rgb evidence, measurements)
        without re-running anything."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            bundle = _audit_inspection_bundle(score=80, grade="B")
            with patch("kiln.original_design.analyze_printability"):
                audit = audit_original_design(
                    path,
                    "test object",
                    inspection_bundle=bundle,
                )
        assert audit.inspection_bundle is bundle

    def test_no_bundle_legacy_path_unchanged(self):
        """When ``inspection_bundle`` is None, behavior matches the
        pre-refactor behavior — analyze_printability runs and the
        result has ``inspection_bundle=None``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            with patch(
                "kiln.original_design.analyze_printability"
            ) as mock_printability:
                # Light mock — the real call shape is fine for this test
                # since we only assert analyze_printability was called and
                # the bundle field is None on the result.
                mock_printability.side_effect = lambda *a, **kw: __import__(
                    "kiln.printability", fromlist=["analyze_printability"]
                ).analyze_printability(*a, **kw)
                audit = audit_original_design(path, "test object")
                mock_printability.assert_called_once()
        assert audit.inspection_bundle is None


# ---------------------------------------------------------------------------
# Optional kiln-pro printability overlay — remedy candidates + dispatch
# ---------------------------------------------------------------------------


def _install_fake_printability_overlay(
    monkeypatch,
    *,
    recommended_actions: list[dict] | None,
    apply_result: dict | None = None,
    apply_raises: bool = False,
):
    """Inject a fake ``kiln_pro.bridge`` exposing only ``printability_overlay``.

    ``recommended_actions`` controls what the fake's
    ``enrich_printability_report`` puts in the enrichment block.  When
    ``None``, the fake returns a copy of the public report with NO
    enrichment block (mimics "overlay not loaded for this material").
    ``apply_result`` is what the fake's ``apply_printability_remedies``
    returns; ``apply_raises=True`` makes it raise instead so the
    best-effort wrapper is exercised.

    A list of recorded calls is attached to the returned spy dict so
    tests can assert what arguments the fake saw.
    """
    import sys
    import types

    try:
        import kiln_pro  # noqa: F401
        import kiln_pro.bridge  # noqa: F401
    except ImportError:
        pass

    spy: dict = {
        "enrich_calls": [],
        "apply_calls": [],
    }

    overlay_module = types.ModuleType("kiln_pro.printability_overlay")

    def _enrich(public_report, material, printer_id=None):
        spy["enrich_calls"].append(
            {
                "report": public_report,
                "material": material,
                "printer_id": printer_id,
            }
        )
        if not isinstance(public_report, dict):
            return public_report
        out = dict(public_report)
        if recommended_actions is not None:
            out["enrichment"] = {"recommended_actions": list(recommended_actions)}
        return out

    def _apply(enrichment_block, mesh_path, **kwargs):
        spy["apply_calls"].append(
            {
                "enrichment_block": enrichment_block,
                "mesh_path": mesh_path,
                "kwargs": dict(kwargs),
            }
        )
        if apply_raises:
            raise RuntimeError("simulated remedy dispatch failure")
        return apply_result

    overlay_module.enrich_printability_report = _enrich
    overlay_module.apply_printability_remedies = _apply

    class _FakeProFeatures:
        printability_overlay = overlay_module

        def is_available(self, feature: str) -> bool:
            return feature == "printability_overlay"

    bridge_module = types.ModuleType("kiln_pro.bridge")
    bridge_module.pro_features = _FakeProFeatures()

    pkg_module = types.ModuleType("kiln_pro")
    pkg_module.bridge = bridge_module

    monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
    monkeypatch.setitem(
        sys.modules, "kiln_pro.printability_overlay", overlay_module
    )
    return spy


class TestAuditOriginalDesignPrintabilityRemedies:
    """The ``apply_remedies`` cross-wire into the kiln-pro overlay."""

    def test_no_kiln_pro_omits_remediation_fields(self, monkeypatch):
        """Free / no-kiln-pro installs leave both remediation fields None."""
        import builtins as _builtins
        real_import = _builtins.__import__

        def _no_kiln_pro(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("kiln_pro"):
                raise ImportError("simulated: kiln-pro not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(_builtins, "__import__", _no_kiln_pro)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                printer_model="bambu_a1",
                apply_remedies=True,  # ignored when overlay is absent
            )

        assert audit.recommended_remedies is None
        assert audit.applied_remedies is None

    def test_dry_run_lists_candidates_without_mutation(self, monkeypatch):
        """``apply_remedies=False`` (default) surfaces candidate actions
        without calling the dispatcher."""
        candidates = [
            {
                "rule_id": "thin_wall",
                "remedy_design": "Thicken walls to 1.2mm",
                "apply_design_resolved": {
                    "fn": "thicken_walls",
                    "params": {"target_mm": 1.2},
                },
            }
        ]
        spy = _install_fake_printability_overlay(
            monkeypatch,
            recommended_actions=candidates,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                material="PLA",
                printer_model="bambu_a1",
                # apply_remedies defaults to False
            )

        assert audit.recommended_remedies is not None
        assert audit.recommended_remedies["applied"] is False
        assert audit.recommended_remedies["actions"][0]["rule_id"] == "thin_wall"
        assert audit.applied_remedies is None

        # The audit-level enrich call ran with the audit's printability
        # dict.  Other public Kiln subsystems may also probe the overlay
        # (e.g. printability scoring), so just assert the audit-level
        # call shape is among the recorded calls.
        assert any(
            call["material"] == "PLA" and call["printer_id"] == "bambu_a1"
            for call in spy["enrich_calls"]
        )

        # And the dispatcher was NOT called.
        assert spy["apply_calls"] == []

    def test_apply_remedies_invokes_dispatcher(self, monkeypatch):
        """``apply_remedies=True`` dispatches the overlay's remediation
        and surfaces the result on ``applied_remedies``."""
        candidates = [
            {
                "rule_id": "thin_wall",
                "remedy_design": "Thicken walls to 1.2mm",
                "apply_design_resolved": {
                    "fn": "thicken_walls",
                    "params": {"target_mm": 1.2},
                },
            },
            {
                "rule_id": "bridging_too_long",
                "remedy_slicer": "Enable supports",
                "apply_slicer_resolved": {
                    "fn": "enable_supports",
                    "params": {},
                },
            },
        ]
        apply_result = {
            "design_fixes_applied": ["thin_wall"],
            "design_fixes_skipped": [],
            "slicer_overrides": {"support_material": "1"},
            "slicer_overrides_deltas": {},
            "remediated_mesh_path": "/tmp/remediated.stl",
            "manual_actions_required": [],
        }
        spy = _install_fake_printability_overlay(
            monkeypatch,
            recommended_actions=candidates,
            apply_result=apply_result,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                material="PLA",
                printer_model="bambu_a1",
                apply_remedies=True,
            )

        # The dispatcher was called exactly once with the enrichment block
        # and the mesh path.
        assert len(spy["apply_calls"]) == 1
        call = spy["apply_calls"][0]
        assert call["mesh_path"] == path
        assert isinstance(call["enrichment_block"], dict)
        # The enrichment block we pass MUST contain the recommended_actions
        # so the dispatcher can route them to design / slicer hooks.
        assert (
            call["enrichment_block"].get("recommended_actions")[0]["rule_id"]
            == "thin_wall"
        )

        assert audit.recommended_remedies is not None
        assert audit.recommended_remedies["applied"] is True
        assert audit.applied_remedies == apply_result

    def test_dispatcher_failure_never_breaks_audit(self, monkeypatch):
        """If the overlay's dispatch raises, the audit still returns and
        ``applied_remedies`` is None while ``recommended_remedies`` still
        lists the candidate actions."""
        candidates = [
            {
                "rule_id": "thin_wall",
                "apply_design_resolved": {
                    "fn": "thicken_walls",
                    "params": {"target_mm": 1.2},
                },
            }
        ]
        _install_fake_printability_overlay(
            monkeypatch,
            recommended_actions=candidates,
            apply_raises=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                material="PLA",
                apply_remedies=True,
            )

        assert audit.recommended_remedies is not None
        # Dispatch raised → ``applied`` stays False, ``applied_remedies`` is None.
        assert audit.recommended_remedies["applied"] is False
        assert audit.applied_remedies is None

    def test_overlay_returns_no_actions_keeps_field_none(self, monkeypatch):
        """When the overlay enriches but emits no actions (clean mesh /
        unknown material), both remediation fields stay None — there's
        nothing to surface."""
        _install_fake_printability_overlay(
            monkeypatch,
            recommended_actions=None,  # no enrichment block at all
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(
                path,
                "simple coaster",
                material="PLA",
                apply_remedies=True,
            )

        assert audit.recommended_remedies is None
        assert audit.applied_remedies is None


# ---------------------------------------------------------------------------
# Unrun checks are reported, not omitted
# ---------------------------------------------------------------------------


def _no_kiln_pro(monkeypatch):
    """Make every kiln_pro import fail, as on a plain public install."""
    import builtins as _builtins

    real_import = _builtins.__import__

    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("kiln_pro"):
            raise ImportError("simulated: kiln-pro not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(_builtins, "__import__", _blocked)


class TestUnrunGatesAreReported:
    """An audit must say which checks did not run.

    ready_for_print was computed over a gate list the intent gates were
    silently missing from, so a design came back clean over checks that
    were never executed.  Nothing in the report said so.
    """

    def test_declared_intent_that_cannot_be_read_is_named(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            # A sidecar kiln-pro wrote earlier, sitting next to the mesh.
            with open(path + ".intent.json", "w") as fh:
                fh.write('{"generator": "generate_from_template", "assertions": []}')
            audit = audit_original_design(path, "simple coaster")

        gate = next(g for g in audit.gates if g.name == "intent_verification")
        assert gate.checked is False
        assert gate.details["declared_intent_present"] is True
        assert "declared intent" in gate.message.lower()
        assert "cannot check" in gate.message.lower()
        # The reader now owns the check, so it belongs in the actions.
        assert any(
            "could not be checked" in action for action in audit.next_actions
        )

    def test_no_declared_intent_says_there_was_nothing_to_check(self, monkeypatch):
        # The two silences are different and the report must not blur
        # them: nothing declared is not the same as could not verify.
        _no_kiln_pro(monkeypatch)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(path, "simple coaster")

        gate = next(g for g in audit.gates if g.name == "intent_verification")
        assert gate.checked is False
        assert gate.details["declared_intent_present"] is False
        assert "nothing to check" in gate.message.lower()
        assert not any(
            "could not be checked" in action for action in audit.next_actions
        )

    def test_unrun_gate_never_reads_as_passed(self, monkeypatch):
        # The whole point: a consumer rendering a tick per passed gate
        # must not tick a check that never ran.
        _no_kiln_pro(monkeypatch)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(path, "simple coaster")

        for gate in audit.gates:
            if not gate.checked:
                assert gate.passed is False, gate.name

    def test_checked_survives_serialisation(self, monkeypatch):
        # Callers read the audit as a dict; the distinction has to reach
        # them, not just live on the dataclass.
        _no_kiln_pro(monkeypatch)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(path, "simple coaster")

        gates = audit.to_dict()["gates"]
        assert all("checked" in g for g in gates)
        assert any(g["checked"] is False for g in gates)

    def test_unrun_gate_does_not_block_printing(self, monkeypatch):
        # Honesty, not a paywall.  A check this install cannot run must
        # not turn into a blocker, or every free user is stuck forever.
        _no_kiln_pro(monkeypatch)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(path, "simple coaster")

        gate = next(g for g in audit.gates if g.name == "intent_verification")
        assert gate.severity == "info"
        assert gate.message not in audit.blockers

    def test_gates_that_ran_are_marked_checked(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_stl(tmpdir, _cube_triangles(10.0), "cube.stl")
            audit = audit_original_design(path, "simple coaster")

        ran = {g.name for g in audit.gates if g.checked}
        assert "mesh_validation" in ran
        assert "printability" in ran
