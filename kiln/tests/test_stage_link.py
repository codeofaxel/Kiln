"""The turn-it-over link for a locally installed Kiln.

Regression cover for 2026-07-29: Kiln's 3D stage was reachable only through
the hosted connection, so a local install — how nearly every user runs it —
ended a design at a flat PNG with the mesh sitting right there on disk.
"""

from __future__ import annotations

import struct
import time

import pytest

from kiln import stage_link


def _stl(path, triangles: int = 2):
    data = bytearray(b"\x00" * 80) + struct.pack("<I", triangles)
    data += b"\x00" * (50 * triangles)
    path.write_bytes(bytes(data))
    return str(path)


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    stage_link._cache.clear()
    monkeypatch.delenv(stage_link._OPT_OUT_ENV, raising=False)
    yield
    stage_link._cache.clear()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {
            "viewer_url": "https://app.kiln3d.com/view#v=tok",
            "expires_in": 1800,
        }

    def json(self):
        return self._body


def _wire(monkeypatch, resp=None, record=None, token="bearer-abc"):
    """Point the module at a fake API and a signed-in bearer."""
    monkeypatch.setattr(
        "kiln.auth_session.resolve_api_bearer",
        lambda *a, **k: type("B", (), {"token": token, "state": "license"})(),
    )
    calls = record if record is not None else []

    def _post(url, **kw):
        calls.append({"url": url, "headers": kw.get("headers", {})})
        return resp or _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _post)
    return calls


class TestStageLinkForOneMesh:
    def test_returns_the_url_and_authenticates(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        got = stage_link.stage_link_for(_stl(tmp_path / "part.stl"))
        assert got["viewer_url"] == "https://app.kiln3d.com/view#v=tok"
        assert got["expires_at"] > time.time()
        assert calls[0]["url"].endswith("/api/view/mesh")
        assert calls[0]["headers"]["Authorization"] == "Bearer bearer-abc"

    def test_sixteen_poses_of_one_mesh_upload_once(self, tmp_path, monkeypatch):
        """The inspection-sheet case — the whole reason the cache is keyed on bytes."""
        calls = _wire(monkeypatch)
        p = _stl(tmp_path / "part.stl")
        first = stage_link.stage_link_for(p)
        for _ in range(15):
            again = stage_link.stage_link_for(p)
            assert again["viewer_url"] == first["viewer_url"]
            assert again["cached"] is True
        assert len(calls) == 1, f"uploaded {len(calls)} times for one mesh"

    def test_edited_mesh_at_the_same_path_gets_a_new_link(self, tmp_path, monkeypatch):
        """A design iterated in place keeps its filename and is a new object."""
        calls = _wire(monkeypatch)
        p = tmp_path / "part.stl"
        _stl(p, triangles=2)
        stage_link.stage_link_for(str(p))
        _stl(p, triangles=9)  # same path, different bytes
        stage_link.stage_link_for(str(p))
        assert len(calls) == 2, "cached on path instead of content"

    def test_signed_out_is_quiet(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch, token="")
        assert stage_link.stage_link_for(_stl(tmp_path / "p.stl")) is None
        assert calls == [], "attempted an upload with no bearer"

    def test_opt_out_makes_no_request(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        monkeypatch.setenv(stage_link._OPT_OUT_ENV, "1")
        assert stage_link.stage_link_for(_stl(tmp_path / "p.stl")) is None
        assert calls == []

    def test_oversize_mesh_is_skipped_not_attempted(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        p = tmp_path / "big.stl"
        p.write_bytes(b"\x00" * 16)
        monkeypatch.setattr(stage_link, "_MAX_UPLOAD_BYTES", 8)
        assert stage_link.stage_link_for(str(p)) is None
        assert calls == [], "started a doomed upload"

    def test_non_mesh_and_missing_files_are_skipped(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        png = tmp_path / "preview.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert stage_link.stage_link_for(str(png)) is None
        assert stage_link.stage_link_for(str(tmp_path / "nope.stl")) is None
        assert calls == []

    def test_transport_failure_returns_none(self, tmp_path, monkeypatch):
        _wire(monkeypatch)
        import httpx

        def _boom(*a, **k):
            raise httpx.ConnectError("down")

        monkeypatch.setattr(httpx, "post", _boom)
        assert stage_link.stage_link_for(_stl(tmp_path / "p.stl")) is None

    def test_api_error_is_not_cached(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch, resp=_Resp(status=503, body={}))
        p = _stl(tmp_path / "p.stl")
        assert stage_link.stage_link_for(p) is None
        assert stage_link.stage_link_for(p) is None
        assert len(calls) == 2, "cached a failure and stopped retrying"

    def test_nearly_expired_link_is_not_handed_out(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        p = _stl(tmp_path / "p.stl")
        stage_link.stage_link_for(p)
        sha = next(iter(stage_link._cache))
        url, _ = stage_link._cache[sha]
        stage_link._cache[sha] = (url, time.time() + 5)  # about to die
        stage_link.stage_link_for(p)
        assert len(calls) == 2, "handed out a link that expires while in use"


class TestFindMeshPath:
    def test_finds_a_produced_mesh(self):
        assert stage_link.find_mesh_path({"stl_path": "/x/a.stl"}) == "/x/a.stl"
        assert stage_link.find_mesh_path({"output_3mf": "/x/a.3mf"}) == "/x/a.3mf"
        assert stage_link.find_mesh_path({"mesh": "/x/a.obj"}) == "/x/a.obj"

    def test_finds_one_level_down(self):
        assert stage_link.find_mesh_path(
            {"artifact": {"mesh_path": "/x/a.stl"}}
        ) == "/x/a.stl"

    def test_ignores_non_mesh_keys_and_suffixes(self):
        assert stage_link.find_mesh_path({"preview_path": "/x/a.png"}) is None
        assert stage_link.find_mesh_path({"gcode_path": "/x/a.gcode"}) is None
        assert stage_link.find_mesh_path({"notes": "/x/a.stl"}) is None
        assert stage_link.find_mesh_path("not a dict") is None


class TestAttachStageLink:
    def test_attaches_url_and_a_hint(self, tmp_path, monkeypatch):
        _wire(monkeypatch)
        result = {"success": True, "stl_path": _stl(tmp_path / "p.stl")}
        stage_link.attach_stage_link(result)
        assert result["viewer_url"].startswith("https://app.kiln3d.com/view#v=")
        assert "viewer_expires_at" in result
        assert "viewer_url" in result["viewer_hint"] or "3D" in result["viewer_hint"]

    def test_does_not_overwrite_an_existing_url(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        result = {"success": True, "stl_path": _stl(tmp_path / "p.stl"),
                  "viewer_url": "https://app.kiln3d.com/view#v=already"}
        stage_link.attach_stage_link(result)
        assert result["viewer_url"].endswith("already")
        assert calls == [], "re-minted over a link the tool already had"

    def test_failure_shaped_results_are_left_alone(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        result = {"success": False, "error": "nope", "stl_path": _stl(tmp_path / "p.stl")}
        stage_link.attach_stage_link(result)
        assert "viewer_url" not in result
        assert calls == []

    def test_non_dict_results_pass_through_untouched(self, monkeypatch):
        _wire(monkeypatch)
        blocks = [{"type": "text", "text": "{}"}]
        assert stage_link.attach_stage_link(blocks) is blocks

    def test_never_raises_even_when_everything_is_broken(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(stage_link, "stage_link_for", _boom)
        result = {"success": True, "stl_path": _stl(tmp_path / "p.stl")}
        assert stage_link.attach_stage_link(result) is result
        assert "viewer_url" not in result
