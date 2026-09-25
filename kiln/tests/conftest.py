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

import contextlib
import functools
import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Relocate HOME before anything imports kiln.  MUST stay first.
# ---------------------------------------------------------------------------
# Kiln keeps its whole per-user world under ``~/.kiln`` — config.yaml,
# kiln.db, daily_stats.json, credentials, calibration.  Tests reach that
# world through a dozen different readers, several of which resolve the
# path at import time, so patching them one by one always leaves one
# behind.  Two real costs, both paid: a suite run wrote phantom activity
# into the developer's telemetry that the next heartbeat would have
# shipped, and test-ORDER pollution made a printer-registry test find a
# real Bambu plus 448 real queued jobs and fail on a clean branch.
#
# Moving the root fixes every reader at once, including the ones nobody
# has enumerated — a new store added next year is isolated for free.
_TEST_HOME = tempfile.mkdtemp(prefix="kiln-test-home-")
os.makedirs(os.path.join(_TEST_HOME, ".kiln"), exist_ok=True)
os.environ["HOME"] = _TEST_HOME
os.environ.pop("KILN_PRINTER_HOST", None)  # no real printer leaks in either
os.environ.pop("KILN_PRINTER_TYPE", None)

# Same class of bug as the HOME move above, one store it doesn't reach:
# kiln.stage_cache keeps an in-process memo that, once set, is never
# re-read from disk for the rest of the process (see its ``document()``
# docstring).  The first test in a worker that calls visualize_model()
# with the default allow_stage=True finds no cache under the fresh
# _TEST_HOME, which is correct -- but that miss also fires
# stage_cache.warm(), a background thread doing a REAL HTTP fetch against
# production api.kiln3d.com.  On a machine with network access that
# fetch usually wins the race against the rest of a multi-minute suite
# run, so the memo goes warm partway through and every LATER test in
# that worker silently gets a real stage-photograph render instead of
# whatever visualize_model()/OpenSCAD path it thought it was exercising
# -- order- and timing-dependent, invisible unless a test happens to
# assert on OpenSCAD's own invocation. Disabling the fetch (not
# find_browser()'s KILN_NO_STAGE_STILLS, which test_stage_still.py's
# happy-path tests need live for their own KILN_STAGE_BROWSER override)
# leaves _stage_document() returning None all suite long unless a test
# opts back in via KILN_STAGE_DOC, same as it already does when offline.
os.environ["KILN_NO_STAGE_FETCH"] = "1"
# No test puts a banner on the developer's screen; a test of the screen
# door installs its own notifier and turns this back on.
os.environ.setdefault("KILN_SCREEN_CODE", "0")
# A test host answers the approval dialog in no time; the too-fast rule is
# tested where it is meant, by setting this back.
os.environ.setdefault("KILN_DIALOG_MIN_READ_S", "0")

# ---------------------------------------------------------------------------
# Monkey-patch FastMCP to accept unknown kwargs (like ``description``)
# so that ``import kiln.server`` succeeds at collection time.
# ---------------------------------------------------------------------------
from kiln.mcp_compat import FastMCP

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
# Overlay skip markers
# ---------------------------------------------------------------------------
# Some assertions only hold once the kiln-pro overlay is merged into the
# knowledge base.  Each probe below reads one field the overlay
# supplies; tests that need it carry the matching marker so they skip
# in a public-only environment and run when kiln-pro is installed.


def _engineering_overlay_loaded() -> bool:
    """True when both the materials and design_templates overlays are
    merged in.  Both must be present: an older kiln-pro install can
    carry one without the other, and the merger falls back silently.
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
    reason="requires the kiln-pro overlay",
)


# ---------------------------------------------------------------------------
# Catalog-overlay skip markers
# ---------------------------------------------------------------------------


def _printer_profiles_overlay_loaded() -> bool:
    """True when the printer_profiles overlay is merged in."""
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
    reason="requires the kiln-pro overlay",
)


def _troubleshooting_overlay_loaded() -> bool:
    """True when the material_troubleshooting overlay is merged in."""
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
    reason="requires the kiln-pro overlay",
)


def _post_processing_overlay_loaded() -> bool:
    """True when the post_processing overlay is merged in."""
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
    reason="requires the kiln-pro overlay",
)


def _multi_material_overlay_loaded() -> bool:
    """True when the multi_material_pairing overlay is merged in."""
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
    reason="requires the kiln-pro overlay",
)


def _printer_intelligence_overlay_loaded() -> bool:
    """True when the printer_intelligence overlay is merged in."""
    try:
        import kiln.printer_intelligence as _mod
        from kiln.printer_intelligence import get_printer_intel
        # Force a fresh load so a previous test that primed the
        # cache without the overlay doesn't poison this probe.
        _mod._reset_caches()
        intel = get_printer_intel("ender3")
        if not intel.quirks or not intel.failure_modes:
            return False
    except Exception:
        return False
    return True


_PRINTER_INTELLIGENCE_OVERLAY_PRESENT = _printer_intelligence_overlay_loaded()

requires_printer_intelligence_overlay = pytest.mark.skipif(
    not _PRINTER_INTELLIGENCE_OVERLAY_PRESENT,
    reason="requires the kiln-pro overlay",
)


def _printer_compatibility_overlay_loaded() -> bool:
    """True when the printer_material_compatibility overlay is merged in."""
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
    reason="requires the kiln-pro overlay",
)


# ---------------------------------------------------------------------------
# License tier bypass for tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_print_watchers():
    """No test may inherit a watch an earlier test left running.

    Kiln counts watched MACHINES process-wide — live entries in
    ``kiln.server._watchers`` plus the print health monitor's sessions — and
    refuses a NEW watch past the tier's limit, which is one machine on the
    free tier every test runs as.  So one test that starts a watch and never
    stops it spends the only slot for the rest of the worker process, and
    every later test that watches anything gets a refusal back instead of a
    watch.

    Measured 2026-09-15: ``test_plugin_tools.py``'s idle-printer watch test
    left one watcher behind, and in that same process six tests in
    ``test_vision_monitoring.py`` failed on the refusal — the file passes on
    its own.  In CI the victims moved between runs as the parallel workers'
    mix changed, which is what made a leak read as flakiness.

    Cleared as TEARDOWN, so a test that starts a watch and reads it within
    itself is unaffected.  ``stop()`` first, not a bare dict clear, so the
    watcher's thread exits instead of polling a printer for the rest of the
    run.  No-op when the modules were never imported.
    """
    yield
    server_mod = sys.modules.get("kiln.server")
    watchers = getattr(server_mod, "_watchers", None) if server_mod else None
    if isinstance(watchers, dict) and watchers:
        for watcher in list(watchers.values()):
            with contextlib.suppress(Exception):  # teardown must never mask a failure
                watcher.stop()
        watchers.clear()
    monitor_mod = sys.modules.get("kiln.print_health_monitor")
    if monitor_mod is not None:
        with contextlib.suppress(Exception):
            monitor = monitor_mod.get_print_health_monitor()
            sessions = getattr(monitor, "_sessions", None)
            if isinstance(sessions, dict) and sessions:
                for session in list(sessions.values()):
                    with contextlib.suppress(Exception):
                        monitor.stop_monitoring(getattr(session, "printer_name", ""))
                sessions.clear()


@pytest.fixture(autouse=True)
def _isolate_printer_registry():
    """No test may inherit printers registered by an earlier test.

    ``kiln.registry`` keeps a process-wide singleton, so a test that
    registers a printer and does not clean up leaves it visible to every
    later test.  That is currently red on main two different ways:
    ``test_server_tools.py::TestKilnHealth::test_registry_count_reflected``
    fails ``assert 2 == 0`` on somebody else's entries, and
    ``test_resources.py::TestResourcePrinters::test_empty`` times out
    because the leaked entries are queried over the NETWORK — a unit-test
    run reaching for real hardware, and ~20 minutes of CI spent waiting.

    Cleared as TEARDOWN, so a test that registers and then reads within
    itself is unaffected; only the leak across the boundary is stopped.
    ``unregister`` (not a bare dict clear) so adapters disconnect instead
    of piling up MQTT threads.  The registry OBJECT is emptied rather than
    replaced, because ``kiln.server`` holds its own reference to the same
    instance.  No-op when ``kiln.registry`` was never imported.
    """
    yield
    mod = sys.modules.get("kiln.registry")
    registry = getattr(mod, "_registry_singleton", None) if mod else None
    if registry is not None:
        for name in registry.list_names():
            try:
                registry.unregister(name)
            except Exception:  # noqa: BLE001 - teardown must never mask a failure
                pass

    # ``kiln.server`` caches the active adapter in a module global, and
    # ``_get_adapter`` returns it without consulting config.  A test that
    # calls ``register_printer`` leaves a LIVE adapter there — and
    # ``resource_printers`` resurrects it whenever the registry is empty,
    # registering it as "default" and querying it over the network.  That
    # is how ``test_resources.py::TestResourcePrinters::test_empty`` ends
    # up dialling 192.0.2.11 for 98 seconds after the fleet-gate tests run.
    # Clearing the registry alone does not stop it; the cached adapter has
    # to go too, or an empty registry just refills itself.
    srv = sys.modules.get("kiln.server")
    if srv is not None and getattr(srv, "_adapter", None) is not None:
        try:
            srv._adapter = None
        except Exception:  # noqa: BLE001 - teardown must never mask a failure
            pass


@pytest.fixture(autouse=True)
def _isolate_plate_record():
    """No test may inherit a plate record an earlier test's start left.

    The adapter template records "the plate holds a part" after every
    start it runs -- fake printers included -- into
    ``$HOME/.kiln/plate_state.json``, and _TEST_HOME is one directory for
    the whole session.  Every door that starts a print, or slices for one,
    reads that record first (``kiln.plate_state.start_refusal`` and the
    slice doors' plate gate), so a fake printer "started" in one test used
    to refuse the next test's start on the same fake host with
    PLATE_OCCUPIED_START_NOT_YET: order-dependent, and invisible before the
    gate existed.  A fresh record per test; a test that wants an occupied
    plate writes one.  A test that sets its own ``KILN_HOME`` keeps its own
    store, untouched by this.
    """
    from kiln.plate_state import _store_path

    _store_path().unlink(missing_ok=True)
    yield


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
def _isolate_kiln_db(tmp_path, monkeypatch):
    """Keep the suite out of the developer's real ``~/.kiln/kiln.db``.

    ``KilnDB()`` with no argument resolves to the default path, so any test
    that constructs one writes print history, jobs and outcomes into the
    real database.  It did: 1,811 phantom prints, 462 jobs and 333 outcomes
    from ``a.gcode`` / ``test.gcode``, against exactly one genuine print.
    The same class the daily-stats isolation already covers — the counters
    were fixed and the database under them was not.

    ``persistence._redirect_if_test_runner`` is the belt (the default path
    is refused under CI env at the source); this is the suspenders, and it
    gives each test its own file so a test that WANTS to assert on
    persistence just works.

    The singleton reset is part of the isolation: ``get_db()`` caches the
    first instance it builds, so without the reset every later test reads
    whichever tmp DB the FIRST caller bound — rows written by one test
    (e.g. pending outcome rows opened by a start_print exercise) leak
    into unrelated tests' assertions in whatever order the worker ran.
    """
    import kiln.persistence as _persistence
    from kiln import auto_record_hook as _hook

    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setattr(_persistence, "_db", None)
    # The outcome hook's observation ledger (previous state per printer,
    # cancel intents, recorded-job dedupe) is process state of the same
    # class as the DB singleton: one test's observed "printing" must not
    # become the next test's phantom terminal transition.
    monkeypatch.setattr(_hook, "_HOOK_STATE", _hook._HookState())
    yield
    monkeypatch.setattr(_persistence, "_db", None)


@pytest.fixture(autouse=True)
def _no_print_watchdog_outlives_its_test(monkeypatch):
    """Stop every print watchdog a test started when that test ends.

    A watchdog is a daemon thread that polls its printer every few seconds
    until something stops it, and a test that starts a print through a door
    that arms one rarely does.  It outlived its test, still polling a fake
    printer, and filed what that fake said into the outcome ledger above
    under the fake's name -- a name the next tests reuse.  A fake "workshop"
    that printed forever reached a later test's ledger between its cancel and
    its ending, read as a new print starting, and wiped the cancel: a
    cancelled print recorded a success, once in about forty CI runs, never
    in isolation.  The same class as the ledger reset: process state one
    test leaves behind for the next.
    """
    from kiln.print_watchdog import PrintWatchdog

    started: list[PrintWatchdog] = []
    real_start = PrintWatchdog.start

    def start(self):
        started.append(self)
        return real_start(self)

    monkeypatch.setattr(PrintWatchdog, "start", start)
    yield
    server = sys.modules.get("kiln.server")
    table = getattr(server, "_print_watchdogs", None)
    lock = getattr(server, "_print_watchdogs_lock", None) or contextlib.nullcontext()
    for watchdog in started:
        watchdog.stop(timeout=1.0)
        # Filed in the server's table, a stopped watchdog is still what the
        # next test's print on that name finds there.
        if isinstance(table, dict):
            with lock:
                for name, filed in list(table.items()):
                    if filed is watchdog:
                        del table[name]


@pytest.fixture(autouse=True)
def _isolate_daily_stats(tmp_path, monkeypatch):
    """Point telemetry counters at a per-test file, never the real one.

    ``daily_stats`` writes ``~/.kiln/daily_stats.json``, and its counters
    now fire from engine chokepoints (adapter ``start_print``,
    ``slicer.slice_file``, the tool-dispatch hook) — so ordinary tests
    exercise them constantly.  Without isolation a suite run pollutes the
    developer's real counters and the next heartbeat ships phantom
    activity to the usage dashboard (2026-07-26: 47 phantom prints from
    one adapter-suite run).  ``daily_stats._recording_suppressed`` is the
    belt (no writes under CI env at the default path); this is the
    suspenders, and it also means a test that wants to ASSERT on counters
    just works — a custom path records normally.

    Tests that need their own path (most daily-stats tests) still set it
    themselves; this default only catches the ones that never think about
    telemetry.
    """
    try:
        from kiln import daily_stats
    except ImportError:  # pragma: no cover — module absent
        yield
        return

    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "daily_stats.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_printer_engagement(tmp_path_factory, monkeypatch):
    """Give every test its own engagement record, never the developer's.

    ``printers.engagement`` writes ``~/.kiln/printer_engagement.json`` from
    ``start_print``, which ordinary adapter tests call constantly.  Without
    isolation a suite run leaves a real engagement on disk naming a fake
    printer, and the next real command on the developer's machine is refused
    by a rule pointing at a machine that never existed.  Same class as
    ``_isolate_daily_stats`` above, with a sharper failure: this one does not
    merely pollute a number, it locks a person out of their own printer.

    The per-test path also resets the in-process verification cache, so one
    test's engaged peer cannot answer for the next test's.
    """
    try:
        from kiln.printers import engagement
    except ImportError:  # pragma: no cover — module absent
        yield
        return

    # tmp_path_factory, NOT tmp_path: the per-test tmp_path doubles as a
    # scratch ROOT for other subsystems, and a directory left in it is
    # something else's data.  Creating "kiln_home" there made
    # list_incidents count it as a fourth incident.
    home = tmp_path_factory.mktemp("kiln_engagement_home")
    monkeypatch.setattr(engagement, "_kiln_dir", lambda: home)
    engagement._verify_cache.clear()
    yield
    engagement._verify_cache.clear()


@pytest.fixture(autouse=True)
def _no_real_pypi_check(monkeypatch):
    """Keep the update check off the network, for the whole suite.

    Same class as ``_isolate_daily_stats`` above, and it bites harder because
    the write happens on a DAEMON THREAD.  ``check_for_update`` is reachable
    from ordinary surfaces — ``get_started``, ``kiln_health``, the agent nudge,
    ``kiln bridge status`` — and on a cold cache it kicks a background fetch
    that later writes ``~/.kiln/update_check.json``.  A test that merely calls
    one of those surfaces therefore hits PyPI for real and pollutes the
    developer's cache.

    Worse, it outlives the test that started it: ``test_version_check`` seeds a
    cache under a patched ``Path.home`` and asserts on it, and a thread left
    running by an EARLIER module lands its real answer in that tmp directory
    mid-assertion.  That was a genuine flake, and it moved between runs
    depending on who won the race.

    Stubbing the fetch leaves the surrounding machinery (thread spawn,
    in-flight guard, cache read) exercised while making the answer "nothing
    published" — so the cold-cache path stays honest and no bytes leave the
    box.  ``test_version_check`` captures the real fetch at import time for its
    one deliberate live test, so that keeps working.
    """
    try:
        from kiln import version_check
    except ImportError:  # pragma: no cover — module absent
        yield
        return

    monkeypatch.setattr(version_check, "_fetch_latest_from_pypi", lambda: None)
    version_check._refresh_in_flight = False
    yield
    version_check._refresh_in_flight = False


@pytest.fixture(autouse=True)
def _public_fault_readings_only(monkeypatch):
    """Every test reads a Bambu fault code from public Kiln's own tables.

    ``kiln.printers.bambu.read_bambu_fault`` asks kiln-pro first, through
    ``kiln._pro_fault_bridge``, and kiln-pro answers by the machine's own
    licence.  On a laptop with kiln-pro installed beside this checkout that
    made a public test's answer depend on which kiln-pro was on the path and
    whose key was in the keychain -- a licensed machine saw the private
    reading where CI saw the public line, and the same assertion passed on
    one and failed on the other.

    So the bridge is silent by default, and a test that wants kiln-pro's
    answer patches ``decode_fault`` itself with the row it is testing
    (``tests/test_fault_reading_doors.py`` does).  A later ``monkeypatch``
    in the test body overrides this one for that test.
    """
    import kiln._pro_fault_bridge as fault_bridge

    monkeypatch.setattr(fault_bridge, "decode_fault", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def _no_real_bambu_hms_text_fetch(monkeypatch):
    """Keep the Bambu fault-sentence lookup off the network, suite-wide.

    Same class as ``_no_real_pypi_check`` above.  Every Bambu status read
    warms the vendor's fault-sentence table in a daemon thread
    (``kiln.printers.bambu_hms_text``), keyed by the serial's first three
    characters -- so any test that builds a Bambu state would otherwise ask
    ``e.bambulab.com`` for the table for device type ``TES`` and, on a real
    answer, write it under ``~/.kiln/bambu_hms/``.  The fetch is stubbed to
    "no table", which leaves the thread spawn, the in-flight guard and the
    cache read exercised while no bytes leave the box; the in-memory tables
    are cleared on both sides so a test that seeds one cannot leak it into
    the next.
    """
    try:
        from kiln.printers import bambu_hms_text
    except ImportError:  # pragma: no cover — module absent
        yield
        return

    monkeypatch.setattr(bambu_hms_text, "_fetch_table", lambda device_type: None)
    bambu_hms_text._reset_for_tests()
    yield
    bambu_hms_text._reset_for_tests()


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


@pytest.fixture(autouse=True)
def _reset_fastener_content_keys():
    """Give every test a fresh SESSION for the fastener advisory.

    ``kiln.fastener_advice`` speaks each content key once per process, and a
    test process is many sessions pretending to be one.  Without this reset
    the first test to build a part with a screw hole silences every later
    test at every other seam — and the failure would read as "the advisory
    stopped working" rather than "the previous test used it up".

    Reset AFTER the test as well as before, so a test that deliberately
    exhausts the key cannot leak that state into an unrelated suite.
    """
    from kiln.fastener_advice import reset_emitted_content_keys

    reset_emitted_content_keys()
    yield
    reset_emitted_content_keys()


def _settle_routine_threads() -> None:
    """Bring ``kiln.printers.routine_ledger`` back to rest, quietly."""
    try:
        from kiln.printers import routine_ledger
    except ImportError:  # pragma: no cover — module absent
        return
    if not routine_ledger._threads and not routine_ledger._holds:
        return
    with contextlib.suppress(Exception):
        routine_ledger.drain("test isolation")
    with contextlib.suppress(Exception):
        routine_ledger.wait_settled(5.0)


@pytest.fixture(autouse=True)
def _no_routine_thread_crosses_a_test():
    """No test inherits or leaks a background routine thread.

    ``kiln.printers.routine_ledger`` runs a served finish — fan on, wait
    for the hand-off temperature, fan off — in a thread that deliberately
    outlives the request that started it.  That is right in a server and
    wrong in a runner that hosts thousands of requests in one process: the
    watch polls through the plain ``time`` module, so a thread still
    running when the NEXT test installs its own fake clock calls THAT
    test's ``time.sleep`` — and in these suites the fake sleep is what
    moves the thermistor mock.  Measured: a leftover watch heated the
    nozzle of a wipe test that had set it cold, the cold-nozzle refusal
    that test pins did not fire, and the extruder move went out.  Order-
    and timing-dependent, so it only ever showed up under ``-n auto``, on
    some Python versions, on some runs.

    Settled on BOTH sides, so neither inheriting one nor leaking one is
    possible: the same reason the HOME move at the top of this file is
    here rather than in the dozen suites that would each have to remember
    it.  Free for every test that starts no routine (both ledgers empty is
    an immediate return), and the teardown runs after ``monkeypatch`` has
    put the real clock back — which is what lets a watch reach its
    deadline and stop instead of being joined against a clock that no
    longer moves.
    """
    _settle_routine_threads()
    yield
    _settle_routine_threads()


def _forget_served_misses() -> None:
    """Drop every bridge's memory of why a served answer was missing."""
    import sys

    for module, attr in (
        ("kiln._pro_motion_bridge", "_misses"),
        ("kiln._pro_cutter_bridge", "_last_miss"),
        ("kiln._pro_nozzle_bridge", "_last_miss"),
    ):
        record = getattr(sys.modules.get(module), attr, None)
        if isinstance(record, dict):
            record.clear()


@pytest.fixture(autouse=True)
def _no_served_miss_crosses_a_test():
    """No test inherits another's reason for a missing served answer.

    Each bridge to Kiln's servers remembers WHY its last answer did not
    arrive -- offline, signed out, unanswered, refused -- so a door can
    word the refusal a person reads.  In a process that serves one user
    that memory is per machine and self-clearing; in a runner it is shared
    state, and a reason left behind by one test changes the SENTENCE the
    next test's door produces.  Measured: a test that fakes "no plan"
    directly, and therefore expects the floor's own fallback line, read a
    neighbour's leftover reason instead and asserted against the wrong
    wording -- green alone, red after a file that had recorded one.

    Same species and same place as the routine-thread settle above, and
    free for any test that never asks: an unimported bridge has no dict to
    clear.
    """
    _forget_served_misses()
    yield
    _forget_served_misses()
