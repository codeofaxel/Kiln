"""The print queue is per-machine, and the hosted server must say so.

``~/.kiln/queue.db`` has schema ``jobs(id, file_name, printer_name, status,
submitted_by, priority, ...)`` — no tenant column — and the hosted server
runs ONE ``~/.kiln`` for every customer.  Measured before the fix, with two
tenants against one queue: tenant B listed tenant A's job (the file name
alone carries client names and part numbers) and could cancel it.  A
cross-tenant read AND a cross-tenant write.

The guard sits on ``_get_queue()`` rather than on a list of tool names
because that is the one resolver every reader passes through.  The first
attempt was a name list and it was already incomplete — ``await_print_completion``
and ``analyze_print_failure`` read a job by id and return the whole record,
and three MCP resources read the queue too.  The name list survives as
kiln-pro's dispatcher fast path (it answers before any work happens and
words the refusal per tool); this is the boundary behind it.
"""

from __future__ import annotations

import pytest

from kiln.errors import HostedUnavailableError


@pytest.fixture()
def _fresh_queue(monkeypatch, tmp_path):
    """Point the module queue at a temp DB and reset the singleton."""
    import kiln.server as srv

    monkeypatch.setattr(srv, "_queue", None, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    yield srv
    monkeypatch.setattr(srv, "_queue", None, raising=False)


class TestQueueRefusesOnHosted:
    def test_the_refusal_is_typed_and_word_for_word(self, _fresh_queue, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(HostedUnavailableError) as excinfo:
            _fresh_queue._get_queue()
        assert isinstance(excinfo.value, ValueError)
        assert str(excinfo.value) == (
            "Your print queue is not available on the hosted Kiln API: it "
            "lives on the machine attached to your printer, and this server "
            "keeps no per-account queue. Run this from your local Kiln "
            "install or the CLI, or connect that machine through the Kiln "
            "bridge and your queue follows."
        )

    def test_no_queue_file_is_created_on_hosted(self, _fresh_queue, monkeypatch, tmp_path):
        """The refusal must land BEFORE the DB is opened.

        A guard that refuses after constructing the store has already
        written to the shared disk.
        """
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(HostedUnavailableError):
            _fresh_queue._get_queue()
        assert not (tmp_path / ".kiln" / "queue.db").exists()

    def test_the_doors_the_name_list_missed_answer_with_the_reason(
        self, _fresh_queue, monkeypatch
    ):
        """The two tools a name list would have to remember.

        Both read a job by id and return ``job.to_dict()`` — including the
        file name.  Neither is in the dispatcher's queue block list, which
        is exactly why the boundary belongs on the resolver.
        """
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        srv = _fresh_queue

        failure = srv.analyze_print_failure(job_id="whatever")
        assert failure.get("success") is False
        assert failure["error"]["code"] == "HOSTED_UNAVAILABLE"
        assert "machine attached to your printer" in failure["error"]["message"]

        waited = srv.await_print_completion(job_id="whatever", timeout=1)
        assert waited.get("success") is False
        assert waited["error"]["code"] == "HOSTED_UNAVAILABLE"
        assert "Unexpected error" not in waited["error"]["message"]


class TestLocalInstallIsUntouched:
    """The operator IS the caller locally; this must cost them nothing.

    Asserted because a false refusal here means a user's print does not
    start — a worse outcome than the leak this guard closes.
    """

    def test_the_queue_still_works_end_to_end(self, _fresh_queue, monkeypatch):
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        q = _fresh_queue._get_queue()
        job_id = q.submit(file_name="benchy.gcode", submitted_by="test")
        assert q.get_job(job_id).file_name == "benchy.gcode"
        assert q.summary().get("queued") == 1
        assert q.cancel(job_id) is not None

    def test_the_singleton_is_still_a_singleton(self, _fresh_queue, monkeypatch):
        """The guard sits in front of the lazy-init; it must not defeat it."""
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        assert _fresh_queue._get_queue() is _fresh_queue._get_queue()

    def test_an_unset_flag_reads_as_local(self, _fresh_queue, monkeypatch):
        """Absent means somebody's own install — the safe default.

        If this ever inverted, every local install would start refusing its
        own queue, so it is pinned rather than assumed.
        """
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        assert _fresh_queue._get_queue() is not None
