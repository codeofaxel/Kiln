"""Which test files ``scripts/tests_reading.py`` names as reading a change.

Coverage: a test that imports the changed module as ``from kiln import foo``
(the spelling that never writes ``kiln.foo`` out) is a reader, including a
parenthesised, aliased import, a nested package's module, and an import
inside a test function; the module's bare name in a file that never imports
it is not a reader; a test file in a subfolder of tests/ is read like any
other; a script, which a test loads by its path rather than importing, is
named by that path however the test spells it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "tests_reading.py"
    spec = importlib.util.spec_from_file_location("tests_reading_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tests_reading = _load_script()


@pytest.fixture
def tests_dir(tmp_path, monkeypatch):
    tests = tmp_path / "kiln" / "tests"
    tests.mkdir(parents=True)
    monkeypatch.setattr(tests_reading, "_ROOT", tmp_path)
    monkeypatch.setattr(tests_reading, "_TESTS", tests)
    return tests


def _readers_of(tests_dir: Path, changed: str, files: dict[str, str]) -> set[str]:
    for name, text in files.items():
        path = tests_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    needles = tests_reading.module_names(Path(changed))
    return {str(p.relative_to(Path("kiln") / "tests")) for p in tests_reading.readers(needles)}


class TestReaders:
    def test_from_package_import_module_is_a_reader(self, tests_dir):
        found = _readers_of(tests_dir, "kiln/src/kiln/foo.py", {"test_a.py": "from kiln import foo\n"})
        assert found == {"test_a.py"}

    def test_a_parenthesised_aliased_import_is_a_reader(self, tests_dir):
        text = "from kiln import (\n    bar,\n    foo as the_foo,\n)\n"
        found = _readers_of(tests_dir, "kiln/src/kiln/foo.py", {"test_a.py": text})
        assert found == {"test_a.py"}

    def test_a_nested_package_module_imported_by_name_is_a_reader(self, tests_dir):
        text = "from kiln.plugins import estimate_tools\n"
        found = _readers_of(tests_dir, "kiln/src/kiln/plugins/estimate_tools.py", {"test_a.py": text})
        assert found == {"test_a.py"}

    def test_an_import_inside_a_test_function_is_a_reader(self, tests_dir):
        text = "def test_x():\n    from kiln import foo\n    assert foo\n"
        found = _readers_of(tests_dir, "kiln/src/kiln/foo.py", {"test_a.py": text})
        assert found == {"test_a.py"}

    def test_the_bare_module_name_alone_is_not_a_reader(self, tests_dir):
        files = {
            "test_word.py": "def test_x():\n    foo = 1\n    assert foo\n",
            "test_other.py": "from kiln import bar\nfrom other import foo\n",
        }
        assert _readers_of(tests_dir, "kiln/src/kiln/foo.py", files) == set()

    def test_the_dotted_spelling_is_still_a_reader(self, tests_dir):
        files = {"test_a.py": "import kiln.foo\n", "test_b.py": 'PATCH = "kiln.foo.thing"\n'}
        assert _readers_of(tests_dir, "kiln/src/kiln/foo.py", files) == {"test_a.py", "test_b.py"}

    def test_a_test_in_a_subfolder_is_a_reader(self, tests_dir):
        files = {"regression/test_sweep.py": "from kiln import foo\n"}
        assert _readers_of(tests_dir, "kiln/src/kiln/foo.py", files) == {"regression/test_sweep.py"}


class TestScriptsLoadedByPath:
    """A script is not importable from tests/, so a test names its path."""

    def test_a_slash_path_to_the_script_is_a_reader(self, tests_dir):
        text = 'SCRIPT = ROOT / "kiln/scripts/audit_thing.py"\n'
        assert _readers_of(tests_dir, "kiln/scripts/audit_thing.py", {"test_a.py": text}) == {"test_a.py"}

    def test_path_segments_naming_the_script_are_a_reader(self, tests_dir):
        text = 'spec_from_file_location("audit_thing", root / "scripts" / "audit_thing.py")\n'
        assert _readers_of(tests_dir, "kiln/scripts/audit_thing.py", {"test_a.py": text}) == {"test_a.py"}

    def test_a_repo_root_script_is_a_reader(self, tests_dir):
        text = 'subprocess.run([sys.executable, "scripts/generate_thing.py"])\n'
        assert _readers_of(tests_dir, "scripts/generate_thing.py", {"test_a.py": text}) == {"test_a.py"}

    def test_a_longer_name_ending_in_the_scripts_name_is_not_a_reader(self, tests_dir):
        files = {"test_a.py": 'X = "old_audit_thing.py"\n', "test_b.py": 'Y = "audit_thing.pyc"\n'}
        assert _readers_of(tests_dir, "kiln/scripts/audit_thing.py", files) == set()

    def test_a_generically_named_script_is_not_matched_by_its_bare_name(self, tests_dir):
        files = {"test_a.py": 'PATH = "main.py"\n'}
        assert _readers_of(tests_dir, "kiln/scripts/main.py", files) == set()

    def test_an_init_file_names_no_particular_file_so_it_is_not_a_needle(self, tests_dir):
        files = {"test_a.py": 'PKG = "__init__.py"\n'}
        assert _readers_of(tests_dir, "other-package/src/pkg/__init__.py", files) == set()
