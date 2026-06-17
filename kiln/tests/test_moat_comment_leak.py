"""CI backstop for the moat-comment leak gate.

Public Kiln must not narrate the kiln-pro overlay's internal strategy in
comments or docstrings (it may state the "exposed for the overlay"
contract, never the reasoning).  A green run here is the proof we didn't
ship the method.  See ``scripts/audit_moat_comment_leak.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_no_moat_comment_leak() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "audit_moat_comment_leak.py"
    assert script.exists(), f"gate script missing: {script}"

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Moat-comment leak — public Kiln narrates the kiln-pro overlay's "
        "strategy. Trim the moat reasoning (keep the math + the 'exposed "
        "for the kiln-pro overlay' contract):\n\n" + result.stdout
    )
