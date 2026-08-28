"""Slicing resolves the profile for the printer the call is AIMED at.

THE DEFECT (2026-08-28)
-----------------------
Every slicing door resolved its slicer profile from the process-global
default printer model (``_PRINTER_MODEL``), even when the caller passed
``printer_name`` for a different registered machine.  Two Bambus —
``bambu_a1`` as the default and a ``bambu_x1c`` registered as
``shop-x1`` — and ``slice_and_print(printer_name="shop-x1")`` sliced
with the A1 profile, then uploaded the result to the X1C.

WHAT THIS PINS
--------------
``_resolve_slice_printer_id`` — the one resolver every door now goes
through — follows the target machine: explicit ``printer_id`` first,
then the named printer's config-declared model, then its adapter's
self-reported model, and falls back to ``_PRINTER_MODEL`` only when
the call is aimed at the default printer.  A named machine whose model
cannot be determined gets NO profile rather than the default machine's.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from kiln.printer_model_resolver import invalidate_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


TWO_BAMBUS = """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    serial: 03900D5C_A1
    printer_model: bambu_a1
  shop-x1:
    type: bambu
    host: 10.0.0.6
    serial: 03900D5C_X1
    printer_model: bambu_x1c
"""


def _use_config(tmp_path: Path, monkeypatch, body: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body)
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)


@pytest.fixture()
def _no_live_adapter(monkeypatch):
    """Keep the resolver off the network/registry: the adapter probe is a
    fallback for undeclared models, and these tests declare them."""
    import kiln.server as srv

    monkeypatch.setattr(
        srv, "_resolve_adapter",
        mock.Mock(side_effect=RuntimeError("no adapters in tests")),
    )


class TestResolveSlicePrinterId:
    def test_named_printer_resolves_its_own_model_not_the_default(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        """The incident: shop-x1 must slice as bambu_x1c, not bambu_a1."""
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        assert srv._resolve_slice_printer_id(None, "shop-x1") == "bambu_x1c"

    def test_default_target_still_uses_the_global_model(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        assert srv._resolve_slice_printer_id(None, None) == "bambu_a1"
        assert srv._resolve_slice_printer_id(None, "default") == "bambu_a1"

    def test_explicit_printer_id_beats_the_named_printer(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        assert srv._resolve_slice_printer_id("prusa_mini", "shop-x1") == "prusa_mini"

    def test_named_printer_without_model_never_borrows_the_defaults(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        """No declared model + no adapter answer = no profile.  Slicer
        defaults are recoverable; the wrong machine's profile is not."""
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, """\
active_printer: default
printers:
  default:
    type: bambu
    host: 10.0.0.5
    serial: 03900D5C_A1
    printer_model: bambu_a1
  shop-x1:
    type: bambu
    host: 10.0.0.6
    serial: 03900D5C_X1
""")
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        assert srv._resolve_slice_printer_id(None, "shop-x1") is None

    def test_named_printer_falls_back_to_its_adapters_model(
        self, tmp_path, monkeypatch
    ):
        """A machine registered at runtime (absent from config.yaml) still
        resolves through what its adapter says it is."""
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        class _Adapter:
            printer_model = "bambu_x1c"

        monkeypatch.setattr(srv, "_resolve_adapter", mock.Mock(return_value=_Adapter()))

        assert srv._resolve_slice_printer_id(None, "runtime-x1") == "bambu_x1c"


class TestProfileContextFollowsTheTarget:
    def test_context_resolves_the_named_printers_profile(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        pid, profile = srv._resolve_slice_profile_context(
            profile=None, printer_id=None, printer_name="shop-x1",
        )
        assert pid == "bambu_x1c"
        assert profile is not None
        assert "x1c" in Path(profile).name.lower() or "x1c" in Path(profile).read_text().lower()

    def test_explicit_profile_path_always_wins(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        import kiln.server as srv

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        pid, profile = srv._resolve_slice_profile_context(
            profile="/tmp/custom.ini", printer_id=None, printer_name="shop-x1",
        )
        assert profile == "/tmp/custom.ini"
        assert pid == "bambu_x1c"


class TestDoorsPassTheTarget:
    """The engine fix is only real if the doors actually hand it the name."""

    def test_slice_and_print_hands_its_printer_name_to_the_resolver(
        self, monkeypatch
    ):
        import kiln.server as srv
        from kiln.plugins.slicer_tools import plugin

        class _MockMCP:
            def __init__(self) -> None:
                self.tools: dict[str, object] = {}

            def tool(self, **_kwargs):
                def decorator(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return decorator

        mcp = _MockMCP()
        plugin.register(mcp)

        spy = mock.Mock(return_value=(None, None))
        monkeypatch.setattr(srv, "_resolve_slice_profile_context", spy)
        monkeypatch.setattr(srv, "_check_auth", mock.Mock(return_value=None))
        monkeypatch.setattr(
            srv, "_resolve_adapter",
            mock.Mock(side_effect=RuntimeError("no adapters in tests")),
        )

        result = mcp.tools["slice_and_print"](
            input_path="/nonexistent/never-sliced.stl",
            printer_name="shop-x1",
        )
        # The tool errors long before printing (missing file) — the pin is
        # that resolution was asked about the TARGET printer first.
        assert spy.called
        assert spy.call_args.kwargs.get("printer_name") == "shop-x1"
        assert isinstance(result, dict)

    def test_quick_print_pipeline_resolves_for_its_named_printer(
        self, tmp_path, monkeypatch, _no_live_adapter
    ):
        import kiln.server as srv
        from kiln import pipelines

        _use_config(tmp_path, monkeypatch, TWO_BAMBUS)
        monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")

        model = tmp_path / "cube.stl"
        model.write_bytes(b"solid cube\nendsolid cube\n")

        result = pipelines.quick_print(
            model_path=str(model),
            printer_name="shop-x1",
            skip_validation=True,
            pause_after_step=1,  # stop right after resolve_profile
        )
        resolve_steps = [s for s in result.steps if s.name == "resolve_profile"]
        assert resolve_steps, "quick_print no longer runs a resolve_profile step"
        step = resolve_steps[0]
        assert step.success
        assert (step.data or {}).get("printer_id") == "bambu_x1c"
