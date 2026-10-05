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

    def test_no_step_kind_commands_a_printer(self):
        assert set(served_steps._STEPS) == {"slice"}

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
