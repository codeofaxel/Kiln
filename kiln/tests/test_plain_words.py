"""A person is shown plain words when Kiln refuses or fails.

The error envelope every door builds (``kiln.server._error_dict``) rewords a
caught failure's own technical text into everyday words, keeps that text
under ``detail`` for an agent, and sets bare code names apart.  A refusal
Kiln wrote itself is left exactly as written.
"""

from __future__ import annotations

import json

import pytest

from kiln.plain_words import mark_code_names, said, technical_words, unplain
from kiln.server import _error_dict


def _answer(raise_it, wrap=lambda e: f"Export failed: {e}"):
    try:
        raise_it()
    except Exception as exc:  # noqa: BLE001
        return _error_dict(wrap(exc), code="EXPORT_ERROR")["error"]
    raise AssertionError("nothing was raised")


def _raise(exc):
    def go():
        raise exc
    return go


@pytest.mark.parametrize("raise_it, words", [
    (lambda: open("/nonexistent-kiln-dir/part.stl"), "the file wasn't found"),
    (lambda: {}["layer_height"], "something went wrong inside Kiln"),
    (lambda: json.loads("{bad"), "couldn't be read"),
    (lambda: 1 / 0, "something went wrong inside Kiln"),
    (lambda: None.height, "something went wrong inside Kiln"),
    (_raise(ConnectionRefusedError(61, "Connection refused")), "the connection didn't go through"),
    (_raise(ValueError("invalid literal for int() with base 10: 'abc'")), "a value it was given can't be used"),
])
def test_a_caught_failures_own_text_becomes_everyday_words(raise_it, words):
    error = _answer(raise_it)
    assert words in error["message"], error
    assert error["message"].startswith("Export failed: ")
    assert not technical_words(error["message"]), error["message"]
    # The exact text is kept for an agent, never shown as the sentence.
    assert error["detail"] and "Error" in error["detail"]


def test_a_refusal_kiln_wrote_is_left_as_written():
    error = _answer(_raise(ValueError("The printer is busy with another print.")), wrap=str)
    assert error["message"] == "The printer is busy with another print."
    assert "detail" not in error


def test_a_code_name_is_set_apart_and_a_path_is_left_alone():
    error = _answer(_raise(ValueError("layer_height must be above 0")))
    # A sentence Kiln wrote is passed on as written; marking is the author's.
    assert error["message"] == "Export failed: layer_height must be above 0"
    marked = mark_code_names("Pass the design_id from /tmp/my_folder/a_b.stl or `goal_id`, not kiln_pro.x")
    assert marked == "Pass the `design_id` from /tmp/my_folder/a_b.stl or `goal_id`, not kiln_pro.x"


def test_the_detail_carries_no_home_folder_name():
    error = _answer(lambda: open("/Users/janedoe/nope/part.stl"))
    assert "janedoe" not in json.dumps(error)


def test_a_message_built_outside_a_failure_is_only_marked():
    assert _error_dict("That printer is offline.", code="OFFLINE")["error"]["message"] == "That printer is offline."


def test_said_gives_a_sentence_for_a_door_that_builds_its_own_reply():
    try:
        {}["x"]
    except KeyError as exc:
        assert said(exc) == "something went wrong inside Kiln (not something you did)"
    assert said(ValueError("That size is too large for the bed.")) == "That size is too large for the bed."


def test_unplain_reads_every_field_a_person_reads():
    reply = {"success": False, "error": {"message": "minkowski() has no analytic B-rep form"},
             "remedy": {"message": "Try a simpler shape."}, "detail": "KeyError: 'x'"}
    assert [field for field, _s, _k in unplain(reply)] == ["error.message"]


def test_a_reply_gains_a_detail_only_when_its_sentence_was_reworded():
    from kiln.plain_words import detail_field

    assert detail_field(ValueError("That size is too large for the bed.")) == {}
    try:
        {}["x"]
    except KeyError as exc:
        assert detail_field(exc) == {"detail": "KeyError: 'x'"}
    assert detail_field(None) == {}


@pytest.mark.parametrize("text, words", [
    ("Awin answered HTTP 401", "didn't accept Kiln's sign-in"),
    ("404 Client Error: Not Found for url: https://x.test/a", "wasn't found"),
    ("status 503 from the store", "had a problem on its side"),
    ("HTTP 429", "too many requests"),
])
def test_a_web_service_answer_is_said_by_what_it_means(text, words):
    assert words in said(RuntimeError(text))


def test_a_failure_that_is_the_whole_message_reads_as_a_sentence():
    from kiln.plain_words import sentence

    assert sentence(RuntimeError("Awin answered HTTP 401")) == "The other service didn't accept Kiln's sign-in."
    assert sentence(ValueError("That size is too large for the bed.")) == "That size is too large for the bed."
    # A failure's own plain words are shown exactly as written.
    assert sentence(ValueError("model_path must name a file")) == "model_path must name a file"
    assert sentence(RuntimeError("boom")) == "boom"
    assert sentence(None)[0].isupper()


def test_a_lookup_failure_with_a_written_message_is_said_as_written():
    assert said(KeyError("No interception session for printer 'shop-a1'")) == (
        "No interception session for printer 'shop-a1'"
    )
    # Only the missing key: that is a slip in the code, said as one.
    assert said(KeyError("nominal_size_mm")) == "something went wrong inside Kiln (not something you did)"


def test_the_english_none_is_not_mistaken_for_a_code_value():
    assert technical_words("None of the attempts worked.") == []
    assert technical_words("The layer height was None.") == ["a code value"]


def test_a_missing_input_is_named_by_its_key():
    from kiln.plain_words import missing_name

    assert missing_name(KeyError("b_mm")) == "`b_mm`"
    assert missing_name(ValueError("b_mm")) == ""
    assert missing_name(None) == ""
