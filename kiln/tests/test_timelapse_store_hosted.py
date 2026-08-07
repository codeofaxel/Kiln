"""The timelapse frame store is per-machine, and hosted must say so.

``~/.kiln/timelapses/<watch_id>/`` holds photographs of one caller's
printer and parts.  The hosted multi-tenant server runs ONE ``~/.kiln``
for every customer with no persistent volume, so frames saved there
would sit on a disk shared with every other tenant until the next
deploy discarded them.

The guard sits on the store's resolver (``_timelapse_root`` /
``_timelapse_dir``), not on the tool: watching a print is monitoring
and is never gated — only saving frames to the shared box refuses.
The refusal is typed (:class:`kiln.errors.HostedUnavailableError`) so
``watch_print`` catches it explicitly before its generic handler and
the caller gets a stated reason, never an "unexpected error".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiln.errors import HostedUnavailableError
from kiln.plugins.monitoring_tools import (
    _PrintWatcher,
    _timelapse_dir,
    _timelapse_root,
)


class _MockMcp:
    """Minimal MCP stub that captures registered tools by name."""

    def __init__(self) -> None:
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn

        return decorator

    def __getitem__(self, name: str):
        return self._tools[name]


def _printing_adapter():
    """An adapter mid-print, so ``watch_print`` gets past its idle check."""
    from kiln.printers.base import (
        JobProgress,
        PrinterCapabilities,
        PrinterState,
        PrinterStatus,
    )

    adapter = MagicMock()
    adapter.get_state.return_value = PrinterState(
        connected=True, state=PrinterStatus.PRINTING
    )
    adapter.get_job.return_value = JobProgress(completion=20.0)
    adapter.capabilities = PrinterCapabilities(can_snapshot=False)
    return adapter


def _make_watch_print(monkeypatch, adapter):
    """Register the plugin against a mock MCP with a stubbed server."""
    import kiln.server as _srv
    from kiln.plugins.monitoring_tools import _MonitoringToolsPlugin

    monkeypatch.setattr(_srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(_srv, "_get_adapter", lambda: adapter)
    monkeypatch.setattr(_srv, "_get_event_bus", lambda: MagicMock())
    monkeypatch.setattr(_srv, "_watchers", {}, raising=False)
    mcp = _MockMcp()
    _MonitoringToolsPlugin().register(mcp)
    return mcp


class TestTimelapseRootIsPerMachine:
    """The resolver is the boundary; every path to the store crosses it."""

    def test_the_refusal_is_typed_and_word_for_word(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(HostedUnavailableError) as excinfo:
            _timelapse_root()
        assert isinstance(excinfo.value, ValueError)
        assert str(excinfo.value) == (
            "Timelapse frames are not saved on the hosted Kiln API: they "
            "are captured by the machine that watches your printer, and "
            "this server keeps no per-account copy of them. Run this from "
            "your local Kiln install, where the camera and the frames are."
        )

    def test_local_install_is_unaffected(self, monkeypatch, tmp_path):
        """The operator IS the caller locally; this must cost them nothing."""
        monkeypatch.setattr(
            "kiln.plugins.monitoring_tools._TIMELAPSES_ROOT", str(tmp_path)
        )
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        assert _timelapse_dir("abc123").endswith("abc123")

    @pytest.mark.parametrize(
        "hostile",
        ["../../pwned", "../escaped", "..", ".", "a/b", "/etc", "", "   "],
    )
    def test_watch_id_cannot_escape_the_root(self, monkeypatch, tmp_path, hostile):
        """Server-generated today; the resolver must not depend on that."""
        monkeypatch.setattr(
            "kiln.plugins.monitoring_tools._TIMELAPSES_ROOT", str(tmp_path)
        )
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        with pytest.raises(ValueError):
            _timelapse_dir(hostile)

    def test_watcher_without_disk_save_is_untouched_on_hosted(self, monkeypatch):
        """Monitoring is never gated — only the frames store refuses."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        watcher = _PrintWatcher("w1", MagicMock(), "p", save_to_disk=False)
        assert watcher._save_dir is None

    def test_direct_construction_with_disk_save_refuses_on_hosted(
        self, monkeypatch
    ):
        """The guard holds even for a caller that skips ``watch_print``."""
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with pytest.raises(HostedUnavailableError):
            _PrintWatcher("w1", MagicMock(), "p", save_to_disk=True)


class TestWatchPrintEnvelope:
    """The refusal reaches the caller as a stated reason, not a stack trace."""

    def test_hosted_save_to_disk_comes_back_as_a_stated_reason(self, monkeypatch):
        mcp = _make_watch_print(monkeypatch, _printing_adapter())
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = mcp["watch_print"](save_to_disk=True)
        assert result.get("success") is False
        err = result.get("error") or {}
        assert err.get("code") == "HOSTED_UNAVAILABLE", (
            "the refusal was downgraded to an ordinary failure"
        )
        assert "local Kiln install" in err.get("message", ""), (
            "the refusal must name where saving frames DOES work"
        )
        assert "Unexpected error" not in err.get("message", "")

    def test_local_save_to_disk_still_starts_and_saves_under_the_root(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "kiln.plugins.monitoring_tools._TIMELAPSES_ROOT", str(tmp_path)
        )
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        mcp = _make_watch_print(monkeypatch, _printing_adapter())
        result = mcp["watch_print"](
            save_to_disk=True, poll_interval=1, timeout=5
        )
        try:
            assert result.get("success") is True
            assert result.get("save_dir", "").startswith(str(tmp_path))
        finally:
            import kiln.server as _srv

            for watcher in list(_srv._watchers.values()):
                watcher.stop()

    def test_hosted_watch_without_disk_save_is_not_refused_by_the_guard(
        self, monkeypatch
    ):
        """Plain monitoring on hosted must not trip the frames-store gate."""
        mcp = _make_watch_print(monkeypatch, _printing_adapter())
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = mcp["watch_print"](save_to_disk=False, poll_interval=1, timeout=5)
        try:
            assert result.get("success") is True
        finally:
            import kiln.server as _srv

            for watcher in list(_srv._watchers.values()):
                watcher.stop()
