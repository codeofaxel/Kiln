"""A generation job is found by a later call wherever that call is answered.

``generate`` starts a job; ``get_job_status`` and ``download_result`` are
LATER calls naming it by id.  A deployment that answers calls from several
processes supplies a store for the jobs of the providers the server shares
(``kiln_pro.hosted_generation_jobs.job_record_store``); a plain install keeps them in
the provider's memory, exactly as before.

"Process 1" and "process 2" here are the server's own provider registry
(``kiln.server._get_generation_provider``) emptied in between, so process 2's
provider has never seen process 1's call.  The supplied store is a stand-in
that keeps no path (only the file's bytes, as a store shared by several
machines must) and answers each caller only for its own jobs.  It is put
where a deployment puts it -- the ``kiln_pro.hosted_generation_jobs`` import -- so the
code under test finds it the way it would on a deployment, and nothing here
needs kiln-pro installed.

A/B: the four tests in TestASuppliedStore fail at 641d2d99 (before job
records: process 2 answers "not found", asks Meshy's text endpoint for an
image job, answers another caller from the vendor, and answers from memory
when the store is down) and pass after; the retryable code fails there too.
TestAPlainInstall passes at both, which is its point.
"""

from __future__ import annotations

import copy
import hashlib
import os
import sys
import types
from pathlib import Path

import pytest
import responses

import kiln.server as srv
from kiln.generation.base import GenerationError, GenerationStatus
from kiln.generation.meshy import _BASE_URL as MESHY_V2
from kiln.generation.meshy import _BASE_URL_V1 as MESHY_V1

STL = (
    b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\n"
    b"vertex 0 10 0\nendloop\nendfacet\nendsolid t\n"
)


class _SharedStore:
    """What several processes share: one record per (caller, provider, id),
    the finished file kept as BYTES, never a path into one process's disk."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], object] = {}
        self.files: dict[str, bytes] = {}
        self.caller = "caller-a"
        self.down = False
        self.calls = 0

    def for_this_call(self):
        self.calls += 1
        if self.down:
            raise GenerationError("the job store did not answer", code="JOB_STORE_UNAVAILABLE")
        return _CallersView(self, self.caller)


class _CallersView:
    strict = True
    keeps_prompts = False

    def __init__(self, shared: _SharedStore, caller: str) -> None:
        self._s, self._caller = shared, caller

    def put(self, record) -> None:
        kept = copy.deepcopy(record)
        kept.prompt = ""
        self._s.rows[(self._caller, record.provider, record.job_id)] = kept

    def get(self, provider, job_id):
        found = self._s.rows.get((self._caller, provider, job_id))
        return copy.deepcopy(found) if found is not None else None

    def update(self, provider, job_id, **changes):
        found = self._s.rows.get((self._caller, provider, job_id))
        if found is None:
            return None
        for name, value in changes.items():
            setattr(found, name, value)
        return copy.deepcopy(found)

    def expire(self) -> int:
        return 0

    def keep_result(self, record, path) -> None:
        data = Path(path).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        self._s.files[sha] = data
        record.result = {"sha256": sha, "name": os.path.basename(path)}

    def result_file(self, record, output_dir):
        result = record.result or {}
        data = self._s.files.get(result.get("sha256", ""))
        if data is None:
            return None
        os.makedirs(output_dir, exist_ok=True)
        dest = os.path.join(output_dir, result["name"])
        Path(dest).write_bytes(data)
        return dest


@pytest.fixture
def openscad_bin(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("the stand-in openscad is a shell script")
    script = tmp_path / "openscad"
    script.write_text(
        "#!/bin/bash\n"
        'if [ "$1" == "--version" ]; then echo "OpenSCAD version 2021.01" >&2; exit 0; fi\n'
        'out=""\n'
        'while [ $# -gt 0 ]; do if [ "$1" == "-o" ]; then out="$2"; shift; fi; shift; done\n'
        "printf '" + STL.decode().replace("\n", "\\n") + "' > \"$out\"\n"
    )
    script.chmod(0o755)
    import kiln.generation.openscad as openscad

    monkeypatch.setattr(openscad, "_find_openscad", lambda explicit=None: str(script))
    return str(script)


@pytest.fixture
def registry(monkeypatch):
    """The server's provider registry, emptied by :func:`new_process`."""
    monkeypatch.setattr(srv, "_generation_providers", {})
    monkeypatch.setattr(srv, "_MESHY_API_KEY", "test-key")

    def new_process() -> None:
        srv._generation_providers.clear()

    return new_process


@pytest.fixture
def supplied(monkeypatch):
    """A deployment's store, where a deployment puts it."""
    shared = _SharedStore()
    hook = types.ModuleType("kiln_pro.hosted_generation_jobs")
    hook.job_record_store = shared.for_this_call
    if "kiln_pro" not in sys.modules:
        try:
            import kiln_pro  # noqa: F401
        except ImportError:
            parent = types.ModuleType("kiln_pro")
            parent.__path__ = []
            monkeypatch.setitem(sys.modules, "kiln_pro", parent)
    monkeypatch.setitem(sys.modules, "kiln_pro.hosted_generation_jobs", hook)
    return shared


@pytest.fixture
def no_store(monkeypatch):
    """A plain install: nothing supplies a store."""
    monkeypatch.setitem(sys.modules, "kiln_pro.hosted_generation_jobs", None)


def _raises(fn, *args, **kwargs) -> str:
    with pytest.raises(GenerationError) as caught:
        fn(*args, **kwargs)
    return caught.value.code or ""


class TestASuppliedStore:
    def test_a_job_started_in_one_process_is_found_and_downloaded_in_another(
        self, supplied, registry, openscad_bin, tmp_path,
    ):
        job = srv._get_generation_provider("openscad").generate(
            "cube(10);", output_dir=str(tmp_path / "one"),
        )
        registry()
        later = srv._get_generation_provider("openscad")
        assert later.get_job_status(job.id).status is GenerationStatus.SUCCEEDED
        result = later.download_result(job.id, output_dir=str(tmp_path / "two"))
        assert Path(result.local_path).parent == tmp_path / "two"
        assert Path(result.local_path).read_bytes() == STL

    @responses.activate
    def test_a_vendor_job_is_asked_at_the_endpoint_that_holds_it(self, supplied, registry):
        responses.add(responses.POST, f"{MESHY_V2}/image-to-3d", json={"result": "task-img"})
        responses.add(responses.GET, f"{MESHY_V1}/image-to-3d/task-img",
                      json={"status": "IN_PROGRESS", "progress": 30})
        srv._get_generation_provider("meshy").generate("", image_url="https://example.com/p.png")
        registry()
        job = srv._get_generation_provider("meshy").get_job_status("task-img")
        assert (job.status, job.progress) == (GenerationStatus.IN_PROGRESS, 30)

    @responses.activate
    def test_another_callers_job_is_not_found_and_its_vendor_is_not_asked(self, supplied, registry):
        responses.add(responses.POST, f"{MESHY_V2}/text-to-3d", json={"result": "task-a"})
        responses.add(responses.GET, f"{MESHY_V2}/text-to-3d/task-a",
                      json={"status": "SUCCEEDED", "progress": 100, "prompt": "caller A's vase"})
        srv._get_generation_provider("meshy").generate("a vase")
        asked = len(responses.calls)
        registry()
        supplied.caller = "caller-b"
        assert _raises(srv._get_generation_provider("meshy").get_job_status, "task-a") == "JOB_NOT_FOUND"
        assert len(responses.calls) == asked

    def test_a_store_that_fails_refuses_rather_than_answering_from_memory(
        self, supplied, registry, openscad_bin, tmp_path,
    ):
        provider = srv._get_generation_provider("openscad")
        job = provider.generate("cube(10);", output_dir=str(tmp_path))
        supplied.down = True
        assert _raises(provider.get_job_status, job.id) == "JOB_STORE_UNAVAILABLE"
        assert _raises(provider.generate, "cube(2);", output_dir=str(tmp_path)) == "JOB_STORE_UNAVAILABLE"

    def test_the_refusal_is_retryable(self):
        assert srv._error_dict("not now", code="JOB_STORE_UNAVAILABLE")["error"]["retryable"] is True


class TestAPlainInstall:
    """Unchanged, and passes at both revisions.  Mutation-pinned: making the
    provider's own memory strict fails the vendor test, and consulting a
    store for a provider built outside the registry fails the last one."""

    def test_a_process_finds_its_own_jobs_and_another_process_does_not(
        self, no_store, registry, openscad_bin, tmp_path,
    ):
        provider = srv._get_generation_provider("openscad")
        job = provider.generate("cube(10);", output_dir=str(tmp_path / "one"))
        result = provider.download_result(job.id, output_dir=str(tmp_path / "ignored"))
        assert Path(result.local_path).parent == tmp_path / "one"
        assert provider.get_job_status(job.id).prompt.startswith("cube(10);")
        registry()
        assert _raises(srv._get_generation_provider("openscad").get_job_status, job.id) == "JOB_NOT_FOUND"

    @responses.activate
    def test_a_vendor_job_this_process_never_saw_is_asked_about(self, no_store, registry):
        responses.add(responses.GET, f"{MESHY_V2}/text-to-3d/task-old",
                      json={"status": "SUCCEEDED", "progress": 100, "prompt": "an old vase"})
        job = srv._get_generation_provider("meshy").get_job_status("task-old")
        assert (job.status, job.prompt) == (GenerationStatus.SUCCEEDED, "an old vase")

    def test_a_provider_built_for_one_call_keeps_its_own_jobs(self, supplied, openscad_bin, tmp_path):
        from kiln.generation.openscad import OpenSCADProvider

        provider = OpenSCADProvider()
        job = provider.generate("cube(10);", output_dir=str(tmp_path))
        assert provider.download_result(job.id).local_path.startswith(str(tmp_path))
        assert supplied.calls == 0
