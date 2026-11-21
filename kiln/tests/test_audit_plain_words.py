"""The plain-words guard: no new sentence a person is shown carries technical
words or a caught failure's raw text, and the frozen list only shrinks.

Runs ``scripts/audit_plain_words.py`` over the shipped package against
``scripts/plain_words_baseline.txt``, and proves on small trees that each
kind of finding is caught and that the chokepoint's own rewording is known.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "audit_plain_words.py"
BASELINE = REPO / "scripts" / "plain_words_baseline.txt"


def _audit():
    spec = importlib.util.spec_from_file_location("audit_plain_words", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_plain_words"] = module
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "door.py").write_text(body)
    return tmp_path


def _kinds(found) -> set[str]:
    return {key.rsplit("::", 1)[1] for key, _line, _text in found}


def test_the_shipped_package_adds_nothing_to_the_frozen_list(capsys):
    code = _audit().main([str(REPO / "kiln" / "src"), "--baseline", str(BASELINE)])
    assert code == 0, capsys.readouterr().out[-3000:]


def test_a_technical_sentence_and_a_raw_failure_are_each_caught(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
def door():
    try:
        work()
    except Exception as exc:
        return {"success": False, "error": f"Export failed: {exc}"}
    return {"error": "minkowski() has no analytic B-rep form", "note": "Pass the design_id next."}
'''))
    assert _kinds(found) == {"raw error", "a code call", "a CAD internal", "a code name"}


def test_plain_sentences_and_marked_names_pass(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
def door(exc_free):
    return {"error": "That printer is offline. Turn it on and try again.",
            "remedy": {"message": "Pass the `design_id` of a saved design."},
            "detail": "KeyError: 'x' in dispatch"}
'''))
    assert found == []


def test_the_envelope_that_rewords_is_not_charged_for_what_it_fixes(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
def door():
    try:
        work()
    except Exception as exc:
        return _error_dict(f"Slice failed for layer_height: {exc}", code="X")
    return _error_dict("The JSON payload was bad", code="Y")
'''))
    assert _kinds(found) == {"a server word", "a programmer's word", "a code name"}


def test_a_new_finding_fails_and_a_fixed_one_must_leave_the_list(tmp_path, capsys):
    audit = _audit()
    tree = _tree(tmp_path, 'def door():\n    return {"error": "The kwargs were wrong"}\n')
    empty = tmp_path / "none.txt"
    empty.write_text("")
    assert audit.main([str(tree), "--baseline", str(empty)]) == 2
    stale = tmp_path / "stale.txt"
    stale.write_text("pkg/door.py::door::error::a programmer's word\npkg/gone.py::x::error::raw error\n")
    assert audit.main([str(tree), "--baseline", str(stale)]) == 2
    assert "STALE pkg/gone.py" in capsys.readouterr().out
