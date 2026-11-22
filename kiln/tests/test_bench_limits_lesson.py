"""The printer session's second lesson: what this printer can really print.

Run through the registered ``printer_bench`` tool, one call per ask, the way
an agent drives it: Kiln's servers make the test print (the served wire,
stood in at :mod:`kiln._pro_limits_bridge` with replies in the wire's own
shape), the sliced files go back before anything is asked, the person
prints each plate through the normal door, and reads the numbers off it.
The courier itself is driven against the real served door and the real
file sending, with the network stood in.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln import _pro_limits_bridge as bridge
from kiln import bench, served_answer
from tests.test_bench import _Adapter, _split

ANGLE_Q = {
    "reading": "overhang", "question": "What is the last arm that printed clean, with no droop underneath?",
    "good_means": "Look under the arm: the leaning face is smooth, with no loops or strands hanging from it.",
    "heading": "ANGLE", "steps": [30.0, 35.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0], "unit": "deg",
    "or": ["none", "all"],
}
SPAN_Q = {
    "reading": "bridge_closed", "question": "What is the longest span that closed?",
    "good_means": "The bar joins both posts in one piece, with no break in it.",
    "heading": "SPAN", "steps": [5.0, 10.0, 15.0, 20.0, 25.0, 30.0], "unit": "mm", "or": ["none", "all"],
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, served_roster_records):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


class _Servers:
    """Kiln's servers for the lesson: each step answered in the wire's shape,
    every ask written down."""

    def __init__(self, tmp_path: Path, plates: int = 1) -> None:
        self.tmp = tmp_path
        self.plates = plates
        self.asked: list[tuple[str, dict]] = []
        self.signed_in = True
        self.miss: served_answer.Miss | None = None
        self.refusal: dict | None = None
        self.next_coupon = "small_features"

    def _plates(self) -> list[dict]:
        out = []
        for n in range(1, self.plates + 1):
            stl = self.tmp / f"limit_test_{n}.stl"
            stl.write_text("solid t\nendsolid t\n")
            out.append({"plate": n, "code": "4821" if self.plates == 1 else f"4821-{n}", "stl_path": str(stl),
                        "size_mm": [120.0, 60.0, 30.0], "headings": ["ANGLE"] if n == 1 else ["SPAN"]})
        return out

    def _questions(self) -> list[dict]:
        last = self.plates
        return [{**ANGLE_Q, "plate": 1, "plates": [1]}, {**SPAN_Q, "plate": last, "plates": [last]}]

    def ask(self, step: str, *, gcode_paths=None, **args):
        self.asked.append((step, {**args, **({"gcode_paths": list(gcode_paths)} if gcode_paths else {})}))
        if self.miss is not None:
            return None, self.miss
        if self.refusal is not None:
            refusal, self.refusal = self.refusal, None
            return refusal, None
        if step == "make":
            return {"status": "ok", "success": True, "step": "make", "test_id": "lt-" + "a" * 32,
                    "coupon": args["coupon"], "plate_code": "4821", "plates": self._plates(),
                    "slice_with": {"supports": "off", "hole_compensation": "off",
                                   "layer_height_mm": args["layer_height_mm"]},
                    "questions": self._questions(), "kept": {"kept": True, "where": "your Kiln account"},
                    "message": "The angles and spans test print for a1 in PLA is ready."}, None
        if step == "sliced":
            return {"status": "ok", "success": True, "step": "sliced", "test_id": args["test_id"], "not_sliced": {},
                    "questions": self._questions(), "kept": {"kept": True, "where": "your Kiln account"},
                    "message": "Every step of the test print is in the sliced file."}, None
        if step == "report":
            heard = ["The last clean arm: 65 deg.", "The longest span that closed: 30 mm."]
            reply = {"status": "ok", "success": True, "step": "report", "test_id": args["test_id"], "heard": heard,
                     "kept": {"kept": True, "where": "your Kiln account"},
                     "plan": "Kept. Kiln Pro designs to this printer's own limits. https://kiln3d.com/pricing",
                     "message": "Kept in your Kiln account for a1 in PLA. " + " ".join(heard)}
            if self.next_coupon:
                reply["next"] = {"coupon": self.next_coupon, "say": "There is a second test print, for small features."}
            return reply, None
        raise AssertionError(step)


@pytest.fixture
def server(monkeypatch, tmp_path):
    import kiln.server as srv

    machine = _Adapter()
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (machine, name or "a1"))
    monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: ([], {"schema": "placement_verdict/1", "ok": True,
                                                                          "refusals": [], "record": {"blanks": []}}))
    servers = _Servers(tmp_path)
    monkeypatch.setattr(bridge, "ask", servers.ask)
    monkeypatch.setattr(bridge, "signed_in", lambda: servers.signed_in)
    import kiln.assumed_nozzle as nozzles

    monkeypatch.setattr(nozzles, "assumed_nozzle",
                        lambda printer_id=None, **kw: SimpleNamespace(diameter_mm=0.4, source="printer_setting"))
    from kiln.plugins import printer_bench_tools as tool

    return SimpleNamespace(machine=machine, tool=tool, servers=servers, tmp=tmp_path)


def _step(server, answer=None, **kw):
    out, _pictures = _split(server.tool.printer_bench(printer_name="a1", answer=answer, **kw))
    assert out.get("success") is True, out
    assert "blanks" not in out and "still_unknown" not in out, "no reply lists what Kiln lacks"
    return out


def _sliced(server, n: int) -> list[str]:
    paths = []
    for i in range(1, n + 1):
        path = server.tmp / f"plate_{i}.gcode"
        path.write_text(";LAYER:0\nG1 X1 Y1 E1\n")
        paths.append(str(path))
    return paths


def _to_the_print(server, plates: int = 1) -> dict:
    """The lesson from its first ask to the first plate's print ask."""
    server.servers.plates = plates
    out = _step(server, lesson="limits")
    assert out["step"] == "limits_intro"
    out = _step(server, "yes")
    assert out["ask"] == "Which plastic will you print the test print in? Say it the way the spool does."
    out = _step(server, "PLA")
    assert out["step"] == "limits_slice"
    out = _step(server, gcode_paths=_sliced(server, plates))
    assert out["step"] == "limits_print"
    return out


class TestTheSecondLesson:
    def test_one_plate_from_the_offer_to_what_was_kept(self, server):
        servers, m = server.servers, server.machine
        out = _step(server, lesson="limits")
        assert out["ask"] == ("Kiln can learn how steep, how far and how thin your Bambu Lab A1 really prints: it "
                              "makes a small test print, you print it, and you read a few numbers off it. Ready?")
        assert servers.asked == [], "nothing is asked of Kiln's servers before the person says yes"
        _step(server, "yes")
        out = _step(server, "PLA")
        # The test print is made for this printer, its nozzle, the layer Kiln
        # slices that nozzle at and the plate Kiln knows it has.
        step, sent = servers.asked[-1]
        assert step == "make" and sent["printer_id"] == "a1" and sent["material"] == "PLA"
        assert sent["coupon"] == "angles_and_spans" and sent["nozzle_mm"] == 0.4 and sent["layer_height_mm"] == 0.2
        assert sent["printer_model"] == "bambu_a1" and sent["bed_mm"] == list(bench.bed_of("bambu_a1"))
        # Before anything is asked, the agent slices it as the servers say.
        assert out["ask"] == "The angles and spans test print for a1 in PLA is ready."
        assert Path(out["print_files"][0]).is_file()
        assert "reslice_with_overrides" in out["how_to_slice"]
        assert "'support_material': '0'" in out["how_to_slice"] and "'layer_height': '0.2'" in out["how_to_slice"]
        assert "gcode_paths" in out["how_to_slice"]
        files = _sliced(server, 1)
        out = _step(server, gcode_paths=files)
        assert servers.asked[-1] == ("sliced", {"printer_id": "a1", "material": "PLA", "test_id": "lt-" + "a" * 32,
                                                "gcode_paths": files})
        # The print is a real print, through the normal door.
        assert out["ask"] == "Kiln will print the test print. Ready to start it?"
        assert "run_reslice_and_print" in out["how_to_start"] and "issue_preview_token" in out["how_to_start"]
        assert "'support_material': '0'" in out["how_to_start"]
        out = _step(server, "yes")
        assert out["waiting"] is True and "isn't printing yet" in out["ask"]
        m.state = "printing"
        out = _step(server, "started")
        assert out["waiting"] is True and out["ask"] == "The test print is printing; Kiln will ask about it when it finishes."
        m.state = "idle"
        out = _step(server)
        # Each question says what good looks like, and takes a step raised on
        # the print, none or all.
        assert out["ask"] == ("On the rows headed ANGLE, what is the last arm that printed clean, with no droop "
                              "underneath? Look under the arm: the leaning face is smooth, with no loops or strands "
                              "hanging from it. Answer with the number raised beside it, or none, or all.")
        assert out["options"][-2:] == ["none", "all"] and "65" in out["options"]
        out = _step(server, "62")
        assert out["step"] == "limits_question" and "isn't one of the numbers" in out["ask"]
        out = _step(server, "65 degrees")
        assert out["ask"].startswith("On the rows headed SPAN, what is the longest span that closed?")
        out = _step(server, "30")
        assert out["ask"] == "Was the spool dried before this print: yes, no, or not sure?"
        out = _step(server, "not sure")
        step, sent = servers.asked[-1]
        assert step == "report" and sent["readings"] == {"overhang": 65.0, "bridge_closed": 30.0}
        assert sent["spool_dried"] is None, "a spool nobody said was dried is never assumed either way"
        # What was kept is said back in the servers' own words, then the other
        # test print is offered.
        assert out["ask"].startswith("Kept in your Kiln account for a1 in PLA. The last clean arm: 65 deg.")
        assert out["ask"].endswith("There is a second test print, for small features. Make it now?")
        out = _step(server, "later")
        assert out["step"] == "done" and out["lesson"] == "limits"
        assert out["learned"] == ["The last clean arm: 65 deg.", "The longest span that closed: 30 mm."]
        assert out["message"].startswith("Kept in your Kiln account")
        assert server.tool._load_session(bench.unit_of(m)) is None
        assert bench.may_offer(f"limits:{bench.unit_of(m)}", "lesson") is False

    def test_each_plate_is_printed_and_read_before_the_next(self, server):
        servers, m = server.servers, server.machine
        out = _to_the_print(server, plates=2)
        assert out["ask"] == "Kiln will print plate 4821-1. Ready to start it?"
        assert out["print_file"].endswith("limit_test_1.stl")
        _step(server, "printed")
        out = _step(server, "65")
        assert out["ask"] == "Take plate 4821-1 off the plate and tell me when it's empty."
        out = _step(server, "it's empty")
        assert out["ask"] == "Kiln will print plate 4821-2. Ready to start it?" and out["print_file"].endswith("limit_test_2.stl")
        out = _step(server, "printed")
        assert out["ask"].startswith("On the rows headed SPAN on plate 4821-2, what is the longest span")
        _step(server, "all")
        _step(server, "yes")
        assert servers.asked[-1][1]["readings"] == {"overhang": 65.0, "bridge_closed": "all"}
        assert servers.asked[-1][1]["spool_dried"] is True

    def test_the_second_test_print_is_made_for_the_same_printer_and_plastic(self, server):
        _to_the_print(server)
        _step(server, "printed"); _step(server, "65"); _step(server, "30")
        _step(server, "no")
        server.servers.next_coupon = None
        out = _step(server, "yes")
        step, sent = server.servers.asked[-1]
        assert step == "make" and sent["coupon"] == "small_features" and sent["material"] == "PLA"
        assert out["step"] == "limits_slice"

    def test_files_that_are_not_one_per_plate_are_asked_for_again_and_nothing_is_sent(self, server):
        server.servers.plates = 2
        _step(server, lesson="limits"); _step(server, "yes"); _step(server, "PLA")
        out = _step(server, gcode_paths=_sliced(server, 1))
        assert out["ask"] == "Kiln needs the sliced file of each plate, in plate order: 2."
        assert [s for s, _ in server.servers.asked] == ["make"]

    def test_a_printers_3mf_is_read_for_the_gcode_inside(self, server):
        _step(server, lesson="limits"); _step(server, "yes"); _step(server, "PLA")
        packed = server.tmp / "plate_1.gcode.3mf"
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr("Metadata/plate_1.gcode", ";LAYER:0\nG1 X1 Y1 E1\n")
        _step(server, gcode_paths=[str(packed)])
        sent = server.servers.asked[-1][1]["gcode_paths"]
        assert sent[0].endswith(".gcode") and Path(sent[0]).read_text() == ";LAYER:0\nG1 X1 Y1 E1\n"

    def test_a_session_that_cannot_slice_says_skip_and_every_drawn_step_is_asked(self, server):
        _step(server, lesson="limits"); _step(server, "yes"); _step(server, "PLA")
        out = _step(server, "skip")
        assert out["step"] == "limits_print" and [s for s, _ in server.servers.asked] == ["make"]


class TestWhatThePersonIsToldWhenTheServersCannotAnswer:
    def test_signed_out_the_lesson_waits_for_a_sign_in_and_sends_nothing(self, server):
        server.servers.signed_in = False
        _step(server, lesson="limits")
        out = _step(server, "yes")
        assert out["step"] == "held" and out["hold"] == "LIMITS_ACCOUNT_REQUIRED"
        assert out["ask"].startswith("Sign in to Kiln to teach it what your printer can print")
        assert "kiln_signin" in out["next"] and server.servers.asked == []
        server.servers.signed_in = True
        out = _step(server)
        assert out["ask"].startswith("Which plastic"), "after the sign-in the lesson goes on where it stopped"

    @pytest.mark.parametrize("cause, said", [
        ("offline", "because this computer is offline"),
        ("unanswered", "because Kiln's servers didn't answer"),
        ("refused", "because Kiln's servers said no"),
        ("signed_out", "because Kiln is signed out"),
    ])
    def test_each_miss_is_said_once_and_the_same_step_is_asked_again(self, server, cause, said):
        _step(server, lesson="limits"); _step(server, "yes")
        server.servers.miss = served_answer.Miss(cause, "SERVER_UNREACHABLE" if cause != "refused" else "NOPE",
                                                 "down for upkeep" if cause == "refused" else "")
        out = _step(server, "PLA")
        assert out["step"] == "limits_make" and said in out["ask"]
        assert out["ask"].startswith("A test print and what you read off it are kept in your Kiln account. "
                                     "Kiln can't make the test print right now")
        assert "has kept nothing" in out["ask"] and out["why"] == cause
        assert "SERVER_UNREACHABLE" not in out["ask"] and "NOPE" not in out["ask"]
        server.servers.miss = None
        out = _step(server, "try again")
        assert out["step"] == "limits_slice"

    def test_readings_the_servers_did_not_keep_are_not_lost(self, server):
        _to_the_print(server)
        _step(server, "printed"); _step(server, "65"); _step(server, "30")
        server.servers.miss = served_answer.Miss("offline", "SERVER_UNREACHABLE")
        out = _step(server, "yes")
        assert "Kiln can't keep your readings right now because this computer is offline" in out["ask"]
        server.servers.miss = None
        _step(server, "try again")
        assert server.servers.asked[-1][1]["readings"] == {"overhang": 65.0, "bridge_closed": 30.0}

    def test_a_plastic_the_servers_cannot_place_is_asked_for_again_in_their_words(self, server):
        _step(server, lesson="limits"); _step(server, "yes")
        server.servers.refusal = {"status": "error", "success": False, "code": "UNKNOWN_MATERIAL",
                                  "error": "Kiln cannot place this plastic, so it has no limits to test it against."}
        out = _step(server, "Mystery goo")
        assert out["step"] == "limits_material"
        assert out["ask"].startswith("Kiln cannot place this plastic") and out["ask"].endswith("Which plastic will you print it in?")

    def test_readings_that_disagree_are_read_again_from_the_first_question(self, server):
        _to_the_print(server)
        _step(server, "printed"); _step(server, "65"); _step(server, "30")
        server.servers.refusal = {"status": "error", "success": False, "code": "INVALID_READING",
                                  "error": "The readings disagree.", "questions": server.servers._questions()}
        out = _step(server, "yes")
        assert out["step"] == "limits_question" and out["ask"].startswith("The readings disagree. On the rows headed ANGLE")

    def test_a_nozzle_kiln_cannot_find_is_asked(self, server, monkeypatch):
        import kiln.assumed_nozzle as nozzles

        monkeypatch.setattr(nozzles, "assumed_nozzle",
                            lambda printer_id=None, **kw: SimpleNamespace(diameter_mm=0.4, source="default"))
        _step(server, lesson="limits"); _step(server, "yes")
        out = _step(server, "PETG")
        assert out["ask"] == "Which nozzle size is on the printer, in mm? 0.4 is the most common."
        assert server.servers.asked == []
        _step(server, "0.6")
        assert server.servers.asked[-1][1]["nozzle_mm"] == 0.6


class TestThePrintersOwnName:
    def test_a_printer_reached_as_the_default_is_taught_under_its_own_name(self, server, monkeypatch):
        import kiln.printer_model_resolver as resolver

        monkeypatch.setattr(resolver, "resolve_active_printer_name", lambda: "garage")
        out, _ = _split(server.tool.printer_bench(printer_name="default", lesson="limits"))
        assert out["step"] == "limits_intro"
        server.tool.printer_bench(printer_name="default", answer="yes")
        server.tool.printer_bench(printer_name="default", answer="PLA")
        step, sent = server.servers.asked[-1]
        assert step == "make" and sent["printer_id"] == "garage" and sent["printer_model"] == "bambu_a1"

    def test_a_printer_with_no_name_of_its_own_is_told_how_to_give_it_one(self, server, monkeypatch):
        import kiln.printer_model_resolver as resolver

        monkeypatch.setattr(resolver, "resolve_active_printer_name", lambda: None)
        out = server.tool.printer_bench(printer_name="default", lesson="limits")
        assert out["success"] is False and out["error"]["code"] == "BENCH_NO_PRINTER_NAME"
        assert "rename_printer" in out["error"]["message"] and server.servers.asked == []


class TestTheWayIn:
    def test_a_printer_whose_moves_kiln_knows_is_offered_the_second_lesson(self, server):
        out = _step(server)
        assert out["step"] == "limits_intro"
        assert out["ask"].startswith("Kiln already knows how the Bambu Lab A1 moves its head. Kiln can learn how steep")

    def test_later_is_honoured_and_lesson_limits_still_starts_it(self, server):
        _step(server, lesson="limits")
        out = _step(server, "later")
        assert out["step"] == "later" and 'lesson="limits"' in out["message"]
        assert _step(server, lesson="limits")["step"] == "limits_intro"

    def test_an_unknown_lesson_is_refused(self, server):
        out = server.tool.printer_bench(printer_name="a1", lesson="colour")
        assert out["success"] is False and out["error"]["code"] == "INVALID_ARGS"


class TestTheCourier:
    """The bridge, against the real served door and the real file sending."""

    @pytest.fixture
    def wire(self, monkeypatch):
        import kiln.server as srv
        from kiln import served_makes

        calls: list[dict] = []
        state = {"bearer": "token", "answer": {"status": "ok", "success": True, "step": "kept", "test_prints": []}}
        monkeypatch.setattr(srv, "_served_bearer", lambda: (state["bearer"], None))

        def call(tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
            calls.append({"tool": tool, **kwargs})
            if isinstance(state["answer"], Exception):
                raise state["answer"]
            return json.loads(json.dumps(state["answer"]))

        monkeypatch.setattr(srv, "_pro_api_call", call)
        # This install's printers, as its config names them.
        monkeypatch.setattr(srv, "_read_config_printers", lambda: {
            "garage": {"type": "bambu", "printer_model": "bambu_a1"},
            "shed": {"type": "bambu", "printer_model": "bambu_a1"},
        })
        monkeypatch.setattr(srv, "_resolve_effective_printer_name", lambda name=None: "garage")
        uploaded: list[str] = []

        def upload(local, *, on_miss=None):
            uploaded.append(Path(local).name)
            return f"tok{len(uploaded):016d}", ""

        monkeypatch.setattr(served_makes, "_upload_file", upload)
        return SimpleNamespace(calls=calls, state=state, uploaded=uploaded)

    def test_the_sliced_files_go_up_with_the_call_under_the_files_parameter(self, wire, tmp_path):
        files = []
        for i in (1, 2):
            path = tmp_path / f"plate_{i}.gcode"
            path.write_text("G1 X1\n")
            files.append(str(path))
        answer, miss = bridge.ask("sliced", printer_id="a1", material="PLA", test_id="lt-x", gcode_paths=files)
        assert miss is None and answer["status"] == "ok"
        sent = wire.calls[-1]
        assert sent["tool"] == "limits_lesson" and sent["step"] == "sliced"
        assert "gcode_paths" not in sent, "a path on this computer is never sent as words"
        assert sent["file_tokens"] == {"gcode_paths": [{"token": "tok0000000000000001", "name": "plate_1.gcode"},
                                                       {"token": "tok0000000000000002", "name": "plate_2.gcode"}]}
        assert wire.uploaded == ["plate_1.gcode", "plate_2.gcode"]

    def test_every_step_carries_this_installs_printers(self, wire):
        """So the servers file what is learned under the printer's own name,
        and a later answer asked by model, or with no printer named, finds
        the right one of the person's printers."""
        for step in ("make", "sliced", "report", "kept"):
            bridge.ask(step, printer_id="garage", material="PLA")
            facts = wire.calls[-1]["step_results"]["printer_facts"]["data"]
            assert [(p["name"], p["model"], p["is_default"]) for p in facts["printers"]] == [
                ("garage", "bambu_a1", True), ("shed", "bambu_a1", False)]
            assert wire.calls[-1]["printer_id"] == "garage"

    def test_the_test_prints_plates_arrive_on_this_computer(self, wire, monkeypatch, tmp_path):
        """The servers hand the first plate over as the make's own file and
        the others as the answer's files; both end up here, where the
        session hands them to the agent, and no path on the servers is left."""
        import httpx

        from kiln import served_makes

        there = "/tmp/kiln-callers/abc123/kiln_limit_test_angles_and_spans_x_{}.stl"
        wire.state["answer"] = {
            "status": "ok", "success": True, "step": "make", "test_id": "lt-x", "stl_path": there.format(1),
            "plates": [{"plate": 1, "code": "1234-1", "stl_path": there.format(1)},
                       {"plate": 2, "code": "1234-2", "stl_path": there.format(2)}],
            "artifact": {"artifact_token": "a" * 32, "format": "stl", "at": ["stl_path"]},
            "files": [{"at": ["plates", 1, "stl_path"], "filename": "plate_2.stl", "format": "stl",
                       "artifact_token": "b" * 32}],
        }
        monkeypatch.setattr(httpx, "get", lambda url, **kw: SimpleNamespace(status_code=200, content=b"solid two\n"))
        monkeypatch.setattr(served_makes, "_fetch_look", lambda token, kind: None)
        monkeypatch.setattr(served_makes, "_bearer", lambda: "token")
        kept_at = tmp_path / "kept_plate_1.stl"
        kept_at.write_text("solid one\n")
        keeps: list[str] = []

        def keep(token):
            keeps.append(token)
            return {"success": True, "status": "success", "mesh_path": str(kept_at)}

        monkeypatch.setattr(served_makes, "keep", keep)
        answer, miss = bridge.ask("make", printer_id="garage", material="PLA", coupon="angles_and_spans")
        assert miss is None and keeps == ["a" * 32]
        paths = [p["stl_path"] for p in answer["plates"]]
        assert paths[0] == str(kept_at) and Path(paths[1]).read_bytes() == b"solid two\n"
        assert answer["stl_path"] == str(kept_at)
        assert "kiln-callers" not in json.dumps(answer), "no path on the servers is handed to the session"

    def test_signed_out_nothing_is_sent(self, wire, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None))
        answer, miss = bridge.ask("kept", printer_id="a1", material="PLA")
        assert answer is None and miss.cause == "signed_out" and wire.calls == []
        assert bridge.signed_in() is False

    def test_the_lessons_own_refusal_is_an_answer_and_anything_else_is_a_miss(self, wire):
        wire.state["answer"] = {"status": "error", "success": False, "code": "INVALID_READING", "error": "x"}
        answer, miss = bridge.ask("report", printer_id="a1", material="PLA", test_id="lt-x", readings={"overhang": 1})
        assert miss is None and answer["code"] == "INVALID_READING"
        wire.state["answer"] = {"status": "error", "error": "busy", "code": "SERVICE_UNAVAILABLE", "why": "unanswered"}
        answer, miss = bridge.ask("kept", printer_id="a1", material="PLA")
        assert answer is None and miss.cause == "unanswered"
        wire.state["answer"] = OSError("network is unreachable")
        answer, miss = bridge.ask("kept", printer_id="a1", material="PLA")
        assert answer is None and miss.cause in ("offline", "unanswered")

    def test_only_the_steps_own_words_are_sent(self, wire):
        bridge.ask("make", printer_id="a1", material="PLA", coupon="angles_and_spans", nozzle_mm=0.4,
                   layer_height_mm=0.2, bed_mm=[256.0, 256.0], unit="secret-serial", fan_percent=None)
        sent = wire.calls[-1]
        assert "unit" not in sent and "fan_percent" not in sent
        assert sent["bed_mm"] == [256.0, 256.0] and sent["step"] == "make"

    @pytest.mark.parametrize("cause", served_answer.CAUSES)
    def test_every_miss_is_one_sentence_with_no_code_in_it(self, cause):
        said = bridge.miss_sentence(served_answer.Miss(cause, "SOME_CODE", "the servers' words"), cannot="keep your readings")
        assert "Kiln can't keep your readings right now because" in said and "has kept nothing" in said
        assert "SOME_CODE" not in said
