"""On the shared hosted server, Kiln reads no process or socket table.

Leftover local servers and printer connection slots are questions about a
person's own computer.  On the shared server a customer's call to
``get_started`` (or the health and status doors that share the detector)
once started ``ps`` there.  The check lives in the one module that reads the
tables, so every door that reaches it is covered.
"""

from __future__ import annotations

import pytest

from kiln import run_program, serve_siblings


@pytest.fixture()
def programs(monkeypatch):
    started: list[list[str]] = []

    def _run(argv, *a, **k):
        started.append([str(x) for x in argv])
        raise FileNotFoundError("not run in tests")

    monkeypatch.setattr(run_program, "run", _run)
    return started


def _scans(started: list[list[str]]) -> list[list[str]]:
    return [argv for argv in started if argv and argv[0] in {"ps", "lsof", "powershell", "pwsh"}]


def test_the_shared_server_starts_no_process_listing(monkeypatch, programs):
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    report = serve_siblings.check_serve_siblings()
    assert report["count"] is None and report["warning"] is None
    assert serve_siblings.printer_connection_holders("192.0.2.10")["supported"] is False
    assert _scans(programs) == []


def test_get_started_on_the_shared_server_starts_no_process_listing(monkeypatch, programs):
    from kiln import server

    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    server.mcp._tool_manager._tools["get_started"].fn()
    assert _scans(programs) == []


def test_a_persons_own_computer_still_looks(monkeypatch, programs):
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setattr(serve_siblings, "_process_table_kind", lambda: "ps")
    serve_siblings.check_serve_siblings()
    assert _scans(programs) and _scans(programs)[0][0] == "ps"
