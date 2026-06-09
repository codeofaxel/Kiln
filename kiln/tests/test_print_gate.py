"""Tests for the pre-print impossibility gate (kiln.printers.print_gate).

These exercise the REAL bed_fit validators and design_intelligence temp data
(no mocks) so a regression in the deterministic block/soft-pass behaviour is
caught.  Geometry is built with trimesh boxes — fast, no slicer/OpenSCAD.
"""
from __future__ import annotations

import pytest

# Import base FIRST (mirrors the working import order) then the gate.
from kiln.printers.base import PrinterAdapter, PrintResult
from kiln.printers import print_gate as pg
from kiln.printers.print_gate import evaluate_pre_print_gate as G

trimesh = pytest.importorskip("trimesh")


def _box(size_mm: float, path: str) -> str:
    """A cube of *size_mm*, sitting on the bed (origin at corner)."""
    m = trimesh.creation.box(extents=[size_mm, size_mm, size_mm])
    m.apply_translation([size_mm / 2, size_mm / 2, size_mm / 2])
    m.export(path)
    return path


@pytest.fixture
def big(tmp_path) -> str:
    return _box(400.0, str(tmp_path / "big.stl"))  # 400mm > 256mm bed


@pytest.fixture
def small(tmp_path) -> str:
    return _box(100.0, str(tmp_path / "small.stl"))


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Isolate the module-level override grants between tests."""
    pg._oversize_grants.clear()
    yield
    pg._oversize_grants.clear()


class TestFitVerdict:
    def test_oversize_is_blocked(self, big):
        r = G(big, "bambu_a1")
        assert r["blocked"] is True
        assert r["code"] == "EXCEEDS_BED"
        assert r["suggestions"]

    def test_normal_passes(self, small):
        assert G(small, "bambu_a1")["blocked"] is False

    def test_override_converts_block_to_allowed(self, big):
        r = G(big, "bambu_a1", allow_oversize=True)
        assert r["blocked"] is False
        assert r.get("overridden") is True


class TestNeverFalseBlocks:
    def test_unknown_printer_soft_passes(self, big):
        # build volume unknown -> we cannot prove impossibility -> allow
        assert G(big, "totally_unknown_printer_zzz")["blocked"] is False

    def test_missing_file_soft_passes(self):
        assert G("/no/such/file.stl", "bambu_a1")["blocked"] is False

    def test_no_printer_soft_passes(self, big):
        assert G(big, None)["blocked"] is False


class TestTempCeiling:
    def test_too_hot_material_blocked(self, small):
        # polycarbonate needs >=270C; ender3 hotend maxes at 260C
        r = G(small, "ender3", material_id="polycarbonate")
        assert r["blocked"] is True
        assert r["code"] == "MATERIAL_EXCEEDS_HOTEND"

    @pytest.mark.parametrize("material", ["pla", "nylon"])
    def test_reachable_material_passes(self, small, material):
        assert G(small, "ender3", material_id=material)["blocked"] is False

    def test_unknown_material_soft_passes(self, small):
        assert G(small, "ender3", material_id="unobtanium_x")["blocked"] is False

    def test_temp_override(self, small):
        r = G(small, "ender3", material_id="polycarbonate", allow_oversize=True)
        assert r["blocked"] is False and r.get("overridden") is True


class TestOverrideGrant:
    def test_grant_is_per_printer_and_expiring(self):
        pg.grant_oversize_override("bambu_a1", ttl_seconds=300)
        assert pg._override_active("bambu_a1") is True
        assert pg._override_active("prusa_mk4") is False
        # case-insensitive key
        assert pg._override_active("BAMBU_A1") is True

    def test_grant_expires(self):
        pg.grant_oversize_override("expiretest", ttl_seconds=-1)  # already expired
        assert pg._override_active("expiretest") is False


class _FakeAdapter(PrinterAdapter):
    """Minimal adapter to exercise the Template Method without hardware."""

    printer_id = "bambu_a1"

    def __init__(self) -> None:
        self.impl_calls: list[str] = []

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.impl_calls.append(file_name)
        return PrintResult(success=True, message="IMPL")


# allow instantiation without implementing every other abstractmethod
_FakeAdapter.__abstractmethods__ = frozenset()


class _EnderAdapter(_FakeAdapter):
    """Fake adapter reporting as an ender3 (260C hotend ceiling) for temp tests."""

    printer_id = "ender3"


_EnderAdapter.__abstractmethods__ = frozenset()


class TestTemplateMethodBackstop:
    def test_block_does_not_call_impl(self, big):
        a = _FakeAdapter()
        r = a.start_print(big)
        assert r.success is False
        assert a.impl_calls == []  # gate fired before delegation

    def test_valid_delegates_to_impl(self, small):
        a = _FakeAdapter()
        r = a.start_print(small)
        assert r.success is True and r.message == "IMPL"
        assert a.impl_calls == [small]

    def test_override_lets_oversize_through(self, big):
        a = _FakeAdapter()
        pg.grant_oversize_override("bambu_a1", ttl_seconds=300)
        r = a.start_print(big)
        assert r.success is True and a.impl_calls == [big]

    def test_fails_open_if_gate_raises(self, small, monkeypatch):
        a = _FakeAdapter()

        def boom(*args, **kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(pg, "run_adapter_gate", boom)
        r = a.start_print(small)
        assert r.success is True and a.impl_calls == [small]  # never block on a gate bug


class TestSwarmFixes:
    """Regressions for the adversarial-verify findings."""

    def test_resume_mode_soft_passes_oversize(self, big):
        # A resume continuation is committed + already validated -> never re-gated,
        # even though `big` is oversize. (false-block fix)
        a = _FakeAdapter()
        assert pg.run_adapter_gate(a, big, {"resume_from_paused": True}) is None
        # ...and via the conventional resume filename (string check; file need not exist)
        assert pg.run_adapter_gate(a, "/tmp/foo_resume_plate.3mf", {}) is None

    def test_override_is_single_use(self, big):
        a = _FakeAdapter()
        pg.grant_oversize_override("bambu_a1", ttl_seconds=300)
        assert a.start_print(big).success is True   # 1st: override rescues, grant consumed
        assert a.start_print(big).success is False  # 2nd: grant gone -> blocked again

    def test_ams_material_string_is_normalized(self, small):
        # "POLYCARBONATE " (uppercase + trailing space) must still resolve and block
        a = _EnderAdapter()
        blocked = pg.run_adapter_gate(a, small, {"material": "POLYCARBONATE "})
        assert blocked is not None and blocked["code"] == "MATERIAL_EXCEEDS_HOTEND"

    def test_temp_verdict_handles_zero_and_equality(self, monkeypatch):
        # Two regressions in one: (a) a [0, X] range must NOT be dropped by a
        # falsy check, and (b) min == max must PASS (printer can just reach it),
        # never false-block.
        class _Mat:
            def __init__(self, lo, hi):
                self.thermal = {"print_temp_range_c": [lo, hi]}

        class _Printer:
            def __init__(self, mx):
                self.max_hotend_temp_c = mx

        import kiln.design_intelligence as di

        # min == max -> reachable -> NOT blocked (strict ">")
        monkeypatch.setattr(di, "get_printer_design_profile", lambda pid: _Printer(260))
        monkeypatch.setattr(di, "get_material_profile", lambda mid: _Mat(260, 310))
        assert pg._temp_verdict("ender3", "x")[0] is False  # 260 > 260 is False

        # strictly above -> blocked
        monkeypatch.setattr(di, "get_material_profile", lambda mid: _Mat(270, 310))
        assert pg._temp_verdict("ender3", "x")[0] is True

        # zero min survives extraction (and 0 < 260 -> not blocked)
        monkeypatch.setattr(di, "get_material_profile", lambda mid: _Mat(0, 300))
        assert pg._temp_verdict("ender3", "x")[0] is False
