"""The per-tool failure counter — the wire that measures things going WRONG.

Every other counter on the machine measures things going right, which meant a
tool broken for every owner of a printer model produced silence
indistinguishable from nobody using it.  Seven Kiln defects in one real first
session reached us by screenshot because of that gap (2026-08-10).

Two failure shapes have to count, and one of them was invisible to the whole
telemetry stack: a tool that RAISES never reached the recorder at all, because
recording happens after a call that returned.  So the loudest failure a tool
can have produced no signal whatsoever.

Same lockstep obligation as every other counter — writer, rollover slot,
heartbeat payload, dashboard reader — and the kiln-pro half of that chain is
pinned in ``kiln_pro/tests/test_tool_failure_counter_wires.py``.
"""

from __future__ import annotations

import json

import pytest

from kiln import daily_stats


@pytest.fixture(autouse=True)
def _own_stats_file(tmp_path, monkeypatch):
    """Point the recorder at a temp file.

    A custom path also disarms ``_recording_suppressed`` on purpose — that is
    the documented way a test says "I am exercising recording" — so this
    fixture is what makes the suite able to test the writer at all without
    writing into the developer's real ~/.kiln/daily_stats.json.
    """
    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "daily_stats.json")


def _today() -> dict:
    """The raw on-disk day, which is what the rollover and the heartbeat
    both read — not the summarised public view."""
    return daily_stats._read()


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_a_failure_is_counted_by_tool_name():
    daily_stats.record_tool_failure("start_print")
    daily_stats.record_tool_failure("start_print")
    daily_stats.record_tool_failure("slice_model")

    failures = _today()["tool_failures"]
    assert failures["start_print"] == 2
    assert failures["slice_model"] == 1


def test_a_tool_that_never_fails_has_no_entry():
    """Absent, not zero.  A zero would claim we measured this tool and it was
    fine; absent says nothing, which is the truth."""
    daily_stats.record_tool_failure("start_print")
    assert "generate_coaster" not in _today()["tool_failures"]


def test_the_counter_never_raises():
    """A telemetry helper that can raise turns a tool failure into a crash —
    it would be the class of bug it exists to detect."""
    for junk in (None, "", "Bad Name!", "x", 42, "a" * 500):
        daily_stats.record_tool_failure(junk)  # type: ignore[arg-type]


def test_a_junk_tool_name_is_not_recorded():
    """Names ride to the server in an anonymous payload, so the same shape
    filter every other name map uses applies here."""
    daily_stats.record_tool_failure("Bad Name!")
    daily_stats.record_tool_failure("/tmp/pp-fuzz")
    daily_stats.record_tool_failure("x")
    assert _today()["tool_failures"] == {}


# ---------------------------------------------------------------------------
# Both failure shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "error": "boom"},
        {"status": "error", "error": "boom"},
        ("unstructured", {"success": False}),
        ("unstructured", {"status": "error"}),
    ],
)
def test_both_failure_spellings_are_recognised(result):
    """``success: False`` is the public house style; ``status: "error"`` is
    common across the kiln-pro plugin surface, which dispatches through the
    same hook.  Reading only the first meant a failed pro tool was counted as
    a successful generation."""
    assert daily_stats._result_looks_failed(result) is True


@pytest.mark.parametrize(
    "result",
    [
        {"success": True, "stl_path": "/tmp/a.stl"},
        {"status": "ok"},
        {"stl_path": "/tmp/a.stl"},
        None,
        "a raw string",
        [],
    ],
)
def test_a_success_is_not_a_failure(result):
    assert daily_stats._result_looks_failed(result) is False


def test_a_content_block_result_is_read():
    """By the time the hook sees it the result is usually a list of content
    blocks whose text is the serialised dict — the shape that actually
    arrives in production."""
    class _Block:
        text = json.dumps({"success": False, "error": "boom"})

    assert daily_stats._result_looks_failed([_Block()]) is True


# ---------------------------------------------------------------------------
# The lockstep chain (public half)
# ---------------------------------------------------------------------------


def test_the_counter_survives_the_day_rollover():
    """A map that does not roll over vanishes from the complete-day report —
    which is the block the dashboard actually trusts, because the *_today
    columns are sampled at server start and read near-zero."""
    assert "tool_failures" in daily_stats._ROLLOVER_MAPS


def test_the_counter_ships_in_the_heartbeat():
    import inspect

    from kiln import heartbeat

    src = inspect.getsource(heartbeat)
    assert '"tool_failures"' in src, (
        "the counter is recorded but never leaves the machine"
    )


def test_it_rides_beside_the_calls_wire():
    """The pair is the point.  A failure count alone ranks popular tools; the
    ratio against tool_calls is what says a tool is broken.  If one ships
    without the other the number is unreadable."""
    import inspect

    from kiln import heartbeat

    src = inspect.getsource(heartbeat)
    assert '"tool_calls"' in src and '"tool_failures"' in src


def test_a_fresh_day_carries_the_slot():
    assert daily_stats._empty_day()["tool_failures"] == {}


# ---------------------------------------------------------------------------
# The chokepoint
# ---------------------------------------------------------------------------


def _drive_the_real_hook(monkeypatch, tool_impl):
    """Call ``tool_impl`` through the ACTUAL installed dispatch wrapper.

    Not a re-implementation and not a source grep: the wrapper is monkey-
    patched onto the live tool manager at import time, so the only way to
    prove it counts anything is to make it dispatch.  A source assertion
    would stay green if ``contextlib`` were unimported, if the recorder
    were misnamed, or if the counter never fired — which is the "green
    over a dead wire" shape this repo has postmortems about.
    """
    import asyncio

    from kiln import server

    mgr = server.mcp._tool_manager

    async def _fake_original(name, arguments, context=None, convert_result=False):
        return tool_impl()

    # Replace the INNER call the wrapper delegates to, leaving the real
    # wrapper — the code under test — in place.
    monkeypatch.setattr(mgr, "_kiln_request_context_capture_installed", False)
    monkeypatch.setattr(mgr, "call_tool", _fake_original)
    server._install_mcp_request_context_capture()
    # The terms gate is a refusal we MEANT and would raise before dispatch.
    monkeypatch.setattr(server, "_terms_gate_blocks", lambda _n: False)
    return asyncio.run(mgr.call_tool("start_print", {}))


def test_a_raising_tool_is_really_counted(monkeypatch):
    """The half that was structurally invisible: recording ran only after a
    call RETURNED, so the loudest failure a tool can have — an exception —
    produced no signal anywhere."""
    def _boom():
        raise RuntimeError("the printer exploded")

    with pytest.raises(RuntimeError, match="the printer exploded"):
        _drive_the_real_hook(monkeypatch, _boom)

    assert _today()["tool_failures"]["start_print"] == 1


def test_the_exception_reaches_the_agent_unchanged(monkeypatch):
    """Counting must never swallow, wrap, or delay the error the caller was
    going to see — a telemetry hook that eats an exception is worse than one
    that misses it."""
    class _Specific(Exception):
        pass

    def _boom():
        raise _Specific("exact identity preserved")

    with pytest.raises(_Specific, match="exact identity preserved"):
        _drive_the_real_hook(monkeypatch, _boom)


def test_a_failure_envelope_is_really_counted(monkeypatch):
    """The other shape, through the same live wrapper."""
    result = _drive_the_real_hook(
        monkeypatch, lambda: {"success": False, "error": "no printer"}
    )
    assert result == {"success": False, "error": "no printer"}
    assert _today()["tool_failures"]["start_print"] == 1


def test_a_successful_call_counts_no_failure(monkeypatch):
    _drive_the_real_hook(monkeypatch, lambda: {"success": True, "job_id": "j1"})
    assert _today()["tool_failures"] == {}


def test_a_terms_refusal_is_not_a_kiln_failure(monkeypatch):
    """The terms gate raises BEFORE dispatch, and it is a refusal we meant —
    counting it would put the product working correctly into the wire that
    says the product is broken."""
    import asyncio

    from kiln import server

    mgr = server.mcp._tool_manager

    async def _never_called(name, arguments, context=None, convert_result=False):
        raise AssertionError("dispatch must not be reached")

    monkeypatch.setattr(mgr, "_kiln_request_context_capture_installed", False)
    monkeypatch.setattr(mgr, "call_tool", _never_called)
    server._install_mcp_request_context_capture()
    monkeypatch.setattr(server, "_terms_gate_blocks", lambda _n: True)
    monkeypatch.setattr(server, "_terms_consent_message", lambda: "accept terms")

    with pytest.raises(RuntimeError, match="accept terms"):
        asyncio.run(mgr.call_tool("start_print", {}))

    assert _today()["tool_failures"] == {}


def test_the_dispatch_hook_counts_a_failure_envelope():
    import inspect

    from kiln import server

    src = inspect.getsource(server._record_local_tool_call)
    assert "record_tool_failure" in src
    assert "_result_looks_failed" in src


def test_recording_is_suppressed_for_a_real_test_run(monkeypatch, tmp_path):
    """The guard that stops CI writing phantom failures into a real install's
    telemetry.  It comes free by living in the same module — but free is not
    the same as proven, and the last time this was assumed a suite left 47
    phantom prints queued for the dashboard."""
    monkeypatch.setattr(
        daily_stats, "_STATS_PATH", daily_stats._DEFAULT_STATS_PATH
    )
    assert daily_stats._recording_suppressed() is True


# ---------------------------------------------------------------------------
# The ask has to reach the agent — and there is more than one door
# ---------------------------------------------------------------------------


def test_both_discovery_doors_carry_the_ask():
    """``get_started`` and ``get_skill_manifest`` are two independent agent
    entry points, built from two different sources — the first assembles its
    own dict, the second delegates to ``kiln.skill_manifest``.  Doctrine in
    one of them is doctrine an agent that used the other never sees, which is
    the one-door fallacy applied to a prompt instead of a security gate.

    The counter tells us a tool is failing.  Only a human's note says WHY, and
    only if some agent was told to send one.
    """
    import inspect

    from kiln.plugins import utility_tools
    from kiln.skill_manifest import generate_manifest

    started = inspect.getsource(utility_tools)
    assert "when_kiln_gets_it_wrong" in started, "get_started lost the ask"

    manifest = generate_manifest().to_dict()
    blob = json.dumps(manifest)
    assert "when_kiln_gets_it_wrong" in blob, "the skill manifest lost the ask"
    assert "field_note" in blob, "the manifest names no way to send one"
    for field in ("observed", "tried", "worked", "evidence"):
        assert field in blob, f"the manifest omits the {field!r} field"


def test_the_ask_does_not_wait_for_an_error():
    """The defects that cost a real user his whole first session returned
    SUCCESS — start_print reporting a print that never began, filament state
    read from a stale cache, RUNNING while the printer sat PAUSED.  None of
    them produce an error envelope, so none of them can be caught by a hint
    attached to a failure.  The norm has to be stated up front, and it has to
    say so explicitly, or an agent waits for a prompt that never comes."""
    import json as _json

    from kiln.skill_manifest import generate_manifest

    ask = _json.dumps(generate_manifest().to_dict())
    assert "DO NOT WAIT TO BE ASKED" in ask
    assert "return success while the printer never starts" in ask, (
        "the manifest must name the shape: a defect that looks like success"
    )
