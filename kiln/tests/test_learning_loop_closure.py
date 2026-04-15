"""End-to-end tests for the closed AI learning feedback loop.

Covers the three public-Kiln wires added in the ``close-learning-loop``
branch:

* **Wire A** — ``resolve_printer_generation_context`` blends live print
  outcome history into ``common_failures`` so the next generation prompt
  automatically avoids the printer's actual failure patterns.
* **Wire B** — ``record_print_outcome`` auto-updates decoration
  ``proven_settings`` counters when the job carried a decoration_slug.
* **Wire C** — ``fetch_community_insights`` pulls aggregate community
  failures for (printer_model, material) and the context resolver blends
  them when local data is sparse.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures — isolate each test from the user's real ~/.kiln state.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_kiln_env(tmp_path, monkeypatch):
    """Point Kiln's DB + decoration + community-cache dirs at a temp root."""
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("KILN_DECORATIONS_DIR", str(tmp_path / "decorations"))
    # Route the community cache under the tmp HOME so nothing bleeds into
    # the real ~/.kiln/community_cache/.
    monkeypatch.setenv("HOME", str(tmp_path))

    # Reset the persistence singleton so the new DB path takes effect
    # for this test, then reset again on teardown to avoid leaking a
    # handle to a now-deleted tmp path into later tests.
    import kiln.persistence as _p
    monkeypatch.setattr(_p, "_db", None, raising=False)
    yield tmp_path
    monkeypatch.setattr(_p, "_db", None, raising=False)


# ---------------------------------------------------------------------------
# Wire A — live outcome history feeds common_failures.
# ---------------------------------------------------------------------------


class TestWireA_LocalLoop:
    def test_live_failure_modes_populate_common_failures(self, tmp_kiln_env):
        """After recording warping failures, the context resolver returns
        'warping' in common_failures — so the next generation prompt gets
        the corresponding mitigation without any user action."""
        from kiln.generation_feedback import resolve_printer_generation_context
        from kiln.persistence import get_db

        db = get_db()
        for _ in range(4):
            db.save_print_outcome({
                "job_id": f"job-{time.time_ns()}",
                "printer_name": "test-bambu",
                "file_name": "vase.3mf",
                "material_type": "PLA",
                "outcome": "failed",
                "failure_mode": "warping",
                "agent_id": "test",
                "created_at": time.time(),
            })

        ctx = resolve_printer_generation_context(printer_name="test-bambu")
        assert ctx.common_failures is not None
        assert "warping" in [f.lower() for f in ctx.common_failures]

    def test_live_failures_precede_static_ones(self, tmp_kiln_env):
        """Live data overrides model-level hearsay — the downstream
        mitigation loop only reads the first 3 entries, so order matters."""
        from kiln.generation_feedback import resolve_printer_generation_context
        from kiln.persistence import get_db

        db = get_db()
        # Recent printer instance failed repeatedly with adhesion loss.
        for _ in range(5):
            db.save_print_outcome({
                "job_id": f"adh-{time.time_ns()}",
                "printer_name": "test-bambu",
                "material_type": "PLA",
                "outcome": "failed",
                "failure_mode": "adhesion",
                "agent_id": "test",
                "created_at": time.time(),
            })

        # Fake a static-intel response with a different failure mode.
        fake_intel_dict = {
            "common_failures": [{"symptom": "stringing"}],
            "agent_notes": [],
        }
        with patch(
            "kiln.printer_intelligence.get_printer_intel",
            return_value=MagicMock(),  # just needs to be truthy
        ), patch(
            "kiln.printer_intelligence.intel_to_dict",
            return_value=fake_intel_dict,
        ), patch("kiln.server._registry") as reg, \
             patch("kiln.server._get_adapter") as ga:
            info = MagicMock()
            info.model = "bambu_x1c"
            info.build_volume = {"x": 256, "y": 256, "z": 256}
            info.nozzle_diameter = 0.4
            adapter = MagicMock()
            adapter.get_printer_info.return_value = info
            reg.get.return_value = adapter
            ga.return_value = adapter

            ctx = resolve_printer_generation_context(
                printer_name="test-bambu", material="PLA"
            )

        assert ctx.common_failures is not None
        # Live "adhesion" must come BEFORE static "stringing".
        failures = [f.lower() for f in ctx.common_failures]
        assert "adhesion" in failures
        assert "stringing" in failures
        assert failures.index("adhesion") < failures.index("stringing")

    def test_prompt_gains_warping_mitigation_from_history(self, tmp_kiln_env):
        """End-to-end: an outcome-populated context flows all the way
        through enhance_prompt_with_design_intelligence into the
        generated prompt text.  This is the Jobs-level demo — fail once,
        get better next time, no button pressed."""
        from kiln.generation_feedback import (
            PrinterGenerationContext,
            enhance_prompt_with_design_intelligence,
        )

        ctx = PrinterGenerationContext(
            material="PLA",
            printer_model="bambu_x1c",
            common_failures=["warping"],
        )
        improved = enhance_prompt_with_design_intelligence(
            "a tall slender vase",
            provider="openscad",
            printer_context=ctx,
        )
        # The warping mitigation text must land in the prompt.
        assert "chamfered corners" in improved.improved_prompt.lower() \
            or "chamfered corners" in " ".join(improved.constraints_added).lower()


# ---------------------------------------------------------------------------
# Wire B — decoration_slug on record_print_outcome auto-updates the library.
# ---------------------------------------------------------------------------


class TestWireB_DecorationAutoSuccess:
    def _seed_decoration(self, slug: str = "mountain-logo") -> None:
        """Write a minimal valid decoration manifest to the library."""
        from kiln.decoration_library import (
            Decoration,
            _write_manifest,
            get_library_dir,
        )

        dec_dir = get_library_dir() / slug
        dec_dir.mkdir(parents=True, exist_ok=True)
        dec = Decoration(
            name="Mountain Logo",
            slug=slug,
            content_type="svg",
            created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        _write_manifest(slug, dec.to_dict())

    def test_success_outcome_increments_success_count(self, tmp_kiln_env):
        from kiln.decoration_library import get_decoration
        from kiln.plugins.learning_tools import record_print_outcome

        self._seed_decoration()

        with patch("kiln.server._check_auth", return_value=None):
            result = record_print_outcome(
                job_id="job-abc",
                outcome="success",
                material_type="PLA",
                decoration_slug="mountain-logo",
                decoration_settings={"depth_mm": 0.6, "mode": "deboss"},
            )
        assert result.get("success") is True

        dec = get_decoration("mountain-logo")
        assert dec is not None
        assert "PLA" in dec.proven_settings
        assert dec.proven_settings["PLA"].success_count == 1
        assert dec.proven_settings["PLA"].failure_count == 0

    def test_failed_outcome_increments_failure_count_and_records_mode(
        self, tmp_kiln_env
    ):
        from kiln.decoration_library import get_decoration
        from kiln.plugins.learning_tools import record_print_outcome

        self._seed_decoration()

        with patch("kiln.server._check_auth", return_value=None):
            record_print_outcome(
                job_id="job-fail",
                outcome="failed",
                failure_mode="warping",
                material_type="PETG",
                decoration_slug="mountain-logo",
                decoration_settings={"depth_mm": 0.5, "mode": "emboss"},
            )

        dec = get_decoration("mountain-logo")
        assert dec is not None
        ps = dec.proven_settings.get("PETG")
        assert ps is not None
        assert ps.failure_count == 1
        assert ps.success_count == 0
        assert ps.last_failure_mode == "warping"


# ---------------------------------------------------------------------------
# Wire C — community pull: aggregates failure_breakdown and caches to disk.
# ---------------------------------------------------------------------------


class TestWireC_CommunityPull:
    def test_fetch_aggregates_failure_modes(self, tmp_kiln_env, monkeypatch):
        monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
        import kiln.community_sync as cs

        fake_rows = [
            {"failure_mode": "warping", "outcome": "failed"},
            {"failure_mode": "warping", "outcome": "failed"},
            {"failure_mode": "adhesion", "outcome": "failed"},
            {"failure_mode": None, "outcome": "success"},
        ]

        class _Resp:
            status = 200
            def read(self):
                return json.dumps(fake_rows).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return None

        with patch("urllib.request.urlopen", return_value=_Resp()):
            result = cs.fetch_community_insights("bambu_x1c", "PLA", use_cache=False)

        assert result is not None
        assert result["sample_size"] == 4
        assert result["success_count"] == 1
        # Sorted by count desc — warping (2) before adhesion (1).
        breakdown = list(result["failure_breakdown"].items())
        assert breakdown[0] == ("warping", 2)
        assert breakdown[1] == ("adhesion", 1)

    def test_cache_round_trip_skips_network(self, tmp_kiln_env, monkeypatch):
        """Second fetch within TTL must NOT hit the network."""
        monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
        import kiln.community_sync as cs

        fake_rows = [{"failure_mode": "stringing", "outcome": "failed"}]

        call_count = {"n": 0}

        class _Resp:
            status = 200
            def read(self):
                return json.dumps(fake_rows).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return None

        def _fake_urlopen(*a, **kw):
            call_count["n"] += 1
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            a = cs.fetch_community_insights("bambu_x1c", "PLA")
            b = cs.fetch_community_insights("bambu_x1c", "PLA")

        assert a is not None and b is not None
        assert call_count["n"] == 1  # second call hit cache

    def test_opt_in_off_returns_none(self, monkeypatch):
        monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "false")
        from kiln.community_sync import fetch_community_insights

        assert fetch_community_insights("bambu_x1c", "PLA") is None
