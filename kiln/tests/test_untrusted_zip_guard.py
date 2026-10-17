"""Backstop for the untrusted-zip gate (``scripts/audit_untrusted_zip.py``).

The gate keeps the whole class from regrowing: a raw read-mode
``zipfile.ZipFile(...)`` of an archive from outside the process is a
memory-exhaustion door, so every such open must go through
``kiln.untrusted_zip``.  These prove the live tree is clean and that each way
the gate can fail does fail — starting with a planted raw read open.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_untrusted_zip.py"  # top-level scripts/, per audit_import_targets


def _load():
    spec = importlib.util.spec_from_file_location("audit_untrusted_zip", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# The live tree is clean
# ---------------------------------------------------------------------------


def test_live_tree_has_no_raw_read_opens():
    findings = gate.scan()
    assert findings == [], "\n".join(str(f) for f in findings)


# ---------------------------------------------------------------------------
# Each way the gate can fail, does
# ---------------------------------------------------------------------------


def test_a_planted_raw_read_open_fails(tmp_path):
    root = _tree(tmp_path, {
        "kiln/reader.py": (
            "import zipfile\n"
            "def read(p):\n"
            "    with zipfile.ZipFile(p) as zf:\n"
            "        return zf.read('3D/3dmodel.model')\n"
        ),
    })
    findings = gate.scan(root)
    assert len(findings) == 1
    assert findings[0].path == "kiln/reader.py"
    assert findings[0].mode == "r"


def test_a_planted_explicit_read_mode_open_fails(tmp_path):
    root = _tree(tmp_path, {
        "kiln/r.py": "import zipfile\nx = zipfile.ZipFile(p, 'r')\n",
    })
    assert len(gate.scan(root)) == 1


def test_a_bare_imported_ZipFile_read_open_fails(tmp_path):
    root = _tree(tmp_path, {
        "kiln/r.py": "from zipfile import ZipFile\nx = ZipFile(p)\n",
    })
    assert len(gate.scan(root)) == 1, "a `from zipfile import ZipFile` alias must be caught"


def test_a_write_open_passes(tmp_path):
    root = _tree(tmp_path, {
        "kiln/w.py": "import zipfile\nx = zipfile.ZipFile(p, 'w')\n",
    })
    assert gate.scan(root) == []


def test_a_routed_open_passes(tmp_path):
    root = _tree(tmp_path, {
        "kiln/ok.py": (
            "from kiln.untrusted_zip import UntrustedZipFile\n"
            "with UntrustedZipFile(p) as zf:\n"
            "    zf.read('m')\n"
        ),
    })
    assert gate.scan(root) == []


def test_an_append_open_fails_unless_allowlisted(tmp_path):
    root = _tree(tmp_path, {
        "kiln/a.py": "import zipfile\nwith zipfile.ZipFile(p, 'a') as zf:\n    pass\n",
    })
    findings = gate.scan(root)
    assert len(findings) == 1 and findings[0].mode == "a"


def test_the_allowlist_is_frozen_and_reasoned():
    # Shrink-only: the only entry is the thumbnail append to Kiln's own output.
    assert len(gate.ALLOWLIST) == 1
    for (rel, line), reason in gate.ALLOWLIST.items():
        assert rel.startswith("kiln/") and 'ZipFile(' in line
        assert len(reason) > 20, "every allowlist entry states why it is safe"
