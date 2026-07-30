"""The refusal a free user actually hits must be visible — to them and to us.

Four defects, one theme: data that exists and is dropped at the last inch.

1. The pro-tool manifest carries a ``tier`` per tool.  The stub registrar
   read it and discarded it, so 345 paid capabilities presented to an
   agent looking exactly like the 54 free ones.
2. A free user calling a pro tool got "pair a Kiln account" — a setup
   chore naming nothing they would want — at the highest-intent moment
   the product ever gets from them.
3. That refusal returns locally and never reaches a server, so nothing
   counted it.  221 installs over seven months produced ONE recorded
   paywall hit, because we were counting the wrong wall.
4. ``_recording_suppressed`` delegated to a CI-only check that misses a
   local pytest run, so developer test runs wrote real telemetry — still
   happening on 2026-07-29, on 1.3.0.  And ``record_tier_denial``
   validated nothing while the identically-shaped ``tool_calls`` map
   validated everything, which is how ``%s%s%s`` and ``/tmp/pp-fuzz``
   reached the production heartbeat table.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiln import daily_stats


@pytest.fixture
def stats_file(tmp_path, monkeypatch):
    """Point the recorder at a temp file (never suppressed — see the guard)."""
    path = tmp_path / "daily_stats.json"
    monkeypatch.setattr(daily_stats, "_STATS_PATH", path)
    return path


class TestCounterHygiene:
    """One recorder, one validation, one cap — for all three maps."""

    @pytest.mark.parametrize(
        "junk", ["%s%s%s", "/tmp/pp-fuzz", "", "   ", "ab", "9lives", "Upper"]
    )
    def test_junk_keys_never_reach_any_map(self, stats_file, junk):
        daily_stats.record_tier_denial(junk)
        daily_stats.record_account_wall(junk)
        daily_stats.record_tool_call(junk)
        # A rejected key is dropped before any write, so the file may not
        # exist at all — which is the strongest form of "not polluted".
        if not stats_file.exists():
            return
        data = json.loads(stats_file.read_text())
        for bucket in ("tier_denials", "account_wall", "tool_calls"):
            assert data.get(bucket, {}) == {}, f"{junk!r} polluted {bucket}"

    def test_real_names_are_counted(self, stats_file):
        daily_stats.record_tier_denial("fleet_status")
        daily_stats.record_tier_denial("fleet_status")
        daily_stats.record_account_wall("apply_image_texture")
        data = json.loads(stats_file.read_text())
        assert data["tier_denials"] == {"fleet_status": 2}
        assert data["account_wall"] == {"apply_image_texture": 1}

    def test_the_two_walls_stay_separate(self, stats_file):
        """A tier denial and an account wall answer different questions."""
        daily_stats.record_tier_denial("fleet_status")
        daily_stats.record_account_wall("fleet_status")
        data = json.loads(stats_file.read_text())
        assert data["tier_denials"] == {"fleet_status": 1}
        assert data["account_wall"] == {"fleet_status": 1}

    def test_distinct_name_cap_applies_to_every_map(self, stats_file):
        for i in range(daily_stats._TOOL_CALLS_MAX_DISTINCT + 25):
            daily_stats.record_account_wall(f"tool_{i}")
        data = json.loads(stats_file.read_text())
        assert len(data["account_wall"]) == daily_stats._TOOL_CALLS_MAX_DISTINCT


class TestSuppression:
    """A test run must not write a developer's real telemetry."""

    def test_local_pytest_run_is_suppressed(self, monkeypatch):
        """The live hole: pytest sets no CI var, so the CI check said 'fine'."""
        monkeypatch.setattr(
            daily_stats, "_STATS_PATH", daily_stats._DEFAULT_STATS_PATH
        )
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        # No CI variable set, and the CI check agrees there is no CI here.
        with patch("kiln.heartbeat._is_ci_environment", return_value=False):
            assert daily_stats._recording_suppressed() is True, (
                "a local pytest run would write to the real ~/.kiln file"
            )

    def test_real_path_write_is_a_noop_under_pytest(self, monkeypatch):
        """End to end: the guard actually stops the write, not just reports."""
        monkeypatch.setattr(
            daily_stats, "_STATS_PATH", daily_stats._DEFAULT_STATS_PATH
        )
        with patch.object(daily_stats.Path, "write_text") as wrote:
            daily_stats.record_account_wall("apply_image_texture")
        wrote.assert_not_called()

    def test_a_custom_path_is_still_exercised(self, stats_file):
        """Tests that opt in by repointing the path must still record."""
        assert daily_stats._recording_suppressed() is False
        daily_stats.record_tool_call("generate_coaster")
        assert json.loads(stats_file.read_text())["tool_calls"]


class TestRolloverNoLongerLosesTheEvidence:
    """A denial at 15:00 was erased at midnight before the heartbeat saw it."""

    def test_maps_survive_the_day_rollover(self):
        day = daily_stats._empty_day()
        day["date"] = "2026-07-28"
        day["tier_denials"] = {"fleet_status": 3}
        day["account_wall"] = {"apply_image_texture": 7}
        day["tool_calls"] = {"generate_coaster": 2}
        day["prints"] = 4

        fresh = daily_stats._archive_completed_day(day)

        prev = fresh["previous"]
        assert prev["date"] == "2026-07-28"
        assert prev["prints"] == 4, "scalar carry must not regress"
        assert prev["tier_denials"] == {"fleet_status": 3}
        assert prev["account_wall"] == {"apply_image_texture": 7}
        assert prev["tool_calls"] == {"generate_coaster": 2}
        # The new day starts clean.
        assert fresh["tier_denials"] == {}
        assert fresh["account_wall"] == {}

    def test_empty_maps_are_not_carried_as_noise(self):
        day = daily_stats._empty_day()
        day["date"] = "2026-07-28"
        prev = daily_stats._archive_completed_day(day)["previous"]
        assert "account_wall" not in prev

    def test_the_carried_maps_reach_the_heartbeat(self):
        import inspect

        from kiln import heartbeat

        src = inspect.getsource(heartbeat)
        assert '"account_wall"' in src, "recorded but never leaves the machine"
        assert '"previous_day"' in src, "the complete-day block must ship"


class TestTheCeilingIsVisible:
    """345 paid tools must not dress as free ones."""

    def test_stub_docstring_names_the_tier(self):
        from kiln import server

        registered: dict[str, str] = {}

        class _FakeMCP:
            def tool(self):
                def _decorator(fn):
                    registered[fn.__name__] = fn.__doc__ or ""
                    return fn
                return _decorator

        server._register_pro_tool_stubs(_FakeMCP())
        assert registered, "no stubs registered — manifest missing?"

        paid = [n for n, t in server._PRO_TOOL_TIERS.items() if t != "free"]
        assert len(paid) > 100, f"expected the paid surface, got {len(paid)}"

        for name in paid[:40]:
            doc = registered.get(name, "")
            assert "Requires Kiln" in doc, f"{name} does not say it is paid"
            assert "kiln3d.com/pricing" in doc, f"{name} offers no way to buy"

    def test_free_tools_are_not_mislabelled_as_paid(self):
        from kiln import server

        registered: dict[str, str] = {}

        class _FakeMCP:
            def tool(self):
                def _decorator(fn):
                    registered[fn.__name__] = fn.__doc__ or ""
                    return fn
                return _decorator

        server._register_pro_tool_stubs(_FakeMCP())
        for name, doc in registered.items():
            if name not in server._PRO_TOOL_TIERS:
                assert "Requires Kiln" not in doc, f"{name} is free but claims a tier"


class TestTheWallIsAnOffer:
    """The first thing a free user learns about a paid feature."""

    def _call_unpaired(self, tool_name):
        from kiln import server

        with patch("kiln.auth_session.resolve_api_bearer", return_value=None):
            with patch.dict("os.environ", {"KILN_LICENSE_KEY": ""}, clear=False):
                with patch.object(server, "_raw_paired_access_token", return_value=""):
                    return server._pro_api_call(tool_name)

    def test_paid_tool_refusal_names_tier_value_and_next_step(self):
        from kiln import server

        server._PRO_TOOL_TIERS["apply_image_texture"] = "pro"
        out = self._call_unpaired("apply_image_texture")

        assert out["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert out["required_tier"] == "pro"
        assert out["upgrade_url"] == "https://kiln3d.com/pricing"
        assert "Kiln Pro" in out["error"], "must name what they are reaching for"
        assert "kiln signin" in out["error"], "must give exactly one next step"

    def test_free_tool_refusal_says_it_is_free(self):
        from kiln import server

        server._PRO_TOOL_TIERS.pop("queue_summary", None)
        out = self._call_unpaired("queue_summary")

        assert out["required_tier"] == "free"
        assert "free" in out["error"].lower()
        assert "Kiln Pro" not in out["error"], "do not invent a paywall"

    def test_the_wall_is_counted(self, stats_file):
        from kiln import server

        server._PRO_TOOL_TIERS["apply_image_texture"] = "pro"
        self._call_unpaired("apply_image_texture")

        data = json.loads(stats_file.read_text())
        assert data["account_wall"] == {"apply_image_texture": 1}
