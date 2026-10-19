"""A paying customer on a plain install is read on their plan.

Each check below used to ask ``kiln_pro.pro_gate`` for the caller's tier and
answer "free" when that import failed, which is every ``pip install kiln3d``:
a signed-in Pro account still hit the free assembly part cap, and a Business
account asking for a QR code was told to upgrade.  The plan of a plain
install is the signed-in Kiln account's, read through ``kiln.licensing``;
these tests run with the private package unimportable and that plan set.
"""

from __future__ import annotations

import sys

import pytest

import kiln.account_plan as account_plan
from kiln.account_plan import LicenseTier


@pytest.fixture
def plan(monkeypatch):
    """A plain install signed in on a chosen plan; returns the setter."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    # What kiln/licensing.py resolves to when kiln-pro is absent.
    monkeypatch.setitem(sys.modules, "kiln.licensing", account_plan)

    def set_plan(tier: LicenseTier) -> None:
        monkeypatch.setattr(account_plan, "get_tier", lambda *a, **k: tier)
        monkeypatch.setattr(account_plan, "refresh_plan", lambda *a, **k: tier)

    return set_plan


def _assembly_with(n: int):
    from kiln.assembly import Assembly, AssemblyPart

    asm = Assembly(assembly_id="a1", name="shelf")
    for i in range(n):
        asm.add_part(AssemblyPart(part_id=f"p{i}", file_path=f"/tmp/p{i}.stl"))
    return asm


class TestAssemblyPartCap:
    def test_a_pro_account_adds_past_the_free_cap(self, plan):
        from kiln.assembly import _MAX_FREE_TIER_PARTS

        plan(LicenseTier.PRO)
        asm = _assembly_with(_MAX_FREE_TIER_PARTS + 1)
        assert len(asm.parts) == _MAX_FREE_TIER_PARTS + 1

    def test_a_free_account_stops_at_the_cap(self, plan):
        from kiln.assembly import _MAX_FREE_TIER_PARTS

        plan(LicenseTier.FREE)
        with pytest.raises(ValueError, match="Free tier limited"):
            _assembly_with(_MAX_FREE_TIER_PARTS + 1)


class TestPrinterBenchClosingLine:
    def test_pro_is_read_as_pro(self, plan):
        from kiln.plugins.printer_bench_tools import _has_pro

        plan(LicenseTier.PRO)
        assert _has_pro() is True

    def test_free_is_read_as_free(self, plan):
        from kiln.plugins.printer_bench_tools import _has_pro

        plan(LicenseTier.FREE)
        assert _has_pro() is False


class TestQrCodeWording:
    def _message(self) -> str:
        from kiln.emboss_generator import generate_qr_openscad_module

        with pytest.raises(ImportError) as caught:
            generate_qr_openscad_module("https://kiln3d.com")
        return str(caught.value)

    @pytest.mark.parametrize("tier", [LicenseTier.BUSINESS, LicenseTier.ENTERPRISE])
    def test_an_account_on_the_plan_is_never_told_to_upgrade(self, plan, tier):
        plan(tier)
        message = self._message()
        assert "upgrade" not in message.lower(), message
        assert "generate_qr_decoration" in message, message

    def test_below_the_plan_names_business(self, plan):
        plan(LicenseTier.PRO)
        message = self._message()
        assert "Business" in message, message
