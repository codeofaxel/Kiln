"""``repair_guide`` -- the maker's own repair guide, one step at a time, at every door.

Born 2026-09-17 from the A1 cutter reseat: an agent found Bambu's own guide,
showed the maker's numbered-arrow photos one step per message, and the
owner said "if Kiln can do this for all its printers, that would be
awesome".  This is that, as a tool: the maker's power-off warning first,
then the maker's steps in the maker's words with the maker's picture (a
URL, attributed -- never copied), the maker's own check at the end.

The guides live in kiln-pro; public Kiln keeps the mechanism and is pinned
here against a stub table shaped exactly like kiln-pro's (the A1 / A1 mini
filament-cutter page, read in the browser on 2026-09-17).  Two conditions:

* kiln-pro present (the bridge answers): plan, step 0, the power-off gate,
  every step with its picture and attribution, the next step, the doctor
  count, the one sentence ``troubleshoot_printer`` adds;
* public only (the bridge is silent): the maker's own maintenance index
  where public Kiln knows it, else "your maker publishes no guide Kiln can
  step through" -- never a generic procedure dressed as the maker's.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import kiln._pro_guide_bridge as guide_bridge
from kiln.plugins import repair_guide_tools as rg

SLUG = "bambu:a1-mini/maintenance/filament-cutter-replacement"
SOURCE = "https://wiki.bambulab.com/en/a1-mini/maintenance/filament-cutter-replacement"
IMG = "https://wiki.bambulab.com/a1m/replace-filament-cutter/"
POWER_OFF = (
    "It's crucial to power off the printer before performing any maintenance work on the printer "
    "and its electronics, including tool head wires, because leaving the printer on while conducting "
    "such tasks can cause a short circuit, which can lead to additional electronic damage and safety hazards."
)
BLADE = "Important! The blade has sharp corners and there is a risk of cutting yourself. Please proceed with caution!"

#: The cutter page as kiln-pro's table carries it (schema of 2026-09-17).
CUTTER_GUIDE: dict[str, Any] = {
    "vendor": "bambu",
    "source_url": SOURCE,
    "source_title": "Filament Cutter Replacement Guide",
    "fetched_at": "2026-09-17",
    "model_ids": ["bambu_a1_mini", "bambu_a1"],
    "machine": "A1 mini / A1",
    "applies_also_to": "Please note that this guide also applies to the A1 3D printer.",
    "topic": "cutter",
    "fixes_codes": [],
    "tools": ["H2.0 Allen key"],
    "interval": "We recommend replacing the filament cutter every 10-15 spools of filament used (or quicker) to ensure the cutting performance is unaffected.",
    "parts": ["Bambu Lab Filament Cutter for A1 Series (spares included in the box at purchase)"],
    "power_off_warning": POWER_OFF,
    "warnings": ["The blade has sharp corners and there is a risk of cutting yourself. Please proceed with caution!"],
    "steps": [
        {"n": 1, "title": "Remove the toolhead front cover",
         "do": "Grab the base of the front cover and gently pull towards you. The clips holding the cover in place will be released, allowing you to remove the front cover.",
         "image_url": IMG + "remove_the_print_head_front_cover.jpeg",
         "image_alt": "the toolhead front cover, base pulled toward the viewer to release the clips", "verify": None},
        {"n": 2, "title": "Release the filament cutter lever",
         "do": "Start by holding the filament cutter pressed to allow for easy removal of the single screw holding it in place. Keep holding the lever pressed until the screw is completely removed, then gently release the cutter.",
         "image_url": IMG + "press_the_filament_cutter_lever_and_remove_the_screw.jpeg",
         "image_alt": "the cutter lever held pressed while the single retaining screw is driven out", "verify": None},
        {"n": 3, "title": "Remove the filament cutter blade",
         "do": "Start by gently pushing the blade in the direction indicated in the image below, then remove it upwards.",
         "image_url": IMG + "remove_the_filament_cutter_blade_from_lever.jpeg",
         "image_alt": "the blade pushed along the lever slot, then lifted out upwards",
         "warning": BLADE, "verify": None},
        {"n": 4, "title": "Optional: Clean the extruder gear",
         "do": "We also recommend cleaning the metallic extruder gear, and the yellow gear before proceeding with installing the new filament cutter.",
         "no_image": True, "verify": None},
        {"n": 5, "title": "Install the toolhead front cover",
         "do": "Attach it on the top of the toolhead by aligning the clips, then gently push on the bottom side. You will hear the clips when the installation is complete.",
         "image_url": IMG + "attach_the_front_cover.jpeg",
         "image_alt": "the front cover hooked on at the top of the toolhead and pressed home at the bottom",
         "verify": "press the filament cutter lever a few times, and ensure the motion is smooth and the lever returns to the initial position"},
    ],
}

#: A guide the maker runs with the printer ON (a cold pull): no power-off sentence.
POWERED_GUIDE: dict[str, Any] = {
    "vendor": "prusa",
    "source_url": "https://help.prusa3d.com/article/cold-pull_2075",
    "source_title": "Cold pull",
    "fetched_at": "2026-09-17",
    "model_ids": ["prusa_mk4"],
    "machine": "MK4S/MK3.9S",
    "applies_also_to": None,
    "topic": "nozzle",
    "fixes_codes": [],
    "tools": [],
    "interval": None,
    "parts": [],
    "power_off_warning": None,
    "warnings": ["The nozzle is hot."],
    "steps": [
        {"n": 1, "title": "Heat the nozzle", "do": "Preheat the nozzle.", "no_image": True, "verify": None},
        {"n": 2, "title": "Pull", "do": "Pull the filament out in one motion.", "no_image": True, "verify": None},
    ],
}

#: A second cutter page the maker publishes for the A1 only (the lever, not the blade).
LEVER_SLUG = "bambu:a1/maintenance/filament_cutter_lever_replacement"
LEVER_GUIDE: dict[str, Any] = {
    **CUTTER_GUIDE,
    "source_url": "https://wiki.bambulab.com/en/a1/maintenance/filament_cutter_lever_replacement",
    "source_title": "Filament Cutter Lever Replacement",
    "model_ids": ["bambu_a1"],
    "machine": "A1",
    "applies_also_to": None,
    "steps": [{"n": 1, "title": "Remove the lever", "do": "Remove the lever.", "no_image": True, "verify": None}],
}

TABLE = {SLUG: CUTTER_GUIDE, LEVER_SLUG: LEVER_GUIDE, "prusa:article/cold-pull_2075": POWERED_GUIDE}
CODE_MAP = {"12008001": (SLUG, 1)}


class _StubReader:
    """kiln-pro's reader, over the stub table, with the same four calls the bridge makes."""

    def _mine(self, printer_id):
        key = printer_id.lower().replace("-", "_")
        return {s: g for s, g in TABLE.items() if key in g["model_ids"]}

    def find_guide(self, printer_id, *, topic="", code="", guide=""):
        mine = self._mine(printer_id)
        if guide and guide in mine:
            return guide, mine[guide], 1
        if code:
            mapped = self.guide_for_code(code)
            if mapped and mapped[0] in mine:
                return mapped[0], mine[mapped[0]], mapped[1]
        want = topic.strip().lower()
        matches = [(slug, g) for slug, g in sorted(mine.items()) if want and want == g["topic"]]
        if len(matches) == 1:
            return matches[0][0], matches[0][1], 1
        return matches or None

    def section_of(self, slug):
        return slug.split(":", 1)[-1].split("/", 1)[0]

    def guide_for_caller(self, slug):
        return dict(TABLE[slug]) if slug in TABLE else None

    def guide_for_code(self, code, *, namespace=None):
        hex_only = "".join(c for c in code.split()[0].upper() if c in "0123456789ABCDEF") if code else ""
        return CODE_MAP.get(hex_only)

    def coverage(self, printer_id):
        mine = self._mine(printer_id)
        return {
            "maker": "Bambu Lab" if printer_id.startswith("bambu") else ("Prusa Research" if mine else None),
            "count": len(mine),
            "topics": sorted({g["topic"] for g in mine.values()}),
            "publishes": "photo_guides_per_model" if printer_id.startswith(("bambu", "prusa", "creality")) else None,
            "publishes_note": None,
            "maker_guides_in_table": None,
        }


@pytest.fixture
def with_pro(monkeypatch):
    """kiln-pro present: the bridge reads the stub table."""
    monkeypatch.setattr(guide_bridge, "_reader", lambda: _StubReader())


@pytest.fixture
def without_pro(monkeypatch):
    """A public-only install: the bridge is silent."""
    monkeypatch.setattr(guide_bridge, "_reader", lambda: None)


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)


def _steps_of(answer):
    return [s["step"] for s in answer["steps"]]


# ---------------------------------------------------------------------------
# 1. kiln-pro present: the walkthrough
# ---------------------------------------------------------------------------


class TestPlan:
    def test_the_plan_is_the_header_and_the_titles_and_serves_no_step(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", plan_only=True)
        assert out["success"] is True
        assert out["maker"] == "Bambu Lab"
        assert out["guide"] == SLUG
        assert out["maker_page_title"] == "Filament Cutter Replacement Guide"
        assert out["maker_page_url"] == SOURCE
        assert out["applies_also_to"] == "Please note that this guide also applies to the A1 3D printer."
        assert out["tools"] == ["H2.0 Allen key"]
        assert "10-15 spools" in out["interval"]
        assert out["parts"] == CUTTER_GUIDE["parts"]
        assert out["printer_must_be_off"] is True
        assert out["step_zero"]["text"] == POWER_OFF
        assert _steps_of(out) == [1, 2, 3, 4, 5]
        assert [s["title"] for s in out["steps"]][0] == "Remove the toolhead front cover"
        # Nothing of a step's body is served by the plan.
        assert "do" not in out and "image_url" not in out
        # The person hears plain words; the argument name is for the agent only.
        assert "power_off_confirmed" not in out["how_to_step"]
        assert "say so" in out["how_to_step"]
        assert "power_off_confirmed=True" in out["agent_note"]

    def test_the_plan_says_the_pictures_are_the_makers_and_not_copied(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", plan_only=True)
        assert "Bambu Lab" in out["image_note"] and "cache" in out["image_note"]

    def test_the_a1_gets_the_same_guide_the_page_says_it_applies_to(self, with_pro):
        out = rg.repair_guide("bambu_a1", guide=SLUG, plan_only=True)
        assert out["guide"] == SLUG and out["printer_id"] == "bambu_a1"


class TestMoreThanOneGuideMatches:
    def test_two_pages_for_one_topic_are_listed_never_picked(self, with_pro):
        out = rg.repair_guide("bambu_a1", topic="cutter", plan_only=True)
        assert out["success"] is True and out["guide"] is None
        assert sorted(c["guide"] for c in out["choices"]) == sorted([LEVER_SLUG, SLUG])
        by_slug = {c["guide"]: c for c in out["choices"]}
        assert by_slug[LEVER_SLUG]["section"] == "a1" and by_slug[SLUG]["section"] == "a1-mini"
        assert by_slug[SLUG]["applies_also_to"] == CUTTER_GUIDE["applies_also_to"]
        assert by_slug[SLUG]["machine"] == "A1 mini / A1" and by_slug[LEVER_SLUG]["machine"] == "A1"
        assert "guide=" in out["kiln_note"]
        assert "steps" not in out and "step_zero" not in out

    def test_the_chosen_slug_selects_it_at_every_door(self, with_pro):
        out = rg.repair_guide("bambu_a1", guide=LEVER_SLUG, step=1, power_off_confirmed=True)
        assert out["guide"] == LEVER_SLUG and out["do"] == "Remove the lever."
        from click.testing import CliRunner

        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["machine", "repair-guide", "--guide", LEVER_SLUG, "--printer-id", "bambu_a1", "--plan", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output[result.output.index("{"):])["data"]["guide"] == LEVER_SLUG

    def test_a_code_still_picks_its_guide_without_asking(self, with_pro):
        out = rg.repair_guide("bambu_a1", hms_code="1200-8001", plan_only=True)
        assert out["guide"] == SLUG and "choices" not in out

    def test_a_slug_for_another_model_is_not_borrowed(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", guide=LEVER_SLUG, topic="cutter", plan_only=True)
        assert out["guide"] == SLUG


class TestStepZeroIsTheMakersWarning:
    def test_step_zero_is_the_power_off_sentence_verbatim(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=0)
        assert out["step"] == 0
        assert out["text"] == POWER_OFF
        assert out["maker_words"] is True
        assert out["printer_must_be_off"] is True
        assert out["also"] == CUTTER_GUIDE["warnings"]
        assert out["next_step"]["step"] == 1
        assert "power_off_confirmed" not in out["next_step"]["requires"]
        assert "power_off_confirmed=True" in out["next_step"]["agent_note"]

    def test_the_default_call_is_step_zero(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter")
        assert out["step"] == 0 and out["text"] == POWER_OFF

    @pytest.mark.parametrize("step", [1, 3, 5])
    def test_no_step_is_reached_without_a_persons_power_off_word(self, with_pro, step):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=step)
        assert out["success"] is False
        assert out["error"]["code"] == "POWER_OFF_REQUIRED"
        # The refusal itself serves step 0, so the warning is read before
        # anything is unscrewed however the door was knocked on.
        assert POWER_OFF in out["error"]["message"]
        assert "power_off_confirmed" not in out["error"]["message"]
        assert "power_off_confirmed=True" in out["agent_note"]
        assert out["step_zero"]["text"] == POWER_OFF
        assert out["power_off_required"] is True

    def test_a_powered_job_does_not_invent_a_power_off_and_says_whose_words(self, with_pro):
        out = rg.repair_guide("prusa_mk4", topic="nozzle", step=0)
        assert out["printer_must_be_off"] is False
        assert out["maker_words"] is False
        assert "Kiln's own note" in out["text"] and "no power-off sentence" in out["text"]
        assert out["also"] == ["The nozzle is hot."]
        # Its steps need no power-off word, and step 0 rides with each one.
        step = rg.repair_guide("prusa_mk4", topic="nozzle", step=1)
        assert step["success"] is True and step["do"] == "Preheat the nozzle."
        assert step["step_zero"]["also"] == ["The nozzle is hot."]


class TestOneStepPerCall:
    def test_a_step_is_the_makers_words_the_makers_picture_and_the_next_step(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=1, power_off_confirmed=True)
        assert out["success"] is True
        assert out["step"] == 1
        assert out["title"] == "Remove the toolhead front cover"
        assert out["do"] == CUTTER_GUIDE["steps"][0]["do"]
        assert out["image_url"] == IMG + "remove_the_print_head_front_cover.jpeg"
        assert out["image_alt"] == CUTTER_GUIDE["steps"][0]["image_alt"]
        assert out["image_attribution"] == f"Image: Bambu Lab, {SOURCE}"
        assert out["next_step"] == {"step": 2, "title": "Release the filament cutter lever"}
        assert out["done"] is False
        # The header rides with every step, so the printer is named off and the page cited.
        assert out["printer_must_be_off"] is True and out["maker_page_url"] == SOURCE
        # No other step's words leak into this one.
        assert "screw" not in out["do"]

    def test_every_pictured_step_carries_the_attribution_line(self, with_pro):
        for n in (1, 2, 3, 5):
            out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=n, power_off_confirmed=True)
            assert out["image_url"].startswith("https://wiki.bambulab.com/")
            assert out["image_attribution"] == f"Image: Bambu Lab, {SOURCE}"
            assert out["image_alt"]

    def test_the_makers_inline_warning_rides_with_its_step(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=3, power_off_confirmed=True)
        assert out["warning"] == BLADE

    def test_kilns_words_appear_only_where_the_picture_cannot_say_it(self, with_pro):
        pictured = rg.repair_guide("bambu_a1_mini", topic="cutter", step=1, power_off_confirmed=True)
        assert "kiln_note" not in pictured
        unpictured = rg.repair_guide("bambu_a1_mini", topic="cutter", step=4, power_off_confirmed=True)
        assert unpictured["no_image"] is True and "image_url" not in unpictured
        assert "no picture" in unpictured["kiln_note"]

    def test_the_last_step_ends_with_the_makers_own_check(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=5, power_off_confirmed=True)
        assert out["done"] is True and out["next_step"] is None
        assert out["verify"].startswith("press the filament cutter lever a few times")

    def test_a_step_past_the_end_is_refused_by_name(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=9, power_off_confirmed=True)
        assert out["success"] is False and out["error"]["code"] == "INVALID_INPUT"
        assert "steps 0 to 5" in out["error"]["message"]

    def test_a_person_partway_in_skips_ahead_with_step_and_kiln_never_assumes(self, with_pro):
        # The plan starts at 1 even though the code maps to the guide: where
        # the person is ("the screw is already out") is theirs to say.
        plan = rg.repair_guide("bambu_a1_mini", hms_code="1200-8001", plan_only=True)
        assert plan["start_at_step"] == 1 and plan["hms_code"] == "12008001"
        out = rg.repair_guide("bambu_a1_mini", hms_code="1200-8001", step=3, power_off_confirmed=True)
        assert out["step"] == 3 and out["title"] == "Remove the filament cutter blade"


class TestACodeFindsTheGuide:
    def test_hms_code_maps_to_the_guide(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", hms_code="1200-8001 290420", plan_only=True)
        assert out["guide"] == SLUG and out["hms_code"] == "12008001"

    def test_a_code_written_in_topic_is_read_as_one(self, with_pro):
        out = rg.repair_guide("bambu_a1", topic="1200-8001", plan_only=True)
        assert out["guide"] == SLUG and "choices" not in out

    def test_a_code_that_maps_to_another_models_guide_does_not_borrow_it(self, with_pro):
        out = rg.repair_guide("prusa_mk4", hms_code="1200-8001", plan_only=True)
        assert out["guide"] is None


class TestNoGuideIsAnswered:
    def test_a_topic_the_table_lacks_names_what_the_maker_does_have(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="belts")
        assert out["success"] is True and out["guide"] is None
        assert out["coverage"]["count"] == 1 and out["coverage"]["topics"] == ["cutter"]
        assert "none for this topic" in out["kiln_note"]

    def test_nothing_to_repair_is_asked_for(self, with_pro):
        out = rg.repair_guide("bambu_a1_mini")
        assert out["success"] is False and out["error"]["code"] == "INVALID_INPUT"

    def test_no_printer_is_asked_for(self, with_pro, monkeypatch):
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)
        out = rg.repair_guide(topic="cutter")
        assert out["success"] is False and out["error"]["code"] == "PRINTER_MODEL_REQUIRED"

    def test_the_named_printers_declared_model_is_used(self, with_pro, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "kiln.printer_model_resolver.resolve_printer_model_for",
            lambda name: seen.append(name) or "A1 mini",
        )
        out = rg.repair_guide(topic="cutter", printer_name="shop", plan_only=True)
        assert seen == ["shop"]
        assert out["printer_id"] == "bambu_a1_mini" and out["guide"] == SLUG


# ---------------------------------------------------------------------------
# 2. public only: honest, and a funnel, never a leak
# ---------------------------------------------------------------------------


class TestPublicOnly:
    def test_the_makers_own_index_is_the_answer_where_kiln_knows_it(self, without_pro):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter")
        assert out["success"] is True and out["guide"] is None
        assert out["maker"] == "Bambu Lab"
        assert out["maker_index_url"] == "https://wiki.bambulab.com/en/a1-mini/maintenance"
        assert rg.NO_GUIDE_SENTENCE in out["kiln_note"]
        assert out["coverage"] is None

    def test_a_maker_kiln_has_no_index_for_gets_the_sentence_alone(self, without_pro):
        out = rg.repair_guide("creality_k1", topic="nozzle")
        assert out["kiln_note"] == rg.NO_GUIDE_SENTENCE + "."
        assert "maker_index_url" not in out

    def test_a_model_without_its_own_index_falls_back_to_the_makers_landing_page(self, without_pro):
        out = rg.repair_guide("bambu_a2l", topic="nozzle")
        assert out["maker_index_url"] == "https://wiki.bambulab.com/en/home"

    def test_the_public_map_points_only_at_the_makers_own_hosts(self):
        hosts = {"bambu": "https://wiki.bambulab.com/", "prusa": "https://help.prusa3d.com/"}
        for vendor, table in rg.MAKER_MAINTENANCE_INDEX.items():
            for key, url in table.items():
                if key == "_maker":
                    continue
                assert url.startswith(hosts[vendor]), (vendor, key, url)

    def test_public_kiln_carries_no_step_and_fetches_no_picture(self):
        src = inspect.getsource(rg)
        for fetcher in ("urllib", "requests", "httpx", "urlopen"):
            assert fetcher not in src, f"the plugin must not fetch or proxy pictures ({fetcher})"
        # No step body of any maker's guide lives in public Kiln.
        assert "Grab the base of the front cover" not in src
        assert "/a1m/replace-filament-cutter/" not in src

    def test_the_bridge_is_silent_without_kiln_pro(self, without_pro):
        assert guide_bridge.find_guide("bambu_a1_mini", topic="cutter") is None
        assert guide_bridge.guide_for_code("12008001") is None
        assert guide_bridge.coverage("bambu_a1_mini") is None


# ---------------------------------------------------------------------------
# 3. the other doors
# ---------------------------------------------------------------------------


class TestCliDoor:
    def test_kiln_repair_runs_the_same_tool(self, with_pro):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["machine", "repair-guide", "cutter", "--printer-id", "bambu_a1_mini", "--plan", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{"):])
        assert payload["status"] == "success"
        assert payload["data"]["guide"] == SLUG and payload["data"]["step_zero"]["text"] == POWER_OFF

    def test_kiln_repair_step_needs_the_power_off_flag_and_exits_nonzero_without_it(self, with_pro):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        refused = CliRunner().invoke(cli, ["machine", "repair-guide", "cutter", "--printer-id", "bambu_a1_mini", "--step", "2"])
        assert refused.exit_code == 1 and "power off" in refused.output.lower()
        assert "power_off_confirmed" not in refused.output
        served = CliRunner().invoke(
            cli, ["machine", "repair-guide", "1200-8001", "--printer-id", "bambu_a1_mini", "--step", "2", "--power-off-confirmed", "--json"],
        )
        assert served.exit_code == 0, served.output
        payload = json.loads(served.output[served.output.index("{"):])
        assert payload["data"]["step"] == 2 and payload["data"]["image_attribution"].startswith("Image: Bambu Lab, ")

    def test_the_cli_door_calls_the_shared_runtime_config(self):
        from kiln.cli import main as cli_main

        assert "ensure_runtime_config()" in inspect.getsource(cli_main.repair_guide_cmd.callback)


class _DoctorAdapter:
    name = "bambu"

    def __init__(self, declared="bambu_a1_mini"):
        self._printer_model = declared

    def declared_printer_model(self):
        return self._printer_model


class TestDoctorDoor:
    def test_the_line_counts_the_makers_guides_where_kiln_pro_has_them(self, with_pro):
        detail, warn = rg.coverage_line("bambu_a1_mini")
        assert warn is False
        assert detail.startswith("Bambu Lab: 1 guide(s) for this model")
        assert "cutter" in detail and "kiln machine repair-guide" in detail

    def test_the_line_is_honest_without_kiln_pro(self, without_pro):
        detail, warn = rg.coverage_line("bambu_a1_mini")
        assert warn is True
        assert rg.NO_GUIDE_SENTENCE in detail
        assert "https://wiki.bambulab.com/en/a1-mini/maintenance" in detail
        assert "Kiln Pro" in detail

    def test_the_line_is_honest_for_a_maker_with_nothing(self, with_pro):
        detail, warn = rg.coverage_line("voron_trident")
        assert warn is True and detail == rg.NO_GUIDE_SENTENCE

    def test_kiln_doctor_reports_the_capability(self, with_pro, monkeypatch):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.cli.main.load_printer_config",
                            lambda *a, **k: {"name": "default", "type": "bambu", "host": "192.168.1.6"})
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda *a, **k: _DoctorAdapter())
        output = CliRunner().invoke(cli, ["doctor", "--json"]).output
        data = json.loads(output[output.index("{"):])
        check = next((c for c in data["checks"] if c["name"] == "repair_guides"), None)
        assert check is not None, [c["name"] for c in data["checks"]]
        assert check["warn"] is False and check["detail"].startswith("Bambu Lab: 1 guide(s)")


class TestTroubleshootCrossLink:
    def test_a_mapped_code_adds_one_sentence_naming_the_door(self, with_pro):
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code="1200-8001")
        assert out["hms_code"] == "1200_8001"
        line = out["repair_guide_next_step"]
        assert line.startswith("Kiln can walk you through the maker's guide")
        assert "repair_guide(" in line and "step=1" in line and "power-off" in line

    def test_no_sentence_without_kiln_pro(self, without_pro):
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code="1200-8001")
        assert "repair_guide_next_step" not in out

    def test_no_sentence_for_a_code_no_guide_claims(self, with_pro):
        import kiln.server as srv

        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code="1200-8007")
        assert "repair_guide_next_step" not in out


class TestRegistration:
    def test_classified_read_only_in_tool_safety(self):
        data = json.loads((Path(inspect.getfile(rg)).parent.parent / "data" / "tool_safety.json").read_text())
        assert data["classifications"]["repair_guide"] == {"level": "safe"}

    def test_the_plugin_registers_one_read_only_tool(self):
        seen: list[tuple[Any, Any]] = []

        class _Mcp:
            def tool(self, **kwargs):
                def register(fn):
                    seen.append((fn, kwargs.get("annotations")))
                    return fn
                return register

        rg.plugin.register(_Mcp())
        assert [fn.__name__ for fn, _ in seen] == ["repair_guide"]
        assert seen[0][1].readOnlyHint is True

    def test_the_plugin_is_discovered_beside_homing_tools(self):
        from kiln import plugin_loader

        names = {m for _f, m, _p in __import__("pkgutil").iter_modules(
            [str(Path(inspect.getfile(plugin_loader)).parent / "plugins")])}
        assert {"homing_tools", "repair_guide_tools"} <= names

    def test_the_tool_never_talks_to_the_printer(self):
        # A table read, not a printer command: no adapter, no engagement gate,
        # no rate limit, no confirmation -- and so nothing to refuse while
        # a print runs.  Pinned so a future edit that reaches for the
        # adapter has to say why.
        src = inspect.getsource(rg)
        assert "_resolve_control_target" not in src and "send_gcode" not in src


def test_the_stub_table_matches_kiln_pros_shape():
    """Every key the plugin reads exists on the stub, spelled as the table spells it."""
    keys = {"vendor", "source_url", "source_title", "fetched_at", "model_ids", "machine", "applies_also_to", "topic",
            "fixes_codes", "tools", "interval", "parts", "power_off_warning", "warnings", "steps"}
    assert set(CUTTER_GUIDE) == keys
    for step in CUTTER_GUIDE["steps"]:
        assert ("image_url" in step) != step.get("no_image", False)
        if "image_url" in step:
            assert step["image_alt"]
