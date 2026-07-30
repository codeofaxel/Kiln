"""The 3D stage document, cached on this machine.

The properties that matter are all about not losing: an offline machine
keeps the stage it already downloaded, a cold cache is quiet rather than
fatal, and nothing that isn't an HTML document ever replaces a good copy.
"""

from __future__ import annotations

import pytest

from kiln import stage_cache

_DOC = "<!DOCTYPE html><html><body>stage</body></html>"


class _Resp:
    def __init__(self, status=200, body=_DOC, etag='"abc"', headers=None):
        self.status_code = status
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = headers if headers is not None else ({"ETag": etag} if etag else {})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test gets its own ~/.kiln and a clean process memo."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    monkeypatch.delenv(stage_cache._OPT_OUT_ENV, raising=False)
    stage_cache._reset_for_tests()
    yield
    stage_cache._reset_for_tests()


def _serve(monkeypatch, *responses, record=None):
    """Point the fetch at a canned sequence of responses."""
    calls = record if record is not None else []

    def _get(url, headers=None, timeout=None, follow_redirects=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    import httpx

    monkeypatch.setattr(httpx, "get", _get)
    return calls


class TestColdCache:
    def test_document_is_none_before_anything_is_downloaded(self):
        assert stage_cache.document() is None

    def test_a_cold_cache_and_no_network_is_quiet(self, monkeypatch):
        import httpx

        def _boom(*a, **k):
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "get", _boom)
        assert stage_cache.refresh() is None
        assert stage_cache.document() is None


class TestFetchAndCache:
    def test_refresh_writes_the_document_and_serves_it(self, monkeypatch):
        _serve(monkeypatch, _Resp())
        assert stage_cache.refresh() == _DOC
        assert stage_cache.document() == _DOC

    def test_a_second_process_reads_it_off_disk(self, monkeypatch):
        _serve(monkeypatch, _Resp())
        stage_cache.refresh()
        stage_cache._reset_for_tests()  # as if the server restarted
        assert stage_cache.document() == _DOC

    def test_the_etag_is_sent_back_on_the_next_check(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(etag='"v1"'), _Resp(status=304, body=b""))
        stage_cache.refresh()
        stage_cache.refresh()
        assert calls[0]["headers"].get("If-None-Match") is None, (
            "nothing cached yet — there is no ETag to revalidate against"
        )
        assert calls[1]["headers"]["If-None-Match"] == '"v1"'

    def test_a_304_keeps_the_document_it_already_has(self, monkeypatch):
        _serve(monkeypatch, _Resp(body=_DOC), _Resp(status=304, body=b""))
        stage_cache.refresh()
        stage_cache._reset_for_tests()
        assert stage_cache.refresh() == _DOC
        assert stage_cache.document() == _DOC

    def test_a_new_document_replaces_the_old_one(self, monkeypatch):
        newer = "<!DOCTYPE html><html><body>newer stage</body></html>"
        _serve(monkeypatch, _Resp(body=_DOC, etag='"v1"'), _Resp(body=newer, etag='"v2"'))
        stage_cache.refresh()
        stage_cache._reset_for_tests()
        stage_cache.refresh()
        assert stage_cache.document() == newer


class TestNeverLosesAGoodCopy:
    """An install that HAS the stage must not lose it to a bad day."""

    def _seed(self, monkeypatch):
        _serve(monkeypatch, _Resp())
        stage_cache.refresh()
        stage_cache._reset_for_tests()

    def test_a_dead_api_leaves_the_cached_copy_alone(self, monkeypatch):
        self._seed(monkeypatch)
        _serve(monkeypatch, _Resp(status=500, body="upstream is unwell"))
        assert stage_cache.refresh() == _DOC  # what you have, not "did it fetch"
        assert stage_cache.document() == _DOC

    def test_a_captive_portal_never_becomes_the_stage(self, monkeypatch):
        """A 200 that is somebody's wifi login page, not our document."""
        self._seed(monkeypatch)
        _serve(monkeypatch, _Resp(body="Sign in to HotelWiFi to continue"))
        stage_cache.refresh()
        assert stage_cache.document() == _DOC

    def test_an_absurdly_large_body_is_refused(self, monkeypatch):
        self._seed(monkeypatch)
        _serve(monkeypatch, _Resp(body=b"<!doctype html>" + b"x" * (stage_cache._MAX_BYTES + 1)))
        stage_cache.refresh()
        assert stage_cache.document() == _DOC

    def test_offline_keeps_the_stage(self, monkeypatch):
        self._seed(monkeypatch)
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
            httpx.ConnectError("offline")))
        assert stage_cache.refresh() == _DOC
        assert stage_cache.document() == _DOC


class TestOptOut:
    def test_opting_out_skips_the_network_entirely(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp())
        monkeypatch.setenv(stage_cache._OPT_OUT_ENV, "1")
        assert stage_cache.refresh() is None
        assert calls == [], "opted out and it phoned home anyway"

    def test_opting_out_still_serves_a_document_already_on_disk(self, monkeypatch):
        _serve(monkeypatch, _Resp())
        stage_cache.refresh()
        stage_cache._reset_for_tests()
        monkeypatch.setenv(stage_cache._OPT_OUT_ENV, "1")
        assert stage_cache.refresh() == _DOC
        assert stage_cache.document() == _DOC


class TestWarm:
    def test_warm_fills_the_cache_without_blocking_the_caller(self, monkeypatch):
        _serve(monkeypatch, _Resp())
        t = stage_cache.warm()
        assert t is not None and t.daemon, (
            "a server start must not be held up by, or outlive, a download"
        )
        t.join(timeout=5)
        assert stage_cache.document() == _DOC

    def test_document_does_not_fetch(self, monkeypatch):
        """It is called while a host waits on resources/read — a synchronous
        download there hangs the panel on a slow line."""
        calls = _serve(monkeypatch, _Resp())
        assert stage_cache.document() is None
        assert calls == []
