"""Every door to Kiln's servers is on the roster, and speaks in one voice.

The question "what is an offline person told?" is answered when a door is
added, not found later.  Three pins:

1. ROSTER -- every module in the package that reaches Kiln's servers (the
   hosted URL, the served door) is named in ``served_answer.HOSTED_DOORS``
   with how it words a miss.  A new module that reaches the servers fails
   here until someone decides.
2. VOICE -- every sentence a ``served_answer`` door can produce, for every
   cause, reads in the one shape: no code inside it, no system word, the
   three anchors of the shape present, one distinct sentence per cause.
3. KIND -- every tool in the bundled paid-tool manifest resolves to a kind,
   so its offline sentence names what is on the line (a verdict not given
   is never a yes).  The private side's manifest gate refuses a tool it
   cannot place; this pins that the bundle never got ahead of that gate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiln import served_answer as sa

_SRC = Path(__file__).resolve().parents[1] / "src"
_PACKAGE = _SRC / "kiln"
_REACHES_THE_SERVERS = re.compile(r"_HOSTED_KILN_API_URL|KILN_API_URL|api\.kiln3d\.com|_pro_api_call\(")

# A sentence for a person: no wire code, no system word, the shape's anchors.
_CODE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
_SYSTEM_WORD = re.compile(
    r"\b(API|endpoint|bridge|kiln-pro|kiln_pro|hosted service|HTTP|urlopen|Errno|traceback|exception|"
    r"backoff|envelope|payload)\b",
    re.IGNORECASE,
)
_ANCHORS = ("Kiln can't", "right now because", ", so it")


def _modules_reaching_the_servers() -> set[str]:
    found: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _REACHES_THE_SERVERS.search(text):
            rel = path.relative_to(_SRC).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            found.add(".".join(parts))
    return found


class TestTheRoster:
    def test_every_module_that_reaches_the_servers_is_on_the_roster(self):
        reaching = _modules_reaching_the_servers()
        roster = set(sa.HOSTED_DOORS)
        missing = sorted(reaching - roster)
        assert not missing, (
            "these modules reach Kiln's servers and are not on served_answer.HOSTED_DOORS -- decide what an "
            f"offline person is told (served_answer / own_vocabulary / infrastructure) and add them: {missing}"
        )
        stale = sorted(roster - reaching)
        assert not stale, f"roster entries that no longer reach the servers: {stale}"

    def test_every_roster_entry_names_a_way_and_a_reason(self):
        for module, (how, note) in sa.HOSTED_DOORS.items():
            assert how in ("served_answer", "own_vocabulary", "infrastructure"), (module, how)
            assert isinstance(note, str) and len(note.split()) >= 3, (module, note)

    def test_a_door_that_words_a_miss_is_never_quietly_demoted(self):
        """Adding a served_answer door is free; taking one away is not.

        These three tell a person WHY a feature they asked for has no
        answer.  Moving one to ``infrastructure`` or ``own_vocabulary``
        would silence that sentence, and the silence is the whole defect
        this roster exists to prevent -- so the set may only grow.
        """
        served = {m for m, (how, _) in sa.HOSTED_DOORS.items() if how == "served_answer"}
        must_word_a_miss = {"kiln.server", "kiln._pro_motion_bridge", "kiln._pro_cutter_bridge"}
        demoted = sorted(must_word_a_miss - served)
        assert not demoted, (
            f"these doors must keep wording a miss through served_answer, and were moved: {demoted}. "
            "A person who asked for the feature learns nothing when they are demoted."
        )

    def test_a_door_that_claims_to_word_a_miss_actually_does(self):
        """The roster must not be able to lie.

        Saying ``served_answer`` in the roster is a claim that this module
        reaches the shared voice.  Without this, a door could be listed as
        wording a miss, word nothing, and still pass every other check
        here -- a roster that reads correct and protects nobody.
        """
        silent = []
        for module, (how, _note) in sa.HOSTED_DOORS.items():
            if how != "served_answer":
                continue
            path = _SRC.joinpath(*module.split(".")).with_suffix(".py")
            if not path.is_file():  # a package, not a module
                path = _SRC.joinpath(*module.split("."), "__init__.py")
            if "served_answer" not in path.read_text(encoding="utf-8", errors="replace"):
                silent.append(module)
        assert not silent, (
            f"these doors are listed as wording a miss but never reach the shared voice: {silent}. "
            "Either word the miss through it, or say on the roster what they really do."
        )


def _lint(text: str, *, where: str) -> None:
    assert text == " ".join(text.split()), f"{where}: stray whitespace: {text!r}"
    assert text.endswith("."), f"{where}: no full stop: {text!r}"
    assert not _CODE_TOKEN.search(text), f"{where}: a wire code inside the sentence: {text!r}"
    assert not _SYSTEM_WORD.search(text), f"{where}: a system word inside the sentence: {text!r}"
    for anchor in _ANCHORS:
        assert anchor in text, f"{where}: missing {anchor!r}: {text!r}"


def _misses() -> list[sa.Miss]:
    return [
        sa.Miss("offline", "SERVER_UNREACHABLE", "no route"),
        sa.Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED", "wall"),
        sa.Miss("unanswered", "SERVER_UNREACHABLE", "timed out"),
        sa.Miss("refused", "MACHINE_NOT_PAIRED", "This device has not reported that printer; register it and ask again."),
        sa.Miss("refused", "WEIRD_CODE", ""),
    ]


class TestTheVoice:
    @pytest.mark.parametrize("kind", sorted(sa.KINDS))
    def test_a_manifest_tool_of_every_kind(self, kind):
        seen = set()
        for miss in _misses():
            text = sa._tool_sentence("sample_tool", miss, kind)
            _lint(text, where=f"stub/{kind}/{miss.cause}")
            assert "sample_tool" in text
            seen.add(text)
        assert len(seen) == len(_misses())

    @pytest.mark.parametrize("verb", ["home", "park", "wipe", "purge"])
    def test_a_motion_door(self, verb):
        from kiln.printers.bambu import BambuAdapter

        on_the_line, cannot, wont = BambuAdapter._MOTION_WORDS[verb]
        remedy = BambuAdapter._WIPE_REMEDY if verb == "wipe" else BambuAdapter._JOG_REMEDY
        seen = set()
        for miss in _misses():
            text = sa.sentence(
                miss, feature="servers", on_the_line=on_the_line.format(model="bambu_a1"),
                cannot=cannot.format(model="bambu_a1"), wont=wont.format(model="bambu_a1"), safe_remedy=remedy,
            )
            _lint(text, where=f"motion/{verb}/{miss.cause}")
            assert "bambu_a1" in text
            seen.add(text)
            if verb == "purge":
                clause = sa.clause(miss, feature="servers", cannot=cannot.format(model="bambu_a1"), then="the next purge parks first")
                assert not _CODE_TOKEN.search(clause) and not _SYSTEM_WORD.search(clause)
                assert clause.startswith("Kiln can't") and not clause.endswith(".")
        assert len(seen) == len(_misses())

    def test_the_blade_line(self, monkeypatch):
        from kiln import _pro_cutter_bridge as cutter

        monkeypatch.setattr(cutter, "_last_miss", {})
        seen = set()
        for miss in _misses():
            cutter._last_miss["a1"] = miss
            gap = cutter.blade_unchecked("a1")
            _lint(gap["line"], where=f"blade/{miss.cause}")
            assert gap["why"] == miss.cause and gap["word"] == "unchecked"
            seen.add(gap["line"])
        assert len(seen) == len(_misses())

    def test_the_nozzle_line(self, monkeypatch):
        from kiln import _pro_nozzle_bridge as nozzle

        monkeypatch.setattr(nozzle, "_last_miss", {})
        for at in ("preflight", "start"):
            seen = set()
            for miss in _misses():
                nozzle._last_miss["a1"] = miss
                gap = nozzle.nozzle_unchecked("a1", at=at)
                _lint(gap["line"], where=f"nozzle/{at}/{miss.cause}")
                assert gap["why"] == miss.cause and gap["word"] == "unchecked"
                seen.add(gap["line"])
            assert len(seen) == len(_misses())

    def test_the_milestone_notices(self, tmp_path, monkeypatch):
        from kiln import nozzle_milestones as nm

        monkeypatch.setenv("KILN_HOME", str(tmp_path))
        for rung in sorted(nm.NOTICED):
            notice = nm.notice_for("a1", {"status": rung, "narrative": "62% of the brass budget on this filament",
                                          "nozzle_material": "brass", "nozzle_grams_through_before": 400.0})
            assert notice is not None, rung
            text = notice["line"]
            assert text == " ".join(text.split()) and text.endswith("."), text
            assert not _CODE_TOKEN.search(text) and not _SYSTEM_WORD.search(text), text
            assert "Your nozzle" in text and "62% of the brass budget" in text
            nm.forget("a1")

    def test_the_stage_link(self):
        from kiln import stage_link

        seen = set()
        for reason in ("offline", "signed_out", "unanswered", "transport", "http_503", "http_401", "http_403"):
            text = stage_link.refusal_sentence(reason)
            assert text.startswith("Kiln can't"), text
            assert not _CODE_TOKEN.search(text) and not _SYSTEM_WORD.search(text), text
            assert "right now (" in text and not text.endswith("."), text
            seen.add(text)
        assert len(seen) >= 4  # the four causes stay distinct

    def test_the_fallback_line_the_bambu_floor_keeps_for_a_reasonless_miss(self):
        from kiln.printers.bambu import BambuAdapter

        text = BambuAdapter._SERVED_LINE.format(model="bambu_a1")
        assert not _CODE_TOKEN.search(text) and not _SYSTEM_WORD.search(text)


class TestEveryBundledToolHasAKind:
    def test_the_bundled_manifest_resolves_whole(self):
        manifest = json.loads((_PACKAGE / "pro_tool_manifest.json").read_text(encoding="utf-8"))
        unplaced = []
        for tool in manifest.get("tools", []):
            name, category = tool.get("name", ""), tool.get("category")
            block = tool.get("offline") if isinstance(tool.get("offline"), dict) else {}
            kind = block.get("kind") if block.get("schema_version") == 1 else None
            if kind not in sa.KINDS and not sa.kind_of_tool(name, category):
                unplaced.append(f"{name}[{category}]")
        assert not unplaced, (
            "bundled tools with no offline kind -- the private manifest gate should have refused these; "
            f"place them by name in kiln_pro's override table or teach served_answer.kind_of_tool the prefix: {unplaced}"
        )

    def test_the_rules_place_the_kinds_they_claim(self):
        assert sa.kind_of_tool("check_skin_contact_suitability", "material_safety") == "verdict"
        assert sa.kind_of_tool("may_i_print", "fleet_operations") == "verdict"
        assert sa.kind_of_tool("generate_coaster", "product_generators") == "made"
        assert sa.kind_of_tool("record_completed_print", "print_intelligence") == "record"
        assert sa.kind_of_tool("list_designs", "design_versioning") == "read"
        assert sa.kind_of_tool("cloud_push_branch", "cloud_sync") == "sync"
        assert sa.kind_of_tool("fleet_pause", "fleet_operations") == "action"
        assert sa.kind_of_tool("maintenance_due", "fleet_operations") == "read"
        assert sa.kind_of_tool("", None) == "" and sa.kind_of_tool("zzz_nothing_like_it") == ""
        assert sa.story_for_tool("zzz_nothing_like_it")[1] == "did nothing"
