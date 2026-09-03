"""Tests for the on-device usage ledger (free-tier local stats).

Public-Kiln port of kiln-pro's ``test_local_usage_{recorder,flush}``
pair.  Pins both layers:

* Layer 1 (record): counts accumulate per ``(day, tool)``; junk names
  are dropped; a broken ledger never raises into the caller (the
  hot-path contract); the device id is stable.
* Layer 2 (flush): absolute counts are posted with the device id and the
  OAuth bearer; the watermark advances only on a CONFIRMED write (so a
  failure re-sends); signed-out / offline is a no-op; the hot-path
  trigger throttles.
"""
import importlib.util
import sqlite3

import pytest

from kiln import usage_ledger as lr


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    monkeypatch.setenv("KILN_USAGE_LEDGER_PATH", str(path))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    return path


def _counts(path) -> dict:
    # record() doesn't create the ledger when every call is dropped, so a
    # missing file / table legitimately means "no counts recorded".
    if not path.exists():
        return {}
    conn = sqlite3.connect(str(path))
    try:
        have = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='daily_tool_calls'"
        ).fetchone()
        if have is None:
            return {}
        return {
            (day, tool): n
            for day, tool, n in conn.execute(
                "SELECT day, tool, n FROM daily_tool_calls"
            )
        }
    finally:
        conn.close()


# --- Layer 1: record ----------------------------------------------------


def test_record_accumulates_per_tool(ledger):
    lr.record("slice_and_print")
    lr.record("slice_and_print")
    lr.record("list_designs")
    counts = _counts(ledger)
    day = lr._today()
    assert counts[(day, "slice_and_print")] == 2
    assert counts[(day, "list_designs")] == 1


def test_record_drops_empty_and_overlong(ledger):
    lr.record("")
    lr.record("x" * 121)
    assert _counts(ledger) == {}


def test_record_never_raises_on_broken_ledger(tmp_path, monkeypatch):
    # Point the ledger at a directory so sqlite can't open it; record
    # must still return cleanly — a tool call is never broken by stats.
    bad = tmp_path / "ledger_is_a_dir"
    bad.mkdir()
    monkeypatch.setenv("KILN_USAGE_LEDGER_PATH", str(bad))
    lr.record("whatever")  # must not raise


def test_device_id_is_stable(ledger):
    first = lr.device_id()
    second = lr.device_id()
    assert first == second
    assert len(first) >= 8


# --- Layer 2: flush -----------------------------------------------------


@pytest.fixture
def flushable(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_USAGE_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    # Override the API base so flush() never imports kiln.server.
    monkeypatch.setenv("KILN_API_URL", "https://api.example.test")
    yield


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"success": True}

    def json(self):
        return self._payload


def test_flush_posts_absolute_counts_and_advances_watermark(flushable, monkeypatch):
    lr.record("slice_and_print")
    lr.record("slice_and_print")
    lr.record("list_designs")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResp(200, {"success": True, "recorded": len(json["entries"])})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(lr, "_oauth_token", lambda: "tok-123")

    assert lr.flush() == 2  # two (day, tool) rows
    assert captured["url"] == "https://api.example.test/api/me/stats/record"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert captured["json"]["device_id"]
    counts = {e["tool"]: e["count"] for e in captured["json"]["entries"]}
    assert counts == {"slice_and_print": 2, "list_designs": 1}

    # Idempotent: nothing new to send → no post, returns 0.
    captured.clear()
    assert lr.flush() == 0
    assert captured == {}


def test_flush_noop_when_signed_out(flushable, monkeypatch):
    lr.record("x")
    hits = {"n": 0}

    def fake_post(*a, **k):
        hits["n"] += 1
        return _FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(lr, "_oauth_token", lambda: None)
    assert lr.flush() == 0
    assert hits["n"] == 0  # never touched the network


def test_flush_keeps_watermark_on_http_failure(flushable, monkeypatch):
    lr.record("y")
    monkeypatch.setattr(lr, "_oauth_token", lambda: "tok")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(503, {"success": False}))
    assert lr.flush() == 0  # server failed

    sent = {}

    def ok_post(url, headers=None, json=None, timeout=None):
        sent["entries"] = json["entries"]
        return _FakeResp(200, {"success": True})

    monkeypatch.setattr("requests.post", ok_post)
    assert lr.flush() == 1  # watermark wasn't advanced, so y re-sends
    assert any(e["tool"] == "y" for e in sent["entries"])


def test_flush_treats_unsuccessful_body_as_failure(flushable, monkeypatch):
    lr.record("z")
    monkeypatch.setattr(lr, "_oauth_token", lambda: "tok")
    # HTTP 200 but success:false (e.g. sync_unavailable) must NOT advance.
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(200, {"success": False}))
    assert lr.flush() == 0


def test_flush_refuses_non_https_base(flushable, monkeypatch):
    # The OAuth bearer must never go out in cleartext.
    monkeypatch.setenv("KILN_API_URL", "http://insecure.example.test")
    lr.record("w")
    monkeypatch.setattr(lr, "_oauth_token", lambda: "tok")
    hits = {"n": 0}
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: hits.__setitem__("n", hits["n"] + 1)
    )
    assert lr.flush() == 0
    assert hits["n"] == 0  # never put the bearer on a non-https wire


def test_maybe_flush_throttles(flushable, monkeypatch):
    spawns = {"n": 0}

    class _FakeThread:
        def __init__(self, *a, **k):
            spawns["n"] += 1

        def start(self):
            pass

    monkeypatch.setattr(lr.threading, "Thread", _FakeThread)
    lr._last_flush = lr._NEVER_FLUSHED
    lr.maybe_flush()  # due → spawns
    lr.maybe_flush()  # within interval → throttled
    assert spawns["n"] == 1


def _pristine_last_flush() -> float:
    """The module's own initial ``_last_flush``, as a fresh import sees it.

    Loaded as a private copy so the value is the module's, not whatever an
    earlier test left behind -- and so this test never hand-copies the
    sentinel it is meant to be checking.
    """
    spec = importlib.util.spec_from_file_location("_usage_ledger_probe", lr.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe._last_flush


def test_first_flush_is_due_on_a_freshly_booted_machine(flushable, monkeypatch):
    """A machine minutes past boot must still flush.

    ``maybe_flush`` throttles on ``time.monotonic()``, which counts seconds
    since boot -- so a 0.0 "never flushed" sentinel reads as a flush that
    just happened, and the first flush is skipped for the box's first five
    minutes.  CI runners are always that young, which is how this reached
    main red: the throttle test above passes on a long-running dev machine
    and fails on a fresh runner.
    """
    spawns = {"n": 0}

    class _FakeThread:
        def __init__(self, *a, **k):
            spawns["n"] += 1

        def start(self):
            pass

    monkeypatch.setattr(lr.threading, "Thread", _FakeThread)
    monkeypatch.setattr(lr.time, "monotonic", lambda: 120.0)  # up 2 minutes
    monkeypatch.setattr(lr, "_last_flush", _pristine_last_flush())

    lr.maybe_flush()

    assert spawns["n"] == 1
