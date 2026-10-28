"""A printer is compared with the model it was set up as.

Until 2026-10-27 Kiln trusted the declared model completely.  Bambu Lab,
Prusa Link, Elegoo and Marlin printers already said what they are
(``PrinterAdapter.get_printer_info``), and nothing compared that with the
declaration: an owner who typed "Mark 3" for a "Mark 2" had every print
judged against the Mark 3's bed, with no word said.

The rule: the declaration still owns the limits.  A start is judged against
the bed of the printer that answered too, and refused only when the part
overruns that bed; a part that fits both starts.  The disagreement is said
at the setup doors, with the one command that settles it.

Each gate test drives the real start template and asserts on what reached
the printer.  Mutation pin: with the start gate and the setup doors put back
to main's code (this comparison module kept), the five refusal and door
tests fail; the tests expecting a start pass on both sides, as the guard that
a good print is never refused over the disagreement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.printer_model_check import MISMATCH_CODE, check_printer_model
from kiln.printers import print_gate as pg
from kiln.printers.base import PrinterAdapter, PrinterInfo, PrintResult

trimesh = pytest.importorskip("trimesh")


@pytest.fixture(autouse=True)
def _no_override_crosses_a_test():
    pg._oversize_grants.clear()
    yield
    pg._oversize_grants.clear()


class _Machine(PrinterAdapter):
    """A printer set up as *model* that reports itself as *reports*."""

    def __init__(self, model: str | None = None, reports: str | None = None) -> None:
        self.started: list[str] = []
        self._reports = reports
        if model:
            self.set_safety_profile(model)

    def get_printer_info(self) -> PrinterInfo | None:
        if self._reports is None:
            return None
        return PrinterInfo(model=self._reports, raw_model=self._reports.upper(), source="test")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")


_Machine.__abstractmethods__ = frozenset()


def _box(tmp_path: Path, size_mm: float) -> str:
    mesh = trimesh.creation.box(extents=[size_mm, size_mm / 4, 10.0])
    mesh.apply_translation([size_mm / 2, size_mm / 8, 5.0])
    path = tmp_path / f"part{int(size_mm)}.stl"
    mesh.export(str(path))
    return str(path)


class TestTheStartIsJudgedAgainstThePrinterThatAnswered:
    """kilntest_big has a 350 mm bed, kilntest_small a 120 mm one."""

    def test_a_part_too_big_for_the_reported_bed_is_refused(self, tmp_path):
        printer = _Machine("kilntest_big", reports="kilntest_small")
        result = printer.start_print(_box(tmp_path, 200.0))
        assert result.success is False
        assert result.refused_before_send is True
        assert getattr(result, "code", None) == MISMATCH_CODE
        assert printer.started == []
        assert "Kilntest Big 350" in result.message
        assert "Kilntest Small 120" in result.message
        assert "kiln set-model kilntest_small" in result.message

    def test_a_part_that_fits_both_beds_starts(self, tmp_path):
        printer = _Machine("kilntest_big", reports="kilntest_small")
        part = _box(tmp_path, 100.0)
        assert printer.start_print(part).success is True
        assert printer.started == [part]

    def test_a_printer_set_up_smaller_than_it_reports_is_not_refused(self, tmp_path):
        """The declared bed is the smaller one, so the ordinary check already
        holds the start to it; the larger report adds nothing to refuse."""
        printer = _Machine("kilntest_small", reports="kilntest_big")
        part = _box(tmp_path, 100.0)
        assert printer.start_print(part).success is True

    def test_a_printer_that_agrees_with_its_setup_starts_as_before(self, tmp_path):
        printer = _Machine("kilntest_big", reports="kilntest_big")
        part = _box(tmp_path, 200.0)
        assert printer.start_print(part).success is True

    def test_a_printer_that_reports_nothing_starts_as_before(self, tmp_path):
        printer = _Machine("kilntest_big", reports=None)
        part = _box(tmp_path, 200.0)
        assert printer.start_print(part).success is True

    def test_the_human_override_lets_one_start_through(self, tmp_path):
        printer = _Machine("kilntest_big", reports="kilntest_small")
        part = _box(tmp_path, 200.0)
        pg.grant_oversize_override("kilntest_big", ttl_seconds=60)
        assert printer.start_print(part).success is True
        assert printer.start_print(part).success is False  # single use


class TestTheComparison:
    def test_the_same_printer_spelled_another_way_matches(self):
        check = check_printer_model(_Machine("Kilntest Big 350", reports="kilntest_big"))
        assert check.status == "match"
        assert check.sentence() == ""

    def test_a_printer_kiln_has_no_record_for_is_told_what_it_reports(self):
        check = check_printer_model(_Machine("acme_mark_3", reports="kilntest_small"))
        assert check.status == "differs"
        assert check.declared_row is None
        sentence = check.sentence("shop")
        assert "generic safe limits" in sentence
        assert "kiln set-model kilntest_small --printer shop" in sentence

    def test_a_door_about_to_change_the_model_compares_the_new_one(self):
        printer = _Machine("kilntest_big", reports="kilntest_small")
        assert check_printer_model(printer, declared="kilntest_small").status == "match"

    @pytest.mark.parametrize(
        ("model", "reports", "why"),
        [
            (None, "kilntest_small", "no model was declared"),
            ("kilntest_big", None, "the printer reports no model"),
            ("kilntest_big", "Acme Mark 9", "its report names no catalogue row"),
        ],
    )
    def test_nothing_is_claimed_when_either_side_cannot_say(self, model, reports, why):
        check = check_printer_model(_Machine(model, reports=reports))
        assert check.status == "unchecked"
        assert check.why == why

    def test_a_report_that_cannot_be_read_claims_nothing(self):
        class _Broken(_Machine):
            def get_printer_info(self):
                raise RuntimeError("no answer")

        assert check_printer_model(_Broken("kilntest_big")).status == "unchecked"


class TestTheSetupDoorsSayIt:
    def test_setting_a_model_the_live_printer_contradicts_says_so(self, monkeypatch):
        import kiln.server as srv
        from kiln.registry import PrinterRegistry

        registry = PrinterRegistry()
        registry.register("shop", _Machine("kilntest_small", reports="kilntest_small"))
        monkeypatch.setattr(srv, "_registry", registry)
        result = srv._with_printer_report(
            {"success": True, "printer": "shop", "printer_model": "kilntest_big", "message": "shop is now set up."}
        )
        assert result["printer_reports"]["reported_row"] == "kilntest_small"
        assert "reports itself as the Kilntest Small 120" in result["message"]
        assert "kiln set-model kilntest_small --printer shop" in result["message"]

    def test_a_printer_this_server_is_not_connected_to_is_left_alone(self, monkeypatch):
        import kiln.server as srv
        from kiln.registry import PrinterRegistry

        monkeypatch.setattr(srv, "_registry", PrinterRegistry())
        given = {"success": True, "printer": "away", "printer_model": "kilntest_big", "message": "ok"}
        assert srv._with_printer_report(given) == given

    @staticmethod
    def _register(monkeypatch, *, answered: bool) -> tuple[dict, list[str]]:
        import kiln.server as srv
        from kiln.printers.moonraker import MoonrakerAdapter
        from kiln.registry import PrinterRegistry

        asked: list[str] = []

        def reports(self):
            asked.append("asked")
            return PrinterInfo(model="voron_0", raw_model="Voron 0", source="test")

        monkeypatch.setattr(srv, "_registry", PrinterRegistry())
        monkeypatch.setattr(srv, "_check_printer_url", lambda host, **_kw: (host, [], answered))
        monkeypatch.setattr(MoonrakerAdapter, "get_printer_info", reports)
        result = srv.register_printer(
            name="shelf",
            printer_type="moonraker",
            host="http://klipper.local",
            printer_model="Voron 2.4",
            printer_make="Voron",
        )
        return result, asked

    def test_registering_a_printer_that_reports_another_model_warns(self, monkeypatch):
        result, _asked = self._register(monkeypatch, answered=True)
        assert result["success"] is True
        warning = "\n".join(result.get("warnings", []))
        assert "reports itself as the Voron 0 / 0.2" in warning
        assert "kiln set-model voron_0 --printer shelf" in warning

    def test_a_printer_that_did_not_answer_is_not_asked(self, monkeypatch):
        """An offline printer would cost the door a timeout; the start gate
        asks at the next start instead."""
        result, asked = self._register(monkeypatch, answered=False)
        assert result["success"] is True
        assert asked == []
        assert "reports itself" not in "\n".join(result.get("warnings", []))
