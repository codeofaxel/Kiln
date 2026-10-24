"""``firmware_resume_print`` is Kiln Pro, and its refusal says what the printer can do itself.

Resuming a print where it stopped is Kiln Pro (https://kiln3d.com/pricing),
as the pricing page says and as the five other resume doors already were.
A free plan is told, second in the refusal and before any pricing, the one
thing the printer can do on its own: Marlin's power-loss recovery continues
a print run from the printer's own memory card, and never one OctoPrint
streamed over USB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import kiln.server as srv

_ARGS = {
    "printer_name": "ender", "job_id": "j1", "z_height_mm": 12.0,
    "hotend_temp_c": 210.0, "bed_temp_c": 60.0, "file_name": "part.gcode",
}


def _plan(monkeypatch, paid: bool) -> None:
    """The caller's plan: kiln-pro's gate where it is installed, the signed-in
    account's plan on a plain install."""
    monkeypatch.setattr("kiln.licensing.check_tier", lambda *_a, **_k: (paid, None if paid else "needs Kiln Pro"))
    monkeypatch.setattr("kiln.account_plan._covers", lambda _required: paid)


class _Printer:
    name = "octoprint"

    def __init__(self) -> None:
        self.resumed: list[dict] = []

    def firmware_resume_print(self, **kwargs):
        self.resumed.append(kwargs)
        return SimpleNamespace(message="positioned")


@pytest.fixture
def printer(monkeypatch):
    printer = _Printer()
    monkeypatch.setattr(srv, "_get_registry", lambda: SimpleNamespace(get=lambda _name: printer))
    monkeypatch.setattr(srv, "_check_auth", lambda _scope: None)
    monkeypatch.setattr(srv, "_emergency_latch_error", lambda *_a, **_k: None)
    return printer


def test_a_free_plan_is_refused_and_told_what_its_printer_can_do(monkeypatch, printer):
    _plan(monkeypatch, False)
    out = srv.firmware_resume_print(**_ARGS)
    assert out["success"] is False and out["code"] == "TIER_REQUIRED" and out["required_tier"] == "pro"
    assert printer.resumed == [], "a refused resume reached the printer"
    said = out["error"]
    assert "own memory card" in said and "OctoPrint sent over USB" in said
    # What the person can do now comes before the pricing.
    assert said.index("own memory card") < said.index("kiln3d.com/pricing")
    assert out["without_it"].startswith("Without it:")


def test_a_pro_plan_resumes(monkeypatch, printer):
    _plan(monkeypatch, True)
    out = srv.firmware_resume_print(**_ARGS)
    assert out["success"] is True and out["resumed_at_z"] == 12.0
    assert printer.resumed and printer.resumed[0]["z_height_mm"] == 12.0
    assert "without_it" not in out


def test_a_refusal_that_is_not_about_the_plan_is_left_alone():
    """A sign-in that ended or a plan Kiln could not confirm is not the plan's
    refusal: that person may own Kiln Pro, so nothing is added."""

    @srv._says_what_to_do_without("Without it: do this")
    def gated():
        return {"success": False, "code": "SIGN_IN_AGAIN", "error": "Sign back in. Then try again."}

    assert gated() == {"success": False, "code": "SIGN_IN_AGAIN", "error": "Sign back in. Then try again."}
