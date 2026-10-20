"""A pull or a clone brings each model once, by its fingerprint, and never loses one.

The courier asks Kiln's servers for an answer naming each version's model
rather than carrying it, fetches each model this computer does not keep, one
a request (``GET /api/cloud/meshes/<sha256>``), checks each against the
fingerprint it asked for, and only then records the history.  A server that
answers with the models inside (one that predates this), or that cannot hand
a model back on its own, is read the old way.  These tests walk the courier
against a stand-in for the cloud routes; ``kiln-pro``'s tests walk the
server, and the round trip runs both.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from kiln import history_courier as courier
from kiln.history_vault import HistoryVault
from tests.test_history_vault import home  # noqa: F401 — fixture

MESH_V1 = b"solid one\nfacet normal 0 0 1\nendsolid one\n"
MESH_V2 = b"solid two\nfacet normal 1 0 0\nendsolid two\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _branch() -> dict:
    return {"branch_id": "mug-main", "design_id": "mug", "name": "main", "head_version_id": "v3",
            "parent_branch_id": None, "base_version_id": None, "created_at": 1.0, "status": "active",
            "annotation": "", "metadata": {}}


def _answer(models: dict[str, bytes], *, named: bool, clone: bool) -> dict:
    """A pull (or clone) answer for versions v1.. with *models*: naming each
    model by fingerprint, or carrying each inside, as an older server does."""
    def recipe(vid: str, mesh: bytes) -> dict:
        out = {"name": "mug", "version_id": vid, "stl_path": f"{_sha(mesh)}.stl"}
        if not named:
            out["mesh_bytes_b64"] = base64.b64encode(mesh).decode("ascii")
        return out

    out = {
        "versions": [{"version_id": vid, "parent_version_id": None, "created_at": float(i),
                      **({"mesh_sha256": _sha(m)} if named else {})}
                     for i, (vid, m) in enumerate(models.items())],
        "memberships": [{"branch_id": "mug-main", "version_id": vid, "position": i, "added_at": 1.0}
                        for i, vid in enumerate(models)],
        "provenance_sidecars": [{"version_id": vid, "recipe_dict": recipe(vid, m)} for vid, m in models.items()],
    }
    if clone:
        out.update(design_id="mug", branches=[_branch()], releases=[])
    else:
        out["branch"] = _branch()
    if named:
        out["mesh_transport"] = "by_sha256"
    return out


class _Account:
    """Kiln's cloud routes as a pull or clone meets them; every call is kept.

    *names_models* answers an ask by fingerprint by naming the models (a
    current server); otherwise every answer carries them (an older one).
    *door* answers a model fetch, by fingerprint, or is the account's own
    bytes when it says nothing.
    """

    def __init__(self, models: dict[str, bytes], *, names_models: bool = True, door=None) -> None:
        self.models, self.names_models, self.door = models, names_models, door or {}
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, tool, method, path, body=None, *, content=None, raw=False):
        self.calls.append((method, path, body))
        if path.endswith("/proven"):
            return {"recipes": []}
        if method == "GET" and path.startswith("/api/cloud/meshes/"):
            sha = path.rsplit("/", 1)[1]
            said = self.door.get(sha)
            if callable(said):
                said = said()
            if said is not None:
                return said
            data = {_sha(m): m for m in self.models.values()}[sha]
            return {"content": data}
        asked = (body or {}).get("mesh_transport") == "by_sha256"
        return _answer(self.models, named=asked and self.names_models, clone=path.endswith("/clone"))

    def fetches(self) -> list[str]:
        return [p for m, p, _ in self.calls if m == "GET" and p.startswith("/api/cloud/meshes/")]

    def asks(self) -> list[dict]:
        return [b for m, p, b in self.calls if m == "POST"]


def _bring(how: str):
    return courier.pull("cloud_pull_branch", "mug") if how == "pull" else courier.clone("cloud_clone_design", "mug")


def _models_here() -> dict[str, bytes]:
    vault = HistoryVault()
    try:
        out = {}
        for b in vault.branches("mug"):
            for m in vault.memberships(b["branch_id"]):
                path = vault.mesh_path((vault.version(m["version_id"]) or {}).get("mesh_sha256"))
                if path is not None:
                    out[m["version_id"]] = path.read_bytes()
        return out
    finally:
        vault.close()


HOW = pytest.mark.parametrize("how", ["pull", "clone"])


class TestEachModelComesOnce:
    @HOW
    def test_only_the_models_not_kept_here_are_fetched_and_every_one_is_kept(self, home, monkeypatch, how):
        vault = HistoryVault()
        vault._put_mesh(MESH_V1, ".stl")
        vault.close()
        account = _Account({"v1": MESH_V1, "v2": MESH_V2, "v3": MESH_V1})
        monkeypatch.setattr(courier, "_request", account)

        answer = _bring(how)

        assert answer["status"] == "success", answer
        assert account.asks()[0]["mesh_transport"] == "by_sha256"
        assert account.fetches() == [f"/api/cloud/meshes/{_sha(MESH_V2)}"]
        assert _models_here() == {"v1": MESH_V1, "v2": MESH_V2, "v3": MESH_V1}
        assert answer["models"] == {"by_fingerprint": True, "fetched": 1, "already_here": 1, "missing": []}

    @HOW
    def test_bytes_that_are_not_the_model_asked_for_stop_it_and_record_nothing(self, home, monkeypatch, how):
        account = _Account({"v1": MESH_V1, "v2": MESH_V2}, door={_sha(MESH_V2): {"content": MESH_V1}})
        monkeypatch.setattr(courier, "_request", account)

        answer = _bring(how)

        assert answer["status"] == "error" and answer["code"] == "MESH_FINGERPRINT_MISMATCH"
        assert answer["version_id"] == "v2" and "v2" in answer["error"]
        vault = HistoryVault()
        try:
            assert vault.branches("mug") == [] and vault.version("v1") is None
            assert vault.mesh_path(_sha(MESH_V2)) is None
        finally:
            vault.close()
        assert [p for _, p, _ in account.calls if p.endswith("/proven")] == []

    def test_a_model_kiln_no_longer_keeps_is_recorded_without_it_and_named(self, home, monkeypatch):
        """As an answer carrying the models would have had it (its bytes are
        gone), and said, never silent."""
        account = _Account({"v1": MESH_V1, "v2": MESH_V2}, door={_sha(MESH_V2): {
            "status": "error", "success": False, "code": "MESH_NOT_FOUND", "http_status": 404,
            "error": "That model is not kept in your Kiln account.",
        }})
        monkeypatch.setattr(courier, "_request", account)

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["status"] == "success", answer
        assert _models_here() == {"v1": MESH_V1}
        assert answer["models"]["missing"] == [{"version_id": "v2", "mesh_sha256": _sha(MESH_V2)}]

    def test_the_apis_request_rate_is_sat_out(self, home, monkeypatch):
        busy = [{"status": "error", "success": False, "http_status": 429, "retry_after": 3, "code": "X"}]
        account = _Account({"v1": MESH_V1}, door={_sha(MESH_V1): lambda: busy.pop() if busy else None})
        monkeypatch.setattr(courier, "_request", account)
        slept: list[float] = []
        monkeypatch.setattr(courier, "_sleep", slept.append, raising=False)

        assert courier.pull("cloud_pull_branch", "mug")["status"] == "success"
        assert slept == [3]
        assert _models_here() == {"v1": MESH_V1}

    @pytest.mark.parametrize("refusal", [
        {"status": "error", "success": False, "http_status": 401, "code": "KILN_AUTH_REJECTED", "error": "Sign in."},
        {"status": "error", "success": False, "http_status": 503, "code": "KILN_API_HTTP_ERROR", "error": "Down."},
        {"status": "error", "success": False, "http_status": 429, "retry_after": 3600, "code": "X", "error": "Later."},
        {"status": "error", "success": False, "code": "KILN_API_UNREACHABLE", "error": "Offline."},
    ], ids=["signed-out", "server-fault", "a-days-wait", "offline"])
    def test_a_model_that_cannot_come_now_stops_it_and_records_nothing(self, home, monkeypatch, refusal):
        account = _Account({"v1": MESH_V1, "v2": MESH_V2}, door={_sha(MESH_V2): refusal})
        monkeypatch.setattr(courier, "_request", account)
        monkeypatch.setattr(courier, "_sleep", lambda s: pytest.fail("a day is not sat out"), raising=False)

        answer = courier.pull("cloud_pull_branch", "mug")

        assert answer["status"] == "error" and answer["code"] == refusal["code"]
        assert answer["version_id"] == "v2" and "nothing was pulled" in answer["error"]
        vault = HistoryVault()
        try:
            assert vault.branches("mug") == []
        finally:
            vault.close()


class TestAServerThatPredatesIt:
    @HOW
    def test_an_answer_carrying_its_models_is_kept_as_before(self, home, monkeypatch, how):
        account = _Account({"v1": MESH_V1, "v2": MESH_V2}, names_models=False)
        monkeypatch.setattr(courier, "_request", account)

        answer = _bring(how)

        assert answer["status"] == "success", answer
        assert account.asks()[0]["mesh_transport"] == "by_sha256"
        assert account.fetches() == [] and "models" not in answer
        assert _models_here() == {"v1": MESH_V1, "v2": MESH_V2}

    @HOW
    @pytest.mark.parametrize("door", [
        {"status": "error", "success": False, "http_status": 404, "code": "KILN_API_HTTP_ERROR"},
        {"status": "error", "success": False, "http_status": 405, "code": "KILN_API_HTTP_ERROR"},
        {"status": "error", "success": False, "http_status": 413, "code": "MESH_TOO_LARGE", "error": "Too big."},
    ], ids=["no-model-door", "model-door-is-upload-only", "model-too-large"])
    def test_a_model_that_cannot_come_alone_brings_the_answer_with_the_models_inside(
        self, home, monkeypatch, how, door,
    ):
        account = _Account({"v1": MESH_V1, "v2": MESH_V2}, door={_sha(MESH_V1): door})
        monkeypatch.setattr(courier, "_request", account)

        answer = _bring(how)

        assert answer["status"] == "success", answer
        first, again = account.asks()
        assert first["mesh_transport"] == "by_sha256" and "mesh_transport" not in again
        assert _models_here() == {"v1": MESH_V1, "v2": MESH_V2}


class TestTheWire:
    def test_a_model_comes_back_as_its_own_bytes_and_a_rate_refusal_says_how_long(self, monkeypatch):
        import httpx

        monkeypatch.setenv("KILN_LICENSE_KEY", "kiln-test-key")
        answers = [
            httpx.Response(429, headers={"Retry-After": "9"}, json={"success": False, "error": "slow"}),
            httpx.Response(200, content=MESH_V1),
        ]
        monkeypatch.setattr(httpx, "request", lambda method, url, **kw: answers.pop(0))
        path = f"/api/cloud/meshes/{_sha(MESH_V1)}"
        busy = courier._request("cloud_pull_branch", "GET", path, raw=True)
        assert busy["http_status"] == 429 and busy["retry_after"] == 9
        assert courier._request("cloud_pull_branch", "GET", path, raw=True) == {"content": MESH_V1}
