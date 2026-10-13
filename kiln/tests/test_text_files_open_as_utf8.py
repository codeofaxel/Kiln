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

The scan also refuses an ``encoding`` on a call that takes none: a
``ZipFile`` member open (bytes; ``ZipFile.open`` has no such parameter,
so it raises ``TypeError``) and a binary-mode open (``ValueError``).
2026-10-12: the scan read the member name in
``zf.open("3D/3dmodel.model")`` as a text mode and asked for an
encoding; the line that answered it made every STEP conversion with a
3MF already at its output name raise ``TypeError``.  A zip member is now
recognised by its receiver, so the scan neither asks for that encoding
nor lets one through.
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

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


#: Calls that hand back a ``ZipFile``: the stock class, and the guarded opens
#: every untrusted archive read goes through (``kiln.untrusted_zip``).
_ZIPFILE_MAKERS = frozenset({"ZipFile", "UntrustedZipFile", "open_untrusted_zip"})


def _is_zipfile(node: ast.AST) -> bool:
    """``ZipFile(...)``, ``zipfile.ZipFile(...)``, or a guarded open."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) in _ZIPFILE_MAKERS


def _own_nodes(scope: ast.AST):
    """Every node inside *scope*, not descending into nested functions."""
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(child, _FUNCTIONS):
            yield from _own_nodes(child)


def _zipfile_names(scope: ast.AST) -> set[str]:
    """Names *scope* binds to a ``ZipFile``: ``with ZipFile(...) as zf``,
    ``zf = ZipFile(...)``, or a parameter annotated ``ZipFile``."""
    names: set[str] = set()
    for node in _own_nodes(scope):
        if isinstance(node, ast.withitem) and _is_zipfile(node.context_expr):
            if isinstance(node.optional_vars, ast.Name):
                names.add(node.optional_vars.id)
        elif isinstance(node, ast.Assign) and _is_zipfile(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _is_zipfile(node.value) or "ZipFile" in ast.unparse(node.annotation):
                names.add(node.target.id)
        elif isinstance(node, ast.arg) and node.annotation is not None and "ZipFile" in ast.unparse(node.annotation):
            names.add(node.arg)
    return names


def _scoped_calls(scope: ast.AST, inherited: frozenset[str] = frozenset()):
    """``(call, zip_names)`` for every call, with the ``ZipFile`` names in
    scope where it is made (a nested function sees its parents' names)."""
    names = inherited | _zipfile_names(scope)
    for node in _own_nodes(scope):
        if isinstance(node, ast.Call):
            yield node, names
        elif isinstance(node, _FUNCTIONS):
            yield from _scoped_calls(node, names)


def _is_zip_member_open(node: ast.Call, zip_names: frozenset[str]) -> bool:
    """``zf.open(member)`` on a ``ZipFile``: its first argument is a member
    name, not a mode, and what it returns is bytes."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "open"):
        return False
    return _is_zipfile(func.value) or (isinstance(func.value, ast.Name) and func.value.id in zip_names)


def _opener_name(func: ast.expr) -> str | None:
    """The file-opening call *func* names, or None when it is not one."""
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
        base = func.value
        base_name = base.id if isinstance(base, ast.Name) else (
            base.attr if isinstance(base, ast.Attribute) else None
        )
        if name == "open" and base_name in _NOT_A_TEXT_OPEN:
            return None
    else:
        return None
    return name if name in _OPENERS else None


def _mode(node: ast.Call, name: str) -> ast.expr | None:
    """The mode argument of an ``open`` / ``Path.open`` call, if written."""
    mode = None
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if name == "open":
        positional = node.args[1:] if isinstance(node.func, ast.Name) else node.args[:1]
        if positional:
            mode = positional[0]
    return mode


def _parse(path: Path) -> tuple[str, ast.Module | None]:
    src = path.read_text(encoding="utf-8")
    try:
        return src, ast.parse(src)
    except SyntaxError:
        return src, None


def _site(src: str, node: ast.Call, fallback: str) -> tuple[int, str]:
    segment = ast.get_source_segment(src, node) or fallback
    return node.lineno, " ".join(segment.split())[:100]


def _unencoded_text_opens(path: Path) -> list[tuple[int, str]]:
    """``(line, source)`` for every text-mode file open in *path* that
    names no encoding."""
    src, tree = _parse(path)
    if tree is None:
        return []
    found: list[tuple[int, str]] = []
    for node, zip_names in _scoped_calls(tree):
        name = _opener_name(node.func)
        if name is None or _is_zip_member_open(node, zip_names):
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "encoding" in keywords or None in keywords:
            continue  # named, or passed through **kwargs (judged at that site)
        mode = _mode(node, name)
        if mode is not None and not isinstance(mode, ast.Constant):
            continue  # a mode chosen at runtime: the binary download path
        if isinstance(mode, ast.Constant) and "b" in str(mode.value):
            continue
        found.append(_site(src, node, name))
    return found


def _encodings_nothing_takes(path: Path) -> list[tuple[int, str]]:
    """``(line, source)`` for every ``encoding=`` in *path* on a call that
    cannot take one: a ``ZipFile`` member open (``TypeError``) or a
    binary-mode open (``ValueError``)."""
    src, tree = _parse(path)
    if tree is None:
        return []
    found: list[tuple[int, str]] = []
    for node, zip_names in _scoped_calls(tree):
        if not any(kw.arg == "encoding" for kw in node.keywords):
            continue
        if _is_zip_member_open(node, zip_names):
            found.append(_site(src, node, "open"))
            continue
        name = _opener_name(node.func)
        if name is None:
            continue
        mode = _mode(node, name)
        if isinstance(mode, ast.Constant) and "b" in str(mode.value):
            found.append(_site(src, node, name))
    return found


def _scan(root: Path, judge) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(_VENDORED):
            continue
        for line, source in judge(path):
            findings.append(f"{rel}:{line}: {source}")
    return findings


def scan_package(root: Path) -> list[str]:
    return _scan(root, _unencoded_text_opens)


def scan_package_for_misplaced_encodings(root: Path) -> list[str]:
    return _scan(root, _encodings_nothing_takes)


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


def test_no_call_is_handed_an_encoding_it_cannot_take() -> None:
    findings = scan_package_for_misplaced_encodings(PACKAGE)
    assert not findings, (
        f"{len(findings)} call(s) pass an encoding they cannot take -- a ZipFile member is "
        "bytes (decode what you read), and a binary-mode open takes no encoding:\n  " + "\n  ".join(findings)
    )


def test_a_zip_member_is_bytes_and_takes_no_encoding(tmp_path: Path) -> None:
    """The 2026-10-12 line is refused, a zip member read as bytes is not
    asked for an encoding, and a text open still is."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "bad.py").write_text(
        textwrap.dedent(
            """
            import zipfile
            from pathlib import Path
            def made_by_us(path):
                with zipfile.ZipFile(path) as zf, zf.open("3D/3dmodel.model", encoding="utf-8") as fh:
                    return fh.read()
            def member(zf: zipfile.ZipFile, name):
                return zf.open(name, encoding="utf-8").read()
            def chained(p):
                return zipfile.ZipFile(p).open("x", encoding="utf-8").read()
            def binary(p):
                with open(p, "rb", encoding="utf-8") as f:
                    f.read()
                with Path(p).open("wb", encoding="utf-8") as f:
                    f.write(b"x")
            """
        ),
        encoding="utf-8",
    )
    (pkg / "good.py").write_text(
        textwrap.dedent(
            """
            import io, zipfile
            from pathlib import Path
            def made_by_us(path):
                with zipfile.ZipFile(path) as zf, zf.open("3D/3dmodel.model") as fh:
                    return fh.read()
            def as_text(path):
                zf = zipfile.ZipFile(path)
                with io.TextIOWrapper(zf.open("a.txt"), encoding="utf-8") as fh:
                    return fh.read()
            def nested(path):
                with zipfile.ZipFile(path) as zf:
                    def inner():
                        return zf.open("a.txt").read()
                    return inner()
            def text(p):
                with Path(p).open("r", encoding="utf-8") as f:
                    f.read()
            """
        ),
        encoding="utf-8",
    )
    misplaced = scan_package_for_misplaced_encodings(pkg)
    assert [f.split(":")[0] for f in misplaced] == ["bad.py"] * 5, misplaced
    assert any("3D/3dmodel.model" in f for f in misplaced)
    # The member name is not a text mode: nothing asks for an encoding there.
    assert scan_package(pkg) == [], scan_package(pkg)


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
