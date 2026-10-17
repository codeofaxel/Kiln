"""A served tool may ask this computer for one step, from a short allow-list.

Kiln's servers have no slicer.  A served tool that must slice something
(the part added to a paused print) answers ``needs_local_step``; this
install slices the model the servers handed back, sends the result, and
calls the same tool again.  The point of these tests is the fence: a
server's answer is data, and it can make this computer run exactly one
kind of thing, on one file it was handed, with flags checked one by one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiln import served_makes, served_steps

GOOD_ARGS = ["--dont-arrange", "--bed-shape", "0x0,256x0,256x256,0x256",
             "--skirts", "0", "--brim-type", "no_brim", "--first-layer-height", "0.2"]


@pytest.fixture(autouse=True)
def this_computer(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    return tmp_path


@pytest.fixture
def handed_model() -> str:
    folder = served_makes.files_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "placed-abc123.stl"
    path.write_bytes(b"solid part\nendsolid part\n")
    return str(path)


@pytest.fixture
def slicer(monkeypatch):
    """A slicer that records what it was asked and writes a G-code."""
    import subprocess

    from kiln import slicer as slicer_mod

    ran: list[list[str]] = []
    monkeypatch.setattr(
        slicer_mod, "find_slicer",
        lambda slicer_path=None: slicer_mod.SlicerInfo(path="/opt/prusa-slicer", name="prusa-slicer"),
    )

    def run(cmd, **_kw):
        ran.append(list(cmd))
        Path(cmd[cmd.index("--output") + 1]).write_text(";LAYER_CHANGE\nG1 X1 Y1 E1\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(served_steps.subprocess, "run", run)
    return ran


def _step(model: str, **over) -> dict:
    return {"kind": "slice", "id": "feature_slice", "model_path": model, "slicer_args": list(GOOD_ARGS), **over}


def _send(path: Path):
    return f"token-for-{path.name}-0123456789", ""


class TestTheSliceStep:
    def test_it_slices_the_handed_model_with_the_given_flags_and_sends_the_result(self, slicer, handed_model):
        done, refused = served_steps.carry_out("add_feature_during_print", [_step(handed_model)], _send)
        assert refused is None
        assert done == {"feature_slice": {"token": "token-for-slice.gcode-0123456789"}}
        (cmd,) = slicer
        assert cmd[:3] == ["/opt/prusa-slicer", "--export-gcode", handed_model]
        assert cmd[5:] == GOOD_ARGS  # after --output <path>: the checked flags, nothing more

    @pytest.mark.parametrize(
        "args",
        [
            ["--load", "/etc/passwd"],
            ["--post-process", "/tmp/evil.sh"],
            ["--output", "/Users/me/.zshrc"],
            ["--dont-arrange", "--save", "/tmp/x.ini"],
            ["--bed-shape", "0x0,256x0,256x256,0x256; rm -rf ~"],
            ["--bed-shape"],
            ["--skirts", "--post-process"],
            ["--first-layer-height", "$(reboot)"],
            ["/etc/passwd"],
            "--dont-arrange",
            [1, 2],
        ],
    )
    def test_a_flag_outside_the_allow_list_runs_nothing(self, slicer, handed_model, args):
        done, refused = served_steps.carry_out("t", [_step(handed_model, slicer_args=args)], _send)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert slicer == []

    @pytest.mark.parametrize("model", ["/etc/passwd", "~/.ssh/id_rsa", "/tmp/anything.stl", "", "relative.stl"])
    def test_only_a_model_the_servers_handed_over_is_sliced(self, slicer, model):
        done, refused = served_steps.carry_out("t", [_step(model)], _send)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert slicer == []

    def test_a_model_in_the_folder_that_is_not_a_model_is_not_sliced(self, slicer):
        folder = served_makes.files_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "notes.gcode").write_text("G1\n")
        done, refused = served_steps.carry_out("t", [_step(str(folder / "notes.gcode"))], _send)
        assert refused["code"] == "LOCAL_STEP_REFUSED" and slicer == []

    def test_no_slicer_here_is_said_with_what_to_install(self, handed_model, monkeypatch):
        from kiln import slicer as slicer_mod

        def none(slicer_path=None):
            raise slicer_mod.SlicerNotFoundError("none")

        monkeypatch.setattr(slicer_mod, "find_slicer", none)
        _done, refused = served_steps.carry_out("add_feature_during_print", [_step(handed_model)], _send)
        assert refused["code"] == "SLICER_NEEDED" and "PrusaSlicer" in refused["error"]

    def test_a_slicer_that_fails_is_said_in_its_own_words(self, handed_model, slicer, monkeypatch):
        import subprocess

        monkeypatch.setattr(
            served_steps.subprocess, "run",
            lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 1, "", "All objects are outside of the print volume."),
        )
        _done, refused = served_steps.carry_out("t", [_step(handed_model)], _send)
        assert refused["code"] == "LOCAL_STEP_FAILED" and "outside of the print volume" in refused["error"]

    def test_a_result_that_cannot_be_sent_stops_the_call(self, slicer, handed_model):
        _done, refused = served_steps.carry_out("t", [_step(handed_model)], lambda p: (None, "offline"))
        assert refused["code"] == "FILE_NOT_SENT" and "offline" in refused["error"]


class TestTheFence:
    @pytest.mark.parametrize(
        "step",
        [
            {"kind": "start_print", "id": "go", "file": "x.gcode"},
            {"kind": "send_gcode", "id": "g", "command": "M104 S300"},
            {"kind": "shell", "id": "s", "command": "rm -rf ~"},
            {"kind": "slice", "id": "../../x", "model_path": "x", "slicer_args": []},
            {"kind": None, "id": "x"},
        ],
    )
    def test_a_kind_this_build_does_not_carry_out_runs_nothing(self, slicer, step):
        done, refused = served_steps.carry_out("some_tool", [step], _send)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert slicer == []

    def test_the_kinds_are_the_decided_ones(self):
        """One kind makes a file, two read, and one ACTS -- through the
        allow-list in ``ACT_TOOLS`` and nothing else (its fence is held by
        test_served_act_steps.py).  Adding a kind, or a tool a server may
        run on a printer, is a decision, and this is where it is recorded."""
        assert set(served_steps._STEPS) == {"slice"}
        assert set(served_steps._READS) == {"printer_facts", "print_history"}
        assert served_steps.ACT_KIND == "act"
        assert set(served_steps.ACT_TOOLS) == {
            "upload_file", "start_print", "pause_print", "resume_print", "set_speed_profile",
            "run_speed_schedule",
        }

    def test_a_tool_cannot_ask_for_many_steps(self, slicer, handed_model):
        steps = [_step(handed_model, id=f"s{i}") for i in range(served_steps.MAX_STEPS + 1)]
        done, refused = served_steps.carry_out("t", steps, _send)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED" and slicer == []

    @pytest.mark.parametrize(
        "answer",
        [
            {"status": "success", "local_steps": [{"kind": "slice"}]},
            {"status": "error", "local_steps": [{"kind": "slice"}]},
            {"status": "needs_local_step"},
            {"status": "needs_local_step", "local_steps": "slice"},
            "needs_local_step",
            None,
        ],
    )
    def test_only_an_answer_that_asks_is_read_as_asking(self, answer):
        assert served_steps.wanted(answer) == []


class TestThroughTheStub:
    @pytest.fixture
    def stub(self, monkeypatch, tmp_path):
        import kiln.server as server

        manifest = {"categories": {}, "tools": [{
            "name": "add_feature_during_print", "description": "Add a part to a paused print.",
            "tier": "pro",
            "parameters": {"type": "object", "properties": {
                "feature": {"type": "string"}, "gcode_path": {"type": "string"},
                "current_layer": {"type": "integer"}}, "required": ["feature", "gcode_path", "current_layer"]},
            "inputs": {"files": {"feature": "inline", "gcode_path": "one"}},
        }]}
        fake = tmp_path / "pkg" / "server.py"
        fake.parent.mkdir()
        (fake.parent / "pro_tool_manifest.json").write_text(json.dumps(manifest))
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
        return server, registered["add_feature_during_print"]

    def _files(self, tmp_path):
        (tmp_path / "hat.stl").write_bytes(b"solid hat\nendsolid hat\n")
        (tmp_path / "print.gcode").write_text(";LAYER_CHANGE\nG1 X1 Y1 E1\n")
        return str(tmp_path / "hat.stl"), str(tmp_path / "print.gcode")

    def test_the_ask_is_carried_out_and_the_same_call_is_made_again(
        self, stub, slicer, handed_model, monkeypatch, tmp_path,
    ):
        server, tool = stub
        calls: list[dict] = []

        def forwarded(name, **kwargs):
            calls.append(kwargs)
            if "step_results" not in kwargs:
                return {"status": "needs_local_step", "local_steps": [_step(handed_model)]}
            return {"status": "ok", "first_modified_layer": 41}

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        # Found end to end: a call that named an output folder had the
        # step's model saved THERE, outside the one folder a step may read.
        arrived: list = []
        real_arrive = served_makes.arrive

        def arrive(tool_name, ans, **kw):
            if served_steps.wanted(ans):
                arrived.append(kw.get("trip"))
            return real_arrive(tool_name, ans, **kw)

        monkeypatch.setattr(served_makes, "arrive", arrive)
        hat, gcode = self._files(tmp_path)
        answer = tool(feature=hat, gcode_path=gcode, current_layer=40)

        assert answer == {"status": "ok", "first_modified_layer": 41}
        assert arrived == [None]  # the step's model is not saved as the call's output
        assert len(calls) == 2 and len(slicer) == 1
        first, second = calls
        assert second["step_results"] == {"feature_slice": {"token": "tok-gcode-0123456789abcdef"}}
        # The same call otherwise: same files, same settings.
        assert {k: v for k, v in second.items() if k != "step_results"} == first

    def test_a_tool_that_asks_twice_is_told_no(self, stub, slicer, handed_model, monkeypatch, tmp_path):
        server, tool = stub
        monkeypatch.setattr(
            server, "_pro_api_call",
            lambda name, **kw: {"status": "needs_local_step", "local_steps": [_step(handed_model)]},
        )
        hat, gcode = self._files(tmp_path)
        answer = tool(feature=hat, gcode_path=gcode, current_layer=40)
        assert answer["code"] == "LOCAL_STEP_REFUSED" and len(slicer) == 1

    def test_a_refused_step_is_the_answer_and_nothing_is_called_again(self, stub, slicer, monkeypatch, tmp_path):
        server, tool = stub
        calls: list[dict] = []

        def forwarded(name, **kwargs):
            calls.append(kwargs)
            return {"status": "needs_local_step", "local_steps": [
                {"kind": "start_print", "id": "go", "file": "resume.3mf"}]}

        monkeypatch.setattr(server, "_pro_api_call", forwarded)
        hat, gcode = self._files(tmp_path)
        answer = tool(feature=hat, gcode_path=gcode, current_layer=40)
        assert answer["code"] == "LOCAL_STEP_REFUSED"
        assert len(calls) == 1 and slicer == []


class TestPrinterFacts:
    """What this install tells a served tool about its printers: what each
    one IS.  Never where it is or how to reach it."""

    CONFIG = {
        "default": {"type": "bambu", "printer_model": "bambu_a1", "host": "192.168.1.50",
                    "access_code": "12345678", "serial": "01P00A123456789"},
        "voron": {"type": "moonraker", "printer_model": "voron_2", "host": "voron.local",
                  "api_key": "SECRET-KEY-abcdef"},
    }

    @pytest.fixture
    def printers(self, monkeypatch):
        import kiln.server as server

        asked: list[str] = []

        class _Adapter:
            def get_printer_config(self):
                asked.append("voron")
                return {"printer": {"kinematics": "corexy"}}

        class _Registry:
            def get(self, name):
                return _Adapter()

        monkeypatch.setattr(server, "_read_config_printers", lambda: dict(self.CONFIG))
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "default")
        monkeypatch.setattr(server, "_get_registry", lambda: _Registry())
        return asked

    def _read(self, **step):
        done, refused = served_steps.carry_out(
            "check_power_loss_recovery",
            [{"kind": "printer_facts", "id": "printer_facts", **step}],
            lambda p: (_ for _ in ()).throw(AssertionError("a read sends no file")),
        )
        return done, refused

    def test_each_printer_is_named_by_kind_and_model_with_when_it_was_read(self, printers):
        done, refused = self._read(printer_name="default", want=[])
        assert refused is None
        facts = done["printer_facts"]["data"]
        assert facts["read_at"].endswith("+00:00")
        assert facts["printers"] == [
            {"name": "default", "type": "bambu", "model": "bambu_a1", "is_default": True},
            {"name": "voron", "type": "moonraker", "model": "voron_2", "is_default": False},
        ]
        assert printers == []  # nothing was asked of any printer

    def test_no_address_serial_or_credential_ever_leaves(self, printers):
        done, _ = self._read(printer_name="voron", want=["klipper_config"])
        sent = json.dumps(done)
        for secret in ("192.168.1.50", "voron.local", "12345678", "01P00A123456789", "SECRET-KEY-abcdef"):
            assert secret not in sent
        assert "host" not in sent and "api_key" not in sent and "access_code" not in sent

    def test_a_klipper_configuration_is_read_only_when_asked_and_only_for_that_printer(self, printers):
        done, _ = self._read(printer_name="voron", want=["klipper_config"])
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["voron"]["klipper_config"] == {"printer": {"kinematics": "corexy"}}
        assert "klipper_config" not in by_name["default"]
        assert printers == ["voron"]

    def test_a_printer_that_cannot_be_asked_is_said_not_guessed(self, printers, monkeypatch):
        import kiln.server as server

        class _Down:
            def get(self, name):
                raise RuntimeError("connection refused to voron.local")

        monkeypatch.setattr(server, "_get_registry", lambda: _Down())
        done, refused = self._read(printer_name="voron", want=["klipper_config"])
        assert refused is None
        voron = [p for p in done["printer_facts"]["data"]["printers"] if p["name"] == "voron"][0]
        assert voron["klipper_config"] is None
        assert voron["klipper_config_error"] == "the printer could not be asked"
        assert "voron.local" not in json.dumps(done)  # not even in the error

    @pytest.mark.parametrize("want", [["access_code"], ["host"], ["klipper_config", "serial"], "klipper_config", [1]])
    def test_asking_for_anything_else_about_a_printer_sends_nothing(self, printers, want):
        done, refused = self._read(printer_name="default", want=want)
        assert done == {} and refused["code"] == "LOCAL_STEP_REFUSED"
        assert printers == []

    def test_an_install_with_no_printer_says_so_as_an_empty_list(self, monkeypatch):
        import kiln.server as server

        monkeypatch.setattr(server, "_read_config_printers", lambda: {})
        done, refused = self._read(printer_name="default", want=[])
        assert refused is None and done["printer_facts"]["data"]["printers"] == []

    @pytest.fixture
    def reading_printers(self, printers, monkeypatch):
        """Adapters that answer a status read, so a job (and the hardware
        moment filed for it) can be read for every printer."""
        import kiln.server as server
        from kiln.printers.base import JobProgress, PrinterState, PrinterStatus

        class _Printing:
            def get_state(self):
                return PrinterState(connected=True, state=PrinterStatus.PAUSED)

            def get_job(self):
                return JobProgress(file_name="bracket-hardware-stops.gcode", current_layer=29)

        class _Down:
            def get_state(self):
                raise RuntimeError("connection refused to voron.local")

            def get_job(self):
                raise AssertionError("never reached")

        monkeypatch.setattr(server, "_resolve_adapter", lambda name: _Printing() if name == "default" else _Down())
        return printers

    def test_the_hardware_moment_rides_with_the_job_when_asked_and_only_then(self, reading_printers, monkeypatch):
        """A served board of every machine waiting for hands reads what this
        install's own hardware_stops answers for each printer -- the same
        words its status gives -- and nothing is sent for a tool that did
        not ask (2026-10-09)."""
        import kiln.hardware_stops as hs

        seen = []

        def observe(adapter, state, job, **kw):
            seen.append(job.file_name)
            return {"stage": "now", "stop": 1, "of": 2, "insert": "2x M3 nut", "say": "Now is the time."}

        monkeypatch.setattr(hs, "observe", observe)
        done, refused = self._read(printer_name="default", want=["every_job", "hardware"])
        assert refused is None
        by_name = {p["name"]: p for p in done["printer_facts"]["data"]["printers"]}
        assert by_name["default"]["job"]["state"] == "paused"
        assert by_name["default"]["hardware"] == {
            "stage": "now", "stop": 1, "of": 2, "insert": "2x M3 nut", "say": "Now is the time.",
        }
        assert by_name["voron"]["hardware"] is None
        assert by_name["voron"]["hardware_error"] == "the printer could not be asked"
        assert "voron.local" not in json.dumps(done)
        assert seen == ["bracket-hardware-stops.gcode"]  # observed once, from the same read as the job

        done, _ = self._read(printer_name="default", want=["every_job"])
        assert all("hardware" not in p for p in done["printer_facts"]["data"]["printers"])

    def test_an_ordinary_print_has_no_hardware_moment(self, reading_printers, monkeypatch):
        import kiln.hardware_stops as hs

        monkeypatch.setattr(hs, "observe", lambda *a, **k: None)
        done, _ = self._read(printer_name="default", want=["job", "hardware"])
        default = [p for p in done["printer_facts"]["data"]["printers"] if p["name"] == "default"][0]
        assert "hardware" in default and default["hardware"] is None and "hardware_error" not in default


class TestPrintHistoryStep:
    """A served tool that learns from past prints is sent this install's own
    records: what the engine reads, and nothing that names a file or a
    person."""

    @pytest.fixture
    def history(self, tmp_path, monkeypatch):
        import kiln.persistence as persistence
        import kiln.server as server

        db = persistence.KilnDB(db_path=str(tmp_path / "prints.db"))
        for i in range(5):
            db.save_print_outcome({
                "job_id": f"job-{i}", "printer_name": "garage" if i < 4 else "office",
                "file_name": f"private-name-{i}.gcode", "file_hash": f"fingerprint-{i}",
                "material_type": "PLA", "outcome": "success" if i % 2 == 0 else "failed",
                "quality_grade": "good" if i % 2 == 0 else None,
                "failure_mode": None if i % 2 == 0 else "warping",
                "settings": {"temp_tool": 210 + i}, "environment": {"ambient_c": 21},
                "notes": "a private note", "agent_id": "someone", "determined_by": "observed",
                "created_at": 1_700_000_000.0 + i,
            })
        db.save_print_outcome({"job_id": "open", "printer_name": "garage", "outcome": "pending"})
        monkeypatch.setattr(persistence, "get_db", lambda: db)
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "garage")
        yield db
        db.close()

    def _read(self, **step):
        return served_steps.carry_out(
            "get_optimal_settings",
            [{"kind": "print_history", "id": "print_history", **step}],
            lambda p: (_ for _ in ()).throw(AssertionError("a read sends no file")),
        )

    def test_it_sends_the_decided_prints_newest_first_with_when_it_read_them(self, history):
        done, refused = self._read(printer_name="garage")
        assert refused is None
        facts = done["print_history"]["data"]
        assert facts["read_at"].endswith("+00:00") and facts["complete"] is True
        assert facts["printer_name"] == "garage"
        # Every printer this install has printed on; the print still running
        # is not a verdict and is not sent.
        assert [r["created_at"] for r in facts["outcomes"]] == [1_700_000_004.0 - i for i in range(5)]
        assert {r["printer_name"] for r in facts["outcomes"]} == {"garage", "office"}
        assert facts["outcomes"][0]["settings"] == {"temp_tool": 214}

    def test_nothing_that_names_a_file_or_a_person_leaves(self, history):
        done, _ = self._read(printer_name="garage")
        facts = done["print_history"]["data"]
        assert set(facts) == {"read_at", "printer_name", "outcomes", "complete"}
        for record in facts["outcomes"]:
            assert set(record) == set(served_steps._HISTORY_FIELDS)
        sent = json.dumps(facts)
        for private in ("private-name", "fingerprint-", "a private note", "someone", "job-"):
            assert private not in sent

    @pytest.mark.parametrize("asked", ["default", "", "active"])
    def test_my_printer_is_named_for_the_server(self, history, asked):
        done, _ = self._read(printer_name=asked)
        assert done["print_history"]["data"]["printer_name"] == "garage"

    def test_more_history_than_is_sent_is_said(self, history, monkeypatch):
        monkeypatch.setattr(served_steps, "_HISTORY_RECORDS", 3)
        facts = self._read(printer_name="garage")[0]["print_history"]["data"]
        assert len(facts["outcomes"]) == 3 and facts["complete"] is False
        assert facts["outcomes"][0]["created_at"] == 1_700_000_004.0

    def test_it_is_cut_to_what_the_servers_accept_keeping_the_newest(self, history, monkeypatch):
        monkeypatch.setattr(served_steps, "_HISTORY_BYTES", 600)
        facts = self._read(printer_name="garage")[0]["print_history"]["data"]
        assert 1 <= len(facts["outcomes"]) < 5 and facts["complete"] is False
        assert facts["outcomes"][0]["created_at"] == 1_700_000_004.0
        assert len(json.dumps(facts["outcomes"])) <= 600

    def test_a_record_that_cannot_be_read_sends_nothing(self, monkeypatch):
        import kiln.persistence as persistence

        def broken():
            raise OSError("disk")

        monkeypatch.setattr(persistence, "get_db", broken)
        done, refused = self._read(printer_name="garage")
        assert done == {} and refused["code"] == "PRINT_HISTORY_NOT_READ"
        assert "Nothing was sent" in refused["error"]

    def test_an_install_that_never_printed_sends_an_empty_history(self, tmp_path, monkeypatch):
        import kiln.persistence as persistence

        db = persistence.KilnDB(db_path=str(tmp_path / "empty.db"))
        monkeypatch.setattr(persistence, "get_db", lambda: db)
        facts = self._read(printer_name="garage")[0]["print_history"]["data"]
        assert facts["outcomes"] == [] and facts["complete"] is True
        db.close()
