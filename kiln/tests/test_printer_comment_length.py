"""Printer modules keep their comments and docstrings short.

A comment block is at most 3 lines and a docstring at most 6; longer
notes are kept outside this repository.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"
MAX_COMMENT_BLOCK = 3
MAX_DOCSTRING = 6

SHORT_FILES = (
    "printers/__init__.py",
    "printers/bambu.py",
    "printers/bambu_3mf.py",
    "printers/bambu_hms_text.py",
    "printers/base.py",
    "printers/bed_fit.py",
    "printers/command_verdict.py",
    "printers/creality.py",
    "printers/duet.py",
    "printers/elegoo.py",
    "printers/engagement.py",
    "printers/gcode_complete.py",
    "printers/home_around_part.py",
    "printers/job_identity.py",
    "printers/marlin_report.py",
    "printers/moonraker.py",
    "printers/moonraker_history.py",
    "printers/motion_plan.py",
    "printers/motion_plan_cache.py",
    "printers/octoprint.py",
    "printers/print_file_scan.py",
    "printers/print_gate.py",
    "printers/progress_motion.py",
    "printers/prusalink.py",
    "printers/qoi.py",
    "printers/routine_ledger.py",
    "printers/safe_motion.py",
    "printers/serial_adapter.py",
    "printers/upload_prep.py",
    "ams_routing.py",
    "multicolor_3mf.py",
    "multi_material.py",
    "bambu_trays.py",
    "print_recovery.py",
    "failure_recovery.py",
    "recovery_floor.py",
    "print_health_monitor.py",
    "print_monitor.py",
    "scheduler.py",
    "queue.py",
    "firmware_command_rules.py",
    "nozzle_clumping_detection.py",
    "machine_motion.py",
    "motion_facts.py",
    "threemf_placement.py",
)


def _long_passages(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    found: list[str] = []
    doc_lines: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            first = body[0]
            doc_lines.update(range(first.lineno, first.end_lineno + 1))
            if first.end_lineno - first.lineno + 1 > MAX_DOCSTRING:
                found.append(f"{path.name}:{first.lineno} docstring of {first.end_lineno - first.lineno + 1} lines")
    lines = src.splitlines()
    run_start, run_len, last = 0, 0, -2
    def close() -> None:
        if run_len > MAX_COMMENT_BLOCK:
            found.append(f"{path.name}:{run_start} comment block of {run_len} lines")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT or tok.start[0] in doc_lines:
            continue
        row = tok.start[0]
        if lines[row - 1][: tok.start[1]].strip():
            continue
        if row == last + 1:
            run_len += 1
        else:
            close()
            run_start, run_len = row, 1
        last = row
    close()
    return found


@pytest.mark.parametrize("rel", SHORT_FILES)
def test_comments_and_docstrings_stay_short(rel: str) -> None:
    assert _long_passages(SRC / rel) == []


def test_the_check_can_fail(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("# a\n" * (MAX_COMMENT_BLOCK + 1) + "x = 1\n", encoding="utf-8")
    assert _long_passages(sample)


def test_new_printer_modules_are_listed() -> None:
    on_disk = {f"printers/{p.name}" for p in (SRC / "printers").glob("*.py")}
    assert on_disk <= set(SHORT_FILES)
