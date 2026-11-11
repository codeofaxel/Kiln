"""A Meshy task Kiln did not start is found, read, and downloaded like its own.

A person can make a model in Meshy's own app and hand Kiln the task id.  Kiln
asks each kind of Meshy task for it in turn (text, image, multi-image,
retexture, remesh), stops on an answer that is about the key or the service
rather than the kind, remembers the kind it found, and gives the download the
same handling as a job Kiln started: the words asked for, the MTL and
textures beside an OBJ, the format actually chosen, and a refusal a person can
act on when the task is missing, unfinished, failed, or past its download
links.  All HTTP is stubbed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import responses

from kiln.generation.base import GenerationAuthError, GenerationError, GenerationStatus
from kiln.generation.job_records import JobRecord
from kiln.generation.meshy import _BASE_URL as V2
from kiln.generation.meshy import _BASE_URL_V1 as V1
from kiln.generation.meshy import MeshyProvider

TASK = "0193-outside-task"

#: Every kind, in the order Kiln asks.
PROBE_URLS = [
    f"{V2}/text-to-3d/{TASK}",
    f"{V1}/image-to-3d/{TASK}",
    f"{V1}/multi-image-to-3d/{TASK}",
    f"{V1}/retexture/{TASK}",
    f"{V1}/remesh/{TASK}",
]

#: A 10 x 10 mm footprint, 40 mm tall, written in glTF's frame (+Y up).
OBJ = (
    b"mtllib model.mtl\n"
    b"v 0 0 0\nv 10 0 0\nv 10 0 10\nv 0 0 10\n"
    b"v 0 40 0\nv 10 40 0\nv 10 40 10\nv 0 40 10\n"
    b"f 1 2 3\nf 1 3 4\nf 5 7 6\nf 5 8 7\n"
    b"f 1 5 6\nf 1 6 2\nf 2 6 7\nf 2 7 3\n"
    b"f 3 7 8\nf 3 8 4\nf 4 8 5\nf 4 5 1\n"
)

NOT_FOUND = {"message": "Task not found"}


def _later_ms(seconds: float = 3600) -> int:
    return int((time.time() + seconds) * 1000)


def _image_task(**extra) -> dict:
    body = {
        "id": TASK,
        "type": "image-to-3d",
        "status": "SUCCEEDED",
        "progress": 100,
        "model_urls": {"obj": "https://assets.meshy.ai/t/model.obj?sig=1"},
        "expires_at": _later_ms(),
    }
    body.update(extra)
    return body


def _code(fn, *args, **kwargs) -> GenerationError:
    with pytest.raises(GenerationError) as caught:
        fn(*args, **kwargs)
    return caught.value


@pytest.fixture
def provider(monkeypatch):
    # A plain install: no deployment store, the provider's own memory.
    monkeypatch.setitem(sys.modules, "kiln_pro.hosted_generation_jobs", None)
    return MeshyProvider(api_key="test-key")


class TestFindingTheKind:
    @responses.activate
    def test_an_image_task_is_found_downloaded_turned_and_remembered(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json=_image_task())
        responses.add(responses.GET, "https://assets.meshy.ai/t/model.obj?sig=1", body=OBJ)

        result = provider.download_result(TASK, output_dir=str(tmp_path))

        assert [c.request.url for c in responses.calls[:2]] == PROBE_URLS[:2]
        assert result.format == "obj"
        from kiln.mesh_frame import load_mesh

        extents = load_mesh(result.local_path, force="mesh", process=False).extents
        assert extents[2] == pytest.approx(40.0)

        asked = len(responses.calls)
        job = provider.get_job_status(TASK)
        assert job.status is GenerationStatus.SUCCEEDED
        assert len(responses.calls) == asked + 1
        assert responses.calls[-1].request.url == PROBE_URLS[1]

    @responses.activate
    def test_a_text_tasks_words_reach_the_result_and_the_note_beside_it(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json={
            "id": TASK, "type": "text-to-3d-refine", "status": "SUCCEEDED", "progress": 100,
            "prompt": "a ceramic owl with folded wings",
            "model_urls": {"obj": "https://assets.meshy.ai/t/owl.obj"},
        })
        responses.add(responses.GET, "https://assets.meshy.ai/t/owl.obj", body=OBJ)

        result = provider.download_result(TASK, output_dir=str(tmp_path))

        assert result.prompt == "a ceramic owl with folded wings"
        from kiln.arrival import read

        note = read(result.local_path)
        assert note is not None
        assert note.prompt == "a ceramic owl with folded wings"
        assert note.by == "Meshy"

    @responses.activate
    def test_an_image_task_with_a_material_brings_its_mtl_and_textures(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json=_image_task(
            model_urls={
                "obj": "https://assets.meshy.ai/t/model.obj",
                "mtl": "https://assets.meshy.ai/t/model.mtl",
            },
            texture_urls=[{"base_color": "https://assets.meshy.ai/t/texture_0.png"}],
            texture_prompt="weathered bronze",
        ))
        responses.add(responses.GET, "https://assets.meshy.ai/t/model.obj", body=OBJ)
        responses.add(responses.GET, "https://assets.meshy.ai/t/model.mtl",
                      body=b"newmtl m\nmap_Kd texture_0.png\n")
        responses.add(responses.GET, "https://assets.meshy.ai/t/texture_0.png", body=b"\x89PNG fake")

        result = provider.download_result(TASK, output_dir=str(tmp_path))

        mtl = tmp_path / f"{TASK}.mtl"
        assert mtl.is_file()
        assert f"map_Kd {TASK}_texture_0.png" in mtl.read_text()
        assert (tmp_path / f"{TASK}_texture_0.png").read_bytes() == b"\x89PNG fake"
        assert result.prompt == "weathered bronze"

    @responses.activate
    def test_a_task_no_kind_knows_is_said_plainly_after_asking_each_once(self, provider):
        for url in PROBE_URLS:
            responses.add(responses.GET, url, json=NOT_FOUND, status=404)

        exc = _code(provider.get_job_status, TASK)

        assert exc.code == "TASK_NOT_FOUND"
        assert "different Meshy account" in str(exc)
        assert TASK in str(exc)
        assert [c.request.url for c in responses.calls] == PROBE_URLS

    @responses.activate
    def test_a_bad_key_stops_at_the_first_answer(self, provider):
        responses.add(responses.GET, PROBE_URLS[0], json={"message": "Unauthorized"}, status=401)

        with pytest.raises(GenerationAuthError) as caught:
            provider.get_job_status(TASK)

        assert caught.value.code == "AUTH_INVALID"
        assert len(responses.calls) == 1

    @responses.activate
    def test_an_account_out_of_credits_stops_and_says_so(self, provider):
        responses.add(responses.GET, PROBE_URLS[0], json={"message": "Payment required"}, status=402)

        exc = _code(provider.get_job_status, TASK)

        assert exc.code == "NO_CREDITS"
        assert len(responses.calls) == 1


class TestWhatADownloadSays:
    @responses.activate
    def test_a_task_still_being_made_says_how_far_along_it_is(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json={
            "id": TASK, "type": "image-to-3d", "status": "IN_PROGRESS", "progress": 42,
        })

        exc = _code(provider.download_result, TASK, output_dir=str(tmp_path))

        assert exc.code == "NOT_READY"
        assert "42%" in str(exc)
        assert "await_generation" in str(exc)

    @responses.activate
    def test_a_failed_task_carries_meshys_own_words(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json={
            "id": TASK, "type": "text-to-3d-preview", "status": "FAILED", "progress": 0,
            "task_error": {"message": "The prompt could not be turned into a shape."},
        })

        exc = _code(provider.download_result, TASK, output_dir=str(tmp_path))

        assert exc.code == "GENERATION_FAILED"
        assert "The prompt could not be turned into a shape." in str(exc)

    @responses.activate
    def test_a_link_past_its_time_is_said_and_carries_no_key(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json=_image_task())
        responses.add(responses.GET, "https://assets.meshy.ai/t/model.obj?sig=1",
                      body="<Error>AccessDenied</Error>", status=403)

        exc = _code(provider.download_result, TASK, output_dir=str(tmp_path))

        assert exc.code == "RESULT_EXPIRED"
        assert "Export it again from Meshy" in str(exc)
        asset = responses.calls[-1].request
        assert asset.url.startswith("https://assets.meshy.ai/")
        assert "Authorization" not in asset.headers

    @responses.activate
    def test_an_expiry_already_passed_is_said_before_asking_the_link(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json=_image_task(expires_at=_later_ms(-60)))

        exc = _code(provider.download_result, TASK, output_dir=str(tmp_path))

        assert exc.code == "RESULT_EXPIRED"
        assert all("assets.meshy.ai" not in c.request.url for c in responses.calls)

    @responses.activate
    def test_an_stl_only_task_downloads_its_stl_and_says_so(self, provider, tmp_path):
        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[2], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[3], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[4], json={
            "id": TASK, "type": "remesh", "status": "SUCCEEDED", "progress": 100,
            "model_urls": {"stl": "https://assets.meshy.ai/t/remeshed.stl"},
        })
        responses.add(responses.GET, "https://assets.meshy.ai/t/remeshed.stl", body=b"solid t\nendsolid t\n")

        result = provider.download_result(TASK, output_dir=str(tmp_path))

        assert result.format == "stl"
        assert Path(result.local_path).suffix == ".stl"
        assert provider.get_job_status(TASK).format == "stl"


class TestJobsKilnStarted:
    @responses.activate
    def test_an_image_job_is_read_at_the_v1_endpoint(self, provider):
        responses.add(responses.POST, f"{V2}/image-to-3d", json={"result": "task-img"})
        responses.add(responses.GET, f"{V1}/image-to-3d/task-img",
                      json={"status": "IN_PROGRESS", "progress": 30})
        provider.generate("", image_url="https://example.com/p.png")

        job = provider.get_job_status("task-img")

        assert (job.status, job.progress) == (GenerationStatus.IN_PROGRESS, 30)
        assert responses.calls[0].request.url == f"{V2}/image-to-3d"
        assert responses.calls[1].request.url == f"{V1}/image-to-3d/task-img"

    @responses.activate
    def test_an_image_job_v1_does_not_hold_is_read_at_v2(self, provider):
        responses.add(responses.POST, f"{V2}/image-to-3d", json={"result": "task-img"})
        responses.add(responses.GET, f"{V1}/image-to-3d/task-img", json=NOT_FOUND, status=404)
        responses.add(responses.GET, f"{V2}/image-to-3d/task-img",
                      json={"status": "SUCCEEDED", "progress": 100})
        provider.generate("", image_url="https://example.com/p.png")

        job = provider.get_job_status("task-img")

        assert job.status is GenerationStatus.SUCCEEDED
        assert [c.request.url for c in responses.calls[1:]] == [
            f"{V1}/image-to-3d/task-img", f"{V2}/image-to-3d/task-img",
        ]

    @responses.activate
    def test_a_strict_store_never_asks_meshy_about_an_id_it_does_not_hold(self, monkeypatch):
        class _Strict:
            strict = True
            keeps_prompts = False

            def get(self, provider, job_id):
                return None

            def put(self, record: JobRecord) -> None:  # pragma: no cover - must not be reached
                raise AssertionError("a strict store is never written by a lookup")

        p = MeshyProvider(api_key="test-key")
        monkeypatch.setattr(p, "_job_records", lambda: _Strict())

        assert _code(p.get_job_status, TASK).code == "JOB_NOT_FOUND"
        assert _code(p.download_result, TASK).code == "JOB_NOT_FOUND"
        assert len(responses.calls) == 0


class TestThroughTheDoor:
    @responses.activate
    def test_download_generated_model_takes_an_outside_image_task(self, monkeypatch, tmp_path):
        import kiln.server as srv
        from kiln.server import download_generated_model

        monkeypatch.setitem(sys.modules, "kiln_pro.hosted_generation_jobs", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", None)
        monkeypatch.setattr(srv, "_generation_providers", {})
        monkeypatch.setattr(srv, "_MESHY_API_KEY", "test-key")

        responses.add(responses.GET, PROBE_URLS[0], json=NOT_FOUND, status=404)
        responses.add(responses.GET, PROBE_URLS[1], json=_image_task(texture_prompt="a garden gnome"))
        responses.add(responses.GET, "https://assets.meshy.ai/t/model.obj?sig=1", body=OBJ)

        with patch("kiln.server._check_auth", return_value=None):
            out = download_generated_model(job_id=TASK, provider="meshy", output_path=str(tmp_path))

        assert out["success"] is True, out
        assert out["conversion"] is not None
        assert out["conversion"]["from_format"] == "obj"
        assert out["result"]["format"] == "stl"
        assert "came_from" in out
        assert "Meshy" in out["came_from"]
        assert "a garden gnome" in out["came_from"]
