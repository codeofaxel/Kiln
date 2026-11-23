"""One model larger than the account keeps never holds back a branch.

A push asks the account which models it keeps, and the answer states the
largest model it keeps.  A model past that is not sent: its version goes as a
record without the model, every other version goes with its model, and the
push says which version, its size, the limit, and that the model stays on
this computer.  A pull or a clone that brings such a record says the model
is kept on the computer it was made on.  An account whose answer states no
limit is sent every model, as before.  These tests walk the courier against
a stand-in for the cloud routes.
"""

from __future__ import annotations

import hashlib

import pytest

from kiln import history_courier as courier
from kiln import history_vault
from tests.test_history_vault import _library, home  # noqa: F401 — fixture

SMALL = b"solid one\nfacet normal 0 0 1\nendsolid one\n"
BIG = b"solid two\n" + b"facet normal 1 0 0\n" * 40 + b"endsolid two\n"
OTHER = b"solid three\nfacet normal 0 1 0\nendsolid three\n"
LIMIT = len(BIG) - 1
OFFER = {"said": "The account's own offer.", "tool": "simplify_mesh_model", "size_param": "max_megabytes"}
MB = 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Account:
    """The cloud routes a push meets; every call is kept."""

    def __init__(self, *, states_limit: bool = True, offers: bool = False) -> None:
        self.states_limit, self.offers = states_limit, offers
        self.calls: list[tuple[str, str, object]] = []
        self.pushed: dict | None = None

    def __call__(self, tool, method, path, body=None, *, content=None, raw=False, timeout=None):
        self.calls.append((method, path, body if content is None else len(content)))
        if path.endswith("/meshes/stored"):
            answer = {"stored": []}
            if self.states_limit and (body or {}).get("model_limit") is True:
                answer["model_limit"] = {"max_bytes": LIMIT, "keeps_record": True}
                if self.offers:
                    answer["model_limit"]["offer"] = dict(OFFER)
            return answer
        if method == "PUT":
            if content is not None and len(content) > LIMIT:
                return {"status": "error", "http_status": 413, "code": "MESH_TOO_LARGE", "error": "too large"}
            return {"stored": True}
        if path.endswith("/branches/push"):
            self.pushed = body
            return {"pushed_versions": [v["version_id"] for v in body["versions"]],
                    "branch_id": body["branch"]["branch_id"]}
        if path.endswith("/proven"):
            return {"recipes": []}
        return {}

    def puts(self) -> list[int]:
        return [size for method, _path, size in self.calls if method == "PUT"]


@pytest.fixture
def lamp(home, monkeypatch):  # noqa: F811
    _library(home, "lamp", [SMALL, BIG, OTHER])
    return home


def _versions(account: _Account) -> list[dict]:
    assert account.pushed is not None, "nothing was pushed"
    return account.pushed["versions"]


def test_the_big_model_stays_and_the_rest_of_the_branch_goes(lamp, monkeypatch):
    account = _Account()
    monkeypatch.setattr(courier, "_request", account)

    pushed = courier.push("cloud_push_branch", "lamp")

    assert pushed["status"] == "success", pushed
    assert sorted(account.puts()) == sorted([len(SMALL), len(OTHER)])
    first, second, third = _versions(account)
    assert first["mesh_sha256"] == _sha(SMALL) and third["mesh_sha256"] == _sha(OTHER)
    assert "mesh_sha256" not in second
    assert second["model_stays_here"] == {"sha256": _sha(BIG), "bytes": len(BIG)}


def test_the_push_says_once_which_version_and_where_its_model_is(lamp, monkeypatch):
    monkeypatch.setattr(courier, "_request", _Account())

    pushed = courier.push("cloud_push_branch", "lamp")

    (said,) = pushed["models_stayed_here"]
    assert "version 2" in said and "stays on this computer" in said
    assert f"up to {LIMIT // MB} MB" in said and "pull or clone" in said
    assert pushed["said_models"] == said


def test_an_account_that_states_no_limit_is_sent_every_model_as_before(lamp, monkeypatch):
    account = _Account(states_limit=False)
    monkeypatch.setattr(courier, "_request", account)

    pushed = courier.push("cloud_push_branch", "lamp")

    assert pushed["status"] == "error" and "nothing was pushed" in pushed["error"]
    assert len(BIG) in account.puts()
    assert account.pushed is None


def test_a_model_this_computers_history_left_out_is_said_too(lamp, monkeypatch):
    monkeypatch.setattr(history_vault, "_MAX_LIBRARY_MESH", len(BIG) - 1)
    account = _Account()
    monkeypatch.setattr(courier, "_request", account)

    pushed = courier.push("cloud_push_branch", "lamp")

    assert pushed["status"] == "success", pushed
    assert _versions(account)[1]["model_stays_here"] == {"sha256": _sha(BIG), "bytes": len(BIG)}
    assert len(pushed["models_stayed_here"]) == 1


def test_a_model_left_out_goes_up_bare_to_an_account_that_states_no_limit(lamp, monkeypatch):
    monkeypatch.setattr(history_vault, "_MAX_LIBRARY_MESH", len(BIG) - 1)
    account = _Account(states_limit=False)
    monkeypatch.setattr(courier, "_request", account)

    pushed = courier.push("cloud_push_branch", "lamp")

    assert pushed["status"] == "success", pushed
    assert "model_stays_here" not in _versions(account)[1]
    assert "models_stayed_here" not in pushed


@pytest.mark.parametrize("door", ["pull", "clone"])
def test_a_record_without_its_model_reads_as_kept_on_the_computer_that_made_it(home, monkeypatch, door):  # noqa: F811
    branch = {"branch_id": "lamp-main", "design_id": "lamp", "name": "main", "head_version_id": "v2",
              "parent_branch_id": None, "base_version_id": None, "created_at": 1.0, "status": "active",
              "annotation": "", "metadata": {}}
    note = {"sha256": _sha(BIG), "bytes": 53 * MB + 1, "limit_bytes": 50 * MB}
    answer = {
        "versions": [{"version_id": "v1", "parent_version_id": None, "created_at": 1.0},
                     {"version_id": "v2", "parent_version_id": "v1", "created_at": 2.0}],
        "memberships": [{"branch_id": "lamp-main", "version_id": vid, "position": i, "added_at": 1.0}
                        for i, vid in enumerate(["v1", "v2"])],
        "provenance_sidecars": [
            {"version_id": "v1", "recipe_dict": {"name": "lamp", "version": 1, "version_id": "v1"}},
            {"version_id": "v2", "recipe_dict": {"name": "lamp", "version": 2, "version_id": "v2",
                                                 "model_not_kept": note}},
        ],
    }
    if door == "clone":
        answer.update(design_id="lamp", branches=[branch], releases=[])
    else:
        answer["branch"] = branch

    def account(tool, method, path, body=None, **_kw):
        return {"recipes": []} if path.endswith("/proven") else dict(answer)

    monkeypatch.setattr(courier, "_request", account)

    got = (
        courier.pull("cloud_pull_branch", "lamp") if door == "pull"
        else courier.clone("cloud_clone_design", "lamp")
    )

    assert got["status"] == "success", got
    (said,) = got["models_kept_elsewhere"]
    assert "version 2" in said and "53.1 MB" in said and "up to 50 MB" in said
    assert "kept on the computer it was made on" in said
    assert got["said_models"] == said


def test_the_accounts_offer_is_said_once_with_the_one_call_that_does_it(lamp, monkeypatch):
    monkeypatch.setattr(courier, "_request", _Account(offers=True))

    pushed = courier.push("cloud_push_branch", "lamp")

    (stayed,) = pushed["models_stayed_here"]
    assert pushed["said_models"] == f"{stayed} {OFFER['said']}"
    (offer,) = pushed["slim_offers"]
    assert offer["tool"] == "simplify_mesh_model" and offer["needs_your_yes"] is True
    assert offer["arguments"]["file_path"].endswith(".stl")
    assert offer["arguments"]["max_megabytes"] == round(LIMIT / MB, 1)


def test_an_account_that_makes_no_offer_gets_none_said(lamp, monkeypatch):
    monkeypatch.setattr(courier, "_request", _Account())

    pushed = courier.push("cloud_push_branch", "lamp")

    assert "slim_offers" not in pushed



def test_a_model_too_detailed_for_the_servers_to_slim_is_told_the_way_out_and_offered_nothing(home, monkeypatch):  # noqa: F811
    """A binary STL past the most triangles the account's servers slim at
    once: offering a slim that would then be refused is a broken promise."""
    import struct

    big = struct.pack("<80sI", b"\0" * 80, 40) + b"\0" * 50 * 40
    _library(home, "lamp", [SMALL, big, OTHER])
    offer = {**OFFER, "max_triangles": 10, "too_detailed_said": "Too detailed: export it again with less detail."}

    class Account(_Account):
        def __call__(self, tool, method, path, body=None, **kw):
            answer = super().__call__(tool, method, path, body, **kw)
            if path.endswith("/meshes/stored") and "model_limit" in answer:
                answer["model_limit"] = {"max_bytes": len(big) - 1, "keeps_record": True, "offer": offer}
            return answer

    monkeypatch.setattr(courier, "_request", Account())

    pushed = courier.push("cloud_push_branch", "lamp")

    assert pushed["status"] == "success", pushed
    assert "slim_offers" not in pushed
    assert "Too detailed: export it again with less detail." in pushed["said_models"]
    assert OFFER["said"] not in pushed["said_models"]
