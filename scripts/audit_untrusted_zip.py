"""Gate: every read of a ZIP from outside the process goes through the guard.

A 3MF / sliced-print / marketplace / printer file is a ZIP a stranger
produced, and ``zipfile``'s whole-member ``read()`` decompresses the real
stream however small the central directory claims the member is — so a raw
``zipfile.ZipFile(path)`` read of an untrusted archive is a memory-exhaustion
door, an outage on the shared 2 GB hosted process (see
``kiln.untrusted_zip``).  Four readers were guarded by hand in 2026-10; this
scan makes the whole class impossible to regrow: it fails when a read-mode
``zipfile.ZipFile(...)`` open appears anywhere in ``kiln/src`` outside the
guard module, unless it is a frozen, reasoned allowlist entry.

A WRITE open (``"w"`` / ``"x"``) is ignored — it never reads a member.  An
APPEND (``"a"``) reads the central directory to append to it, so it is held
to the allowlist like a read; today the only one appends to a 3MF Kiln wrote
earlier in the same call.  ``kiln.untrusted_zip.UntrustedZipFile`` subclasses
``zipfile.ZipFile`` but is a different call name, so routed opens do not trip
the scan.

Run: ``python3 scripts/audit_untrusted_zip.py`` (exit 0 clean, 2 on a
finding).  Backstop: ``tests/test_untrusted_zip_guard.py``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "kiln" / "src"

#: The module every untrusted-archive read must open through.
GUARD_MODULE = "kiln.untrusted_zip"

#: Modes that only CREATE/WRITE and never read a member — ignored.
_WRITE_ONLY_MODES = frozenset({"w", "x"})

#: Frozen, shrink-only.  Key: (relative path, the stripped source line of the
#: open).  Value: why this read-ish open of a ``zipfile.ZipFile`` is safe to
#: leave raw.  A line keyed here is NOT reading untrusted input — it is Kiln
#: reading back a ZIP it wrote in the same call.  Shrinking is always fine;
#: growing it is a security decision the test pins.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "kiln/server.py",
        'with zipfile.ZipFile(output_path, "a") as zf:',
    ): (
        "mode 'a' appends thumbnails to output_path, the 3MF Kiln wrote "
        "earlier in this same call — Kiln's own output, not untrusted input."
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str
    lineno: int
    mode: str
    line: str

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.lineno}: raw zipfile.ZipFile open in read mode "
            f"({self.mode!r}) — open it through {GUARD_MODULE}.open_untrusted_zip "
            f"/ UntrustedZipFile, or add a reasoned allowlist entry.\n"
            f"    {self.line}"
        )


def _literal_mode(call: ast.Call) -> str | None:
    """The open mode if it is a literal, else ``None`` (a computed mode)."""
    if len(call.args) >= 2:
        arg = call.args[1]
        return arg.value if isinstance(arg, ast.Constant) else None
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    return "r"  # zipfile.ZipFile's own default


def _zipfile_open_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call that opens a ``zipfile.ZipFile`` — ``zipfile.ZipFile(...)``
    and any ``ZipFile`` / alias bound by ``from zipfile import ZipFile``."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "zipfile":
            for n in node.names:
                if n.name == "ZipFile":
                    aliases.add(n.asname or n.name)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and fn.attr == "ZipFile"
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "zipfile"
        ) or (isinstance(fn, ast.Name) and fn.id in aliases):
            calls.append(node)
    return calls


def scan(src_root: Path = _SRC) -> list[Finding]:
    """Every read-ish raw ``zipfile.ZipFile`` open not covered by the allowlist."""
    findings: list[Finding] = []
    for path in sorted(src_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = str(path.relative_to(src_root))
        for call in _zipfile_open_calls(tree):
            mode = _literal_mode(call)
            if mode in _WRITE_ONLY_MODES:
                continue  # create/write never reads a member
            line = lines[call.lineno - 1].strip() if call.lineno <= len(lines) else ""
            if (rel, line) in ALLOWLIST:
                continue
            findings.append(Finding(rel, call.lineno, mode if mode is not None else "<expr>", line))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=_SRC)
    args = ap.parse_args()
    findings = scan(args.root)
    if not findings:
        print(f"untrusted-zip gate: clean ({GUARD_MODULE} covers every read).")
        return 0
    print(f"untrusted-zip gate: {len(findings)} raw read open(s) outside {GUARD_MODULE}:\n")
    for f in findings:
        print(str(f) + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
