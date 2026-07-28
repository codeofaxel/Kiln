"""The community layer is a bonus, never a dependency.

Everyone contributes print outcomes; reading the pool back is what Kiln Pro
adds.  These tests pin the consequence at the two surfaces a user actually
meets it: the ``get_community_insight`` / ``community_stats`` tools, and the
generation context that blends community failure modes into a prompt.

The invariant under test is the same in both places — **when the community
layer is unavailable for ANY reason (no account, offline, or a plan without
it), the local answer still comes back, unchanged and un-degraded.**
"""

from __future__ import annotations

import importlib
from unittest import mock

import pytest


def _capture_tools(plugin_module: str) -> dict:
    plugin = importlib.import_module(plugin_module).plugin
    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(FakeMCP())
    return tools


@pytest.fixture
def tools():
    return _capture_tools("kiln.plugins.intelligence_tools")


_LOCAL_INSIGHT = {
    "geometric_signature": "sig-1",
    "total_prints": 4,
    "success_rate": 0.75,
    "top_printer_models": [{"model": "bambu_a1", "count": 4}],
    "top_materials": [{"material": "PLA", "count": 4}],
    "recommended_settings": {"layer_height": 0.2},
    "common_failures": [{"mode": "stringing", "count": 1, "percentage": 25.0}],
    "average_print_time_seconds": 900,
    "confidence": "low",
}

_COMMUNITY_INSIGHT = {**_LOCAL_INSIGHT, "total_prints": 220, "confidence": "high"}


class _Insight:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return dict(self._payload)


# ---------------------------------------------------------------------------
# get_community_insight
# ---------------------------------------------------------------------------


def test_local_answer_survives_a_refused_community_layer(tools):
    """The upgrade shape is an invitation printed next to a real answer."""
    with mock.patch(
        "kiln.community_registry.get_community_insight",
        return_value=_Insight(_LOCAL_INSIGHT),
    ), mock.patch(
        "kiln.community_sync.fetch_community_insight_for_signature",
        return_value=None,
    ):
        result = tools["get_community_insight"]("sig-1")

    assert result["success"] is True
    assert result["has_data"] is True
    assert result["insight"] == _LOCAL_INSIGHT
    assert "community_insight" not in result
    assert result["community"]["available"] is False
    assert "Kiln Pro" in result["community"]["note"]
    assert result["community"]["learn_more"] == "https://kiln3d.com/pricing"


def test_no_data_anywhere_is_still_a_success(tools):
    with mock.patch(
        "kiln.community_registry.get_community_insight", return_value=None,
    ), mock.patch(
        "kiln.community_sync.fetch_community_insight_for_signature",
        return_value=None,
    ):
        result = tools["get_community_insight"]("sig-unknown")

    assert result["success"] is True
    assert result["has_data"] is False
    assert result["community"]["available"] is False
    assert "message" in result


def test_community_layer_enriches_when_available(tools):
    with mock.patch(
        "kiln.community_registry.get_community_insight",
        return_value=_Insight(_LOCAL_INSIGHT),
    ), mock.patch(
        "kiln.community_sync.fetch_community_insight_for_signature",
        return_value=_COMMUNITY_INSIGHT,
    ):
        result = tools["get_community_insight"]("sig-1")

    assert result["has_data"] is True
    assert result["insight"] == _LOCAL_INSIGHT
    assert result["community_insight"] == _COMMUNITY_INSIGHT
    assert result["community"] == {"available": True, "sample_size": 220}


def test_community_read_blowing_up_never_costs_the_local_answer(tools):
    with mock.patch(
        "kiln.community_registry.get_community_insight",
        return_value=_Insight(_LOCAL_INSIGHT),
    ), mock.patch(
        "kiln.community_sync.fetch_community_insight_for_signature",
        side_effect=RuntimeError("network on fire"),
    ):
        result = tools["get_community_insight"]("sig-1")

    assert result["success"] is True
    assert result["insight"] == _LOCAL_INSIGHT
    assert result["community"]["available"] is False


def test_refusal_carries_no_community_data_fields(tools):
    """A refused community layer must not leak a partial aggregate."""
    with mock.patch(
        "kiln.community_registry.get_community_insight", return_value=None,
    ), mock.patch(
        "kiln.community_sync.fetch_community_insight_for_signature",
        return_value=None,
    ):
        result = tools["get_community_insight"]("sig-1")

    community = result["community"]
    assert set(community) == {"available", "note", "learn_more"}
    for leaky in ("sample_size", "insight", "rows", "failure_breakdown"):
        assert leaky not in community


# ---------------------------------------------------------------------------
# community_stats
# ---------------------------------------------------------------------------


class _Stats:
    def to_dict(self):
        return {
            "total_records": 3,
            "unique_models": 2,
            "unique_printers": 1,
            "unique_materials": 1,
            "overall_success_rate": 1.0,
            "last_updated": 0.0,
        }


def test_stats_local_only_when_corpus_unreachable(tools):
    with mock.patch(
        "kiln.community_registry.get_community_stats", return_value=_Stats(),
    ), mock.patch(
        "kiln.community_sync.fetch_community_corpus_stats", return_value=None,
    ):
        result = tools["community_stats"]()

    assert result["success"] is True
    assert result["stats"]["total_records"] == 3
    assert result["community"]["available"] is False


def test_stats_include_corpus_totals_when_available(tools):
    with mock.patch(
        "kiln.community_registry.get_community_stats", return_value=_Stats(),
    ), mock.patch(
        "kiln.community_sync.fetch_community_corpus_stats",
        return_value={"total_records": 90210, "overall_success_rate": 0.88},
    ):
        result = tools["community_stats"]()

    assert result["community"] == {
        "available": True,
        "total_records": 90210,
        "overall_success_rate": 0.88,
    }


# ---------------------------------------------------------------------------
# generation_feedback — inherits the gate through the same fetch
# ---------------------------------------------------------------------------


class _StubInfo:
    model = "bambu_a1"
    build_volume = {"x": 256, "y": 256, "z": 256}
    nozzle_diameter = 0.4


class _StubAdapter:
    """Just enough printer for the resolver to reach the community branch."""

    def get_printer_info(self):
        return _StubInfo()


@pytest.fixture
def resolver(monkeypatch):
    """The real context resolver, with a printer present and history sparse."""
    import kiln.server as _srv
    from kiln import generation_feedback

    monkeypatch.setattr(_srv, "_get_adapter", lambda *a, **k: _StubAdapter())
    return generation_feedback


def _resolve(gf):
    return gf.resolve_printer_generation_context(material="PLA")


def test_generation_context_builds_without_community(resolver):
    """Free path: the fetch returns None and the context still resolves."""
    with mock.patch(
        "kiln.community_sync.fetch_community_insights", return_value=None,
    ) as fetch:
        ctx = _resolve(resolver)

    assert ctx.printer_model == "bambu_a1"
    assert ctx.material == "PLA"
    assert ctx.build_volume_mm["x"] == 256
    fetch.assert_called_once_with("bambu_a1", "PLA")
    assert not any(
        "warping on large flat bases" in entry
        for entry in (ctx.common_failures or [])
    )


def test_generation_context_blends_community_when_present(resolver):
    """Pro path: the same fetch returns an aggregate and it lands in context."""
    with mock.patch(
        "kiln.community_sync.fetch_community_insights",
        return_value={
            "failure_breakdown": {"warping on large flat bases": 12},
            "sample_size": 40,
            "success_count": 28,
            "source": "community",
            "fetched_at": 0.0,
        },
    ):
        ctx = _resolve(resolver)

    assert any(
        "warping on large flat bases" in entry
        for entry in (ctx.common_failures or [])
    )
