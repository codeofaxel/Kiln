"""A served tool may ask this computer to ACT on the printer -- through the
fence, through the tool's own door, and only once the agent has seen it.

The paid part of a printer feature (a speed checked against the machine's
range, a resume file built with its safety checks) runs on Kiln's servers,
which have no printer.  The ``act`` step is how the result reaches the
machine: the servers name one of this install's own tools from a short
allow-list, with checked arguments, and this computer runs it exactly as
the user's agent would have -- every gate that tool makes fires here.
These tests hold the fence: nothing outside the list runs, nothing runs
before the agent has seen it, and what goes back is the tool's answer and
nothing about this computer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import served_makes, served_steps
from kiln.printers.base import JobProgress, PrinterState, PrinterStatus


@pytest.fixture(autouse=True)
def this_computer(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    return tmp_path


@pytest.fixture
def handed_file() -> str:
    """A resume file the servers handed over with the answer, saved by arrival."""
    folder = served_makes.files_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "part_resume_L9-abc123.3mf"
    path.write_bytes(b"PK\x03\x04resume")
    return str(path)


@pytest.fixture
def doors(monkeypatch):
    """This install's own printer tools, each recording what it was asked."""
    import kiln.server as server

    calls: list[tuple[str, dict]] = []
    answers: dict[str, dict] = {
        "upload_file": {"success": True, "file_name": "part_resume_L9-abc123.3mf", "size_bytes": 12,
                        "local_path": "/Users/me/secret/part.3mf"},
        "start_print": {"success": True, "print_start": "started", "preflight": {"ready": True, "summary": "ok", "checks": []}},
        "pause_print": {"success": True, "message": "paused"},
        "resume_print": {"success": True, "message": "resumed"},
        "set_speed_profile": {"success": True, "profile": "sport", "outcome": "confirmed"},
    }

    def door(name):
        def run(**kwargs):
            calls.append((name, kwargs))
            return dict(answers[name])
        return run

    for name in answers:
        monkeypatch.setattr(server, name, door(name))
    return calls, answers


def _send(path: Path):
    raise AssertionError("an action sends no file")


def _act(tool: str, step_id: str, **args) -> dict:
    return {"kind": "act", "id": step_id, "tool": tool, "args": args, "why": f"because {step_id}"}


class TestTheFence:
    def test_the_allow_list_is_the_five_decided_tools_and_nothing_hot(self):
        """Adding a tool a server may run on a printer is a decision, and
        this is where it is recorded."""
        assert set(served_steps.ACT_TOOLS) == {
            "upload_file", "start_print", "pause_print", "resume_print", "set_speed_profile",
        }
        for forbidden in ("send_gcode", "set_temperature", "update_printer_firmware",
                          "emergency_stop", "cancel_print", "register_printer", "confirm_action"):
            assert forbidden not in served_steps.ACT_TOOLS

    @pytest.mark.parametrize(
        "step",
        [
            _act("send_gcode", "g", commands="M104 S300"),
            _act("set_temperature", "t", tool_temp=300),
            _act("update_printer_firmware", "f"),
            _act("cancel_print", "c"),
            _act("emergency_stop", "e"),
            {"kind": "act", "id": "x", "tool": None, "args": {}},
            {"kind": "act", "id": "x", "args": {}},
        ],
    )
    def test_a_tool_outside_the_list_runs_nothing(self, doors, step):
        calls, _ = doors
        done, refused = served_steps.carry_out("t", [step], _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert calls == []

    @pytest.mark.parametrize(
        "step",
        [
            # a person's word, never a server's
            _act("resume_print", "r", printer_name="voron", hardware_confirmed=True),
            # a preview token is minted on this computer for the person
            _act("start_print", "s", file_name="x.3mf", preview_token="abc"),
            # the file-name is a name on the printer, never a path
            _act("start_print", "s", file_name="../../etc/passwd"),
            _act("start_print", "s", file_name="/tmp/x.3mf"),
            _act("start_print", "s", file_name=""),
            _act("start_print", "s", file_name="x.3mf", plate_number=0),
            _act("start_print", "s", file_name="x.3mf", plate_number=True),
            _act("start_print", "s", file_name="x.3mf", use_ams="maybe"),
            _act("start_print", "s", file_name="x.3mf", ams_mapping=[1, "2"]),
            _act("start_print", "s", file_name="x.3mf", ams_mapping=list(range(40))),
            _act("start_print", "s", file_name="x.3mf", bed_leveling="no"),
            _act("set_speed_profile", "sp", profile="warp"),
            _act("set_speed_profile", "sp", profile=166),
            _act("pause_print", "p", keep_temps="yes"),
            _act("pause_print", "p", printer_name="a\nb"),
            {"kind": "act", "id": "p", "tool": "pause_print", "args": ["voron"]},
        ],
    )
    def test_an_argument_outside_the_tools_shape_runs_nothing(self, doors, step):
        calls, _ = doors
        done, refused = served_steps.carry_out("t", [step], _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert calls == []

    @pytest.mark.parametrize("path", ["/etc/passwd", "~/Downloads/mine.3mf", "/tmp/anything.3mf", "", "relative.3mf"])
    def test_only_a_file_the_servers_handed_over_is_uploaded(self, doors, path):
        calls, _ = doors
        done, refused = served_steps.carry_out(
            "t", [_act("upload_file", "u", file_path=path)], _send, act=True,
        )
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert calls == []

    def test_a_file_in_the_folder_that_is_not_there_is_not_uploaded(self, doors):
        calls, _ = doors
        ghost = str(served_makes.files_dir() / "ghost.3mf")
        done, refused = served_steps.carry_out("t", [_act("upload_file", "u", file_path=ghost)], _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED" and calls == []

    def test_nothing_runs_until_the_agent_has_said_to(self, doors, handed_file):
        calls, _ = doors
        done, refused = served_steps.carry_out(
            "t", [_act("upload_file", "u", file_path=handed_file)], _send,
        )
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert "nobody here said to" in refused["error"]
        assert calls == []

    def test_one_bad_action_in_a_list_runs_none_of_them(self, doors, handed_file):
        calls, _ = doors
        steps = [
            _act("upload_file", "u", file_path=handed_file),
            _act("send_gcode", "g", commands="G28"),
        ]
        done, refused = served_steps.carry_out("t", steps, _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert calls == []

    def test_too_many_actions_run_nothing(self, doors):
        calls, _ = doors
        steps = [_act("pause_print", f"p{i}", printer_name=f"m{i}") for i in range(served_steps.MAX_ACT_STEPS + 1)]
        done, refused = served_steps.carry_out("t", steps, _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED" and calls == []

    def test_a_make_step_is_still_limited_separately(self, doors):
        steps = [{"kind": "slice", "id": f"s{i}", "model_path": "x", "slicer_args": []} for i in range(served_steps.MAX_STEPS + 1)]
        done, refused = served_steps.carry_out("t", steps, _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"


class TestThroughTheDoor:
    def test_an_action_runs_the_tool_with_exactly_the_checked_arguments(self, doors, handed_file):
        calls, _ = doors
        done, refused = served_steps.carry_out(
            "resume_interrupted_print",
            [_act("upload_file", "upload", file_path=handed_file, printer_name="voron")],
            _send, act=True,
        )
        assert refused is None
        assert calls == [("upload_file", {"file_path": handed_file, "printer_name": "voron"})]
        assert done["upload"]["data"]["ran"] is True and done["upload"]["data"]["success"] is True
        assert done["upload"]["data"]["file_name"] == "part_resume_L9-abc123.3mf"

    def test_what_goes_back_is_the_answer_and_nothing_about_this_computer(self, doors, handed_file):
        done, _ = served_steps.carry_out(
            "t", [_act("upload_file", "upload", file_path=handed_file)], _send, act=True,
        )
        sent = json.dumps(done)
        assert "/Users/me" not in sent and "local_path" not in sent and "size_bytes" not in sent

    def test_a_start_that_needs_an_upload_runs_after_it_and_takes_its_file_name(self, doors, handed_file):
        calls, _ = doors
        steps = [
            _act("upload_file", "upload", file_path=handed_file, printer_name="voron"),
            {**_act("start_print", "start", printer_name="voron", resume_from_paused=True,
                    file_name={"from_step": "upload", "field": "file_name"}),
             "needs": ["upload"]},
        ]
        done, refused = served_steps.carry_out("t", steps, _send, act=True)
        assert refused is None
        assert [c[0] for c in calls] == ["upload_file", "start_print"]
        assert calls[1][1] == {"printer_name": "voron", "resume_from_paused": True,
                               "file_name": "part_resume_L9-abc123.3mf"}
        assert done["start"]["data"]["print_start"] == "started"
        assert done["start"]["data"]["preflight"] == {"ready": True, "summary": "ok"}

    def test_a_start_never_follows_a_failed_upload(self, doors, handed_file):
        calls, answers = doors
        answers["upload_file"] = {"success": False, "error": "FTPS refused", "code": "UPLOAD_FAILED"}
        steps = [
            _act("upload_file", "upload", file_path=handed_file),
            {**_act("start_print", "start", file_name={"from_step": "upload", "field": "file_name"}), "needs": ["upload"]},
        ]
        done, refused = served_steps.carry_out("t", steps, _send, act=True)
        assert refused is None
        assert [c[0] for c in calls] == ["upload_file"]
        assert done["upload"]["data"]["success"] is False
        assert done["start"]["data"]["ran"] is False and "upload" in done["start"]["data"]["skipped_because"]

    def test_a_tool_that_stops_to_ask_the_person_is_not_a_success(self, doors, handed_file):
        calls, answers = doors
        answers["start_print"] = {"confirmation_required": True, "token": "abcd1234", "message": "confirm"}
        answers["upload_file"] = {"success": True, "file_name": "r.3mf"}
        steps = [
            _act("upload_file", "upload", file_path=handed_file),
            {**_act("start_print", "start", file_name={"from_step": "upload", "field": "file_name"}), "needs": ["upload"]},
            {**_act("pause_print", "after", printer_name="voron"), "needs": ["start"]},
        ]
        done, _ = served_steps.carry_out("t", steps, _send, act=True)
        assert done["start"]["data"]["confirmation_required"] is True
        assert done["start"]["data"]["token"] == "abcd1234"  # the agent needs it
        assert not served_steps.act_succeeded(done["start"]["data"])
        assert done["after"]["data"]["ran"] is False
        assert [c[0] for c in calls] == ["upload_file", "start_print"]

    def test_a_fleet_of_machines_is_each_reached_whatever_the_others_answered(self, doors, monkeypatch):
        calls, answers = doors
        import kiln.server as server

        def pause(**kwargs):
            calls.append(("pause_print", kwargs))
            if kwargs["printer_name"] == "ender":
                return {"success": False, "error": "offline", "code": "PRINTER_OFFLINE"}
            return {"success": True}

        monkeypatch.setattr(server, "pause_print", pause)
        steps = [_act("pause_print", f"pause_{i}", printer_name=n, keep_temps=True) for i, n in enumerate(["voron", "ender", "mk4"])]
        done, refused = served_steps.carry_out("fleet_pause", steps, _send, act=True)
        assert refused is None
        assert [c[1]["printer_name"] for c in calls] == ["voron", "ender", "mk4"]
        assert done["pause_1"]["data"]["success"] is False and done["pause_2"]["data"]["success"] is True

    def test_a_tool_that_raises_is_an_answer_not_a_crash(self, doors, handed_file, monkeypatch):
        import kiln.server as server

        def boom(**_kw):
            raise RuntimeError("printer exploded (not really)")

        monkeypatch.setattr(server, "upload_file", boom)
        done, refused = served_steps.carry_out("t", [_act("upload_file", "u", file_path=handed_file)], _send, act=True)
        assert refused is None
        assert done["u"]["data"]["success"] is False and "RuntimeError" in done["u"]["data"]["error"]

    def test_a_tool_this_version_does_not_have_is_said(self, doors, monkeypatch):
        import kiln.server as server

        monkeypatch.delattr(server, "pause_print")
        done, refused = served_steps.carry_out("t", [_act("pause_print", "p", printer_name="v")], _send, act=True)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED" and "this version" in refused["error"]


class TestWhatTheAgentIsShown:
    def test_the_proposal_names_each_tool_its_arguments_and_the_reason(self, handed_file):
        steps = [
            _act("upload_file", "upload", file_path=handed_file, printer_name="voron"),
            {**_act("start_print", "start", printer_name="voron", resume_from_paused=True,
                    file_name={"from_step": "upload", "field": "file_name"}), "needs": ["upload"]},
        ]
        shown = served_steps.propose("resume_interrupted_print", steps)
        assert shown["code"] == "ACTIONS_PROPOSED" and shown["success"] is False
        assert [a["tool"] for a in shown["actions"]] == ["upload_file", "start_print"]
        assert shown["actions"][0]["args"] == {"file_path": "part_resume_L9-abc123.3mf", "printer_name": "voron"}
        assert shown["actions"][0]["why"] == "because upload"
        assert "run_actions=true" in shown["error"] and "Nothing has run" in shown["error"]
        assert str(served_makes.files_dir()) not in json.dumps(shown)

    def test_a_proposed_action_outside_the_fence_is_marked(self):
        shown = served_steps.propose("t", [_act("send_gcode", "g", commands="G28")])
        assert shown["actions"][0]["allowed"] is False and "NOT ALLOWED" in shown["error"]


class TestReadFirst:
    @pytest.fixture
    def printers(self, monkeypatch):
        import kiln.server as server

        monkeypatch.setattr(server, "_read_config_printers", lambda: {
            "default": {"type": "bambu", "printer_model": "bambu_a1", "host": "10.0.0.5", "access_code": "1234"},
        })
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "default")

    def test_a_listed_reading_is_done_before_the_first_call(self, printers):
        done, refused = served_steps.read_first(
            "set_speed_percent", [{"kind": "printer_facts", "want": []}], printer_name="",
        )
        assert refused is None
        assert done["printer_facts"]["data"]["printers"][0]["type"] == "bambu"
        assert "10.0.0.5" not in json.dumps(done)

    @pytest.mark.parametrize(
        "reads",
        [
            [{"kind": "slice", "model_path": "x"}],
            [{"kind": "act", "tool": "pause_print"}],
            [{"kind": "shell"}],
            [{"kind": "printer_facts"}] * (served_steps.MAX_STEPS + 1),
            "printer_facts",
        ],
    )
    def test_only_a_reading_is_ever_done_on_nobodys_say_so(self, printers, reads):
        done, refused = served_steps.read_first("t", reads, printer_name="")
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"


class TestTheJobFact:
    class _Adapter:
        def __init__(self, host, state=PrinterStatus.PRINTING, layer=40):
            self.host = host
            self.serial = ""
            self.name = "fake"
            self._state = state
            self._layer = layer

        def get_state(self):
            return PrinterState(state=self._state, connected=True, tool_temp_actual=200.0,
                                tool_temp_target=200.0, bed_temp_actual=60.0, bed_temp_target=60.0)

        def get_job(self):
            return JobProgress(file_name="private-name.3mf", completion=20.0, current_layer=self._layer,
                               total_layers=200)

    @pytest.fixture
    def fleet(self, monkeypatch):
        import kiln.server as server

        adapters = {
            "default": self._Adapter("10.0.0.5"),
            "a1": self._Adapter("10.0.0.5"),  # the same machine under its config name
            "voron": self._Adapter("voron.local", PrinterStatus.PAUSED, 77),
        }
        monkeypatch.setattr(server, "_read_config_printers", lambda: {
            "default": {"type": "bambu", "printer_model": "bambu_a1", "host": "10.0.0.5"},
            "a1": {"type": "bambu", "printer_model": "bambu_a1", "host": "10.0.0.5"},
            "voron": {"type": "moonraker", "printer_model": "voron_2", "host": "voron.local"},
        })
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "default")
        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: adapters[name])
        return adapters

    def _read(self, **step):
        return served_steps.carry_out(
            "t", [{"kind": "printer_facts", "id": "printer_facts", **step}], _send,
        )

    def test_the_named_printers_job_is_its_state_and_how_far_along_never_the_file(self, fleet):
        done, refused = self._read(printer_name="voron", want=["job"])
        assert refused is None
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["voron"]["job"] == {
            "state": "paused", "has_job": True, "current_layer": 77, "total_layers": 200, "completion": 20.0,
        }
        assert "job" not in by_name["default"]
        assert "private-name" not in json.dumps(done) and "voron.local" not in json.dumps(done)

    def test_a_printer_named_by_its_model_is_the_one_read(self, fleet):
        done, _ = self._read(printer_name="voron_2", want=["job"])
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["voron"]["job"]["current_layer"] == 77 and "job" not in by_name["default"]

    def test_every_job_reads_every_printer_and_marks_the_same_machine_once(self, fleet):
        done, _ = self._read(printer_name="", want=["every_job"])
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["default"]["job"]["state"] == "printing"
        assert by_name["voron"]["job"]["state"] == "paused"
        assert by_name["a1"]["same_machine_as"] == "default"
        assert "same_machine_as" not in by_name["default"] and "same_machine_as" not in by_name["voron"]

    def test_a_printer_that_cannot_be_asked_is_said_not_guessed(self, fleet, monkeypatch):
        import kiln.server as server

        def resolve(name=None):
            if name == "voron":
                raise RuntimeError("connection refused to voron.local")
            return fleet[name]

        monkeypatch.setattr(server, "_resolve_adapter", resolve)
        done, refused = self._read(printer_name="", want=["every_job"])
        assert refused is None
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["voron"]["job"] is None and by_name["voron"]["job_error"] == "the printer could not be asked"
        assert "voron.local" not in json.dumps(done)

    @pytest.mark.parametrize("want", [["job", "host"], ["file_name"], ["every_job", "serial"]])
    def test_asking_for_more_sends_nothing(self, fleet, want):
        done, refused = self._read(printer_name="", want=want)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"


class TestThroughTheStub:
    """The stub shows the agent what the servers want, runs it only on the
    agent's say-so, and reads first what the tool is listed as needing."""

    MANIFEST = {"categories": {}, "tools": [{
        "name": "resume_interrupted_print", "description": "Resume an interrupted print.",
        "tier": "pro",
        "parameters": {"type": "object", "properties": {
            "original_gcode_path": {"type": "string"}, "layer": {"type": "integer", "default": 0},
            "printer_name": {"type": "string", "default": ""}}, "required": ["original_gcode_path"]},
        "inputs": {"files": {"original_gcode_path": "one"}, "printer": "printer_name"},
        "local_steps": {"schema_version": 1, "reads_first": [{"kind": "printer_facts", "want": ["job"]}], "acts": True},
    }]}

    @pytest.fixture
    def stub(self, monkeypatch, tmp_path):
        import kiln.server as server

        fake = tmp_path / "pkg" / "server.py"
        fake.parent.mkdir()
        (fake.parent / "pro_tool_manifest.json").write_text(json.dumps(self.MANIFEST))
        monkeypatch.setattr(server, "__file__", str(fake))
        registered: dict = {}

        class _Registry:
            def tool(self, *a, **k):
                def register(fn):
                    registered[fn.__name__] = fn
                    return fn
                return register

        server._register_pro_tool_stubs(_Registry())
        monkeypatch.setattr(served_makes, "_upload_file", lambda p: (f"tok-{p.suffix[1:]}-0123456789abcdef", ""))
        monkeypatch.setattr(served_makes, "_bring_files", lambda answer, trip: None)
        monkeypatch.setattr(server, "_read_config_printers", lambda: {
            "default": {"type": "bambu", "printer_model": "bambu_a1", "host": "10.0.0.5"}})
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "default")
        monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: TestTheJobFact._Adapter("10.0.0.5", PrinterStatus.PAUSED, 9))
        monkeypatch.setattr(server, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
        return server, registered["resume_interrupted_print"]

    def _gcode(self, tmp_path) -> str:
        (tmp_path / "print.gcode").write_text(";LAYER_CHANGE\nG1 X1 Y1 E1\n")
        return str(tmp_path / "print.gcode")

    def _asks(self, handed_file):
        return {"status": "needs_local_step", "code": "LOCAL_STEP_NEEDED", "error": "needs the printer", "local_steps": [
            _act("upload_file", "upload", file_path=handed_file, printer_name="default"),
            {**_act("start_print", "start", printer_name="default", resume_from_paused=True,
                    file_name={"from_step": "upload", "field": "file_name"}), "needs": ["upload"]},
        ]}

    def test_the_stub_takes_a_run_actions_switch_and_says_so(self, stub):
        import inspect

        _server, tool = stub
        assert "run_actions" in inspect.signature(tool).parameters
        assert "run_actions=true" in tool.__doc__

    def test_what_the_tool_needs_read_goes_with_the_first_call(self, stub, doors, handed_file, monkeypatch, tmp_path):
        server, tool = stub
        calls: list[dict] = []

        def forwarded(name, **kwargs):
            calls.append(kwargs)
            return {"status": "success", "resumed_from_layer": 9}

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        tool(original_gcode_path=self._gcode(tmp_path))
        (first,) = calls
        facts = first["step_results"]["printer_facts"]["data"]
        assert facts["printers"][0]["job"]["current_layer"] == 9
        assert facts["printers"][0]["type"] == "bambu"
        assert first["printer_name"] == "bambu_a1"  # the stub's own naming of an unnamed printer
        assert "run_actions" not in first

    def test_an_action_is_shown_first_and_nothing_runs(self, stub, doors, handed_file, monkeypatch, tmp_path):
        server, tool = stub
        door_calls, _ = doors
        calls: list[dict] = []

        def forwarded(name, **kwargs):
            calls.append(kwargs)
            return self._asks(handed_file)

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        answer = tool(original_gcode_path=self._gcode(tmp_path))
        assert answer["code"] == "ACTIONS_PROPOSED"
        assert [a["tool"] for a in answer["actions"]] == ["upload_file", "start_print"]
        assert len(calls) == 1 and door_calls == []

    def test_on_the_agents_say_so_the_actions_run_and_the_same_call_is_made_again(
        self, stub, doors, handed_file, monkeypatch, tmp_path,
    ):
        server, tool = stub
        door_calls, _ = doors
        calls: list[dict] = []

        def forwarded(name, **kwargs):
            calls.append(kwargs)
            results = kwargs.get("step_results") or {}
            if "upload" not in results:
                return self._asks(handed_file)
            return {"status": "success", "stage": "start_print", "resumed_from_layer": 9,
                    "stages": {"upload_file": results["upload"]["data"], "start_print": results["start"]["data"]}}

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        answer = tool(original_gcode_path=self._gcode(tmp_path), run_actions=True)
        assert answer["status"] == "success" and answer["stages"]["start_print"]["print_start"] == "started"
        assert [c[0] for c in door_calls] == ["upload_file", "start_print"]
        assert len(calls) == 2
        first, second = calls
        # The pre-read facts ride BOTH calls; the actions' answers ride the second.
        assert "printer_facts" in first["step_results"] and "upload" not in first["step_results"]
        assert set(second["step_results"]) == {"printer_facts", "upload", "start"}
        assert {k: v for k, v in second.items() if k != "step_results"} == {k: v for k, v in first.items() if k != "step_results"}

    def test_a_tool_that_asks_twice_is_told_no_and_nothing_more_runs(self, stub, doors, handed_file, monkeypatch, tmp_path):
        server, tool = stub
        door_calls, _ = doors
        monkeypatch.setattr(server, "_pro_api_call", lambda name, **kw: self._asks(handed_file))
        answer = tool(original_gcode_path=self._gcode(tmp_path), run_actions=True)
        assert answer["code"] == "LOCAL_STEP_REFUSED" and "twice" in answer["error"]
        assert [c[0] for c in door_calls] == ["upload_file", "start_print"]

    def test_a_tool_not_listed_as_acting_cannot_be_told_to(self, stub, doors, handed_file, monkeypatch, tmp_path):
        """The switch exists only where the manifest says the tool acts: a
        tool that was never listed gets its actions proposed, and a stray
        run_actions=true is an unknown argument, not consent."""
        import kiln.server as server

        manifest = {"categories": {}, "tools": [{**self.MANIFEST["tools"][0], "local_steps": {"schema_version": 1, "reads_first": [], "acts": False}}]}
        fake = Path(server.__file__).parent / "pro_tool_manifest.json"
        fake.write_text(json.dumps(manifest))
        registered: dict = {}

        class _Registry:
            def tool(self, *a, **k):
                def register(fn):
                    registered[fn.__name__] = fn
                    return fn
                return register

        server._register_pro_tool_stubs(_Registry())
        tool = registered["resume_interrupted_print"]
        import inspect

        assert "run_actions" not in inspect.signature(tool).parameters
        door_calls, _ = doors
        monkeypatch.setattr(server, "_pro_api_call", lambda name, **kw: self._asks(handed_file))
        answer = tool(original_gcode_path=self._gcode(tmp_path))
        assert answer["code"] == "ACTIONS_PROPOSED" and door_calls == []
