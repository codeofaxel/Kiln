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
        calls.append({"url": url, "headers": kw.get("headers", {}),
                      "data": kw.get("data")})
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

    def test_the_installs_printer_rides_the_upload(self, tmp_path, monkeypatch):
        """The /view page draws the maker's bed only if the upload names the
        machine — resolved through the same stage_plate resolver the inline
        stage uses, so the two surfaces cannot disagree."""
        calls = _wire(monkeypatch)
        monkeypatch.setattr(
            "kiln.stage_plate.resolve_stage_plate",
            lambda *a, **k: {"source": "printer", "printer_id": "prusa_mk4"},
        )
        assert stage_link.stage_link_for(_stl(tmp_path / "part.stl"))
        assert calls[0]["data"] == {"printer": "prusa_mk4"}

    def test_an_unknown_printer_uploads_without_a_claim(self, tmp_path, monkeypatch):
        calls = _wire(monkeypatch)
        monkeypatch.setattr(
            "kiln.stage_plate.resolve_stage_plate",
            lambda *a, **k: {"source": "default", "printer_id": None},
        )
        assert stage_link.stage_link_for(_stl(tmp_path / "part.stl"))
        assert calls[0]["data"] is None

    def test_a_printer_change_is_a_new_link_not_a_stale_bed(
        self, tmp_path, monkeypatch
    ):
        """The cache is keyed by (bytes, printer): swapping the configured
        machine between calls must re-stage, or the link keeps claiming the
        old bed for its whole half-hour."""
        calls = _wire(monkeypatch)
        plate = {"source": "printer", "printer_id": "bambu_a1"}
        monkeypatch.setattr(
            "kiln.stage_plate.resolve_stage_plate", lambda *a, **k: plate
        )
        path = _stl(tmp_path / "part.stl")
        assert stage_link.stage_link_for(path)["cached"] is False
        plate["printer_id"] = "prusa_xl"
        assert stage_link.stage_link_for(path)["cached"] is False
        assert len(calls) == 2

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


class TestInputVersusOutputMesh:
    """A mesh-changer reports both meshes; the link must name the RESULT.

    Dict order must decide nothing — a repair that linked its input would
    hand the user the broken version and call it the fix.
    """

    def test_input_key_never_wins_even_when_it_comes_first(self):
        assert stage_link.find_mesh_path(
            {"input_mesh_path": "/x/before.stl", "repaired_stl": "/x/after.stl"}
        ) == "/x/after.stl"

    def test_input_key_never_wins_even_when_it_comes_last(self):
        assert stage_link.find_mesh_path(
            {"repaired_stl": "/x/after.stl", "input_mesh_path": "/x/before.stl"}
        ) == "/x/after.stl"

    def test_an_input_only_result_gets_no_link(self):
        """Better nothing than a link to the thing they already had."""
        assert stage_link.find_mesh_path({"source_mesh": "/x/before.stl"}) is None
        assert stage_link.find_mesh_path({"original_stl_path": "/x/b.stl"}) is None

    @pytest.mark.parametrize("marker", ["input", "source", "original", "before", "src"])
    def test_every_input_marker_is_disqualified(self, marker):
        assert stage_link.find_mesh_path({f"{marker}_stl": "/x/b.stl"}) is None

    def test_named_product_beats_a_bare_mesh_key(self):
        assert stage_link.find_mesh_path(
            {"mesh": "/x/tmp.stl", "output_stl": "/x/real.stl"}
        ) == "/x/real.stl"


class TestAsyncAttachDoesNotStallTheLoop:
    """The dispatch hook is a coroutine; a blocking upload there freezes the
    whole local server.  This pins the offload."""

    def test_loop_keeps_running_during_a_slow_upload(self, tmp_path, monkeypatch):
        import asyncio

        _wire(monkeypatch)
        import httpx

        def _slow_post(url, **kw):
            time.sleep(0.5)  # a real upload, in real seconds
            return _Resp()

        monkeypatch.setattr(httpx, "post", _slow_post)
        result = {"success": True, "stl_path": _stl(tmp_path / "p.stl")}

        async def _run():
            ticks = 0

            async def _heartbeat():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.05)
                    ticks += 1

            beat = asyncio.create_task(_heartbeat())
            await stage_link.attach_stage_link_async(result)
            beat.cancel()
            return ticks

        ticks = asyncio.run(_run())
        assert result["viewer_url"], "link was not attached"
        # A blocked loop cannot tick.  ~0.5s of upload at a 0.05s beat is
        # ~10 ticks; anything above a couple proves the loop stayed alive.
        assert ticks >= 3, (
            f"event loop stalled during the upload (only {ticks} ticks) — a "
            "local server would be frozen for the whole transfer"
        )

    def test_async_variant_never_raises(self, monkeypatch):
        import asyncio

        def _boom(*a, **k):
            raise RuntimeError("nope")

        monkeypatch.setattr(stage_link, "find_mesh_path", _boom)
        r = {"success": True}
        assert asyncio.run(stage_link.attach_stage_link_async(r)) is r

    def test_async_variant_skips_work_when_there_is_no_mesh(self, monkeypatch):
        import asyncio

        calls = _wire(monkeypatch)
        r = {"success": True, "message": "no geometry here"}
        asyncio.run(stage_link.attach_stage_link_async(r))
        assert calls == [] and "viewer_url" not in r


class TestNeverRaisesContract:
    """``stage_link_for`` documents "never raises".  Tools run in a thread
    pool, so a concurrent eviction between the cache read and the expiry read
    is reachable — and it used to escape as a KeyError."""

    def test_concurrent_eviction_does_not_raise(self, tmp_path, monkeypatch):
        _wire(monkeypatch)
        p = _stl(tmp_path / "p.stl")
        stage_link.stage_link_for(p)  # populate
        real_get = stage_link._cache_get

        def _evict_then_hit(sha):
            entry = real_get(sha)
            stage_link._cache.pop(sha, None)  # another thread got there first
            return entry

        monkeypatch.setattr(stage_link, "_cache_get", _evict_then_hit)
        got = stage_link.stage_link_for(p)  # must not raise
        assert got["viewer_url"]

    def test_parallel_callers_all_get_a_link(self, tmp_path, monkeypatch):
        """The real shape: many threads staging the same mesh at once."""
        from concurrent.futures import ThreadPoolExecutor

        _wire(monkeypatch)
        p = _stl(tmp_path / "p.stl")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: stage_link.stage_link_for(p), range(24)))
        assert all(r and r["viewer_url"] for r in results), "a parallel caller got nothing"


class TestGenericProductKeyIsFound:
    """Regression for 2026-08-01: the inline stage opened EMPTY.

    ``apply_geometric_texture`` reports its mesh under ``output_path``. The key
    matcher required the key itself to say stl/3mf/mesh/obj, so a verified
    ``.stl`` sitting under a generic product key was rejected — no mesh found,
    no token minted, no geometry attached. The tool is still stamped as
    stage-bearing, so the panel opened with its chrome and nothing inside: a
    silent failure with no error anywhere to read.

    The value's suffix is ground truth; the key name only disambiguates WHICH
    mesh a result means. It must never veto a suffix that already checked out.
    """

    def test_mesh_under_a_generic_output_key_is_found(self):
        assert (
            stage_link.find_mesh_path({"status": "success", "output_path": "/t/a.stl"})
            == "/t/a.stl"
        )

    def test_the_incident_result_shape_resolves(self):
        """apply_geometric_texture's real return, verbatim in shape."""
        got = stage_link.find_mesh_path(
            {
                "status": "success",
                "output_path": "/t/coaster_lava_deboss.stl",
                "input_path": "/t/coaster.stl",
                "scad_path": "/t/coaster_deboss.scad",
                "dat_path": "/t/lava_heightmap.dat",
            }
        )
        assert got == "/t/coaster_lava_deboss.stl", "the product mesh must win"

    def test_an_input_is_still_never_the_answer(self):
        """Broadening the key match must not start linking the mesh handed IN —
        a repair would hand back the broken version and call it the fix."""
        assert stage_link.find_mesh_path({"input_path": "/t/in.stl"}) is None
        assert stage_link.find_mesh_path({"source_mesh": "/t/in.stl"}) is None
        assert (
            stage_link.find_mesh_path(
                {"input_path": "/t/in.stl", "output_path": "/t/out.stl"}
            )
            == "/t/out.stl"
        )

    def test_a_non_mesh_under_a_product_key_is_ignored(self):
        """The suffix check is the other half of the pair: a generic key only
        counts when the value is actually a mesh."""
        for value in ("/t/out.scad", "/t/out.png", "/t/out.gcode", "/t/outdir"):
            assert stage_link.find_mesh_path({"output_path": value}) is None, value
