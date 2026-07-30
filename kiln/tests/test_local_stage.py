"""The flag-gated inline 3D stage on a local install.

Dark by default: with ``KILN_LOCAL_STAGE`` unset nothing here runs, which is
the property that lets it ship before the host question is settled.
"""

from __future__ import annotations

import json
import struct

import pytest

from kiln import local_stage


def _stl(path, triangles: int = 2):
    data = bytearray(b"\x00" * 80) + struct.pack("<I", triangles)
    data += b"\x00" * (50 * triangles)
    path.write_bytes(bytes(data))
    return str(path)


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, payload, isError=False, structuredContent=None):
        self.content = [_Block(json.dumps(payload))] if payload is not None else []
        self.isError = isError
        self.structuredContent = structuredContent


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    local_stage._tokens.clear()
    monkeypatch.delenv(local_stage._ENABLE_ENV, raising=False)
    yield
    local_stage._tokens.clear()


class TestDarkByDefault:
    def test_disabled_without_the_flag(self):
        assert local_stage.enabled() is False

    def test_no_token_minted_when_off(self, tmp_path):
        r = _Result({"success": True, "stl_path": _stl(tmp_path / "a.stl")})
        assert local_stage.token_for_call_result(r) is None

    def test_install_is_a_noop_when_off(self):
        out = local_stage.install(object())  # would explode if it did anything
        assert out == {"enabled": False, "resource": False,
                       "payload_tool": False, "stamped": 0}


class TestTokenMinting:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(local_stage._ENABLE_ENV, "1")

    def test_mints_for_a_mesh_result_and_resolves_back(self, tmp_path):
        mesh = _stl(tmp_path / "part.stl")
        tok = local_stage.token_for_call_result(_Result({"success": True, "stl_path": mesh}))
        assert tok and local_stage.resolve(tok) == mesh

    def test_token_is_opaque_and_never_a_path(self, tmp_path):
        """It round-trips through the host; a user's disk layout must not."""
        mesh = _stl(tmp_path / "secret_project.stl")
        tok = local_stage.token_for_call_result(_Result({"success": True, "stl_path": mesh}))
        assert "/" not in tok and "secret_project" not in tok

    def test_no_token_for_a_result_without_geometry(self):
        assert local_stage.token_for_call_result(
            _Result({"success": True, "materials": ["PLA"]})) is None

    def test_no_token_for_a_failed_call(self, tmp_path):
        r = _Result({"success": False, "error": "nope",
                     "stl_path": _stl(tmp_path / "a.stl")})
        assert local_stage.token_for_call_result(r) is None

    def test_no_token_when_the_result_is_an_error(self, tmp_path):
        r = _Result({"success": True, "stl_path": _stl(tmp_path / "a.stl")}, isError=True)
        assert local_stage.token_for_call_result(r) is None

    def test_no_token_for_a_path_that_does_not_exist(self):
        assert local_stage.token_for_call_result(
            _Result({"success": True, "stl_path": "/nope/gone.stl"})) is None

    def test_input_mesh_never_becomes_the_staged_one(self, tmp_path):
        """A repair reports both; staging the input would show the break."""
        before, after = _stl(tmp_path / "b.stl"), _stl(tmp_path / "a.stl", 5)
        tok = local_stage.token_for_call_result(
            _Result({"success": True, "input_mesh_path": before, "repaired_stl": after}))
        assert local_stage.resolve(tok) == after

    def test_existing_hosted_token_is_left_alone(self, tmp_path):
        r = _Result({"success": True, "stl_path": _stl(tmp_path / "a.stl")},
                    structuredContent={"artifact": {"artifact_token": "hosted-tok"}})
        assert local_stage.token_for_call_result(r) is None

    def test_prose_content_does_not_raise(self):
        assert local_stage.token_for_call_result(_Result(None)) is None
        r = _Result(None)
        r.content = [_Block("just some prose, not JSON")]
        assert local_stage.token_for_call_result(r) is None

    def test_token_store_is_bounded(self, tmp_path):
        for i in range(local_stage._TOKENS_MAX + 20):
            local_stage.token_for_call_result(
                _Result({"success": True, "stl_path": _stl(tmp_path / f"m{i}.stl", i % 4 + 1)}))
        assert len(local_stage._tokens) <= local_stage._TOKENS_MAX

    def test_never_raises_on_a_junk_result(self):
        class _Junk:
            content = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        assert local_stage.token_for_call_result(_Junk()) is None


class TestInstallOnARealFastMCP:
    """Against a real FastMCP, since that is what `kiln serve` runs."""

    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(local_stage._ENABLE_ENV, "1")

    def _server(self):
        pytest.importorskip("kiln_pro._rest.mcp_apps")
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("test")

        @mcp.tool(name="compile_scad")
        def compile_scad() -> dict:
            return {"success": True}

        @mcp.tool(name="list_materials")
        def list_materials() -> dict:
            return {"success": True}

        return mcp

    def test_registers_resource_payload_tool_and_stamps_only_mesh_tools(self):
        mcp = self._server()
        out = local_stage.install(mcp)
        assert out["resource"] and out["payload_tool"] and out["token_hook"]
        tools = mcp._tool_manager._tools
        assert (tools["compile_scad"].meta or {})["ui"]["resourceUri"] == (
            "ui://kiln/mesh-viewer"
        )
        assert not (tools["list_materials"].meta or {}).get("ui"), (
            "stamped a tool that produces no mesh — every call would try to "
            "open a 3D panel"
        )

    def test_install_is_idempotent(self):
        mcp = self._server()
        local_stage.install(mcp)
        second = local_stage.install(mcp)
        assert second["resource"], "a second install must not break the first"
