"""A note on how a print came out, in the words of whoever wrote it.

Every print Kiln starts is one row (``print_outcomes`` in :mod:`kiln.persistence`)
and the row has always had a ``notes`` column.  What it did not say was who
wrote them: a hook's "auto-recorded on terminal state transition" and a
person's "the back-left corner lifted at layer 40" were the same kind of
thing.  ``note_by`` labels a note as the person's or their agent's; a string
with no label is a system string and stays on this computer.

The rule that follows the note everywhere (this computer's row, the courier,
the account's merge): a person's note is never overwritten by an agent's; an
agent's note is replaced by a later agent's only when the ending word is at
least as strong; a person's note is replaced only by a person's.  Free at
every tier: it is the person's own record of their own print.
"""

from __future__ import annotations

import re
from typing import Any

#: The longest note a row keeps; longer text is cut, never refused.
NOTE_MAX_CHARS = 2000
#: Who wrote the note.  Anything else is refused at every door.
NOTE_BY = ("person", "agent")

# Control characters other than a line break or a tab, which a note may keep,
# and the Unicode direction marks and overrides, which would make a note read
# differently from what was typed wherever it is shown.  Joiners stay: an
# emoji is spelled with them.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def clean_note(value: Any) -> str | None:
    """*value* as a row keeps it: text only, control characters dropped, each
    line stripped at its ends, cut to :data:`NOTE_MAX_CHARS`.  ``None`` for
    anything that is not text or is blank once cleaned."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(lines).strip("\n").strip()
    if not text:
        return None
    return text[:NOTE_MAX_CHARS].rstrip()


def note_by_word(value: Any) -> str | None:
    """*value* as a label, or ``None`` when it is none; raises for a label
    that is neither of the two words."""
    if value in (None, ""):
        return None
    if value not in NOTE_BY:
        raise ValueError(f"Invalid note_by {value!r}. Must be one of: {', '.join(NOTE_BY)}")
    return str(value)


def note_lands(
    arriving_by: str | None,
    current_by: str | None,
    *,
    ending_taken: bool,
) -> bool:
    """Whether an arriving note replaces the note already on a row.

    *arriving_by* and *current_by* are the labels (``None`` for a system
    string or no note); *ending_taken* says whether the arrival's ending word
    was at least as strong as the row's and so was taken.
    """
    if arriving_by == "person":
        return True
    if current_by == "person":
        return False
    if arriving_by == "agent":
        return ending_taken
    # A system string travels with its ending, as notes always did: it lands
    # only when that ending was taken, and never over a labelled note.
    return ending_taken and current_by is None
