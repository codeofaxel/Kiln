"""Capturing the firmware code locally is only half of what it was for.

A single machine cannot characterise a fault: six deliberate A1 runs bought
"a Z-homing fault, latching about a third of the time" and could not say
which models, which firmware, or what predicts a latch.  Those questions are
answered by many printers or not at all, so the code has to reach the shared
corpus — and until now it stopped at the local row, because
``sync_community_print`` builds its wire body from a fixed list of named
fields and silently drops everything else.  A code added to the contribution
payload alone would have sat in the outbox forever, looking wired.

The corpus does NOT have this column yet (zero hits for ``print_error``
across kiln-pro's SQL as of 2026-08-13).  That is survivable by design
rather than by luck: PostgREST rejects the WHOLE insert over one unknown
column, so the send drops the fields a server names and retries, and the
outcome lands with or without its code.  When the cloud column ships,
codes start accumulating with no further client change.

What is deliberately NOT here: cancelled prints still contribute nothing.
``translate_outcome`` refuses them because they carry no verdict on the
settings, and loosening that to carry a code would put cancels back into
the quality corpus — the exact bug the cancel-intent work removed.  A
cancel's code stays local until the corpus grows a record type that is
about faults rather than about print quality.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest import mock

import pytest

_Z_HOMING_FAILED = 50348044  # 0x0300400C, as measured on the A1


@pytest.fixture(autouse=True)
def _opt_in(monkeypatch):
    """Exercise the wire function against a mocked urllib, as its own suite does."""
    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    monkeypatch.setenv("KILN_COMMUNITY_TEST_SEND", "1")


class _FakeResp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(message: str) -> urllib.error.HTTPError:
    """A PostgREST rejection, with the body it really returns."""
    payload = json.dumps({"code": "PGRST204", "message": message}).encode()
    return urllib.error.HTTPError(
        "https://example.invalid", 400, "Bad Request", {},  # type: ignore[arg-type]
        io.BytesIO(payload),
    )


def _sender(*, fail_first_with: str | None = None):
    """Capture every body POSTed; optionally reject the first attempt."""
    bodies: list[dict[str, Any]] = []

    def fake_urlopen(req, *a, **k):
        bodies.append(json.loads(req.data.decode()))
        if fail_first_with is not None and len(bodies) == 1:
            raise _http_error(fail_first_with)
        return _FakeResp()

    return bodies, fake_urlopen


def _record(**over: Any) -> dict[str, Any]:
    base = {
        "geometric_signature": "sig-abc",
        "geometric_signature_v2": "v2:1234567890abcdef",
        "printer_model": "bambu_a1",
        "material": "PLA",
        "outcome": "failed",
        "print_error": _Z_HOMING_FAILED,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# The code actually leaves the machine
# ---------------------------------------------------------------------------


def test_the_code_reaches_the_wire():
    """The bug this exists to prevent: a field that never leaves the outbox.

    ``sync_community_print`` names every field it sends, so anything the
    contribution path adds without a line here is dropped in silence.
    """
    from kiln import community_sync

    bodies, fake = _sender()
    with mock.patch("urllib.request.urlopen", fake):
        assert community_sync.sync_community_print(_record()) is True

    assert bodies[0]["print_error"] == _Z_HOMING_FAILED
    # And the fields it always carried are untouched.
    assert bodies[0]["outcome"] == "failed"
    assert bodies[0]["geometric_signature_v2"] == "v2:1234567890abcdef"


def test_a_print_with_no_code_ships_no_column():
    """Absent means absent — not a zero standing in for one.

    A corpus counting 0 as a fault would report one on every clean print.
    """
    from kiln import community_sync

    bodies, fake = _sender()
    with mock.patch("urllib.request.urlopen", fake):
        community_sync.sync_community_print(_record(print_error=0))

    assert "print_error" not in bodies[0]


# ---------------------------------------------------------------------------
# A corpus that does not have the column yet
# ---------------------------------------------------------------------------


def test_a_server_without_the_column_still_gets_the_print():
    """Today's reality: the cloud column does not exist.

    PostgREST rejects the whole insert over the one unknown field, so
    without this the change would not merely fail to add codes — it would
    LOSE every contribution that had one.
    """
    from kiln import community_sync

    bodies, fake = _sender(
        fail_first_with=(
            "Column 'print_error' of relation 'community_prints' does not exist"
        )
    )
    with mock.patch("urllib.request.urlopen", fake):
        assert community_sync.sync_community_print(_record()) is True

    assert len(bodies) == 2, "the row was retried, not abandoned"
    assert "print_error" not in bodies[1]
    assert bodies[1]["outcome"] == "failed", "the outcome still landed"


def test_the_retry_keeps_the_fields_the_server_did_not_refuse():
    """The regression this design exists to avoid.

    A retry that dropped every optional field would cost the v2 signature on
    every contribution for as long as the corpus lagged on ``print_error`` —
    trading a column we are adding against one already in use.  PostgREST
    names the column it could not find, so only that one goes.
    """
    from kiln import community_sync

    bodies, fake = _sender(
        fail_first_with=(
            "Column 'print_error' of relation 'community_prints' does not exist"
        )
    )
    with mock.patch("urllib.request.urlopen", fake):
        community_sync.sync_community_print(_record())

    assert bodies[1]["geometric_signature_v2"] == "v2:1234567890abcdef"


def test_an_unreadable_rejection_drops_every_optional_field():
    """When the server names nothing we know, the row still matters more.

    Losing an optional column is always the better trade than losing the
    print, so an unrecognised 400 falls back to the widest retry.
    """
    from kiln import community_sync

    bodies, fake = _sender(fail_first_with="something went wrong")
    with mock.patch("urllib.request.urlopen", fake):
        assert community_sync.sync_community_print(_record()) is True

    assert "print_error" not in bodies[1]
    assert "geometric_signature_v2" not in bodies[1]
    assert bodies[1]["outcome"] == "failed"


def test_a_rejection_with_a_real_cause_is_not_retried_forever():
    """A row with nothing optional left to drop fails rather than looping."""
    from kiln import community_sync

    bodies, fake = _sender(fail_first_with="permission denied for table")
    with mock.patch("urllib.request.urlopen", fake):
        ok = community_sync.sync_community_print(
            {"geometric_signature": "sig-abc", "outcome": "failed"}
        )

    assert ok is False
    assert len(bodies) == 1


# ---------------------------------------------------------------------------
# The contribution door, and the vocabulary it must NOT loosen
# ---------------------------------------------------------------------------


def test_the_door_normalizes_the_code_once():
    """Owned by the door, like the outcome word and the model name.

    Two spellings of one code would count one fault as two, which is the
    whole failure mode ``canonical_printer_model`` already exists to stop.
    """
    from kiln import community_outbox

    sent: list[dict[str, Any]] = []
    with mock.patch.object(
        community_outbox, "contribute",
        lambda key, record, **k: sent.append(record) or {"queued": True},
    ):
        community_outbox.contribute_print_outcome(
            outcome="failed",
            geometric_signature="sig-abc",
            print_error="50348044",          # a digit string is still a code
        )
        community_outbox.contribute_print_outcome(
            outcome="failed",
            geometric_signature="sig-def",
            print_error=0,                   # no fault to name
        )

    assert sent[0]["print_error"] == _Z_HOMING_FAILED
    assert "print_error" not in sent[1]


def test_a_cancelled_print_still_contributes_nothing():
    """Deliberately unchanged, and the reason is worth stating.

    A cancel says nothing about whether the settings were good, so it has no
    place in a quality corpus — that is the bug the cancel-intent work
    removed.  Carrying a code is not a reason to put it back; the code stays
    on the local row until there is a record type that is about faults
    rather than about print quality.
    """
    from kiln import community_outbox

    out = community_outbox.contribute_print_outcome(
        outcome="cancelled",
        geometric_signature="sig-abc",
        print_error=_Z_HOMING_FAILED,
    )
    assert out == {"contributed": False, "reason": "non_quality_outcome"}


@pytest.mark.parametrize(
    ("module", "symbol"),
    [
        ("kiln.plugins.learning_tools", "record_print_outcome"),
        ("kiln.community_autofire", "contribute_resolved_outcome"),
        ("kiln.auto_record_hook", "reconcile_pending_outcomes"),
    ],
)
def test_every_federating_door_passes_the_code(module, symbol):
    """Pinned by source, because what regresses here is an argument going missing.

    A door that federates an outcome without its code is invisible from the
    corpus — the print is there, counted, simply never carrying the one
    field a fleet would group it by.
    """
    import importlib
    import inspect

    mod = importlib.import_module(module)
    fn = getattr(mod, symbol)
    source = inspect.getsource(getattr(fn, "fn", getattr(fn, "callback", fn)))

    assert "print_error=" in source, (
        f"{module}.{symbol} federates an outcome without carrying its "
        "firmware code"
    )
