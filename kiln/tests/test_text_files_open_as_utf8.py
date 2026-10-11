"""Guard: every text file Kiln reads or writes is opened as UTF-8, by name.

Python's ``open()`` with no ``encoding`` uses the operating system's
default.  On macOS and Linux that is UTF-8; on Windows it is the ANSI
code page (cp1252 in the West, cp936 in China) until UTF-8 mode becomes
the default in Python 3.15.  Kiln's bundled data is UTF-8 -- the printer
catalogue carries Chinese firmware text, material notes carry ``µ`` --
so a read with no encoding is a read that works on the author's Mac and
not on a third of the installs.

2026-10-11: ``set_printer_model`` failed for every spelling on Windows
with ``'charmap' codec can't decode byte 0x90``.  The catalogue loader
in ``printers/bed_fit.py`` opened ``printer_intelligence.json`` with no
encoding, and the head-motion records added on 2026-09-17 carried a
byte cp1252 does not define.  The same loader feeds the slice bed-fit
gate, the start-print preflight and printability placement, and those
doors swallow the error and SKIP their safety checks -- the loud failure
was the one door that did not.

Two halves, because each can pass while the other fails:

* **Structural** -- an AST scan of the package.  A text-mode ``open``,
  ``Path.open``, ``read_text`` or ``write_text`` with no ``encoding``
  keyword fails, naming the site.  Vendored OpenSCAD libraries are not
  Kiln's code and are left alone.
* **Behavioural** -- a fresh interpreter whose default encoding is
  forced to cp1252 loads the bundled data through the real loaders.  A
  control proves the forcing is real (the incident's own bytes fail
  through an unencoded read) before any loader is trusted to pass.

A/B: both halves fail at the revision before the fix (the structural
half with 65 findings, the behavioural half with the incident's exact
error) and pass after it.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import kiln

PACKAGE = Path(kiln.__file__).resolve().parent
SRC_ROOT = PACKAGE.parent

# Not Kiln's code: upstream OpenSCAD libraries shipped verbatim.
_VENDORED = ("data/scad_libraries/",)

# ``x.open(...)`` where ``x`` is one of these is not a file open in text
# mode: an image decoder, the OS-level descriptor call, a store's own
# API.  Anything else called ``open`` is judged.
_NOT_A_TEXT_OPEN = {"Image", "os", "store", "webbrowser"}

_OPENERS = {"open", "read_text", "write_text"}


def _unencoded_text_opens(path: Path) -> list[tuple[int, str]]:
    """``(line, source)`` for every text-mode file open in *path* that
    names no encoding."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            base = func.value
            base_name = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else None
            )
            if name == "open" and base_name in _NOT_A_TEXT_OPEN:
                continue
        else:
            continue
        if name not in _OPENERS:
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "encoding" in keywords or None in keywords:
            continue  # named, or passed through **kwargs (judged at that site)
        mode = None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if name == "open":
            positional = node.args[1:] if isinstance(func, ast.Name) else node.args[:1]
            if positional:
                mode = positional[0]
        if mode is not None and not isinstance(mode, ast.Constant):
            continue  # a mode chosen at runtime: the binary download path
        if isinstance(mode, ast.Constant) and "b" in str(mode.value):
            continue
        segment = ast.get_source_segment(src, node) or name
        found.append((node.lineno, " ".join(segment.split())[:100]))
    return found


def scan_package(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(_VENDORED):
            continue
        for line, source in _unencoded_text_opens(path):
            findings.append(f"{rel}:{line}: {source}")
    return findings


def test_every_text_open_names_utf8() -> None:
    findings = scan_package(PACKAGE)
    assert not findings, (
        f"{len(findings)} text-mode file open(s) with no encoding -- each one uses the OS "
        "default, which is the ANSI code page on Windows.  Add encoding=\"utf-8\" "
        "(plus errors=\"replace\" for a file a user wrote):\n  " + "\n  ".join(findings)
    )


def test_the_scan_sees_the_incident_shape(tmp_path: Path) -> None:
    """The gate can fail: the exact line that broke Windows is a finding,
    and each way of naming the encoding is not."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        textwrap.dedent(
            """
            import json
            from pathlib import Path
            def load(p):
                with open(p) as f:          # the bed_fit line
                    return json.load(f)
            def other(p):
                Path(p).read_text()
                Path(p).write_text("x")
                with Path(p).open("w") as f:
                    f.write("x")
            """
        ),
        encoding="utf-8",
    )
    (pkg / "good.py").write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path
            from PIL import Image
            def load(p, mode):
                with open(p, encoding="utf-8") as f:
                    f.read()
                with open(p, "rb") as f:
                    f.read()
                with open(p, mode) as f:
                    f.read()
                Path(p).read_text(encoding="utf-8", errors="replace")
                Path(p).write_text("x", encoding="utf-8")
                Image.open(p)
                os.open(p, os.O_RDONLY)
            """
        ),
        encoding="utf-8",
    )
    (pkg / "data" / "scad_libraries" / "lib").mkdir(parents=True)
    (pkg / "data" / "scad_libraries" / "lib" / "vendored.py").write_text(
        "open('x')\n", encoding="utf-8"
    )
    findings = scan_package(pkg)
    assert [f.split(":")[0] for f in findings] == ["bad.py"] * 4, findings
    assert any("open(p)" in f for f in findings)


# ---------------------------------------------------------------------------
# Behavioural: the real loaders, in a process whose default is cp1252.
# ---------------------------------------------------------------------------

# Firmware text from the catalogue's head-motion records: ``限`` encodes
# to a byte (0x90) that cp1252 leaves undefined, which is the incident.
_INCIDENT_TEXT = "默认喷嘴停止位置（暂停一开喷嘴，防止烫坏模型）轴限位开关"

_HARNESS = textwrap.dedent(
    """
    import builtins, io, json, sys
    sys.path.insert(0, sys.argv[1])
    _real = builtins.open

    def cp1252_open(file, mode="r", buffering=-1, encoding=None, errors=None, *a, **kw):
        if "b" not in str(mode) and encoding is None:
            encoding = "cp1252"   # what Windows does with no encoding named
        return _real(file, mode, buffering, encoding, errors, *a, **kw)

    builtins.open = cp1252_open
    io.open = cp1252_open   # pathlib.Path.open / read_text / write_text go through here

    # Control: the forcing is real.  An unencoded read of the incident's
    # own bytes must fail here, or a green run below proves nothing.
    sample = sys.argv[2]
    try:
        with open(sample) as f:
            f.read()
    except UnicodeDecodeError:
        pass
    else:
        print("CONTROL_INERT"); sys.exit(3)

    from kiln.printers import bed_fit
    from kiln.printer_profile_ids import resolve_declared_model
    from kiln import components, pre_estimate, printability, support_assessment, tool_schema

    results = {}
    results["catalogue_rows"] = len(bed_fit._load_printer_intelligence())
    results["resolved"] = resolve_declared_model("Bambu Lab P2S")[0]
    results["components"] = len(components._get_catalog())
    results["slicer_profiles"] = len(pre_estimate._load_slicer_profiles())
    results["materials"] = len(printability._load_materials_json())
    results["support_profiles"] = len(support_assessment._load_profiles())
    results["tool_safety"] = len(tool_schema._load_tool_safety())
    print(json.dumps(results))
    """
)


def test_bundled_data_loads_under_the_windows_code_page(tmp_path: Path) -> None:
    sample = tmp_path / "incident.json"
    sample.write_text(json.dumps({"note": _INCIDENT_TEXT}, ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", _HARNESS, str(SRC_ROOT), str(sample)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 3, "the cp1252 forcing did not take; the harness measures nothing"
    assert proc.returncode == 0, (
        "a bundled data file does not load when the OS default encoding is cp1252:\n"
        + proc.stderr[-2000:]
    )
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    assert results["resolved"] == "bambu_p2s", results
    assert results["catalogue_rows"] > 50, results
    for key, count in results.items():
        if key != "resolved":
            assert count > 0, (key, results)
