"""High-level auditing for original AI-generated 3D designs.

Combines Kiln's design intelligence, mesh validation, printability
analysis, orientation scoring, and feedback generation into one harsh
readiness audit. This is intended for original creations where agents
need a single answer to "is this actually ready to print, and if not,
what should change?"
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from kiln.auto_orient import find_optimal_orientation
from kiln.design_intelligence import (
    get_design_constraints,
    get_printer_design_profile,
)
from kiln.design_validator import validate_design
from kiln.generation.validation import validate_mesh
from kiln.generation_feedback import (
    analyze_for_feedback,
    design_validation_to_feedback,
    enhance_prompt_with_design_intelligence,
    generate_improved_prompt,
)
from kiln.printability import analyze_printability


@dataclass
class AuditGate:
    """Single readiness gate inside the original-design audit."""

    name: str
    passed: bool
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OriginalDesignAudit:
    """Complete audit output for an original 3D design."""

    file_path: str
    requirements_text: str
    material: str | None
    printer_model: str | None
    build_volume_mm: dict[str, float] | None
    readiness_score: int
    readiness_grade: str
    ready_for_print: bool
    blockers: list[str]
    next_actions: list[str]
    design_brief: dict[str, Any]
    enhanced_prompt: dict[str, Any]
    mesh_validation: dict[str, Any]
    printability: dict[str, Any]
    design_validation: dict[str, Any]
    mesh_diagnostics: dict[str, Any] | None
    orientation: dict[str, Any] | None
    gates: list[AuditGate] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gates"] = [gate.to_dict() for gate in self.gates]
        return data


@dataclass
class OriginalDesignGenerationAttempt:
    """Single generation attempt in the original-design loop."""

    attempt_number: int
    prompt_used: str
    provider: str
    status: str
    ready_for_print: bool
    readiness_score: int
    readiness_grade: str
    job: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    mesh_validation: dict[str, Any] | None = None
    audit: dict[str, Any] | None = None
    feedback: list[dict[str, Any]] = field(default_factory=list)
    next_prompt_suggestion: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OriginalDesignGeneration:
    """Closed-loop original-design generation outcome."""

    requirements_text: str
    provider_requested: str
    provider_used: str
    provider_selection_reason: str
    material: str | None
    printer_model: str | None
    style: str | None
    max_attempts: int
    attempts_made: int
    ready_for_print: bool
    best_attempt_number: int | None
    best_readiness_score: int
    best_readiness_grade: str
    best_result_path: str | None
    summary: str
    next_actions: list[str]
    design_brief: dict[str, Any]
    initial_prompt: dict[str, Any]
    attempts: list[OriginalDesignGenerationAttempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attempts"] = [attempt.to_dict() for attempt in self.attempts]
        return data


def _score_to_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _build_volume_dict(
    build_volume: tuple[float, float, float] | None,
    printer_model: str | None,
) -> dict[str, float] | None:
    if build_volume is not None:
        return {
            "x": float(build_volume[0]),
            "y": float(build_volume[1]),
            "z": float(build_volume[2]),
        }

    if not printer_model:
        return None

    profile = get_printer_design_profile(printer_model)
    if profile is None:
        return None

    return {
        "x": float(profile.build_volume_mm["x"]),
        "y": float(profile.build_volume_mm["y"]),
        "z": float(profile.build_volume_mm["z"]),
    }


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _dedupe_feedback(items: list[Any]) -> list[dict[str, Any]]:
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...], str]] = set()
    deduped: list[dict[str, Any]] = []

    for item in items:
        data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        key = (
            str(data.get("feedback_type", "")),
            tuple(data.get("constraints", []) or []),
            tuple(data.get("issues", []) or []),
            str(data.get("severity", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(data)

    return deduped


def _looks_like_openscad_code(text: str) -> bool:
    lowered = text.lower()
    scad_tokens = (
        "cube(",
        "cylinder(",
        "sphere(",
        "translate(",
        "rotate(",
        "linear_extrude(",
        "rotate_extrude(",
        "difference(",
        "union(",
        "module ",
    )
    return ";" in text and any(token in lowered for token in scad_tokens)


def _instantiate_generation_provider(provider_name: str) -> Any:
    normalized = provider_name.strip().lower()

    if normalized == "meshy":
        from kiln.generation.meshy import MeshyProvider

        return MeshyProvider()
    if normalized == "gemini":
        from kiln.generation.gemini import GeminiDeepThinkProvider

        return GeminiDeepThinkProvider()
    if normalized == "tripo3d":
        from kiln.generation.tripo3d import Tripo3DProvider

        return Tripo3DProvider()
    if normalized == "stability":
        from kiln.generation.stability import StabilityProvider

        return StabilityProvider()

    from kiln.generation.base import GenerationError

    raise GenerationError(
        f"Provider {provider_name!r} is not supported for original-design generation.",
        code="UNKNOWN_PROVIDER",
    )


def _resolve_original_design_provider(
    provider_name: str,
    requirements_text: str,
) -> tuple[str, Any, str]:
    from kiln.generation.base import GenerationAuthError, GenerationError

    normalized = (provider_name or "auto").strip().lower()

    if normalized == "openscad":
        raise GenerationError(
            "OpenSCAD is a compile-only backend here, not an idea-to-design backend. "
            "Use provider='gemini' for natural-language original design, or use "
            "generate_model with explicit OpenSCAD code.",
            code="INVALID_PROVIDER_INPUT",
        )

    if normalized != "auto":
        return (
            normalized,
            _instantiate_generation_provider(normalized),
            "User-selected provider.",
        )

    if _looks_like_openscad_code(requirements_text):
        raise GenerationError(
            "generate_original_design expects a natural-language design brief, not "
            "raw OpenSCAD code. Use generate_model(provider='openscad') for direct "
            "code compilation.",
            code="INVALID_INPUT",
        )

    candidates = [
        (
            "gemini",
            "Gemini is preferred for original printable designs because it reasons "
            "into OpenSCAD and produces deterministic STL output.",
        ),
        (
            "meshy",
            "Meshy selected as the best available cloud text-to-3D fallback.",
        ),
        (
            "tripo3d",
            "Tripo3D selected as the next available text-to-3D backend.",
        ),
        (
            "stability",
            "Stability selected as the next available text-to-3D backend.",
        ),
    ]

    errors: list[str] = []
    for candidate, reason in candidates:
        try:
            return candidate, _instantiate_generation_provider(candidate), reason
        except (GenerationAuthError, GenerationError) as exc:
            errors.append(f"{candidate}: {exc}")

    raise GenerationError(
        "No original-design generation backend is configured. Preferred path is "
        "Gemini for idea-to-CAD, then Meshy, Tripo3D, or Stability. Configure one "
        "of: KILN_GEMINI_API_KEY, KILN_MESHY_API_KEY, KILN_TRIPO3D_API_KEY, "
        f"KILN_STABILITY_API_KEY. Details: {' | '.join(errors)}",
        code="NO_PROVIDER",
    )


def _await_generation_job(
    provider: Any,
    job: Any,
    *,
    timeout: int,
    poll_interval: int,
) -> Any:
    from kiln.generation.base import GenerationStatus, GenerationTimeoutError

    if job.status in {
        GenerationStatus.SUCCEEDED,
        GenerationStatus.FAILED,
        GenerationStatus.CANCELLED,
    }:
        return job

    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            raise GenerationTimeoutError(
                f"Timed out after {timeout}s waiting for generation.",
                code="GENERATION_TIMEOUT",
            )

        job = provider.get_job_status(job.id)
        if job.status in {
            GenerationStatus.SUCCEEDED,
            GenerationStatus.FAILED,
            GenerationStatus.CANCELLED,
        }:
            return job

        time.sleep(poll_interval)


def _feedback_dicts_to_objects(
    feedback_items: list[dict[str, Any]],
    *,
    original_prompt: str,
) -> list[Any]:
    if not feedback_items:
        return []

    from kiln.generation_feedback import FeedbackType, PrintFeedback

    converted: list[PrintFeedback] = []
    for item in feedback_items:
        feedback_type = item.get("feedback_type", "printability")
        try:
            kind = FeedbackType(feedback_type)
        except ValueError:
            kind = FeedbackType.PRINTABILITY
        converted.append(
            PrintFeedback(
                original_prompt=original_prompt,
                feedback_type=kind,
                issues=list(item.get("issues", []) or []),
                constraints=list(item.get("constraints", []) or []),
                severity=str(item.get("severity", "moderate")),
            )
        )
    return converted


def _is_better_attempt(
    candidate: OriginalDesignGenerationAttempt,
    current_best: OriginalDesignGenerationAttempt | None,
) -> bool:
    if current_best is None:
        return True
    if candidate.ready_for_print != current_best.ready_for_print:
        return candidate.ready_for_print
    if candidate.readiness_score != current_best.readiness_score:
        return candidate.readiness_score > current_best.readiness_score
    return candidate.attempt_number < current_best.attempt_number


def audit_original_design(
    file_path: str,
    requirements_text: str,
    *,
    material: str | None = None,
    printer_model: str | None = None,
    build_volume: tuple[float, float, float] | None = None,
    nozzle_diameter: float = 0.4,
    layer_height: float = 0.2,
    max_overhang_angle: float = 45.0,
) -> OriginalDesignAudit:
    """Run a harsh audit of an original design from intent to printability."""
    build_volume_dict = _build_volume_dict(build_volume, printer_model)
    build_volume_tuple = (
        (
            build_volume_dict["x"],
            build_volume_dict["y"],
            build_volume_dict["z"],
        )
        if build_volume_dict
        else None
    )

    brief = get_design_constraints(
        requirements_text,
        material=material,
        printer_model=printer_model,
    )
    prompt = enhance_prompt_with_design_intelligence(
        requirements_text,
        material=material,
        printer_model=printer_model,
    )
    mesh_validation = validate_mesh(file_path)
    printability = analyze_printability(
        file_path,
        nozzle_diameter=nozzle_diameter,
        layer_height=layer_height,
        max_overhang_angle=max_overhang_angle,
        build_volume=build_volume_tuple,
    )
    design_validation = validate_design(
        file_path,
        requirements_text,
        material=material,
        printer_model=printer_model,
        build_volume=build_volume_tuple,
    )

    orientation = None
    try:
        orientation = find_optimal_orientation(file_path)
    except ValueError:
        orientation = None

    diagnostics = None
    try:
        from kiln.mesh_diagnostics import diagnose_mesh

        diagnostics = diagnose_mesh(file_path)
    except ImportError:
        diagnostics = None
    except ValueError:
        diagnostics = None

    feedback_items: list[Any] = []
    feedback_items.extend(
        design_validation_to_feedback(design_validation, requirements_text)
    )
    feedback_items.extend(
        analyze_for_feedback(
            file_path,
            original_prompt=requirements_text,
            printability_report={
                "report": printability.to_dict(),
                "validation": mesh_validation.to_dict(),
                "mesh_diagnostics": diagnostics.to_dict() if diagnostics else None,
                "build_volume": build_volume_dict,
            },
        )
    )
    feedback = _dedupe_feedback(feedback_items)

    gates: list[AuditGate] = [
        AuditGate(
            name="mesh_validation",
            passed=mesh_validation.valid,
            severity="critical" if not mesh_validation.valid else "info",
            message=(
                "Mesh validation passed."
                if mesh_validation.valid
                else "; ".join(mesh_validation.errors)
            ),
            details=mesh_validation.to_dict(),
        ),
        AuditGate(
            name="manifold_geometry",
            passed=mesh_validation.is_manifold,
            severity="critical" if not mesh_validation.is_manifold else "info",
            message=(
                "Mesh appears manifold."
                if mesh_validation.is_manifold
                else "Mesh is non-manifold or open."
            ),
            details={"is_manifold": mesh_validation.is_manifold},
        ),
        AuditGate(
            name="design_requirements",
            passed=design_validation.overall_pass,
            severity="critical" if not design_validation.overall_pass else "info",
            message=design_validation.summary,
            details=design_validation.to_dict(),
        ),
        AuditGate(
            name="printability",
            passed=printability.printable and printability.score >= 75,
            severity=(
                "critical"
                if not printability.printable
                else "warning" if printability.score < 75 else "info"
            ),
            message=(
                f"Printability score {printability.score}/100 "
                f"({printability.grade})."
            ),
            details=printability.to_dict(),
        ),
    ]

    if diagnostics is not None:
        diag_passed = diagnostics.severity in {"clean", "minor"}
        gates.append(
            AuditGate(
                name="advanced_mesh_diagnostics",
                passed=diag_passed,
                severity="critical" if diagnostics.severity == "severe" else "warning" if not diag_passed else "info",
                message=(
                    "Advanced mesh diagnostics look healthy."
                    if diag_passed
                    else "; ".join(diagnostics.defects[:3])
                ),
                details=diagnostics.to_dict(),
            )
        )
    else:
        gates.append(
            AuditGate(
                name="advanced_mesh_diagnostics",
                passed=True,
                severity="info",
                message="Advanced mesh diagnostics skipped (trimesh not installed).",
            )
        )

    if orientation is not None:
        improvement = float(orientation.improvement_percentage)
        gates.append(
            AuditGate(
                name="orientation_efficiency",
                passed=improvement < 10.0,
                severity="warning" if improvement >= 10.0 else "info",
                message=(
                    f"Best orientation improves score by {improvement:.1f}%."
                    if improvement >= 10.0
                    else "Current orientation is already close to optimal."
                ),
                details=orientation.to_dict(),
            )
        )

    score = int(printability.score)
    if not mesh_validation.valid:
        score = min(score, 20)
    if not mesh_validation.is_manifold:
        score -= 20
    score -= design_validation.critical_count * 12
    score -= design_validation.warning_count * 4
    if diagnostics is not None:
        severity_penalty = {
            "clean": 0,
            "minor": 0,
            "moderate": 8,
            "severe": 20,
        }
        score -= severity_penalty.get(diagnostics.severity, 0)
    if orientation is not None and orientation.improvement_percentage >= 10.0:
        score -= 5
    score = max(0, min(100, score))
    grade = _score_to_grade(score)

    blockers = _dedupe_strings(
        [
            gate.message
            for gate in gates
            if not gate.passed and gate.severity == "critical"
        ]
    )

    next_actions = list(printability.recommendations)
    next_actions.extend(
        check.fix_suggestion
        for check in design_validation.checks
        if not check.passed and check.fix_suggestion
    )
    if diagnostics is not None:
        next_actions.extend(diagnostics.recommendations[:3])
    if orientation is not None and orientation.improvement_percentage >= 10.0:
        next_actions.append(
            "Re-orient the model before slicing to reduce supports and improve stability."
        )
    if feedback:
        next_actions.append(
            f"Regenerate using the design-aware prompt: {prompt.improved_prompt}"
        )
    next_actions = _dedupe_strings(next_actions)

    ready_for_print = all(
        gate.passed or gate.severity == "info"
        for gate in gates
        if gate.severity == "critical"
    ) and score >= 75

    return OriginalDesignAudit(
        file_path=file_path,
        requirements_text=requirements_text,
        material=material,
        printer_model=printer_model,
        build_volume_mm=build_volume_dict,
        readiness_score=score,
        readiness_grade=grade,
        ready_for_print=ready_for_print,
        blockers=blockers,
        next_actions=next_actions,
        design_brief=brief.to_dict(),
        enhanced_prompt=prompt.to_dict(),
        mesh_validation=mesh_validation.to_dict(),
        printability=printability.to_dict(),
        design_validation=design_validation.to_dict(),
        mesh_diagnostics=diagnostics.to_dict() if diagnostics else None,
        orientation=orientation.to_dict() if orientation else None,
        gates=gates,
        feedback=feedback,
    )


def generate_original_design(
    requirements_text: str,
    *,
    provider: str = "auto",
    material: str | None = None,
    printer_model: str | None = None,
    style: str | None = None,
    output_dir: str | None = None,
    build_volume: tuple[float, float, float] | None = None,
    nozzle_diameter: float = 0.4,
    layer_height: float = 0.2,
    max_overhang_angle: float = 45.0,
    timeout: int = 600,
    poll_interval: int = 10,
    max_attempts: int = 2,
) -> OriginalDesignGeneration:
    """Generate, validate, and audit an original design across a few attempts."""
    from kiln.generation import (
        GenerationError,
        GenerationStatus,
        GenerationTimeoutError,
        GenerationValidationError,
        convert_to_stl,
    )

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")
    if poll_interval < 1:
        raise ValueError("poll_interval must be at least 1 second.")

    build_volume_dict = _build_volume_dict(build_volume, printer_model)
    effective_output_dir = output_dir or os.path.join(
        tempfile.gettempdir(),
        "kiln_original_designs",
    )
    os.makedirs(effective_output_dir, exist_ok=True)

    brief = get_design_constraints(
        requirements_text,
        material=material,
        printer_model=printer_model,
    )
    seed_prompt = enhance_prompt_with_design_intelligence(
        requirements_text,
        material=material,
        printer_model=printer_model,
    )
    provider_used, generation_provider, selection_reason = _resolve_original_design_provider(
        provider,
        requirements_text,
    )

    current_prompt = seed_prompt.improved_prompt
    attempts: list[OriginalDesignGenerationAttempt] = []
    best_attempt: OriginalDesignGenerationAttempt | None = None

    for attempt_number in range(1, max_attempts + 1):
        attempt = OriginalDesignGenerationAttempt(
            attempt_number=attempt_number,
            prompt_used=current_prompt,
            provider=provider_used,
            status="pending",
            ready_for_print=False,
            readiness_score=0,
            readiness_grade="F",
        )

        try:
            job = generation_provider.generate(
                current_prompt,
                format="stl",
                style=style,
                output_dir=effective_output_dir,
            )
            job = _await_generation_job(
                generation_provider,
                job,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            attempt.job = job.to_dict()

            if job.status != GenerationStatus.SUCCEEDED:
                attempt.status = "generation_failed"
                attempt.error = job.error or f"Generation {job.status.value}."
                attempts.append(attempt)
                if _is_better_attempt(attempt, best_attempt):
                    best_attempt = attempt
                break

            result = generation_provider.download_result(
                job.id,
                output_dir=effective_output_dir,
            )

            if result.format == "obj":
                stl_path = convert_to_stl(result.local_path)
                result = type(result)(
                    job_id=result.job_id,
                    provider=result.provider,
                    local_path=stl_path,
                    format="stl",
                    file_size_bytes=os.path.getsize(stl_path),
                    prompt=result.prompt,
                )

            if result.format not in {"stl", "obj"}:
                raise GenerationValidationError(
                    f"Provider returned unsupported audit format: {result.format}.",
                    code="UNSUPPORTED_FORMAT",
                )

            attempt.result = result.to_dict()
            validation = validate_mesh(result.local_path)
            attempt.mesh_validation = validation.to_dict()

            if validation.valid:
                audit = audit_original_design(
                    result.local_path,
                    requirements_text,
                    material=material,
                    printer_model=printer_model,
                    build_volume=build_volume,
                    nozzle_diameter=nozzle_diameter,
                    layer_height=layer_height,
                    max_overhang_angle=max_overhang_angle,
                )
                attempt.audit = audit.to_dict()
                attempt.feedback = audit.feedback
                attempt.ready_for_print = audit.ready_for_print
                attempt.readiness_score = audit.readiness_score
                attempt.readiness_grade = audit.readiness_grade
                attempt.status = "audited"
            else:
                validation_feedback = analyze_for_feedback(
                    result.local_path,
                    original_prompt=current_prompt,
                    printability_report={
                        "validation": validation.to_dict(),
                        "build_volume": build_volume_dict,
                    },
                )
                attempt.feedback = _dedupe_feedback(validation_feedback)
                attempt.status = "validation_failed"
                attempt.error = "; ".join(validation.errors) or "Generated mesh failed validation."

            if _is_better_attempt(attempt, best_attempt):
                best_attempt = attempt

            if attempt.ready_for_print:
                attempts.append(attempt)
                break

            retry_feedback = _feedback_dicts_to_objects(
                attempt.feedback,
                original_prompt=current_prompt,
            )
            if attempt_number < max_attempts and retry_feedback:
                improved = generate_improved_prompt(
                    current_prompt,
                    retry_feedback,
                    iteration=attempt_number,
                )
                attempt.next_prompt_suggestion = improved.improved_prompt
                current_prompt = improved.improved_prompt

            attempts.append(attempt)

            if attempt_number < max_attempts and not attempt.next_prompt_suggestion:
                break

        except (
            GenerationError,
            GenerationTimeoutError,
            ValueError,
        ) as exc:
            attempt.status = "error"
            attempt.error = str(exc)
            attempts.append(attempt)
            if _is_better_attempt(attempt, best_attempt):
                best_attempt = attempt
            break

    if best_attempt is None:
        raise RuntimeError("Original design generation produced no attempts.")

    next_actions: list[str] = []
    if best_attempt.audit:
        next_actions.extend(best_attempt.audit.get("next_actions", []) or [])
    elif best_attempt.feedback:
        next_actions.append(
            "Regenerate using the improved prompt constraints from the previous attempt."
        )
    if not best_attempt.ready_for_print and not next_actions:
        next_actions.append(
            "Refine the prompt, switch to Gemini for better CAD reasoning, and retry."
        )
    next_actions = _dedupe_strings(next_actions)

    best_result_path = None
    if best_attempt.result:
        best_result_path = str(best_attempt.result.get("local_path") or "")

    summary = (
        f"Best attempt scored {best_attempt.readiness_score}/100 "
        f"({best_attempt.readiness_grade}) via {provider_used}."
    )
    if best_attempt.ready_for_print:
        summary += " The design is ready for print."
    elif best_attempt.error:
        summary += f" Blocked by: {best_attempt.error}"
    else:
        summary += " More iteration is needed before printing."

    return OriginalDesignGeneration(
        requirements_text=requirements_text,
        provider_requested=provider,
        provider_used=provider_used,
        provider_selection_reason=selection_reason,
        material=material,
        printer_model=printer_model,
        style=style,
        max_attempts=max_attempts,
        attempts_made=len(attempts),
        ready_for_print=best_attempt.ready_for_print,
        best_attempt_number=best_attempt.attempt_number,
        best_readiness_score=best_attempt.readiness_score,
        best_readiness_grade=best_attempt.readiness_grade,
        best_result_path=best_result_path,
        summary=summary,
        next_actions=next_actions,
        design_brief=brief.to_dict(),
        initial_prompt=seed_prompt.to_dict(),
        attempts=attempts,
    )
