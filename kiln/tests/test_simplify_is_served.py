"""``simplify_mesh_model`` on an install without kiln-pro is answered by Kiln's servers.

Public Kiln has no simplifier of its own.  Signed in, the file goes up, the
same tool runs on Kiln's servers, and the simplified file lands where the
call asked for it.  Signed out, offline, unanswered or refused, nothing is
written, the file given is left as it was, and the answer says which.

Driven through the real MCP dispatch with the bundled manifest; only the
network is stood in for (the harness of ``test_served_public.py``).

Coverage: the call sent and the file brought home; the servers' own refusal
kept word for word; each of the four misses worded with its cause and
writing nothing; a missing file said here without asking; the call made
outside the dispatcher; and that no simplifier is left in this package.
"""

from __future__ import annotations

import socket

import pytest

import kiln.server as srv
from tests._signed_in import sign_out
from tests.test_served_public import (  # noqa: F401 -- fixtures
    MADE,
    SERVER_DIR,
    STL,
    _call,
    part,
    servers,
)

TOOL = "simplify_mesh_model"


def _nothing_written(part, before: bytes) -> bool:
    """The file given is as it was and nothing new sits beside it."""
    return part.read_bytes() == before and sorted(p.name for p in part.parent.iterdir() if p.suffix == ".stl") == [part.name]


class TestSignedInTheServersSimplifyIt:
    def test_the_file_goes_up_and_the_result_lands_where_it_was_asked(self, servers, part, tmp_path):
        wanted = tmp_path / "out" / "small.stl"
        small = b"solid small\nendsolid small\n"
        servers.answer = {
            "success": True,
            "path": f"{SERVER_DIR}/small.stl",
            "original_triangles": 1000,
            "simplified_triangles": 500,
            "watertight_after": True,
            "files": [{
                "at": ["path"], "param": "output_path", "filename": "small.stl",
                "format": "stl", "artifact_token": MADE,
            }],
        }
        servers.files[MADE] = small
        result = _call(TOOL, file_path=str(part), target_ratio=0.5, output_path=str(wanted))
        (upload,) = servers.uploads
        assert upload["data"] == STL
        ((name, sent),) = servers.calls
        assert name == TOOL and sent["target_ratio"] == 0.5
        assert sent["output_names"] == {"output_path": "small.stl"} and "output_path" not in sent
        assert result["success"] is True and result["simplified_triangles"] == 500
        assert result["path"] == str(wanted) and wanted.read_bytes() == small
        assert part.read_bytes() == STL

    def test_the_servers_own_refusal_is_the_answer_word_for_word(self, servers, part):
        refusal = {
            "success": False,
            "error": {
                "code": "MESH_TOO_LARGE_TO_SIMPLIFY",
                "message": "Kiln did not simplify part.stl: it has 2,000,000 triangles. Your file is unchanged.",
            },
        }
        servers.answer = refusal
        assert _call(TOOL, file_path=str(part)) == refusal
        assert _nothing_written(part, STL)

    def test_called_outside_the_dispatcher_it_still_asks_the_servers(self, servers, part):
        servers.answer = {"success": True, "path": str(part), "method": "unchanged"}
        tool = srv.mcp._tool_manager._tools[TOOL]
        assert tool.fn(file_path=str(part))["method"] == "unchanged"
        assert [called for called, _ in servers.calls] == [TOOL]
        # And the servers' refusal is passed on as they said it.
        refusal = {"success": False, "error": {"code": "EDIT_WOULD_DAMAGE", "message": "Kiln did not simplify part.stl."}}
        servers.answer = refusal
        assert tool.fn(file_path=str(part)) == refusal


class TestAMissWritesNothingAndSaysWhich:
    def test_signed_out_says_sign_in_and_never_a_plan(self, servers, monkeypatch, part):
        sign_out()
        monkeypatch.setattr(srv, "_served_bearer", lambda: ("", None), raising=False)
        result = _call(TOOL, file_path=str(part))
        assert result["success"] is False and result["why"] == "signed_out"
        assert result["error"] == (
            "Kiln simplifies a mesh on its servers, free on every plan. Kiln can't simplify "
            "part.stl right now because Kiln is signed out, so it wrote nothing and left part.stl "
            "as it was. Your slicer or the program that made the file can simplify it too, or "
            "sign in and try again."
        )
        assert "kiln signin" in result["setup_hint"] and "upgrade_url" not in result
        assert "Kiln Pro" not in result["error"]
        assert servers.calls == [] and servers.uploads == []
        assert _nothing_written(part, STL)

    def test_offline_says_offline(self, servers, monkeypatch, part):
        from kiln.served_answer import envelope_for_transport

        def offline(tool_name, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
            servers.calls.append((tool_name, kwargs))
            return envelope_for_transport(tool_name, socket.gaierror("no route"), host="https://kiln.test")

        monkeypatch.setattr(srv, "_pro_api_call", offline)
        result = _call(TOOL, file_path=str(part))
        assert result["success"] is False and result["why"] == "offline"
        assert "this computer is offline" in result["error"]
        assert "reconnect to the internet and try again" in result["error"]
        # The miss is the answer: the servers are not asked a second time
        # from inside the tool, and nothing claims it was answered here.
        assert len(servers.calls) == 1 and "answered_on_this_computer" not in result
        assert _nothing_written(part, STL)

    @pytest.mark.parametrize(
        ("status", "answer", "why", "says"),
        [
            (502, {"success": False, "error": "bad gateway"}, "unanswered", "Kiln's servers didn't answer"),
            (403, {"success": False, "error": "Not for this account.", "code": "FORBIDDEN"}, "refused", "Not for this account."),
        ],
    )
    def test_no_answer_and_a_refusal_say_which(self, servers, part, status, answer, why, says):
        servers.status, servers.answer = status, answer
        result = _call(TOOL, file_path=str(part))
        assert result["success"] is False and result["why"] == why
        assert says in result["error"] and "wrote nothing and left part.stl as it was" in result["error"]
        assert result["retryable"] is (why != "refused")
        assert _nothing_written(part, STL)

    def test_a_missing_file_is_said_here_without_asking(self, servers, tmp_path):
        result = _call(TOOL, file_path=str(tmp_path / "gone.stl"))
        assert result["success"] is False
        assert "No file at" in result["error"]["message"]
        assert servers.calls == [] and servers.uploads == []


class TestNoSimplifierIsLeftHere:
    def test_the_package_holds_no_simplify_function(self):
        import kiln.generation.validation as validation

        assert not hasattr(validation, "simplify_mesh")

    def test_the_bridge_is_a_hosted_door(self):
        from kiln.served_answer import HOSTED_DOORS

        assert HOSTED_DOORS["kiln._pro_simplify_bridge"][0] == "served_answer"
