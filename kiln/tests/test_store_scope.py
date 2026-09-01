"""Tests for kiln.store_scope — scope disclosure on local-store answers.

Coverage areas:
    - Tier resolution: absent kiln-pro, registered shim, present-but-unresolvable
    - Cloud half: merged, free tier, no cloud half, and every degraded state
    - Degraded reads are LOUD: incomplete + a top-level warning, never a
      clean success that reads as a complete library
    - Data is preserved: existing response keys are untouched
    - Wiring: every tool that reads a scoped store and answers with a
      count actually calls the shared helper
"""

from __future__ import annotations

import ast
import pathlib
import sys
import types
from typing import Any

import pytest

from kiln.store_scope import (
    DECORATION_LIBRARY,
    DESIGN_CACHE,
    DESIGN_VERSIONS,
    DESIGN_VERSIONS,
    MODEL_CACHE,
    CloudRead,
    current_tier,
    is_paid_tier,
    read_cloud_half,
    scoped_store_response,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _no_kiln_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process look like a free install (no kiln-pro at all)."""
    monkeypatch.setitem(sys.modules, "kiln.licensing", None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", None)


def _licensing(monkeypatch: pytest.MonkeyPatch, tier: str | None) -> None:
    """Register a fake ``kiln.licensing`` shim answering *tier*.

    ``tier=None`` registers a shim whose resolver raises — the
    "kiln-pro is here but the tier will not resolve" state.
    """
    mod = types.ModuleType("kiln.licensing")

    def get_tier() -> str:
        if tier is None:
            raise RuntimeError("licence server unreachable")
        return tier

    mod.get_tier = get_tier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiln.licensing", mod)
    monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))


def _bridge(monkeypatch: pytest.MonkeyPatch, reader: Any) -> None:
    """Install a fake ``kiln_pro.bridge.pro_features``.

    *reader* is attached as ``list_cloud_store``; pass ``None`` for a
    bridge that does not expose the seam at all.
    """
    pkg = types.ModuleType("kiln_pro")
    bridge = types.ModuleType("kiln_pro.bridge")

    class _Features:
        pass

    features = _Features()
    if reader is not None:
        features.list_cloud_store = reader  # type: ignore[attr-defined]
    bridge.pro_features = features  # type: ignore[attr-defined]
    pkg.bridge = bridge  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiln_pro", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)


def _paid(monkeypatch: pytest.MonkeyPatch, reader: Any = None) -> None:
    """Paid tier with a bridge whose seam is *reader* (None = absent)."""
    _licensing(monkeypatch, "pro")
    _bridge(monkeypatch, reader)


# ---------------------------------------------------------------------------
# TestCurrentTier
# ---------------------------------------------------------------------------


class TestCurrentTier:
    def test_no_kiln_pro_is_free(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        assert current_tier() == "free"

    def test_reads_licensing_shim(self, monkeypatch):
        _licensing(monkeypatch, "business")
        assert current_tier() == "business"

    def test_enum_tier_unwrapped(self, monkeypatch):
        class _Tier:
            value = "pro"

        mod = types.ModuleType("kiln.licensing")
        mod.get_tier = lambda: _Tier()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kiln.licensing", mod)
        assert current_tier() == "pro"

    def test_kiln_pro_present_but_tier_unresolvable_is_unknown(self, monkeypatch):
        _licensing(monkeypatch, None)
        assert current_tier() == "unknown"

    def test_unknown_counts_as_possibly_paid(self):
        # Not a paid grant — it only means the answer must disclose that a
        # cloud half may be missing rather than claim local is everything.
        assert is_paid_tier("unknown") is True
        assert is_paid_tier("free") is False
        assert is_paid_tier("") is False
        assert is_paid_tier("pro") is True


# ---------------------------------------------------------------------------
# TestReadCloudHalf
# ---------------------------------------------------------------------------


class TestReadCloudHalf:
    @pytest.mark.parametrize(
        "store", [MODEL_CACHE, DECORATION_LIBRARY, DESIGN_CACHE]
    )
    def test_store_without_cloud_half(self, monkeypatch, store):
        # DECORATION_LIBRARY and DESIGN_CACHE claim no cloud half on
        # purpose: kiln-pro keeps no cloud copy of either — the web's
        # /decorations pages are the decoration PRESET store, a different
        # artifact family — so a paid caller is missing nothing and a
        # seam read here would only invent a library that does not exist.
        _paid(monkeypatch, reader=lambda _c, **_kw: {"status": "ok", "items": [{"a": 1}]})
        result = read_cloud_half(store)
        assert result.status == "no_cloud_half"
        assert result.complete is True

    def test_free_tier_never_reads_cloud(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        result = read_cloud_half(DESIGN_VERSIONS)
        assert result.status == "tier_local_only"
        assert result.complete is True

    def test_paid_without_seam_is_unavailable(self, monkeypatch):
        _paid(monkeypatch, reader=None)
        result = read_cloud_half(DESIGN_VERSIONS)
        assert result.status == "unavailable"
        assert result.complete is False
        assert "list_cloud_store" in result.detail

    def test_seam_raising_is_an_error_not_an_empty_library(self, monkeypatch):
        def _boom(_capability, **_kw):
            raise TimeoutError("cloud unreachable")

        _paid(monkeypatch, reader=_boom)
        result = read_cloud_half(DESIGN_VERSIONS)
        assert result.status == "error"
        assert result.complete is False
        assert result.items == []

    def test_unauthenticated_is_reported(self, monkeypatch):
        _paid(monkeypatch, reader=lambda _c, **_kw: {"status": "unauthenticated"})
        result = read_cloud_half(DESIGN_VERSIONS)
        assert result.status == "unauthenticated"
        assert result.complete is False

    def test_unparseable_response_is_an_error(self, monkeypatch):
        _paid(monkeypatch, reader=lambda _c, **_kw: "sorry")
        assert read_cloud_half(DESIGN_VERSIONS).status == "error"

    def test_ok_without_a_list_is_an_error(self, monkeypatch):
        _paid(monkeypatch, reader=lambda _c, **_kw: {"status": "ok"})
        assert read_cloud_half(DESIGN_VERSIONS).status == "error"

    def test_unknown_status_is_an_error(self, monkeypatch):
        _paid(monkeypatch, reader=lambda _c, **_kw: {"status": "partially-ish"})
        assert read_cloud_half(DESIGN_VERSIONS).status == "error"

    def test_ok_returns_items(self, monkeypatch):
        _paid(
            monkeypatch,
            reader=lambda _c, **_kw: {"status": "ok", "items": [{"name": "Kiln Logo"}]},
        )
        result = read_cloud_half(DESIGN_VERSIONS)
        assert result.status == "ok"
        assert result.complete is True
        assert result.items == [{"name": "Kiln Logo"}]

    def test_capability_key_is_passed_through(self, monkeypatch):
        seen: list[str] = []

        def _reader(capability, **_kw):
            seen.append(capability)
            return {"status": "ok", "items": []}

        _paid(monkeypatch, reader=_reader)
        read_cloud_half(DESIGN_VERSIONS)
        assert seen == [DESIGN_VERSIONS.cloud_capability]

    def test_filters_reach_the_seam(self, monkeypatch):
        seen: dict[str, Any] = {}

        def _reader(_capability, filters=None):
            seen.update(filters or {})
            return {"status": "ok", "items": []}

        _paid(monkeypatch, reader=_reader)
        read_cloud_half(DESIGN_VERSIONS, filters={"design_id": "coaster"})
        assert seen == {"design_id": "coaster"}

    def test_seam_with_the_wrong_signature_is_loud(self, monkeypatch):
        # A seam that cannot take the filters is a seam that cannot answer
        # the same question — reported, not quietly treated as empty.
        _paid(monkeypatch, reader=lambda _capability: {"status": "ok", "items": []})
        result = read_cloud_half(DESIGN_VERSIONS)
        assert result.status == "error"
        assert result.complete is False


# ---------------------------------------------------------------------------
# TestScopedStoreResponse
# ---------------------------------------------------------------------------


class TestScopedStoreResponse:
    @staticmethod
    def _call(**kwargs):
        response = {
            "success": True,
            "count": 1,
            "decorations": [{"name": "Test Coaster"}],
        }
        return scoped_store_response(
            response, store=DESIGN_VERSIONS, items_key="decorations", **kwargs
        )

    def test_free_tier_is_complete_and_names_the_store(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        result = self._call()
        assert result["scope"]["complete"] is True
        assert result["scope"]["stores_read"] == ["local"]
        assert "incomplete" not in result
        assert "warning" not in result
        assert "local design version history" in result["scope"]["summary"]

    def test_free_tier_summary_names_the_local_location(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        assert "~/.kiln/designs/" in self._call()["scope"]["summary"]

    def test_paid_without_cloud_read_is_loud(self, monkeypatch):
        # THE regression: a paid caller whose cloud library cannot be read
        # must not receive a clean success that reads as a whole library.
        _paid(monkeypatch, reader=None)
        result = self._call()
        assert result["incomplete"] is True
        assert result["scope"]["complete"] is False
        assert result["scope"]["stores_missing"] == ["cloud"]
        assert result["warning"].startswith("INCOMPLETE")
        assert "not your whole library" in result["warning"]

    def test_paid_cloud_error_is_loud(self, monkeypatch):
        def _boom(_capability, **_kw):
            raise ConnectionError("no route to host")

        _paid(monkeypatch, reader=_boom)
        result = self._call()
        assert result["incomplete"] is True
        assert result["scope"]["cloud"]["status"] == "error"

    def test_union_merges_and_tags_both_sides(self, monkeypatch):
        _paid(
            monkeypatch,
            reader=lambda _c, **_kw: {"status": "ok", "items": [{"name": "Kiln Logo"}]},
        )
        result = self._call()
        assert result["count"] == 2
        assert [d["store"] for d in result["decorations"]] == ["local", "cloud"]
        assert result["scope"]["complete"] is True
        assert result["scope"]["stores_read"] == ["local", "cloud"]
        assert "incomplete" not in result

    def test_data_is_not_rewritten(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        result = self._call()
        assert result["success"] is True
        assert result["count"] == 1
        assert result["decorations"][0]["name"] == "Test Coaster"

    def test_existing_source_marking_is_respected(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        response = {"count": 1, "rows": [{"name": "x", "store": "elsewhere"}]}
        result = scoped_store_response(
            response, store=DESIGN_VERSIONS, items_key="rows"
        )
        assert result["rows"][0]["store"] == "elsewhere"

    def test_non_dict_items_survive(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        response = {"count": 2, "names": ["a", "b"]}
        result = scoped_store_response(
            response, store=DESIGN_VERSIONS, items_key="names"
        )
        assert result["names"] == ["a", "b"]

    def test_missing_count_key_is_not_invented(self, monkeypatch):
        _no_kiln_pro(monkeypatch)
        response = {"rows": []}
        result = scoped_store_response(
            response, store=DESIGN_VERSIONS, items_key="rows"
        )
        assert "count" not in result

    def test_empty_local_store_still_discloses(self, monkeypatch):
        # "Zero found" is the shape most easily mistaken for "you have none".
        _paid(monkeypatch, reader=None)
        response = {"success": True, "count": 0, "decorations": []}
        result = scoped_store_response(
            response, store=DESIGN_VERSIONS, items_key="decorations"
        )
        assert result["count"] == 0
        assert result["incomplete"] is True

    def test_helper_failure_fails_loud_not_silent(self, monkeypatch):
        monkeypatch.setattr(
            "kiln.store_scope.read_cloud_half",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = self._call()
        assert result["incomplete"] is True
        assert "possibly incomplete" in result["warning"]

    def test_non_dict_response_passes_through(self):
        assert (
            scoped_store_response(
                "nope", store=DESIGN_VERSIONS, items_key="x"
            )
            == "nope"
        )


# ---------------------------------------------------------------------------
# TestCloudReadDataclass
# ---------------------------------------------------------------------------


class TestCloudReadDataclass:
    def test_ok_reports_item_count(self):
        assert CloudRead("ok", items=[1, 2]).to_dict()["items_returned"] == 2

    def test_detail_omitted_when_empty(self):
        assert "detail" not in CloudRead("tier_local_only").to_dict()


# ---------------------------------------------------------------------------
# Tool-level regression pins
#
# These assert on the tool RESPONSE only — no import of store_scope — so
# they run unchanged against the pre-fix code, where they fail on the
# missing disclosure rather than on an import.
# ---------------------------------------------------------------------------


class _MockMcp:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def __getitem__(self, name: str):
        return self.tools[name]


class TestListDecorationsDeclaresItsScope:
    """list_decorations must never answer as if local were everything."""

    @staticmethod
    def _tool(tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_DECORATIONS_DIR", str(tmp_path / "decorations"))
        from kiln.plugins.decoration_library_tools import plugin

        mcp = _MockMcp()
        plugin.register(mcp)
        return mcp["list_decorations"]

    def test_free_install_says_local_only(self, tmp_path, monkeypatch):
        _no_kiln_pro(monkeypatch)
        result = self._tool(tmp_path, monkeypatch)()
        assert result["success"] is True
        assert result["scope"]["stores_read"] == ["local"]
        assert "local decoration library" in result["scope"]["summary"]

    def test_paid_install_is_complete_without_a_cloud_read(
        self, tmp_path, monkeypatch
    ):
        """Inverted 2026-09-01 — this used to assert the OPPOSITE.

        The original test called a paid install without a cloud read
        incomplete, on the premise that the web's /decorations pages
        are this library's cloud half.  They are the decoration PRESET
        store — a different artifact family with different rows and its
        own doors — and kiln-pro keeps no cloud copy of the library
        itself.  So a paid caller's local library IS the whole library,
        and the loud-incomplete warning the old premise demanded would
        have fired on every paid listing forever, about rows that do
        not exist.  Inverted rather than deleted so the decision is on
        the record; if a cloud library sync ever ships, flip the
        constant's capability and this test with it.
        """
        _paid(monkeypatch, reader=None)
        result = self._tool(tmp_path, monkeypatch)()
        assert "incomplete" not in result
        assert result["scope"]["complete"] is True
        assert result["scope"]["store"]["has_cloud_half"] is False

    def test_the_seam_is_never_consulted_for_the_library(
        self, tmp_path, monkeypatch
    ):
        calls: list = []

        def _reader(*a, **kw):
            calls.append(a)
            return {"status": "ok", "items": [{"name": "Kiln Logo"}]}

        _paid(monkeypatch, reader=_reader)
        result = self._tool(tmp_path, monkeypatch)()
        assert calls == []
        assert result["count"] == 0
        assert result["scope"]["complete"] is True


class TestDesignVersionToolsDeclareTheirScope:
    @staticmethod
    def _mcp(monkeypatch, tmp_path):
        monkeypatch.setattr(
            "kiln.plugins.version_tools._DESIGNS_ROOT", str(tmp_path)
        )
        from kiln.plugins.version_tools import _VersionToolsPlugin

        mcp = _MockMcp()
        _VersionToolsPlugin().register(mcp)
        return mcp

    def test_list_design_versions_discloses(self, monkeypatch, tmp_path):
        _no_kiln_pro(monkeypatch)
        result = self._mcp(monkeypatch, tmp_path)["list_design_versions"](
            design_id="ghost"
        )
        assert result["ok"] is True
        assert result["count"] == 0
        assert result["scope"]["stores_read"] == ["local"]

    def test_search_miss_on_empty_root_still_discloses(self, monkeypatch, tmp_path):
        # The most dangerous shape in the file: a clean success and a zero
        # returned before any store was even walked.
        _paid(monkeypatch, reader=None)
        missing_root = tmp_path / "not-created"
        result = self._mcp(monkeypatch, missing_root)["search_design_versions"](
            query="kiln logo"
        )
        assert result["count"] == 0
        assert result["incomplete"] is True

    def test_search_hit_is_scoped(self, monkeypatch, tmp_path):
        _no_kiln_pro(monkeypatch)
        mcp = self._mcp(monkeypatch, tmp_path)
        mcp["save_design_version"](
            design_id="coaster", scad_source="cube([10,10,3]);", notes="kiln logo"
        )
        result = mcp["search_design_versions"](query="kiln logo")
        assert result["count"] == 1
        assert result["versions"][0]["store"] == "local"
        assert result["scope"]["complete"] is True


class TestCacheToolsDeclareTheirScope:
    @staticmethod
    def _mcp():
        from kiln.plugins.cache_tools import _CacheToolsPlugin

        mcp = _MockMcp()
        _CacheToolsPlugin().register(mcp)
        return mcp

    def test_model_cache_has_no_cloud_half_and_says_so(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KILN_MODEL_CACHE_DIR", str(tmp_path / "models"))
        _paid(monkeypatch, reader=None)
        result = self._mcp()["list_cached_models"]()
        assert result["scope"]["store"]["has_cloud_half"] is False
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result

    def test_design_cache_has_no_cloud_half_and_names_its_real_path(
        self, monkeypatch, tmp_path
    ):
        # The location once advertised ~/.kiln/design_cache/, a directory
        # nothing writes; the store lives at ~/.kiln/cache/designs/.
        monkeypatch.setenv("KILN_CACHE_DIR", str(tmp_path / "cache"))
        _paid(monkeypatch, reader=None)
        result = self._mcp()["list_cached_designs"]()
        assert result["scope"]["store"]["has_cloud_half"] is False
        assert result["scope"]["complete"] is True
        assert "incomplete" not in result
        assert result["scope"]["store"]["location"] == "~/.kiln/cache/designs/"


# ---------------------------------------------------------------------------
# Wiring pin — a shared helper nobody calls is the same bug with extra steps
# ---------------------------------------------------------------------------

#: Modules whose contents are a user-owned local store with a scope to
#: declare.  Derived FROM the source below rather than paired with a
#: hand-written list of tool names, so a new door onto one of these
#: stores fails this test until it is wired.
_SCOPED_STORE_MODULES = {
    "kiln.decoration_library",
    "kiln.model_cache",
    "kiln.design_cache",
    "kiln.design_recipe",
}


def _is_mcp_tool(fn: ast.AST) -> bool:
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _counting_store_readers() -> list[tuple[str, str, bool]]:
    """Find every MCP tool that reads a scoped store and answers a count."""
    plugins = pathlib.Path(__file__).resolve().parents[1] / "src/kiln/plugins"
    found: list[tuple[str, str, bool]] = []
    for path in sorted(plugins.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_mcp_tool(node):
                continue
            imported = {
                n.module
                for n in ast.walk(node)
                if isinstance(n, ast.ImportFrom) and n.module
            }
            if not imported & _SCOPED_STORE_MODULES:
                continue
            answers_a_count = any(
                isinstance(d, ast.Dict)
                and any(
                    isinstance(k, ast.Constant) and k.value == "count"
                    for k in d.keys
                )
                for d in ast.walk(node)
            )
            if not answers_a_count:
                continue
            calls = {
                c.func.id
                for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            found.append((path.name, node.name, "scoped_store_response" in calls))
    return found


class TestEveryCountingStoreReaderIsWired:
    def test_the_scan_finds_the_known_doors(self):
        names = {name for _f, name, _w in _counting_store_readers()}
        # Guards the scan itself: a filter that silently matches nothing
        # would let the assertion below pass forever.
        assert "list_decorations" in names
        assert len(names) >= 6

    def test_every_door_calls_the_shared_helper(self):
        unwired = [
            f"{fname}::{tool}" for fname, tool, wired in _counting_store_readers()
            if not wired
        ]
        assert unwired == [], (
            "these tools read a local store and answer with a count but do "
            f"not declare their scope: {unwired}"
        )
