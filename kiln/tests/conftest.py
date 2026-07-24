"""Shared fixtures for the Kiln test suite.

Provides reusable mock data for OctoPrint API responses, pre-configured
adapter instances, and environment variable helpers used across multiple
test modules.

NOTE: The installed ``mcp`` library's ``FastMCP`` does not accept the
``description`` keyword argument used in ``kiln.server``.  We monkey-patch
``FastMCP.__init__`` at import time so that the server module can be
loaded by the test suite without modification.
"""

from __future__ import annotations

import functools
import sys

# ---------------------------------------------------------------------------
# Monkey-patch FastMCP to accept unknown kwargs (like ``description``)
# so that ``import kiln.server`` succeeds at collection time.
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

_original_fastmcp_init = FastMCP.__init__


@functools.wraps(_original_fastmcp_init)
def _patched_fastmcp_init(self, *args, **kwargs):
    # Strip out any kwargs the current FastMCP does not understand.
    import inspect
    sig = inspect.signature(_original_fastmcp_init)
    valid_params = set(sig.parameters.keys())
    filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_params}
    return _original_fastmcp_init(self, *args, **filtered_kwargs)


FastMCP.__init__ = _patched_fastmcp_init  # type: ignore[method-assign]

# ---------------------------------------------------------------------------
# Now safe to import everything else.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from kiln.printers.base import (  # noqa: E402
    JobProgress,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
)
from kiln.printers.octoprint import OctoPrintAdapter  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OCTOPRINT_HOST = "http://octopi.local"
OCTOPRINT_API_KEY = "TESTAPIKEY123"


# ---------------------------------------------------------------------------
# OctoPrint API response payloads
# ---------------------------------------------------------------------------

@pytest.fixture()
def printer_state_idle():
    """OctoPrint /api/printer response when idle and operational."""
    return {
        "temperature": {
            "tool0": {"actual": 24.5, "target": 0.0},
            "bed": {"actual": 23.1, "target": 0.0},
        },
        "state": {
            "text": "Operational",
            "flags": {
                "operational": True,
                "paused": False,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "error": False,
                "ready": True,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def printer_state_printing():
    """OctoPrint /api/printer response when actively printing."""
    return {
        "temperature": {
            "tool0": {"actual": 205.0, "target": 210.0},
            "bed": {"actual": 59.8, "target": 60.0},
        },
        "state": {
            "text": "Printing",
            "flags": {
                "operational": True,
                "paused": False,
                "printing": True,
                "cancelling": False,
                "pausing": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def printer_state_paused():
    """OctoPrint /api/printer response when paused."""
    return {
        "temperature": {
            "tool0": {"actual": 200.0, "target": 210.0},
            "bed": {"actual": 58.0, "target": 60.0},
        },
        "state": {
            "text": "Paused",
            "flags": {
                "operational": True,
                "paused": True,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def printer_state_error():
    """OctoPrint /api/printer response when in error state."""
    return {
        "temperature": {
            "tool0": {"actual": 0.0, "target": 0.0},
            "bed": {"actual": 0.0, "target": 0.0},
        },
        "state": {
            "text": "Error",
            "flags": {
                "operational": False,
                "paused": False,
                "printing": False,
                "cancelling": False,
                "pausing": False,
                "error": True,
                "ready": False,
                "closedOrError": True,
            },
        },
    }


@pytest.fixture()
def printer_state_cancelling():
    """OctoPrint /api/printer response when cancelling a job."""
    return {
        "temperature": {
            "tool0": {"actual": 195.0, "target": 0.0},
            "bed": {"actual": 55.0, "target": 0.0},
        },
        "state": {
            "text": "Cancelling",
            "flags": {
                "operational": True,
                "paused": False,
                "printing": False,
                "cancelling": True,
                "pausing": False,
                "error": False,
                "ready": False,
                "closedOrError": False,
            },
        },
    }


@pytest.fixture()
def job_response_printing():
    """OctoPrint /api/job response for an active print job."""
    return {
        "job": {
            "file": {
                "name": "benchy.gcode",
                "origin": "local",
                "size": 1234567,
            },
            "estimatedPrintTime": 3600,
        },
        "progress": {
            "completion": 45.6789,
            "printTime": 1620,
            "printTimeLeft": 1980,
        },
        "state": "Printing",
    }


@pytest.fixture()
def job_response_idle():
    """OctoPrint /api/job response when no active job."""
    return {
        "job": {
            "file": {"name": None, "origin": None, "size": None},
        },
        "progress": {
            "completion": None,
            "printTime": None,
            "printTimeLeft": None,
        },
        "state": "Operational",
    }


@pytest.fixture()
def files_response_flat():
    """OctoPrint /api/files/local response with flat file list."""
    return {
        "files": [
            {
                "name": "benchy.gcode",
                "path": "benchy.gcode",
                "type": "machinecode",
                "size": 1234567,
                "date": 1700000000,
            },
            {
                "name": "cube.gcode",
                "path": "cube.gcode",
                "type": "machinecode",
                "size": 456789,
                "date": 1700001000,
            },
        ],
    }


@pytest.fixture()
def files_response_nested():
    """OctoPrint /api/files/local response with nested folders."""
    return {
        "files": [
            {
                "name": "benchy.gcode",
                "path": "benchy.gcode",
                "type": "machinecode",
                "size": 1234567,
                "date": 1700000000,
            },
            {
                "name": "calibration",
                "type": "folder",
                "children": [
                    {
                        "name": "first_layer.gcode",
                        "path": "calibration/first_layer.gcode",
                        "type": "machinecode",
                        "size": 99999,
                        "date": 1700002000,
                    },
                    {
                        "name": "subfolder",
                        "type": "folder",
                        "children": [
                            {
                                "name": "deep_file.gcode",
                                "path": "calibration/subfolder/deep_file.gcode",
                                "type": "machinecode",
                                "size": 55555,
                                "date": 1700003000,
                            },
                        ],
                    },
                ],
            },
        ],
    }


@pytest.fixture()
def upload_response_success():
    """OctoPrint /api/files/local upload success response."""
    return {
        "files": {
            "local": {
                "name": "test_print.gcode",
                "origin": "local",
            },
        },
        "done": True,
    }


# ---------------------------------------------------------------------------
# Pre-configured adapter
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    """Return an OctoPrintAdapter configured for testing (retries=1, timeout=5)."""
    return OctoPrintAdapter(
        host=OCTOPRINT_HOST,
        api_key=OCTOPRINT_API_KEY,
        timeout=5,
        retries=1,
    )


@pytest.fixture()
def adapter_with_retries():
    """Return an OctoPrintAdapter configured with 3 retries for retry tests."""
    return OctoPrintAdapter(
        host=OCTOPRINT_HOST,
        api_key=OCTOPRINT_API_KEY,
        timeout=5,
        retries=3,
    )


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env_configured(monkeypatch):
    """Set the environment variables required by the server module."""
    monkeypatch.setenv("KILN_PRINTER_HOST", OCTOPRINT_HOST)
    monkeypatch.setenv("KILN_PRINTER_API_KEY", OCTOPRINT_API_KEY)
    monkeypatch.setenv("KILN_PRINTER_TYPE", "octoprint")


@pytest.fixture()
def env_missing_host(monkeypatch):
    """Ensure KILN_PRINTER_HOST is unset."""
    monkeypatch.delenv("KILN_PRINTER_HOST", raising=False)
    monkeypatch.setenv("KILN_PRINTER_API_KEY", OCTOPRINT_API_KEY)


@pytest.fixture()
def env_missing_api_key(monkeypatch):
    """Ensure KILN_PRINTER_API_KEY is unset."""
    monkeypatch.setenv("KILN_PRINTER_HOST", OCTOPRINT_HOST)
    monkeypatch.delenv("KILN_PRINTER_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Mock adapter for server tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_printer_state_idle():
    """Return a PrinterState representing an idle printer."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.IDLE,
        tool_temp_actual=24.5,
        tool_temp_target=0.0,
        bed_temp_actual=23.1,
        bed_temp_target=0.0,
    )


@pytest.fixture()
def mock_printer_state_printing():
    """Return a PrinterState representing a printing printer."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.PRINTING,
        tool_temp_actual=205.0,
        tool_temp_target=210.0,
        bed_temp_actual=59.8,
        bed_temp_target=60.0,
    )


@pytest.fixture()
def mock_printer_state_offline():
    """Return a PrinterState representing an offline printer."""
    return PrinterState(
        connected=False,
        state=PrinterStatus.OFFLINE,
    )


@pytest.fixture()
def mock_printer_state_error():
    """Return a PrinterState representing an errored printer."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.ERROR,
        tool_temp_actual=0.0,
        tool_temp_target=0.0,
        bed_temp_actual=0.0,
        bed_temp_target=0.0,
    )


@pytest.fixture()
def mock_job_progress():
    """Return a JobProgress for an active print."""
    return JobProgress(
        file_name="benchy.gcode",
        completion=45.68,
        print_time_seconds=1620,
        print_time_left_seconds=1980,
    )


@pytest.fixture()
def mock_capabilities():
    """Return default PrinterCapabilities."""
    return PrinterCapabilities()


@pytest.fixture()
def mock_file_list():
    """Return a list of PrinterFile objects."""
    return [
        PrinterFile(name="benchy.gcode", path="benchy.gcode", size_bytes=1234567, date=1700000000),
        PrinterFile(name="cube.gcode", path="cube.gcode", size_bytes=456789, date=1700001000),
    ]


# ---------------------------------------------------------------------------
# Engineering-overlay skip marker
# ---------------------------------------------------------------------------
# Public Kiln's materials.json carries only the safety floor (thermal
# limits, food/UV/outgassing safety, process geometry floors).  The
# engineering moat (mechanical properties, design_limits beyond
# process floor, use_case_ratings, agent_guidance paragraphs, brand-
# tunings beyond safety) ships in kiln-pro's overlay and is restored
# at runtime by ``_merge_pro_overlay_if_available``.
#
# Tests that assert moat fields should be marked with
# ``@requires_engineering_overlay`` so they SKIP in public-only CI
# (where the overlay isn't installed) and RUN cleanly in kiln-pro CI
# (where the overlay is present).


def _engineering_overlay_loaded() -> bool:
    """Probe whether BOTH the kiln-pro materials AND design_templates
    overlays are loaded.

    Materials probe: PLA must have mechanical fields (post-2026-04-09
    materials split — those fields ship in the kiln-pro overlay).

    Templates probe: snap_fit_cantilever must have design_rules
    (post-2026-05-05 design_templates split — those fields ship in
    the kiln-pro overlay).

    Both must be present.  Returning False here makes the
    ``@requires_engineering_overlay`` mark skip every assertion in
    test_material_data_sanity.py — the right behavior when either
    overlay is missing OR when an older kiln-pro install lacks the
    design_templates overlay kind entirely (load_overlay raises
    KeyError, the merger silently falls back to public-only data,
    and tests would then see only the discovery surface — false
    negatives if the gate let them run).
    """
    try:
        from kiln.design_intelligence import (
            _get_kb,
            _reset_knowledge_base,
            get_material_profile,
        )
        _reset_knowledge_base()
        pla = get_material_profile("pla")
        if pla is None or not pla.mechanical:
            return False
        templates = _get_kb().templates
        snap = templates.get("snap_fit_cantilever") or {}
        if not snap.get("design_rules"):
            return False
    except Exception:
        return False
    return True


_ENGINEERING_OVERLAY_PRESENT = _engineering_overlay_loaded()

requires_engineering_overlay = pytest.mark.skipif(
    not _ENGINEERING_OVERLAY_PRESENT,
    reason=(
        "kiln-pro engineering moat overlay not loaded; this assertion "
        "requires mechanical / design_limits / use_case_ratings / "
        "agent_guidance / brand-tuning fields that ship in kiln-pro. "
        "Install kiln-pro to run this test."
    ),
)


# ---------------------------------------------------------------------------
# Catalog-overlay skip markers (Phase 2 moat split — printer_profiles,
# material_troubleshooting, post_processing, multi_material_pairing)
# ---------------------------------------------------------------------------
# Each of these catalogs had curated SME content stripped out of public
# Kiln in the Phase 2 catalog moat split.  Public files now carry only
# the safety-floor / discovery surface; the moat fields ship in a
# kiln-pro overlay and are restored at runtime by
# ``_merge_pro_overlay_if_available``.
#
# Tests that assert on moat fields — or whose code path goes through a
# call site that hard-keys a moat field (e.g. PrinterDesignProfile's
# ``agent_notes``) — should be marked with the matching skip decorator
# so they SKIP in public-only CI and RUN cleanly when the overlay is
# installed.


def _printer_profiles_overlay_loaded() -> bool:
    """Probe whether the kiln-pro printer_profiles overlay is loaded.

    Public printer_profiles.json carries only the safety-floor fields
    (display_name, build_volume_mm, supported_materials, etc.).  The
    ``agent_notes`` moat field ships in the kiln-pro overlay; without
    it, ``get_printer_design_profile`` raises KeyError at the
    constructor call site.  Probing that path tells us if the overlay
    is present.
    """
    try:
        from kiln.design_intelligence import (
            _reset_knowledge_base,
            get_printer_design_profile,
        )
        _reset_knowledge_base()
        profile = get_printer_design_profile("bambu_x1c")
        if profile is None or not profile.agent_notes:
            return False
    except Exception:
        return False
    return True


_PRINTER_PROFILES_OVERLAY_PRESENT = _printer_profiles_overlay_loaded()

requires_printer_profiles_overlay = pytest.mark.skipif(
    not _PRINTER_PROFILES_OVERLAY_PRESENT,
    reason=(
        "kiln-pro printer_profiles overlay not loaded; this assertion "
        "exercises a code path that requires the agent_notes moat field "
        "(or any other field that ships in the kiln-pro overlay). "
        "Install kiln-pro to run this test."
    ),
)


def _troubleshooting_overlay_loaded() -> bool:
    """Probe whether the kiln-pro material_troubleshooting overlay is loaded.

    Public material_troubleshooting.json carries only the
    ``storage_requirements`` safety-floor field.  The moat fields
    (common_issues, break_in_tips, severity rankings, fix priorities)
    ship in the kiln-pro overlay.
    """
    try:
        from kiln.design_intelligence import (
            _reset_knowledge_base,
            troubleshoot_print_issue,
        )
        _reset_knowledge_base()
        result = troubleshoot_print_issue("pla")
        if result is None or not result.matched_issues or not result.break_in_tips:
            return False
    except Exception:
        return False
    return True


_TROUBLESHOOTING_OVERLAY_PRESENT = _troubleshooting_overlay_loaded()

requires_troubleshooting_overlay = pytest.mark.skipif(
    not _TROUBLESHOOTING_OVERLAY_PRESENT,
    reason=(
        "kiln-pro material_troubleshooting overlay not loaded; this "
        "assertion requires common_issues / break_in_tips / severity "
        "moat fields. Install kiln-pro to run this test."
    ),
)


def _post_processing_overlay_loaded() -> bool:
    """Probe whether the kiln-pro post_processing overlay is loaded.

    Public post_processing.json carries technique names + tool lists
    (safety floor — what materials can use what techniques).  The
    detailed ``procedure`` walkthroughs and ``difficulty`` calibrations
    ship in the kiln-pro overlay.
    """
    try:
        from kiln.design_intelligence import (
            _reset_knowledge_base,
            get_post_processing,
        )
        _reset_knowledge_base()
        guide = get_post_processing("pla")
        if guide is None or not guide.techniques:
            return False
        first = guide.techniques[0]
        if "procedure" not in first:
            return False
    except Exception:
        return False
    return True


_POST_PROCESSING_OVERLAY_PRESENT = _post_processing_overlay_loaded()

requires_post_processing_overlay = pytest.mark.skipif(
    not _POST_PROCESSING_OVERLAY_PRESENT,
    reason=(
        "kiln-pro post_processing overlay not loaded; this assertion "
        "requires the procedure-walkthrough moat field. Install "
        "kiln-pro to run this test."
    ),
)


def _multi_material_overlay_loaded() -> bool:
    """Probe whether the kiln-pro multi_material_pairing overlay is loaded.

    Public multi_material_pairing.json carries the support_pairs +
    co_print_compatibility matrix (safety floor — what pairs work).
    The ``general_rules`` guidance bullets ship in the kiln-pro
    overlay.
    """
    try:
        from kiln.design_intelligence import (
            _reset_knowledge_base,
            check_multi_material_compatibility,
        )
        _reset_knowledge_base()
        report = check_multi_material_compatibility("pla", "petg")
        if report is None or not report.general_rules:
            return False
    except Exception:
        return False
    return True


_MULTI_MATERIAL_OVERLAY_PRESENT = _multi_material_overlay_loaded()

requires_multi_material_overlay = pytest.mark.skipif(
    not _MULTI_MATERIAL_OVERLAY_PRESENT,
    reason=(
        "kiln-pro multi_material_pairing overlay not loaded; this "
        "assertion requires the general_rules moat field. Install "
        "kiln-pro to run this test."
    ),
)


def _printer_intelligence_overlay_loaded() -> bool:
    """Probe whether the kiln-pro printer_intelligence overlay is loaded.

    Public printer_intelligence.json carries the spec sheet + per-
    material recipe numbers + the structured ``has_input_shaping``
    bool.  The curated ``quirks``, ``calibration`` recipes, and
    ``failure_modes`` ship in the kiln-pro overlay.  Probe by
    reading a known profile (ender3) that has empty moat lists on
    the safety-floor side.
    """
    try:
        from kiln.printer_intelligence import (
            _cache,
            _load,
            get_printer_intel,
        )
        # Force a fresh load so a previous test that primed the
        # cache without the overlay doesn't poison this probe.
        _cache.clear()
        import kiln.printer_intelligence as _mod
        _mod._loaded = False
        _load()
        intel = get_printer_intel("ender3")
        if not intel.quirks or not intel.failure_modes:
            return False
    except Exception:
        return False
    return True


_PRINTER_INTELLIGENCE_OVERLAY_PRESENT = _printer_intelligence_overlay_loaded()

requires_printer_intelligence_overlay = pytest.mark.skipif(
    not _PRINTER_INTELLIGENCE_OVERLAY_PRESENT,
    reason=(
        "kiln-pro printer_intelligence overlay not loaded; this "
        "assertion requires the curated ``quirks`` / ``calibration`` "
        "/ ``failure_modes`` moat fields. Install kiln-pro to run "
        "this test."
    ),
)


def _printer_compatibility_overlay_loaded() -> bool:
    """Probe whether the kiln-pro printer_material_compatibility overlay is loaded.

    Public printer_material_compatibility.json carries the
    ``status`` + ``upgrades_needed`` safety-floor fields (what
    works on what printer, what upgrade is required).  The curated
    ``notes`` prose for each (printer, material) cell ships in the
    kiln-pro overlay.
    """
    try:
        from kiln.design_intelligence import _get_kb, _reset_knowledge_base
        _reset_knowledge_base()
        compat = _get_kb().printer_compatibility
        for printer_id, mat_map in compat.items():
            if printer_id.startswith("_"):
                continue
            for entry in mat_map.values():
                if isinstance(entry, dict) and entry.get("notes"):
                    return True
        return False
    except Exception:
        return False


_PRINTER_COMPATIBILITY_OVERLAY_PRESENT = _printer_compatibility_overlay_loaded()

requires_printer_compatibility_overlay = pytest.mark.skipif(
    not _PRINTER_COMPATIBILITY_OVERLAY_PRESENT,
    reason=(
        "kiln-pro printer_material_compatibility overlay not loaded; "
        "this assertion requires the curated ``notes`` moat field on "
        "each (printer, material) cell. Install kiln-pro to run this test."
    ),
)


# ---------------------------------------------------------------------------
# License tier bypass for tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bypass_license_tier(monkeypatch, tmp_path):
    """Ensure all tests run with tier checks bypassed by default.

    This prevents tier-gated MCP tools from returning LICENSE_REQUIRED
    errors in existing tests.  Tests that specifically test licensing
    behaviour can override this by patching ``kiln.licensing._manager``
    or ``check_tier`` themselves.
    """
    try:
        import kiln.licensing  # noqa: F401 — ensure shim is resolved
        monkeypatch.setattr(
            "kiln.licensing.check_tier", lambda _tier: (True, None)
        )
    except (ImportError, AttributeError):
        pass  # Licensing not available; stub requires_tier in server.py handles it


@pytest.fixture(autouse=True)
def _bypass_openscad_runnable_probe(request, monkeypatch):
    """Default the OpenSCAD runnable probe to 'OK' for the whole suite.

    ``kiln.generation.openscad._find_openscad`` calls
    ``kiln.emboss_generator._probe_openscad_runs`` to reject binaries
    that exist + are executable but can't run on this host (e.g.,
    x86_64 OpenSCAD on Apple Silicon without Rosetta — surfaces as
    EBADARCH).  The probe spawns ``openscad --version`` and rejects
    binaries whose output doesn't contain ``OpenSCAD``.  Test stubs
    that write a bare ``#!/bin/sh\\n`` script would fail that check.

    Tests that specifically exercise the probe's own behaviour
    (mocking subprocess.run to feed it crafted outputs) opt out with:

        @pytest.mark.use_real_openscad_probe
        class TestFindOpenscadProbe: ...

    Tests that want a specific probe verdict from inside
    ``_find_openscad`` (e.g., the EBADARCH path) can still override
    inline with ``patch("kiln.emboss_generator._probe_openscad_runs",
    return_value=(False, "Bad CPU type"))``.
    """
    if "use_real_openscad_probe" in request.keywords:
        return
    try:
        import kiln.emboss_generator  # noqa: F401 — ensure module loads
        monkeypatch.setattr(
            "kiln.emboss_generator._probe_openscad_runs",
            lambda _path: (True, None),
        )
    except (ImportError, AttributeError):
        pass  # emboss_generator absent; nothing to bypass


@pytest.fixture(autouse=True)
def _bypass_terms_gate(monkeypatch):
    """Default every test to "terms already accepted" so the one-time CLI terms
    gate (in ``kiln.cli.main``'s group callback) never blocks an unrelated
    command in a fresh test environment.

    Only affects callers that look up ``kiln.terms.is_current`` at call time
    (the lazy import in the CLI gate).  Tests that exercise the gate or the
    acceptance flow set ``kiln.terms.is_current`` explicitly — a later
    monkeypatch on the same shared instance wins — and ``test_terms.py`` binds
    ``is_current`` at import, so its direct calls still hit the real function.
    """
    try:
        import kiln.terms  # noqa: F401 — ensure module loads
        monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    except (ImportError, AttributeError):
        pass  # terms module absent; nothing to bypass


@pytest.fixture(autouse=True)
def _isolate_decoration_quota(tmp_path_factory, monkeypatch):
    """Give every test its own decoration-quota file and a fresh singleton.

    ``DecorationQuota`` defaults to ``~/.kiln/decoration_usage.json`` and is
    handed out through a module-level singleton, so without this the free-tier
    allowance (3/month) is *shared by the whole suite* and written to the real
    home directory.  Once three decoration tests have run, every later call to
    ``decorate_surface`` short-circuits with ``DECORATION_QUOTA_EXCEEDED``
    before reaching the check under test — which made the ``face="wall"``
    validation tests fail in a full run while passing in isolation.

    Tests that exercise quota behaviour directly construct
    ``DecorationQuota(quota_path=...)`` with their own path and are unaffected.
    """
    try:
        from kiln import decoration_quota
    except ImportError:  # pragma: no cover — module absent
        yield
        return

    qdir = tmp_path_factory.getbasetemp() / "decoration_quota"
    qdir.mkdir(exist_ok=True)
    qpath = qdir / "decoration_usage.json"
    if qpath.exists():
        qpath.unlink()  # fresh allowance per test

    monkeypatch.setattr(decoration_quota, "DEFAULT_QUOTA_PATH", qpath)
    monkeypatch.setattr(decoration_quota, "_quota", None)
    yield
    # Never leave a singleton bound to this test's temp path behind.
    decoration_quota._quota = None  # noqa: SLF001 — module-level test seam


@pytest.fixture(autouse=True)
def _restore_kiln_pro_stubs():
    """Undo ``kiln_pro`` stubs a test installs directly into ``sys.modules``.

    Several suites inject fake pro modules with a bare
    ``sys.modules["kiln_pro"] = fake`` instead of ``monkeypatch.setitem``, so
    the stub outlives the test.  A fake package that omits an attribute then
    breaks an unrelated test that imports it for real — e.g. a ``kiln_pro``
    without ``data_overlays`` made the printer-intelligence overlay lookup
    raise ``AttributeError`` in a full run but not in isolation.

    Snapshot the ``kiln_pro*`` entries and restore them afterwards so no test
    can leak a partial pro package into the next one.
    """
    def _snapshot() -> dict:
        return {
            name: mod
            for name, mod in sys.modules.items()
            if name == "kiln_pro" or name.startswith("kiln_pro.")
        }

    saved = _snapshot()
    yield
    for name in list(_snapshot()):
        if name not in saved:
            del sys.modules[name]
    sys.modules.update(saved)
