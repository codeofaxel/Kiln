"""The finishing guide names each finish by what it is, not what it is sold as.

A finish names its chemistry and form ("a brush-on two-part epoxy coating")
rather than a retail product, and a finish name is a lookup key, so a
spelling that carries a product in brackets or after "with", or lacks a form
word, still resolves: the lookup matches on the name's CORE rather than on a
table of old names, which would keep the products in this repo.  These pin
the rule and that a miss (no answer from Kiln's servers) still resolves such
a spelling to the finish and its safety note.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Any

import pytest

from kiln._pro_finishing_bridge import CAUTIONS_FILE, find_technique, technique_core

# (material, a spelling of the kind a saved note may carry, the name it resolves to)
_OLD_SPELLINGS = [
    ("pla", "Epoxy Coating (some product)", "Epoxy Coating"),
    ("nylon", "Dyeing with some product", "Dyeing"),
    ("tpu", "Flexible Paint (brand a / brand b)", "Flexible Rubber Paint"),
    ("tpu_85a", "Flexible Coating (brand a / rubber coating)", "Flexible Rubber Coating"),
]


@pytest.fixture()
def offline_guide(monkeypatch):
    import kiln.server as srv
    from kiln.plugins.design_tools import plugin

    def no_network(*_a, **_k):
        raise socket.gaierror("no such host")

    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(srv, "_pro_api_call", no_network)
    tools: dict[str, Any] = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(_MCP())
    return tools["get_post_processing_guide"]


def _table() -> dict[str, Any]:
    raw = json.loads(CAUTIONS_FILE.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@pytest.mark.parametrize(("material", "old", "new"), _OLD_SPELLINGS)
def test_an_old_spelling_still_names_the_finish_and_its_warning_offline(offline_guide, material, old, new):
    out = offline_guide(material, old)

    assert out["basic"] is True and out["why"] == "offline", out
    assert [t["name"] for t in out["techniques"]] == [new]
    assert out["techniques"][0]["safety_notes"].strip()


def test_old_spellings_resolve_in_any_case():
    techniques = _table()["pla"]["techniques"]
    found = find_technique(techniques, "  epoxy COATING (anything at all) ")
    assert found is not None and found["name"] == "Epoxy Coating"


def test_the_core_rule():
    assert technique_core("Epoxy Coating (some product)") == "epoxy coating"
    assert technique_core("Dyeing with some product") == "dyeing"
    assert technique_core("Flexible Rubber Paint") == "flexible paint"
    assert technique_core("  IPA Vapor Smoothing (Structural) ") == "ipa vapor smoothing"
    assert technique_core("") == ""


def test_no_two_finishes_of_one_plastic_share_a_core():
    """The core is the lookup key for an old spelling: two finishes with one
    core would make an old name ambiguous, and the lookup answers None."""
    for key, record in _table().items():
        cores = [technique_core(t["name"]) for t in record.get("techniques") or []]
        assert len(cores) == len(set(cores)), (key, sorted(cores))


def test_an_ambiguous_old_spelling_answers_none_not_a_guess():
    rows = [{"name": "Dyeing"}, {"name": "Dyeing with a brush"}]
    assert find_technique(rows, "Dyeing with anything") is None
    assert find_technique(rows, "Dyeing with a brush")["name"] == "Dyeing with a brush"
