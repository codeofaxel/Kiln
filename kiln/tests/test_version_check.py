"""Tests for :mod:`kiln.version_check` — the PyPI update-availability nudge.

Coverage:
- version comparison (release segments + PEP 440 pre-releases)
- disk cache read/write/TTL/corruption
- opt-out env vars
- ``check_for_update`` / ``update_banner_line`` decision logic
- the background-refresh thread (warms cache, dedups, respects opt-out)
- the agent surfaces: ``get_started`` / ``kiln_health`` ``update`` field and
  the ``_build_instructions`` banner
- the ``kiln upgrade`` CLI command (dry-run path)

The CLI *startup banner* (the isatty-gated stderr echo in the ``cli()``
group callback) is exercised end-to-end under a real pty during release
verification; here we test the banner-string builder it calls.

Network is stubbed off by default (autouse fixture); the one live-PyPI
test is marked ``slow`` and calls the real fetch directly.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from kiln import version_check as vc

# Real fetch captured before the autouse fixture stubs it, for the live test.
_REAL_FETCH = vc._fetch_release_info


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Route ~/.kiln to a temp dir and keep the suite off the network.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("KILN_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("KILN_OFFLINE", raising=False)
    monkeypatch.setattr(vc, "_fetch_release_info", lambda: None)
    vc._refresh_in_flight = False
    yield
    vc._refresh_in_flight = False


def _seed_cache(tmp_path: Path, latest: str, checked_at: float | None = None) -> None:
    d = tmp_path / ".kiln"
    d.mkdir(parents=True, exist_ok=True)
    (d / "update_check.json").write_text(
        json.dumps(
            {"latest": latest, "checked_at": time.time() if checked_at is None else checked_at}
        )
    )


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("1.1.5.2", "1.1.5.1", True),
        ("1.1.6", "1.1.5.2", True),
        ("1.2.0", "1.1.9", True),
        ("2.0.0", "1.9.9", True),
        ("1.1.5.1", "1.1.5.1", False),
        ("1.1.5.0", "1.1.5.1", False),
        ("1.1.4", "1.1.5", False),
        ("1.1.5", "unknown", False),
        ("", "1.1.5", False),
        ("1.1.5", "", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert vc.is_newer(latest, current) is expected


def test_is_newer_prerelease_when_packaging_available():
    pytest.importorskip("packaging")
    assert vc.is_newer("1.2.0", "1.2.0rc1") is True
    assert vc.is_newer("1.2.0rc1", "1.2.0") is False


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.1.5.2", (1, 1, 5, 2)),
        ("1.1.5", (1, 1, 5)),
        ("1.2.0rc1", (1, 2, 0)),
        ("2.0.0.dev3", (2, 0, 0)),
        ("garbage", ()),
        ("", ()),
    ],
)
def test_release_tuple(version, expected):
    assert vc._release_tuple(version) == expected


# ---------------------------------------------------------------------------
# opt-out
# ---------------------------------------------------------------------------


def test_enabled_by_default():
    assert vc.update_check_enabled() is True


@pytest.mark.parametrize("var", ["KILN_NO_UPDATE_CHECK", "KILN_OFFLINE"])
@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
def test_opt_out_disables(monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    assert vc.update_check_enabled() is False


def test_opt_out_blocks_nudge_even_with_newer_cached(monkeypatch, tmp_path):
    _seed_cache(tmp_path, "9.9.9")
    monkeypatch.setenv("KILN_NO_UPDATE_CHECK", "1")
    assert vc.check_for_update(current_version="1.1.5.1") is None


# ---------------------------------------------------------------------------
# disk cache
# ---------------------------------------------------------------------------


def test_write_then_load_roundtrip(tmp_path):
    vc._write_cache("1.2.3")
    cache = vc._load_cache()
    assert cache["latest"] == "1.2.3"
    assert "checked_at" in cache


def test_load_missing_cache_is_none():
    assert vc._load_cache() is None


def test_load_corrupt_cache_is_none(tmp_path):
    d = tmp_path / ".kiln"
    d.mkdir(parents=True)
    (d / "update_check.json").write_text("{ not valid json")
    assert vc._load_cache() is None


def test_staleness():
    assert vc._is_stale({"checked_at": 0}) is True
    assert vc._is_stale({"checked_at": time.time()}) is False
    assert vc._is_stale({}) is True  # missing stamp → treat as stale


# ---------------------------------------------------------------------------
# check_for_update / update_banner_line
# ---------------------------------------------------------------------------


def test_fresh_cache_newer_returns_nudge(tmp_path):
    _seed_cache(tmp_path, "1.1.5.2")
    assert vc.check_for_update(current_version="1.1.5.1") == {
        "available": True,
        "current": "1.1.5.1",
        "latest": "1.1.5.2",
        "command": "pip install --upgrade kiln3d",
        "summary": "Kiln 1.1.5.2 is available (you're on 1.1.5.1).",
        # Enriched to an offer the agent can act on, naming the tool to call.
        "offer": (
            "A newer Kiln (1.1.5.2) is out. Happy to update it for you "
            "whenever you like — just say the word."
        ),
        "action": "upgrade_kiln",
    }


def test_fresh_cache_same_version_returns_none(tmp_path):
    _seed_cache(tmp_path, "1.1.5.1")
    assert vc.check_for_update(current_version="1.1.5.1") is None


def test_fresh_cache_older_returns_none(tmp_path):
    _seed_cache(tmp_path, "1.1.4")
    assert vc.check_for_update(current_version="1.1.5.1") is None


def test_cold_cache_returns_none(tmp_path):
    # No cache yet → nothing to compare; the background thread warms it.
    assert vc.check_for_update(current_version="1.1.5.1") is None


def test_stale_cache_newer_still_nudges(tmp_path):
    # Stale but newer → still nudge now; refresh runs for next time.
    _seed_cache(tmp_path, "1.1.5.2", checked_at=0)
    info = vc.check_for_update(current_version="1.1.5.1")
    assert info is not None
    assert info["latest"] == "1.1.5.2"


def test_unknown_current_returns_none(tmp_path):
    _seed_cache(tmp_path, "1.1.5.2")
    assert vc.check_for_update(current_version="unknown") is None


def test_banner_line_formats(tmp_path):
    _seed_cache(tmp_path, "1.1.5.2")
    assert vc.update_banner_line(current_version="1.1.5.1") == (
        "Kiln 1.1.5.2 is available (you're on 1.1.5.1). "
        "Update: pip install --upgrade kiln3d"
    )


def test_banner_line_none_when_current(tmp_path):
    _seed_cache(tmp_path, "1.1.5.1")
    assert vc.update_banner_line(current_version="1.1.5.1") is None


# ---------------------------------------------------------------------------
# background refresh thread
# ---------------------------------------------------------------------------


def test_refresh_runner_writes_cache_and_clears_flag(monkeypatch):
    monkeypatch.setattr(vc, "_fetch_release_info", lambda: {"latest": "1.2.3", "highlights": []})
    vc._refresh_in_flight = True
    vc._refresh_runner()
    assert vc._load_cache()["latest"] == "1.2.3"
    assert vc._refresh_in_flight is False


def test_refresh_runner_no_write_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(vc, "_fetch_release_info", lambda: None)
    vc._refresh_runner()
    assert vc._load_cache() is None


def test_kick_warms_cache(monkeypatch):
    monkeypatch.setattr(vc, "_fetch_release_info", lambda: {"latest": "1.2.3", "highlights": []})
    vc.kick_background_check()
    for t in threading.enumerate():
        if t.name == "kiln-update-check":
            t.join(timeout=3)
    assert vc._load_cache()["latest"] == "1.2.3"


def test_kick_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("KILN_NO_UPDATE_CHECK", "1")
    fetched = []
    monkeypatch.setattr(vc, "_fetch_release_info", lambda: fetched.append(1) or {"latest": "1.2.3", "highlights": []})
    vc.kick_background_check()
    time.sleep(0.05)
    assert fetched == []


def test_kick_dedups_when_in_flight(monkeypatch):
    vc._refresh_in_flight = True
    created = []

    class _SpyThread:
        def __init__(self, *a, **k):
            created.append(1)

        def start(self):
            pass

    monkeypatch.setattr(vc.threading, "Thread", _SpyThread)
    vc.kick_background_check()
    assert created == []  # in-flight guard prevented a second thread


# ---------------------------------------------------------------------------
# agent surfaces (get_started / kiln_health / instructions banner)
# ---------------------------------------------------------------------------


class TestAgentSurfaces:
    def test_get_started_surfaces_update(self, tmp_path):
        _seed_cache(tmp_path, "9.9.9")
        from kiln.server import get_started

        update = get_started()["update"]
        assert update is not None
        assert update["latest"] == "9.9.9"
        assert update["command"] == "pip install --upgrade kiln3d"

    def test_get_started_no_update_when_current(self, tmp_path):
        _seed_cache(tmp_path, "0.0.1")  # older than anything installed
        from kiln.server import get_started

        assert get_started()["update"] is None

    def test_kiln_health_surfaces_update(self, tmp_path):
        _seed_cache(tmp_path, "9.9.9")
        from kiln.server import kiln_health

        update = kiln_health()["update"]
        assert update is not None
        assert update["latest"] == "9.9.9"

    def test_build_instructions_includes_banner(self, tmp_path):
        _seed_cache(tmp_path, "9.9.9")
        from kiln.server import _build_instructions

        text = _build_instructions()
        assert "UPDATE AVAILABLE" in text
        assert "9.9.9" in text

    def test_build_instructions_no_banner_when_current(self, tmp_path):
        _seed_cache(tmp_path, "0.0.1")
        from kiln.server import _build_instructions

        assert "UPDATE AVAILABLE" not in _build_instructions()


# ---------------------------------------------------------------------------
# kiln upgrade command
# ---------------------------------------------------------------------------


class TestSelfUpdateCommand:
    def test_dry_run_prints_command_without_running(self, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr(vc, "_fetch_latest_from_pypi", lambda: "1.2.3")
        result = CliRunner().invoke(cli, ["self-update", "--dry-run"])
        assert result.exit_code == 0
        assert "Would run" in result.output
        assert "pip install --upgrade kiln3d" in result.output

    def test_cancel_aborts(self, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr(vc, "_fetch_latest_from_pypi", lambda: "1.2.3")
        # Answer "n" to the confirmation prompt.
        result = CliRunner().invoke(cli, ["self-update"], input="n\n")
        assert "Cancelled." in result.output


# ---------------------------------------------------------------------------
# live PyPI (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_live_pypi_fetch_returns_a_version():
    info = _REAL_FETCH()
    assert info is not None
    assert vc._release_tuple(info["latest"])  # parses to a non-empty release tuple
    assert isinstance(info["highlights"], list)


# ---------------------------------------------------------------------------
# upgrade highlights (parsed from the PyPI description, sold by the nudge)
# ---------------------------------------------------------------------------

_DESC = (
    "# Kiln\n\n"
    "<!-- kiln-highlights: 1.2.3\n"
    "* designs export as real CAD files\n"
    "* watch prints from the web\n"
    "kiln-highlights:end -->\n\n"
    "Body text.\n"
)


class TestParseHighlights:
    def test_parses_matching_version(self):
        assert vc._parse_highlights(_DESC, "1.2.3") == [
            "designs export as real CAD files",
            "watch prints from the web",
        ]

    def test_other_version_block_is_ignored(self):
        assert vc._parse_highlights(_DESC, "1.2.4") == []

    def test_absent_block_and_non_string_description(self):
        assert vc._parse_highlights("no block here", "1.2.3") == []
        assert vc._parse_highlights(None, "1.2.3") == []

    def test_caps_item_count_and_length(self):
        desc = (
            "<!-- kiln-highlights: 1.2.3\n"
            "* one\n* two\n* three\n* four\n"
            f"* {'x' * 500}\n"
            "kiln-highlights:end -->"
        )
        items = vc._parse_highlights(desc, "1.2.3")
        assert items == ["one", "two", "three"]

    def test_non_bullet_lines_are_ignored(self):
        desc = (
            "<!-- kiln-highlights: 1.2.3\n"
            "not a bullet\n* real item\n\n"
            "kiln-highlights:end -->"
        )
        assert vc._parse_highlights(desc, "1.2.3") == ["real item"]


class TestHighlightsThroughTheCache:
    def _seed(self, tmp_path, highlights):
        d = tmp_path / ".kiln"
        d.mkdir(parents=True, exist_ok=True)
        (d / "update_check.json").write_text(
            json.dumps(
                {"latest": "9.9.9", "checked_at": time.time(), "highlights": highlights}
            )
        )

    def test_refresh_writes_highlights_and_nudge_carries_them(self, monkeypatch):
        monkeypatch.setattr(
            vc,
            "_fetch_release_info",
            lambda: {"latest": "9.9.9", "highlights": ["a gain", "another gain"]},
        )
        vc._refresh_runner()
        info = vc.check_for_update(current_version="1.0.0")
        assert info["highlights"] == ["a gain", "another gain"]

    def test_no_highlights_key_when_release_published_none(self, tmp_path):
        self._seed(tmp_path, [])
        info = vc.check_for_update(current_version="1.0.0")
        assert info is not None
        assert "highlights" not in info

    def test_legacy_cache_without_highlights_still_nudges(self, tmp_path):
        _seed_cache(tmp_path, "9.9.9")  # pre-highlights cache shape
        info = vc.check_for_update(current_version="1.0.0")
        assert info is not None
        assert "highlights" not in info

    def test_corrupt_highlights_are_dropped_not_fatal(self, tmp_path):
        self._seed(tmp_path, [7, "", "  ", {"x": 1}, "kept"])
        info = vc.check_for_update(current_version="1.0.0")
        assert info["highlights"] == ["kept"]


class TestReadmeHighlightsBlock:
    """The README block is what every below-latest install will read via PyPI.

    Pins: the block exists for the CURRENT package version (so a release
    that forgets to refresh it goes red at the bump), parses with the real
    parser, and holds the copy rules for a prompt every tier sees.
    """

    def _readme_and_version(self):
        # Read the version the way test_version.py does rather than with
        # tomllib: that module arrived in 3.11 and kiln supports 3.10, so
        # importing it here took the whole 3.10 CI job down while every
        # other interpreter stayed green.
        root = Path(__file__).resolve().parents[1]
        match = re.search(
            r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$',
            (root / "pyproject.toml").read_text(encoding="utf-8"),
        )
        assert match, "could not find version in kiln/pyproject.toml"
        return (root / "README.md").read_text(encoding="utf-8"), match.group(1)

    def test_block_exists_for_current_version_and_parses(self):
        readme, version = self._readme_and_version()
        items = vc._parse_highlights(readme, version)
        assert 2 <= len(items) <= 3, (
            f"README carries no parseable kiln-highlights block for {version}; "
            "refresh it at release (wording signed off with the changelog)."
        )

    def test_block_copy_rules(self):
        readme, version = self._readme_and_version()
        for item in vc._parse_highlights(readme, version):
            assert "—" not in item and "–" not in item, item  # no em/en dashes
            assert "desktop" not in item.lower(), item
            assert len(item) <= 160, item
