"""A door's one line about a miss stays one line when the servers' answer
arrives already worded.

The served door wraps an HTTP error in an envelope that carries its own
sentence for the tool in ``error`` and the servers' words in ``why_detail``.
A door that rebuilds its own line from that envelope must take the servers'
words, never the envelope's sentence, or its line grows a second sentence
that names a tool the person never called.
"""

from __future__ import annotations

from kiln import served_answer
from kiln.material_basics import basic_pick_line


def _envelope(status: int, detail: str) -> dict:
    return served_answer.envelope_for_http("material_substitutes", status, {"detail": detail})


def test_a_worded_envelope_gives_the_servers_words_as_the_detail():
    miss = served_answer.classify_answer(_envelope(404, "Not Found"))
    assert miss is not None
    assert miss.detail == "Not Found"


def test_the_door_line_never_names_the_tool_the_envelope_named():
    for status, detail in ((404, "Not Found"), (409, "That request could not be read.")):
        line = basic_pick_line(served_answer.classify_answer(_envelope(status, detail)), feature="material pick")
        assert "material_substitutes" not in line
        assert line.count("Kiln can't") == 1


def test_an_answer_without_a_worded_detail_keeps_its_own_text():
    miss = served_answer.classify_answer({"success": False, "why": "refused", "error": "Not on this plan."})
    assert miss is not None and miss.detail == "Not on this plan."
