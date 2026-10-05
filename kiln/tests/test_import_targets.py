"""Backstop for the import-target check (``scripts/audit_import_targets.py``).

On 2026-03-20 the ordering code moved to the private package and twelve
``kiln order`` terminal commands kept importing it, inside their bodies, with
no guard.  Every plain install crashed on them for six months and nothing
noticed: importing the CLI and collecting the tests both succeed, and the
commands' tests skip whenever the module is absent.  The check reads every
first-party import instead of waiting for someone to run the line.

These prove the live tree has nothing new, and that each way the check can
fail does fail, starting with the incident's own shape.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_import_targets.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_import_targets", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()
#: The private package's top name, read from the check rather than typed
#: into public source; every module under it below is invented.
PRO = gate.PRIVATE_TOP


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """A package tree under *tmp_path*; returns the top package folder."""
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path / "kiln"


def _kinds(found) -> list[tuple[str, str, str]]:
    return sorted((f.kind, f.module, f.name) for f in found)


ORDER_COMMAND = '''
def order_place(quote_id):
    from kiln.fulfillment import OrderRequest
    return OrderRequest(quote_id)
'''


class TestTheLiveTree:
    def test_nothing_new_and_nothing_stale(self):
        found, _ = gate.scan(
            {"kiln": gate.PUBLIC_TREE}, test_dirs=[gate.PUBLIC_TESTS], shown_from=_REPO_ROOT,
        )
        new, stale = gate.judge(found, gate.KNOWN)
        assert not new, "a public import names something a plain install does not have:\n" + "\n".join(map(str, new))
        assert not stale, f"fixed — delete from KNOWN in scripts/audit_import_targets.py: {stale}"

    def test_the_command_line_entry_point_exits_clean(self):
        assert gate.main([]) == 0


class TestTheIncident:
    def test_an_unguarded_import_of_code_that_moved_out_is_caught(self, tmp_path):
        root = _tree(tmp_path, {"kiln/__init__.py": "", "kiln/cli/__init__.py": "", "kiln/cli/main.py": ORDER_COMMAND})
        found, _ = gate.scan({"kiln": root})
        assert _kinds(found) == [("needs_private", "kiln.fulfillment", "")]
        assert found[0].line == 3 and found[0].guarded is False

    def test_the_same_command_before_the_code_moved_is_clean(self, tmp_path):
        root = _tree(tmp_path, {
            "kiln/__init__.py": "", "kiln/cli/__init__.py": "", "kiln/cli/main.py": ORDER_COMMAND,
            "kiln/fulfillment/__init__.py": "class OrderRequest:\n    pass\n",
        })
        found, _ = gate.scan({"kiln": root})
        assert found == []

    def test_a_guard_that_catches_other_errors_is_not_a_guard(self, tmp_path):
        """``_get_fulfillment_provider`` wrapped its import in
        ``except (KeyError, RuntimeError, ValueError)``: an ImportError
        passes straight through it."""
        root = _tree(tmp_path, {"kiln/__init__.py": "", "kiln/cli.py": (
            "def provider():\n"
            "    try:\n"
            "        from kiln.fulfillment import get_provider\n"
            "    except (KeyError, RuntimeError, ValueError):\n"
            "        return None\n"
        )})
        found, _ = gate.scan({"kiln": root})
        assert _kinds(found) == [("needs_private", "kiln.fulfillment", "")]


class TestWhatAPlainInstallHas:
    def test_a_guarded_import_of_the_private_package_is_the_allowed_pattern(self, tmp_path):
        root = _tree(tmp_path, {"kiln/__init__.py": "", "kiln/a.py": (
            f"try:\n    from {PRO}.bridge import pro_features\nexcept ImportError:\n    pro_features = None\n"
            "import contextlib\n"
            f"with contextlib.suppress(ImportError):\n    import {PRO}\n"
        )})
        found, unjudged = gate.scan({"kiln": root})
        assert found == []
        assert {f"{PRO}.bridge", PRO} <= unjudged

    def test_an_unguarded_import_of_the_private_package_is_caught(self, tmp_path):
        root = _tree(tmp_path, {"kiln/__init__.py": "", "kiln/a.py": (
            "def fallback():\n"
            "    try:\n        from kiln.gone import x\n"
            f"    except ImportError:\n        from {PRO}.gone import x\n"
            "    return x\n"
        )})
        found, _ = gate.scan({"kiln": root})
        # The first import is guarded; the one in the handler is not.
        assert _kinds(found) == [("needs_private", f"{PRO}.gone", "")]

    def test_a_name_public_kiln_does_not_define_is_caught(self, tmp_path):
        root = _tree(tmp_path, {
            "kiln/__init__.py": "", "kiln/licensing.py": "def get_tier():\n    return 'free'\n",
            "kiln/b.py": "def f():\n    from kiln.licensing import get_tier, LicenseManager\n    return LicenseManager\n",
        })
        found, _ = gate.scan({"kiln": root})
        assert _kinds(found) == [("needs_private", "kiln.licensing", "LicenseManager")]

    def test_type_only_imports_and_test_helpers_are_not_read(self, tmp_path):
        root = _tree(tmp_path, {
            "kiln/__init__.py": "",
            "kiln/c.py": f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from {PRO}.x import Y\n",
            "tests/_stress_helper.py": "import kiln.nowhere\n",
        })
        found, _ = gate.scan({"kiln": root}, test_dirs=[tmp_path / "tests"])
        assert found == []

    def test_names_a_module_binds_in_a_loop_through_globals_are_read(self, tmp_path):
        """Public Kiln's server re-exports the tools that moved into
        plugins this way; reading it as "not defined" would cry wolf on
        every test that imports one."""
        root = _tree(tmp_path, {
            "kiln/__init__.py": "",
            "kiln/server.py": "_REEXPORTS = ['watch_print']\nfor _name in _REEXPORTS:\n    globals()[_name] = len\n",
            "kiln/d.py": "def f():\n    from kiln.server import watch_print, gone\n",
        })
        found, _ = gate.scan({"kiln": root})
        assert _kinds(found) == [("needs_private", "kiln.server", "gone")]


class TestWithBothTrees:
    """The run that has the private package's tree too: kiln-pro's."""

    def _both(self, tmp_path, public: dict[str, str], private: dict[str, str], tests: dict[str, str] | None = None):
        _tree(tmp_path / "pub", {"kiln/__init__.py": "", **public})
        _tree(tmp_path / "pro", {f"{PRO}/__init__.py": "", **private})
        if tests:
            _tree(tmp_path / "pub", tests)
        return gate.scan(
            {"kiln": tmp_path / "pub" / "kiln", PRO: tmp_path / "pro" / PRO},
            test_dirs=[tmp_path / "pub" / "tests"],
            provided={"kiln.ordering": f"{PRO}.ordering"},
        )

    def test_a_guarded_import_that_resolves_nowhere_is_a_fallback_always_taken(self, tmp_path):
        found, _ = self._both(tmp_path, {"kiln/a.py": f"try:\n    import {PRO}.never_landed\nexcept ImportError:\n    pass\n"}, {})
        assert _kinds(found) == [("missing_module", f"{PRO}.never_landed", "")]

    def test_a_shim_to_a_module_that_exists_resolves(self, tmp_path):
        found, _ = self._both(
            tmp_path,
            {"kiln/a.py": "try:\n    from kiln.ordering import base\nexcept ImportError:\n    base = None\n"},
            {f"{PRO}/ordering/__init__.py": "", f"{PRO}/ordering/base.py": ""},
        )
        assert found == []

    def test_a_test_that_skips_on_a_module_that_exists_nowhere_never_runs(self, tmp_path):
        found, _ = self._both(tmp_path, {}, {}, {"tests/test_x.py": "import pytest\nmod = pytest.importorskip('kiln.gone_forever')\n"})
        assert _kinds(found) == [("skips_forever", "kiln.gone_forever", "")]

    def test_the_private_packages_own_guarded_imports_are_left_to_its_runtime_check(self, tmp_path):
        found, _ = self._both(tmp_path, {}, {f"{PRO}/x.py": f"try:\n    from {PRO}.nope import y\nexcept Exception:\n    y = None\n"})
        assert found == []

    def test_the_shim_map_is_read_without_importing_the_package(self, tmp_path):
        init = tmp_path / "__init__.py"
        init.write_text(f"import os\n_COMPAT_SHIMS: dict[str, str] = {{\n    'kiln.ledger': '{PRO}.ledger.core',\n}}\n")
        assert gate.read_shims(init) == {"kiln.ledger": f"{PRO}.ledger.core"}


class TestTheKnownList:
    def test_a_known_finding_that_is_fixed_is_reported_stale(self):
        known = frozenset({("kiln/src/kiln/cli/main.py", "kiln.gone", "")})
        new, stale = gate.judge([], known)
        assert new == [] and stale == [("kiln/src/kiln/cli/main.py", "kiln.gone", "")]

    def test_a_new_finding_is_not_hidden_by_a_known_one_in_the_same_file(self, tmp_path):
        root = _tree(tmp_path, {"kiln/__init__.py": "", "kiln/cli.py": ORDER_COMMAND + f"\ndef other():\n    import {PRO}.ledger\n"})
        found, _ = gate.scan({"kiln": root}, shown_from=tmp_path)
        known = frozenset({("kiln/cli.py", "kiln.fulfillment", "")})
        new, _ = gate.judge(found, known)
        assert [f.module for f in new] == [f"{PRO}.ledger"]


@pytest.mark.parametrize("kind", ["needs_private", "missing_module", "skips_forever"])
def test_every_kind_is_explained_in_the_checks_own_words(kind):
    assert kind in (gate.__doc__ or "")
