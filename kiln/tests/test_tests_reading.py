"""Which test files ``scripts/tests_reading.py`` names as reading a change.

Coverage: a test that imports the changed module as ``from kiln import foo``
(the spelling that never writes ``kiln.foo`` out) is a reader, including a
parenthesised, aliased import, a nested package's module, and an import
inside a test function; the module's bare name in a file that never imports
it is not a reader; a test file in a subfolder of tests/ is read like any
other.
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
