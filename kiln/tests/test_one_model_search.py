"""One model search: Kiln's own public designs first, then every connected site.

``search_models`` and ``search_all_models`` are the same search.  A site that
is not connected is simply not searched; no answer tells a person to set a
setting of a computer they do not run, and every refusal carries a code.
"""

from __future__ import annotations

from typing import Any

import pytest

import kiln.server as srv
from kiln import model_search
from kiln.marketplaces import MarketplaceRegistry
from kiln.marketplaces.base import MarketplaceAdapter, ModelSummary


def _tool(name: str):
    return srv.mcp._tool_manager.get_tool(name).fn


def search_models(*args, **kwargs):
    return _tool("search_models")(*args, **kwargs)


def search_all_models(*args, **kwargs):
    return _tool("search_all_models")(*args, **kwargs)


class _Site(MarketplaceAdapter):
    def __init__(self, name: str, models: list[str], *, fails: bool = False) -> None:
        self._name, self._models, self._fails = name, models, fails

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._name.title()

    def search(self, query, *, page=1, per_page=10, sort="relevant"):
        if self._fails:
            from kiln.marketplaces.base import MarketplaceError

            raise MarketplaceError("down")
        return [ModelSummary(id=f"{self._name}-{n}", name=n, url=f"https://{self._name}.example/{n}",
                             creator="someone", source=self._name) for n in self._models]

    def get_details(self, model_id):  # pragma: no cover - not searched
        raise NotImplementedError

    def get_files(self, model_id):  # pragma: no cover
        raise NotImplementedError

    def download_file(self, file_id, dest_dir, *, file_name=None):  # pragma: no cover
        raise NotImplementedError


def _kiln_answer(names: list[str]) -> dict[str, Any]:
    return {"status": "success", "address": "https://kiln3d.com/search?q=hook", "results": {
        "designs": [{"username": "ada", "slug": n.lower().replace(" ", "-"), "name": n,
                     "description": f"A {n}.", "license": "cc-by-4.0", "has_file": True,
                     "picture": f"https://kiln3d.com/p/{n}.png", "stars": 3, "printed_by": 5}
                    for n in names],
        "total": len(names), "page": 1, "page_size": 20}}


@pytest.fixture
def search(monkeypatch):
    """A registry of sites and Kiln's own search, each set by the test."""
    registry = MarketplaceRegistry()
    monkeypatch.setattr(srv, "_marketplace_registry", registry)
    monkeypatch.setattr(srv, "_init_marketplace_registry", lambda: None)
    state: dict[str, Any] = {"kiln": _kiln_answer(["Desk Hook"]), "asked": []}

    def kiln_search(**call):
        state["asked"].append(call)
        answer = state["kiln"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(model_search, "_kiln_search_door", lambda: kiln_search)
    state["registry"] = registry
    return state


def _names(result):
    return [(m["source"], m["name"]) for m in result["models"]]


class TestTheOldNameIsTheSameSearch:
    def test_it_answers_with_kilns_public_designs_when_no_site_is_connected(self, search):
        result = search_models("hook")
        assert result["success"] is True and result["count"] == 1
        assert _names(result) == [("kiln", "Desk Hook")]
        first = result["models"][0]
        assert first["id"] == "ada/desk-hook" and first["creator"] == "ada"
        assert first["url"].startswith("https://kiln3d.com/")
        assert first["open_with"] == "get_public_design"
        assert result["searched"] == ["kiln"]

    def test_it_never_names_a_setting_of_someone_elses_computer(self, search):
        result = search_models("hook")
        assert "KILN_THINGIVERSE_TOKEN" not in str(result)

    def test_kiln_comes_first_then_every_connected_site(self, search):
        search["registry"].register(_Site("thingiverse", ["Wall Hook"]))
        search["registry"].register(_Site("myminifactory", ["Coat Hook"]))
        result = search_models("hook")
        assert _names(result)[0] == ("kiln", "Desk Hook")
        assert {s for s, _ in _names(result)} == {"kiln", "thingiverse", "myminifactory"}
        assert result["searched"][0] == "kiln"

    def test_both_names_give_the_same_models(self, search):
        search["registry"].register(_Site("thingiverse", ["Wall Hook"]))
        assert _names(search_models("hook")) == _names(search_all_models("hook"))


class TestEveryMissIsCodedAndPlain:
    def test_no_match_anywhere_is_an_empty_success(self, search):
        search["kiln"] = _kiln_answer([])
        result = search_models("zzzz")
        assert result["success"] is True and result["count"] == 0 and result["models"] == []

    def test_kilns_search_out_of_reach_still_searches_the_sites(self, search):
        search["kiln"] = {"status": "error", "why": "offline", "error": "This computer is offline."}
        search["registry"].register(_Site("thingiverse", ["Wall Hook"]))
        result = search_models("hook")
        assert result["success"] is True and _names(result) == [("thingiverse", "Wall Hook")]
        assert result["not_searched"]["kiln"]

    def test_a_site_that_fails_is_said_and_the_rest_answer(self, search):
        search["registry"].register(_Site("thingiverse", [], fails=True))
        result = search_models("hook")
        assert result["success"] is True and _names(result) == [("kiln", "Desk Hook")]
        assert "thingiverse" in result["failed"]

    def test_nothing_to_search_on_a_persons_own_computer_says_how_to_connect(self, search):
        search["kiln"] = {"status": "error", "why": "signed_out", "error": "Sign in to Kiln first."}
        result = search_models("hook")
        assert result["success"] is False
        assert result["error"]["code"] == "NO_MODEL_SOURCES"
        assert "kiln3d.com/search" in result["error"]["message"]

    def test_nothing_to_search_on_kilns_servers_names_no_setting(self, search, monkeypatch):
        from kiln import runtime_env

        monkeypatch.setattr(runtime_env, "is_hosted_multitenant", lambda: True)
        search["kiln"] = RuntimeError("store down")
        result = search_models("hook")
        assert result["success"] is False
        assert result["error"]["code"] == "SEARCH_UNAVAILABLE" and result["error"]["retryable"] is True
        assert "KILN_" not in str(result) and "export " not in str(result)

    def test_every_refusal_has_a_code(self, search, monkeypatch):
        search["kiln"] = RuntimeError("boom")
        for tool in (search_models, search_all_models):
            result = tool("hook")
            assert result["success"] is False and result["error"]["code"] not in ("", "ERROR")


class TestNamedSources:
    def test_naming_sites_leaves_kiln_out(self, search):
        search["registry"].register(_Site("thingiverse", ["Wall Hook"]))
        result = search_all_models("hook", sources=["thingiverse"])
        assert _names(result) == [("thingiverse", "Wall Hook")] and search["asked"] == []

    def test_kiln_can_be_named(self, search):
        search["registry"].register(_Site("thingiverse", ["Wall Hook"]))
        result = search_all_models("hook", sources=["kiln"])
        assert _names(result) == [("kiln", "Desk Hook")]
