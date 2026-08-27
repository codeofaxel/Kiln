"""The ``fastener_advice`` block, built in one place for every seam.

Several tools arrive at the same moment from different directions: a part
now exists with a hole in it, and nothing checked that hole against the
hardware meant to go in.  A template build used a dimension the caller
supplied; a printability run found holes in whatever mesh it was handed;
a compile ran against code for a fastener the user actually named.  Same
message, three doors.

Two rules live here so they cannot drift apart:

**One shape.**  Every seam returns the same four keys — ``parameters``,
``pro_depth_applied``, ``note``, ``agent_instruction`` — under the same
field name, so an agent that learned to read one has learned to read all
of them.  With the kiln-pro package installed and the caller entitled,
that field is replaced with the real verdict; the surface deepens, it
does not change shape.

**One line per session, keyed by CONTENT.**  Not a per-seam counter and
not a session counter: :data:`HOLE_SIZING` is a short string naming what
the advisory is ABOUT, and the first seam to claim it is the only one
that speaks.  A user who builds a bracket, analyses it, and recompiles it
hears this once, not three times.  A genuinely different message would
carry a different key and would still get its one turn.  Adding a fourth
door later is one argument to one function, not new state.

Deliberately carries NO recommended size, clearance, tolerance or fit
class: the sizing itself is a Kiln Pro capability (``recommend_hole``),
and a free result that describes the paid answer's mechanics gives it
away for nothing.  ``tests/test_design_tools.py`` greps every string in
this module for exactly that.
"""

from __future__ import annotations

from typing import Any

#: What this advisory is about.  Seams pass it to the builders below; the
#: first one to claim it in a session is the only one that emits.  A new
#: message on a genuinely different subject gets its own key here rather
#: than sharing this one — sharing would silence it.
HOLE_SIZING = "hole_sizing"

#: Content keys already spoken in this process.  The MCP server is one
#: long-lived process per session, so process lifetime IS session
#: lifetime; nothing is written to disk and nothing survives a restart.
#: Tests reset it with :func:`reset_emitted_content_keys`.
_EMITTED: set[str] = set()

#: The one benefit line, written once.  Every seam's note ends with it,
#: so there is exactly one sentence to review and one to leak-grep.
_OFFER = (
    "Kiln Pro's recommend_hole can size a hole for a named screw, tuned "
    "for how holes actually print."
)

#: Addressed to the AGENT on purpose: whether the user named a fastener
#: lives in the conversation, which these tools cannot see and the agent
#: can.  It is NOT called ``agent_guidance`` — that name belongs to the
#: curated design-knowledge field the SME-leak gate
#: (``scripts/audit_public_sme_leak.py``) watches for in public source,
#: and this is a relay rule, not curated content.
#:
#: It no longer asks the agent to ration the mention.  The content key
#: above already guarantees one line per session, so telling the agent to
#: manage frequency would be asking it to enforce something already
#: enforced — and an instruction that can be ignored reads as optional.
_AGENT_INSTRUCTION = (
    "Kiln raises this at most once per session, so nothing here needs "
    "rationing. Mention Kiln Pro only if the user named an actual "
    "fastener in this conversation (for example an M-size, a screw "
    "number, or a rod or magnet size). If they never named one, say "
    "nothing about Pro — just hand over the part."
)


def reset_emitted_content_keys() -> None:
    """Forget every key spoken so far.  For tests and long-lived hosts."""
    _EMITTED.clear()


def _claim(content_key: str) -> bool:
    """True the first time ``content_key`` is asked for, False after."""
    if content_key in _EMITTED:
        return False
    _EMITTED.add(content_key)
    return True


def _block(
    subjects: list[str],
    note: str,
    *,
    content_key: str = HOLE_SIZING,
) -> dict[str, Any] | None:
    """The advisory, or ``None`` if this key was already spoken.

    ``subjects`` is what the advisory is about, in the caller's own
    words: the template parameter names they overrode, the fastener they
    named, or nothing at all when the subject is geometry rather than
    anything the caller typed.
    """
    if not _claim(content_key):
        return None
    return {
        "parameters": list(subjects),
        "pro_depth_applied": False,
        "note": note,
        "agent_instruction": _AGENT_INSTRUCTION,
    }


def advice_for_template_parameters(
    names: list[str],
    *,
    content_key: str = HOLE_SIZING,
) -> dict[str, Any] | None:
    """``generate_from_template``: the caller supplied the dimension.

    Kiln built the part to the number they gave.  That is the right
    default — it is their part — but it means nobody checked the number
    against the hardware it is for, and a silent result reads as
    "checked, fine".
    """
    listed = ", ".join(names)
    subject = "they were" if len(names) > 1 else "it was"
    return _block(
        list(names),
        (
            f"Kiln built this part to the {listed} you gave, using the "
            f"template's own geometry — {subject} not sized for any "
            f"particular fastener. {_OFFER}"
        ),
        content_key=content_key,
    )


def advice_for_detected_holes(
    *,
    content_key: str = HOLE_SIZING,
) -> dict[str, Any] | None:
    """``analyze_printability``: the mesh turned out to have holes.

    The widest door of the three — it reaches any mesh, however it was
    made, including one Kiln never generated.  Nothing the caller typed
    is the subject here, so ``parameters`` is empty; the holes
    themselves are already listed in the report beside this block.
    """
    return _block(
        [],
        (
            "This report found holes in the part and measured them as "
            f"they are — nothing checked them against the hardware "
            f"meant to go in. {_OFFER}"
        ),
        content_key=content_key,
    )


def advice_for_named_fastener(
    fastener: str,
    *,
    content_key: str = HOLE_SIZING,
) -> dict[str, Any] | None:
    """``compile_scad``: the caller said which fastener this is for.

    DECLARED intent only.  Nothing reads the OpenSCAD source looking for
    screw holes — guessing fastener intent out of geometry would be
    wrong often, and a field that fires wrongly is a field agents learn
    to skip.  No argument, no advisory.

    The name the caller passed rides in ``parameters``, never in the
    prose, so the note stays free of anything that looks like a size.
    """
    return _block(
        [fastener],
        (
            "Kiln compiled the geometry in your code exactly as written "
            "— nothing resized the hole for the fastener you named. "
            f"{_OFFER}"
        ),
        content_key=content_key,
    )
