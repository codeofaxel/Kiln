"""What ``decorate_surface`` does on an install without Kiln's server part.

Kiln's servers add three things to this tool (a depth raised, a recess under
a bottom-face decoration, a QR code).  On a plain install none of them is in
the process: a carve still runs here exactly as it always has, a call that
needs the servers is sent whole, and a miss is said in words.  The servers'
part is stood in for at the bridge's own seams.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiln import _pro_decorate_bridge as bridge
from kiln.served_answer import Miss

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="carving needs OpenSCAD"
)


@pytest.fixture
def plain_install(monkeypatch):
    """No server part in this process, and every ask of the servers kept."""
    import kiln.decoration_quota as allowance

    asked: list[dict] = []
    answer: dict = {"reply": (None, Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED"))}

    def on_servers(arguments):
        asked.append(dict(arguments))
        return answer["reply"]

    monkeypatch.setattr(bridge, "before_carve", lambda **_: None)
    monkeypatch.setattr(bridge, "qr_on_part", lambda **_: None)
    monkeypatch.setattr(bridge, "sketch_face", lambda *_: None)
    monkeypatch.setattr(bridge, "on_servers", on_servers)
    monkeypatch.setattr(bridge, "plan_includes_recess", lambda: False)
    monkeypatch.setattr(allowance, "check_decoration_quota", lambda *a, **k: (True, None))
    answer["asked"] = asked
    return answer


@pytest.fixture
def plate(tmp_path):
    import trimesh

    part = trimesh.creation.box(extents=(80.0, 40.0, 6.0))
    part.apply_translation((0.0, 0.0, 3.0))
    # A name of its own: the carve writes beside a shared folder by the
    # model's name, and parallel workers carving "plate.stl" collide.
    path = tmp_path / f"plate_{tmp_path.name}.stl"
    part.export(str(path))
    return str(path)


def _decorate(**kw):
    import kiln.server as server
    from kiln.tool_results import unwrap_tool_result

    return unwrap_tool_result(server.decorate_surface(**kw))


class TestAQrCodeIsTheServers:
    def test_signed_out_it_writes_nothing_and_says_why(self, plain_install, plate):
        answer = _decorate(model_path=plate, content="qr:https://kiln3d.com", face="top")
        assert answer["success"] is False and answer["why"] == "signed_out"
        assert "signed out" in answer["error"] and "Kiln Pro" in answer["error"]
        assert Path(plate).name in answer["error"]
        assert plain_install["asked"][0]["content"] == "qr:https://kiln3d.com"

    def test_offline_is_said_as_offline_and_worth_another_try(self, plain_install, plate):
        plain_install["reply"] = (None, Miss("offline"))
        answer = _decorate(model_path=plate, content="qr:https://kiln3d.com")
        assert answer["why"] == "offline" and answer["retryable"] is True

    def test_the_servers_own_answer_is_handed_back_whole(self, plain_install, plate):
        made = {"status": "success", "qr_pads_stl": "/x/pads.stl", "print_recipe": {"qr_color": "black"}}
        plain_install["reply"] = (made, None)
        answer = _decorate(model_path=plate, content="qr:https://kiln3d.com")
        assert answer["qr_pads_stl"] == "/x/pads.stl" and answer["print_recipe"]["qr_color"] == "black"

    def test_the_servers_refusal_is_theirs_word_for_word(self, plain_install, plate):
        plain_install["reply"] = (None, Miss("refused", "TIER_REQUIRED", "A QR code on a part needs Kiln Pro."))
        answer = _decorate(model_path=plate, content="qr:https://kiln3d.com")
        assert answer["code"] == "TIER_REQUIRED" and answer["retryable"] is False
        assert "A QR code on a part needs Kiln Pro." in answer["error"]


@needs_openscad
class TestACarveStillRunsHere:
    def test_a_top_face_word_asks_nobody(self, plain_install, plate):
        carved = _decorate(model_path=plate, content="text:KILN", face="top", depth_mm=0.6)
        assert carved["success"] is True and plain_install["asked"] == []
        assert "changed_for_you" not in carved and "pro_adds" not in carved

    def test_a_bottom_face_word_on_a_plan_without_the_recess_asks_nobody(self, plain_install, plate):
        carved = _decorate(model_path=plate, content="text:KILN", face="bottom", depth_mm=0.6)
        assert carved["success"] is True and plain_install["asked"] == []
        assert any("flattened" in w for w in carved["warnings"])

    def test_with_the_plan_the_servers_run_it_and_their_answer_comes_back(
        self, plain_install, plate, monkeypatch,
    ):
        monkeypatch.setattr(bridge, "plan_includes_recess", lambda: True)
        theirs = {"success": True, "output_stl": "/x/out.stl", "changed_for_you": ["Put the decoration in a recess."]}
        plain_install["reply"] = (theirs, None)
        carved = _decorate(model_path=plate, content="text:KILN", face="bottom", depth_mm=0.6)
        assert carved == theirs
        assert plain_install["asked"][0]["face"] == "bottom"

    def test_with_the_plan_a_miss_carves_here_and_says_why_once(
        self, plain_install, plate, monkeypatch,
    ):
        from kiln import served_public

        served_public._reset_for_tests()
        monkeypatch.setattr(bridge, "plan_includes_recess", lambda: True)
        plain_install["reply"] = (None, Miss("offline"))
        first = _decorate(model_path=plate, content="text:KILN", face="bottom", depth_mm=0.6)
        again = _decorate(model_path=plate, content="text:KILN", face="bottom", depth_mm=0.6)
        assert first["success"] is True and again["success"] is True
        said = [w for w in first["warnings"] if "offline" in w]
        assert len(said) == 1 and "recess" in said[0]
        assert not [w for w in again["warnings"] if "offline" in w]

    def test_a_photo_on_the_bottom_is_never_sent_for_a_recess(self, plain_install, plate, monkeypatch, tmp_path):
        from PIL import Image

        monkeypatch.setattr(bridge, "plan_includes_recess", lambda: True)
        art = tmp_path / "photo.png"
        Image.radial_gradient("L").resize((96, 96)).save(art)
        _decorate(model_path=plate, content=str(art), face="bottom")
        assert plain_install["asked"] == []


def _step_converts() -> bool:
    try:
        from kiln.step_import import _ocp_available

        return bool(_ocp_available())
    except Exception:  # noqa: BLE001 -- no backend, no test
        return False


@needs_openscad
@pytest.mark.skipif(not _step_converts(), reason="turning a STEP file into a mesh needs the CAD backend")
class TestACadFileComesInThroughTheSharedDoor:
    def test_a_word_is_carved_on_a_real_step_export_and_the_reply_says_so(self, plain_install):
        from pathlib import Path

        import trimesh

        step = Path(__file__).parent / "real_exports" / "enclosure_v2.step"
        carved = _decorate(model_path=str(step), content="text:KILN", face="auto", depth_mm=0.8)
        assert carved["success"] is True, carved
        assert any("STEP" in line for line in carved["changed_for_you"]), carved
        assert trimesh.load(carved["output_stl"]).is_volume
