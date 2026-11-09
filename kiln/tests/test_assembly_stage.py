"""The note a composed assembly keeps beside its file, and the block a stage
reads from it (:mod:`kiln.assembly_stage`).

Which parts print as one piece is answered by Kiln's servers.  These tests
stand in for the servers at the one place public Kiln calls them and pin what
public Kiln sends, what it keeps of each kind of answer, what it says when no
answer comes, and the shape every reader holds a block to.
"""

from __future__ import annotations

import json

import pytest
import trimesh

from kiln import _pro_assembly_bridge as bridge
from kiln import assembly_stage as stage
from kiln.assembly import Assembly, AssemblyPart, JointMotion, MatingInterface, compose_assembly
from kiln.served_answer import SESSION_EXPIRED_CODE, Miss

ONE_PIECE = {
    "pieces": [{"parts": ["body", "lid"], "joined": [{"parts": ["body", "lid"], "why": "captive"}]}],
    "kept_apart": [],
}
TWO_PIECES = {"pieces": [{"parts": ["body"], "joined": []}, {"parts": ["lid"], "joined": []}], "kept_apart": []}


def _assembly(tmp_path, *interfaces: MatingInterface) -> Assembly:
    assembly = Assembly(assembly_id="a", name="a")
    for k, name in enumerate(("body", "lid")):
        path = tmp_path / f"{name}.stl"
        trimesh.creation.box(extents=(4 + k, 4, 4)).export(path)
        assembly.parts.append(AssemblyPart(part_id=name, file_path=str(path), position_mm=(10.0 * k, 0.0, 0.0)))
    assembly.interfaces.extend(interfaces)
    return assembly


@pytest.fixture()
def servers(monkeypatch):
    """Kiln's servers as this install reaches them, with no kiln-pro here:
    every request recorded, the answer the test's."""
    import builtins

    real_import = builtins.__import__

    def no_kiln_pro(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_kiln_pro)
    calls: list[dict] = []
    state: dict = {"answer": {"schema": "joint_sweep_answer/1", "joints": [], "pieces": ONE_PIECE}}

    def call(tool, **kwargs):
        calls.append({k: v for k, v in kwargs.items() if not k.startswith("_")})
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer(calls[-1]) if callable(answer) else answer

    import kiln.server

    monkeypatch.setattr(kiln.server, "_pro_api_call", call)
    return calls, state


def _composed(tmp_path, assembly) -> tuple[dict, str]:
    out = str(tmp_path / "composed.stl")
    return compose_assembly(assembly, out), out


class TestWhatComposeAsksAndKeeps:
    def test_compose_asks_once_for_the_pieces_and_the_sweeps_together(self, tmp_path, servers):
        calls, _state = servers
        hinge = MatingInterface(
            part_a_id="body", part_b_id="lid", joint_type="clearance_fit", clearance_mm=0.4,
            motion=JointMotion(kind="hinge", axis_point_mm=(0, 0, 0), axis_direction=(1, 0, 0), range_min=0, range_max=90),
        )

        _composed(tmp_path, _assembly(tmp_path, hinge))

        (request,) = calls
        assert request["pieces"] is True and request["sweep"] == [0]
        assert [p["part_id"] for p in request["parts"]] == ["body", "lid"]

    def test_the_reply_and_the_block_say_the_pieces_as_answered(self, tmp_path, servers):
        reply, out = _composed(tmp_path, _assembly(tmp_path))

        assert reply["assembly"] == {"parts": 2, "pieces": 1, "kept_together": {"captive": 1}, "kept_apart_by_fit": 0}
        block = stage.stage_block(out)
        assert block["kind"] == "kiln.assembly.v1" and block["pieces"] == ONE_PIECE["pieces"]
        assert [(p["id"], p["spans"]) for p in block["parts"]] == [("body", [[0, 12]]), ("lid", [[12, 12]])]
        assert block["total_triangles"] == 24

    def test_an_assembly_of_one_part_asks_nothing_and_keeps_no_note(self, tmp_path, servers):
        calls, _state = servers
        assembly = _assembly(tmp_path)
        assembly.parts.pop()

        reply, out = _composed(tmp_path, assembly)

        assert calls == [] and "assembly" not in reply and stage.stage_block(out) is None

    def test_a_press_fit_found_with_a_gap_all_round_is_said_with_the_fix(self, tmp_path, servers):
        _calls, state = servers
        state["answer"] = {"schema": "joint_sweep_answer/1", "joints": [], "pieces": {
            **TWO_PIECES, "kept_apart": [{"parts": ["body", "lid"], "fit": "press_fit", "gap_all_round_mm": 0.4}],
        }}

        reply, _out = _composed(tmp_path, _assembly(tmp_path))

        assert reply["assembly"]["pieces"] == 2 and reply["assembly"]["kept_apart_by_fit"] == 1
        (said,) = reply["assembly"]["check"]
        assert "press fit declared between body and lid has a 0.40 mm gap all round" in said
        assert "declare a clearance fit instead" in said

    def test_an_answer_that_does_not_name_these_parts_is_not_kept(self, tmp_path, servers):
        _calls, state = servers
        state["answer"] = {"schema": "joint_sweep_answer/1", "joints": [], "pieces": {
            "pieces": [{"parts": ["body", "someone_elses_part"], "joined": []}], "kept_apart": [],
        }}

        reply, out = _composed(tmp_path, _assembly(tmp_path))

        assert reply["assembly"]["pieces"] is None and "left unsaid" in reply["assembly"]["pieces_why_not"]
        assert stage.stage_block(out) is None


class TestWhenNoAnswerComes:
    """The floor: the note keeps the parts, no block is made, a stage shows
    the file the way it shows any other, and the reply says why once."""

    @pytest.mark.parametrize(("miss", "cause", "fix"), [
        (Miss("offline"), "this computer is offline", "Reconnect to the internet and compose again."),
        (Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"), "Kiln is signed out", "Sign in and compose again."),
        (Miss("signed_out", SESSION_EXPIRED_CODE), "your Kiln session has expired", "Sign back in and compose again."),
        (Miss("unanswered"), "Kiln's piece reading didn't answer", "Wait a minute and compose again."),
        (Miss("refused", detail="Try again shortly."), "Kiln's piece reading said no", "Try again shortly."),
    ])
    def test_each_miss_is_said_in_its_own_words_and_nothing_is_guessed(self, tmp_path, monkeypatch, miss, cause, fix):
        monkeypatch.setattr(bridge, "_sent", lambda request, here: (None, miss))

        reply, out = _composed(tmp_path, _assembly(tmp_path))

        said = reply["assembly"]
        assert said["parts"] == 2 and said["pieces"] is None
        assert said["pieces_why_not"].startswith("Which of this assembly's parts print as one piece is read by Kiln's servers.")
        assert cause in said["pieces_why_not"] and said["pieces_why_not"].endswith(fix)
        assert "upgrade" not in said["pieces_why_not"].lower() and "pricing" not in said["pieces_why_not"]
        assert stage.stage_block(out) is None and stage.sidecar_for_mesh(out) is None
        assert "assembly" not in stage.attach_to_payload({"counts": {"triangles": 24}}, out)
        note = json.loads(open(stage.note_path_for(out), encoding="utf-8").read())
        assert [p["id"] for p in note["parts"]] == ["body", "lid"] and note["pieces"] is None

    def test_a_signed_out_install_is_told_to_sign_in_without_reaching_the_network(self, tmp_path, monkeypatch):
        """The real served door, with no sign-in: nothing is stood in."""
        import builtins

        real_import = builtins.__import__
        monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name))
                            if name.startswith("kiln_pro") else real_import(name, *a, **k))

        reply, _out = _composed(tmp_path, _assembly(tmp_path))

        assert reply["assembly"]["pieces"] is None
        assert "Kiln is signed out" in reply["assembly"]["pieces_why_not"]

    def test_servers_from_before_the_flag_still_sweep_and_the_pieces_are_said_unread(self, tmp_path, servers):
        calls, state = servers
        state["answer"] = lambda request: (
            {"success": False, "error": "Unknown parameters: pieces"} if "pieces" in request
            else {"schema": "joint_sweep_answer/1", "joints": []}
        )
        hinge = MatingInterface(
            part_a_id="body", part_b_id="lid", joint_type="clearance_fit", clearance_mm=0.4,
            motion=JointMotion(kind="hinge", axis_point_mm=(0, 0, 0), axis_direction=(1, 0, 0), range_min=0, range_max=90),
        )

        reply, out = _composed(tmp_path, _assembly(tmp_path, hinge))

        assert ["pieces" in c for c in calls] == [True, False] and calls[1]["sweep"] == [0]
        assert "newer than the Kiln that answered" in reply["assembly"]["pieces_why_not"]
        assert stage.stage_block(out) is None

    def test_an_assembly_the_servers_did_not_read_says_their_reason(self, tmp_path, servers):
        _calls, state = servers
        state["answer"] = {"schema": "joint_sweep_answer/1", "joints": [], "pieces": None,
                           "pieces_why_not": "the parts hold more triangles than one reading takes"}

        reply, _out = _composed(tmp_path, _assembly(tmp_path))

        assert reply["assembly"]["pieces_why_not"] == (
            "The pieces are left unsaid: the parts hold more triangles than one reading takes."
        )


class TestTheNoteBelongsToItsFile:
    def test_a_different_file_under_the_same_name_carries_no_record(self, tmp_path, servers):
        _reply, out = _composed(tmp_path, _assembly(tmp_path))
        assert stage.stage_block(out) is not None

        trimesh.creation.box(extents=(1, 1, 1)).export(out)

        assert stage.stage_block(out) is None

    def test_every_compose_asks_again(self, tmp_path, servers):
        calls, state = servers
        assembly = _assembly(tmp_path)
        _composed(tmp_path, assembly)
        state["answer"] = {"schema": "joint_sweep_answer/1", "joints": [], "pieces": TWO_PIECES}

        reply, out = _composed(tmp_path, assembly)

        assert len(calls) == 2 and reply["assembly"]["pieces"] == 2
        assert len(stage.stage_block(out)["pieces"]) == 2


class TestTheGuide:
    def test_a_guide_adds_each_parts_way_in_and_step(self, tmp_path, servers):
        _reply, out = _composed(tmp_path, _assembly(tmp_path))

        kept = stage.attach_guide(out, [
            {"part": "body", "direction": None, "step": 1},
            {"part": "lid", "direction": [0, 0, -2], "step": 2},
            {"part": "lid", "direction": [1, 0, 0], "step": 5},  # the first step naming a part is its own
            {"part": "not_in_the_note", "direction": [0, 0, 1], "step": 3},
        ])

        inserts = {p["id"]: p["insert"] for p in stage.stage_block(out)["parts"]}
        assert kept is True
        assert inserts == {"body": {"direction": None, "step": 1}, "lid": {"direction": [0.0, 0.0, -1.0], "step": 2}}

    def test_the_compose_tool_keeps_a_guides_steps_given_with_the_parts(self, tmp_path, servers, monkeypatch):
        import kiln.server
        from kiln.plugins.assembly_tools import plugin

        tools = {}

        class _Mcp:
            def tool(self, **_kwargs):
                return lambda fn: tools.setdefault(fn.__name__, fn)

        plugin.register(_Mcp())
        monkeypatch.setattr(kiln.server, "_check_auth", lambda scope: None)
        out = str(tmp_path / "composed.stl")

        reply = tools["compose_assembly_parts"](
            assembly_json=_assembly(tmp_path).to_dict(), output_path=out,
            guide_steps=[{"part": "body", "direction": None, "step": 1}, {"part": "lid", "direction": [0, 0, -1], "step": 2}],
        )

        assert reply["success"] is True and reply["data"]["assembly"]["guide_kept"] is True
        assert stage.stage_block(out)["parts"][1]["insert"] == {"direction": [0.0, 0.0, -1.0], "step": 2}

    def test_a_direction_that_is_not_one_is_skipped(self, tmp_path, servers):
        _reply, out = _composed(tmp_path, _assembly(tmp_path))

        assert stage.attach_guide(out, [{"part": "lid", "direction": [0, 0, 0], "step": 2}]) is False


def _block(**changes) -> dict:
    block = {
        "kind": "kiln.assembly.v1", "frame": "mesh", "total_triangles": 24,
        "parts": [{"id": "body", "spans": [[0, 12]], "insert": None},
                  {"id": "lid", "spans": [[12, 12]], "insert": {"direction": [0.0, 0.0, -1.0], "step": 2}}],
        "pieces": TWO_PIECES["pieces"], "kept_apart": [],
    }
    block.update(changes)
    return block


class TestABlockIsReadAsData:
    def test_the_block_kiln_writes_passes(self):
        assert stage.checked_block(_block()) is not None
        assert stage.checked_block(_block(pieces=ONE_PIECE["pieces"])) is not None
        assert stage.checked_block(_block(kept_apart=[{"parts": ["body", "lid"], "fit": "snap_fit"}])) is not None

    @pytest.mark.parametrize("changes", [
        {"kind": "kiln.assembly.v2"},
        {"total_triangles": 20},  # a run past the file's end
        {"parts": [{"id": "body", "spans": [[0, 13]], "insert": None}, {"id": "lid", "spans": [[12, 12]], "insert": None}]},
        {"parts": [{"id": "body", "spans": [[0, 12]], "insert": None}, {"id": "body", "spans": [[12, 12]], "insert": None}]},
        {"parts": [{"id": "body", "spans": [[0, 12]], "insert": {"direction": [0, 0, 2], "step": 1}},
                   {"id": "lid", "spans": [[12, 12]], "insert": None}]},
        {"pieces": None},
        {"pieces": [{"parts": ["body"], "joined": []}]},  # a part in no piece
        {"pieces": [{"parts": ["body", "lid"], "joined": []}, {"parts": ["lid"], "joined": []}]},  # a part in two
        {"pieces": [{"parts": ["body", "lid"], "joined": [{"parts": ["body", "lid"], "why": "glued_by_hand"}]}]},
        {"pieces": [{"parts": ["body", "lid"], "joined": [{"parts": ["body", "elsewhere"], "why": "touching"}]}]},
        {"kept_apart": [{"parts": ["body", "lid"], "fit": "wishful"}]},
        {"kept_apart": [{"parts": ["body", "lid"], "fit": "press_fit", "gap_all_round_mm": -1}]},
        {"pieces": ONE_PIECE["pieces"], "kept_apart": [{"parts": ["body", "lid"], "fit": "snap_fit"}]},
    ])
    def test_a_block_out_of_shape_is_refused_whole(self, changes):
        assert stage.checked_block(_block(**changes)) is None

    def test_an_answer_is_held_to_the_same_shape(self):
        assert stage.accepted(ONE_PIECE, ["body", "lid"]) == ONE_PIECE
        assert stage.accepted(ONE_PIECE, ["body", "lid", "hinge_pin"]) is None  # a part it does not name
        assert stage.accepted({"pieces": "all of them"}, ["body", "lid"]) is None
        assert stage.accepted(None, ["body", "lid"]) is None


class TestWhatTravelsToAStage:
    def test_the_local_stage_payload_carries_the_block(self, tmp_path, servers):
        _reply, out = _composed(tmp_path, _assembly(tmp_path))

        payload = stage.attach_to_payload({"counts": {"triangles": 24}}, out)

        assert payload["assembly"]["pieces"] == ONE_PIECE["pieces"] and len(payload["assembly"]["parts"]) == 2

    def test_a_simplified_mesh_keeps_the_pieces_and_drops_the_runs_with_why(self, tmp_path, servers):
        _reply, out = _composed(tmp_path, _assembly(tmp_path))

        payload = stage.attach_to_payload({"counts": {"triangles": 10}, "decimated_from": 24}, out)

        block = payload["assembly"]
        assert block["pieces"] == ONE_PIECE["pieces"] and block["parts"] == []
        assert "simplified" in block["parts_why_not"] and stage.checked_block(block) is not None

    def test_a_turned_part_keeps_each_step_and_loses_each_direction(self):
        block = _block()

        stage.turned(block)

        assert block["parts"][1]["insert"] == {"direction": None, "step": 2}

    def test_the_stage_payload_for_a_composed_file_carries_it(self, tmp_path, servers):
        from kiln.local_stage import _payload_for_mesh

        _reply, out = _composed(tmp_path, _assembly(tmp_path))

        payload = _payload_for_mesh(out)

        assert payload["assembly"]["kind"] == "kiln.assembly.v1"
        assert payload["assembly"]["pieces"] == ONE_PIECE["pieces"]

    def test_the_view_link_upload_sends_the_block_beside_the_file(self, tmp_path, servers, monkeypatch):
        from pathlib import Path

        from kiln import stage_link

        _reply, out = _composed(tmp_path, _assembly(tmp_path))

        sent = stage_link._assembly_sidecar(Path(out))

        assert json.loads(sent)["pieces"] == ONE_PIECE["pieces"]
        assert stage_link._assembly_sidecar(Path(str(tmp_path / "body.stl"))) is None  # a lone part has none
