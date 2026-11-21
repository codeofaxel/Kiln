"""The words a person is shown when Kiln refuses or fails: plain, never Kiln's own.

A reply's person-facing fields (:data:`PERSON_FIELDS`) are read by the person,
often relayed word for word by their agent: what happened, what still works,
and what to do next, in everyday words.  The exact technical text an agent or
a developer needs (an exception, a parameter, an internal reason) goes in the
reply's ``detail`` field, which no person is shown.

:func:`technical_words` names the words that do not belong in a sentence a
person reads.  A name a person is meant to type or copy stands in backticks
(`kiln setup`, `design_id`) and is not judged; the rest of the sentence is.
"""

from __future__ import annotations

import re
from typing import Any

#: The fields of a reply a person reads.  ``remedy`` may be a sentence or a
#: ``{"message": ...}`` block; ``error`` may be a sentence or a block with one.
PERSON_FIELDS = ("error", "message", "note", "warning", "hint", "remedy", "reason", "suggestion")
#: Where the technical text goes instead.
DETAIL_FIELD = "detail"

#: What each pattern catches, by the name a finding reports.
_TECHNICAL: tuple[tuple[str, str], ...] = (
    ("a raw error", r"\b(?:Traceback|Errno|NoneType|stack ?trace)\b|\[Errno|object has no attribute|"
                    r"unexpected keyword argument|positional arguments?\b|not subscriptable|not callable|"
                    r"invalid literal for|unsupported operand|No module named|line \d+ column \d+|\(char \d+\)|<class '|"
                    r"\b0x[0-9a-fA-F]{4,}\b"),
    ("an exception name", r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b"),
    ("a code value", r"\b(?:None|True|False|null|undefined|NaN)\b"),
    ("a code call", r"\b[A-Za-z_][A-Za-z0-9_.]*\(\)"),
    ("a code name", r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b"),
    ("a programmer's word", r"\b(?:kwargs?|argv|stdout|stderr|subprocess|payload|endpoint|schema|"
                            r"serializ\w*|deserializ\w*|regex|dict|bool|enum|struct|runtime)\b"),
    ("a CAD internal", r"\b(?:B-rep|BRep|analytic|faceted|transpil\w*|tessellat\w*|CSG|kernel)\b"),
    ("a server word", r"\b(?:HTTP ?[1-5]\d\d|status code|tenant|dispatch\w*|sha256|SHA-256|JSON|JWT|RPC)\b"),
)
_PATTERNS = tuple((name, re.compile(rx)) for name, rx in _TECHNICAL)
#: Text a person is meant to type or copy, which is not judged.
_QUOTED = re.compile(r"`[^`]*`")
#: Web addresses and file names are names, not wording.
_NAMES = re.compile(r"https?://\S+|\b[\w.-]+\.(?:com|ai|io|stl|3mf|step|stp|gcode|scad|obj|png|svg)\b", re.IGNORECASE)


def technical_words(text: Any) -> list[str]:
    """What in *text* does not belong in a sentence a person reads, by kind,
    each kind once.  ``[]`` for plain words or for anything not text."""
    if not isinstance(text, str) or not text.strip():
        return []
    judged = _NAMES.sub(" ", _QUOTED.sub(" ", text))
    return [name for name, pattern in _PATTERNS if pattern.search(judged)]


def person_sentences(reply: Any) -> list[tuple[str, str]]:
    """``(field, sentence)`` for every person-facing sentence of *reply*:
    its own fields, and ``error`` / ``remedy`` written as a block."""
    if not isinstance(reply, dict):
        return []
    found: list[tuple[str, str]] = []
    for field in PERSON_FIELDS:
        value = reply.get(field)
        if isinstance(value, str):
            found.append((field, value))
        elif isinstance(value, dict):
            for inner in ("message", "error", "sentence"):
                if isinstance(value.get(inner), str):
                    found.append((f"{field}.{inner}", value[inner]))
    return found


def unplain(reply: Any) -> list[tuple[str, str, list[str]]]:
    """``(field, sentence, kinds)`` for each person-facing sentence of
    *reply* that carries technical words.  ``[]`` when it speaks plainly."""
    return [(f, s, kinds) for f, s in person_sentences(reply) if (kinds := technical_words(s))]


#: A code name standing bare in a sentence: lower-case words joined by
#: underscores, not already quoted and not part of a path or a file name.
_BARE_CODE_NAME = re.compile(r"(?<![\w/\\.`'\"-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w/\\`'\"-]|\.\w|\()")
#: The kind a marked code name answers; every other kind is wording.
CODE_NAME = "a code name"
#: Python's own failures whose text is never a sentence, whatever words it
#: holds (a missing key's text is the key).  Matched by exact type: a failure
#: Kiln defines on top of one of these speaks for itself.
_NEVER_A_SENTENCE = frozenset({
    KeyError, IndexError, AttributeError, AssertionError, NameError, ZeroDivisionError,
    UnicodeDecodeError, UnicodeEncodeError, ModuleNotFoundError, RecursionError,
})
_NEVER_A_SENTENCE_NAMES = frozenset({"JSONDecodeError", "CalledProcessError", "BadZipFile"})
#: Packages whose own failures are written for a person to read.
_KILN_PACKAGES = ("kiln", "kiln_pro")


def mark_code_names(text: str) -> str:
    """*text* with each bare code name set apart in backticks: a name an
    agent passes back stays exact, and reads as a name, not as wording.
    For an author writing a sentence; nothing applies it to a finished one."""
    return _BARE_CODE_NAME.sub(lambda m: f"`{m.group(0)}`", text) if isinstance(text, str) else text


def _is_plain(exc: BaseException) -> bool:
    """Whether *exc*'s own text may be shown as it is: a failure Kiln raises
    under its own name was written to be read; anything else is judged by
    its words."""
    text = str(exc).strip()
    if not text:
        return False
    if (type(exc).__module__ or "").split(".")[0] in _KILN_PACKAGES:
        return True
    if type(exc) in _NEVER_A_SENTENCE or type(exc).__name__ in _NEVER_A_SENTENCE_NAMES:
        return False
    return not [kind for kind in technical_words(text) if kind != CODE_NAME]


def said(exc: BaseException | None) -> str:
    """What *exc* says, for a sentence a person reads: its own words when
    they are plain (Kiln's own refusals are), else the failure in everyday
    words (:func:`kiln.failure_kind.in_words`)."""
    from kiln.failure_kind import in_words

    if exc is None:
        return in_words(None)
    return str(exc).strip() if _is_plain(exc) else in_words(exc)


def reworded(message: str, exc: BaseException | None) -> tuple[str, str | None]:
    """``(message, detail)``: *message* as a person reads it, built while
    *exc* was being handled.  Where the message carries the exception's own
    technical text, that text becomes everyday words and is returned as the
    detail (secrets and home folders taken out) for an agent or a developer.
    A sentence the door wrote is never changed.  ``detail`` is ``None`` when
    nothing was reworded."""
    if not isinstance(message, str):
        return message, None
    detail: str | None = None
    raw = str(exc).strip() if exc is not None else ""
    if exc is not None and not _is_plain(exc):
        from kiln.failure_kind import in_words

        words = in_words(exc)
        named = f"{type(exc).__name__}: {raw}" if raw else type(exc).__name__
        for technical in (named, raw, type(exc).__name__):
            if technical and technical in message:
                whole = message.strip() == technical
                message = message.replace(technical, words[:1].upper() + words[1:] + "." if whole else words)
                from kiln.redaction import redact_for_report

                detail = redact_for_report(named)
                break
    return message, detail


__all__ = [
    "CODE_NAME", "DETAIL_FIELD", "PERSON_FIELDS", "mark_code_names", "person_sentences",
    "reworded", "said", "technical_words", "unplain",
]
