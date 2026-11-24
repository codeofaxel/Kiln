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


def _keys(found) -> set[str]:
    return {key.split("::", 2)[2] for key, _line, _text in found}


def test_a_message_set_later_or_passed_by_name_is_read(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
def door(reply):
    try:
        work()
    except Exception as exc:
        reply["error"] = f"Upload failed: {exc}"
        return Result(message=f"Backup failed: {exc}", reason="Pass the design_id next.")
'''))
    assert _keys(found) == {"[error]::raw error", "Result(message=)::raw error", "Result(reason=)::a code name"}


def test_kiln_s_own_failure_is_a_sentence_shown_as_written(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
class PrinterError(Exception):
    pass

def door():
    try:
        work()
    except OSError as exc:
        raise PrinterError(f"Could not reach the printer: {exc}") from exc
    raise PrinterError("Set printer_model first")
'''))
    assert _keys(found) == {"raise PrinterError::raw error", "raise PrinterError::a code name"}


def test_a_string_built_from_a_failure_is_caught_wherever_it_goes(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
def door(notes):
    try:
        work()
    except Exception as exc:
        notes.append(f"The read failed: {exc}")
        return format_error("Save failed: " + str(exc))
'''))
    assert _keys(found) == {"built::raw error"}


def test_text_that_is_technical_on_purpose_is_left_alone(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
import logging
logger = logging.getLogger(__name__)

def door():
    try:
        work()
    except Exception as exc:
        logger.warning(f"work failed: {exc}")
        cause = f"{type(exc).__name__}: {exc}"
        assert cause, f"no cause for {exc}"
        if cause:
            raise ValueError(f"Bad value: {exc}") from exc
        return {"success": False, "error": "The work didn't finish.", "detail": f"{exc}"}
'''))
    assert found == []


def test_text_kept_under_a_technical_name_is_read_once_a_message_is_built_from_it(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
class PrinterError(Exception):
    pass

def door(reply):
    try:
        connect()
    except OSError as exc:
        detail = f"MQTT connect failed: {exc}\\n"
        cause = f"{exc}"
        reply["error"] = cause
        raise PrinterError(detail + "Retry with get_state().", detail=f"{exc}")
'''))
    assert _keys(found) == {"detail (shown)::raw error", "cause (shown)::raw error",
                            "raise PrinterError::a code call", "raise PrinterError::a code name"}


def test_a_helper_handed_the_failure_itself_is_not_charged(tmp_path):
    """``raise _wrap_error(exc)`` builds a failure from the object; rewording
    its argument would hand the helper a string where it reads an error."""
    found = _audit().scan(_tree(tmp_path, '''
class PrinterError(Exception):
    pass

def door():
    try:
        work()
    except OSError as exc:
        if exc.errno:
            raise _wrap_error(exc) from exc
        raise PrinterError(exc) from exc
'''))
    assert _keys(found) == {"raise PrinterError::raw error"}


# --- a collection printed whole into a sentence ---


def test_a_collection_put_into_a_sentence_whole_is_caught(tmp_path):
    """A list, set or dict put into an f-string whole prints as Python writes
    it (``['a', 'b']``), in a reply and in a builtin failure's message alike:
    both are findings."""
    found = _audit().scan(_tree(tmp_path, '''
def door(name):
    choices = {"a", "b"}
    if name not in choices:
        return {"success": False, "error": f"Unknown {name!r}. Valid: {sorted(choices)}"}
    raise ValueError(f"pick from {list(choices)}")
'''))
    assert sum(1 for key, _line, _text in found if key.endswith("printed list")) == 2, found


def test_the_items_said_in_words_pass(tmp_path):
    found = _audit().scan(_tree(tmp_path, '''
from kiln.plain_words import listed

def door(name):
    choices = {"a", "b"}
    if name not in choices:
        return {"success": False, "error": f"Unknown {name!r}. Valid: {listed(sorted(choices))}"}
    logger.warning(f"internal: {sorted(choices)}")
'''))
    assert not [key for key, _l, _t in found if key.endswith("printed list")], found
