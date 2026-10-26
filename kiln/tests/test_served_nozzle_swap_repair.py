"""A nozzle swap recorded through Kiln's servers marks the printer here repaired.

A swap Kiln records is a repair of that printer (``done`` / ``nozzle``, no
second question).  On a plain install the swap is recorded on Kiln's
servers, which keep the account's nozzle record but no printer, so the
served stub marks the printer on this computer once the servers confirm the
swap, through the one helper every swap door calls
(:func:`kiln.machine_repairs.mark_nozzle_swap`).  Pinned through the real
stub:

* the bundled manifest serves the tool, so a plain install lists it;
* a confirmed swap marks the printer it names repaired (nozzle), dated as
  the swap was;
* a refused or unanswered swap marks nothing;
* an id naming no printer here says so, in place of the servers' sentence.

A/B: every test here fails at public cf080cea (no stub was registered, and
the stub marked nothing).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from kiln import machine_repairs as pr
from kiln import persistence
from kiln.persistence import KilnDB
from kiln.registry import machine_fingerprint

SERVERS_SAID = {
    "success": True,
    "status": "ok",
    "tool": "record_nozzle_replacement",
    "nozzle": {"printer_id": "shop", "material": "hardened_steel"},
    "machine_repair": {"marked": False, "next": "Kiln's servers keep no printers, so this swap did not mark one repaired."},
}


class _Registry:
    def __init__(self, **printers):
        self._by_name = dict(printers)

    def list_names(self):
        return sorted(self._by_name)

    def get(self, name):
        if name not in self._by_name:
            from kiln.printers.base import PrinterNotFoundError

            raise PrinterNotFoundError(name)
        return self._by_name[name]


@pytest.fixture
def plain_install(tmp_path, monkeypatch):
    """The served stub as a plain install registers it, one printer set up
    here, and the servers' answer chosen by the test."""
    from kiln import server

    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    shop = SimpleNamespace(name="bambu", serial="01P00A000000SHOP", host="10.0.0.5", _kiln_registered_name="shop")
    registry = _Registry(shop=shop)
    monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_registry", lambda: registry)
    monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)
    db = KilnDB(str(tmp_path / "kiln.db"))
    monkeypatch.setattr(persistence, "get_db", lambda: db)

    manifest = json.loads((server.Path(server.__file__).parent / "pro_tool_manifest.json").read_text())
    (entry,) = [t for t in manifest["tools"] if t["name"] == "record_nozzle_replacement"]
    (tmp_path / "pro_tool_manifest.json").write_text(json.dumps({"tools": [entry]}), encoding="utf-8")
    monkeypatch.setattr(server, "Path", lambda _p: tmp_path / "kiln")
    for table in ("_PRO_TOOL_NUDGES", "_PRO_TOOL_TIERS", "_PRO_TOOL_QUOTA"):
        monkeypatch.setattr(server, table, {})
    answer = {"value": dict(SERVERS_SAID)}
    sent = []
    monkeypatch.setattr(server, "_pro_api_call", lambda name, **kw: sent.append((name, kw)) or answer["value"])

    registered = {}

    class _MCP:
        def tool(self, **_kwargs):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    server._register_pro_tool_stubs(_MCP())
    yield SimpleNamespace(tools=registered, db=db, shop=shop, answer=answer, sent=sent)
    db.close()


def _condition(env):
    return env.db.machine_condition(machine_fingerprint(env.shop))


def test_the_bundled_manifest_serves_the_swap():
    from kiln import server

    manifest = json.loads((server.Path(server.__file__).parent / "pro_tool_manifest.json").read_text())
    (entry,) = [t for t in manifest["tools"] if t["name"] == "record_nozzle_replacement"]
    assert entry.get("served", True) is not False


def test_a_confirmed_swap_marks_the_printer_here_repaired(plain_install):
    out = plain_install.tools["record_nozzle_replacement"](
        printer_id="shop", reason="worn_out", new_material="hardened_steel",
    )

    assert plain_install.sent and plain_install.sent[0][0] == "record_nozzle_replacement"
    repair = out["machine_repair"]
    assert repair["marked"] is True and repair["printer"] == "shop" and repair["kind"] == "nozzle"
    assert "repaired (nozzle)" in repair["next"]
    cond = _condition(plain_install)
    assert cond["state"] == "repaired" and cond["kind"] == "nozzle"
    assert cond["marks"][0]["determined_by"] == "user_reported"


def test_a_swap_dated_earlier_keeps_its_date(plain_install):
    day = (date.today() - timedelta(days=3)).isoformat()

    out = plain_install.tools["record_nozzle_replacement"](
        printer_id="shop", reason="worn_out", new_material="hardened_steel", install_date=day,
    )

    assert out["machine_repair"]["marked_at"].startswith(day)


@pytest.mark.parametrize("refused", [
    {"status": "error", "code": "TIER_REQUIRED", "required_tier": "pro", "error": "Kiln Pro records swaps."},
    {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out", "error": "Sign in."},
    {"status": "error", "code": "SERVER_UNREACHABLE", "why": "offline", "error": "This computer is offline."},
])
def test_a_swap_the_servers_did_not_record_marks_nothing(plain_install, refused):
    plain_install.answer["value"] = refused

    out = plain_install.tools["record_nozzle_replacement"](
        printer_id="shop", reason="worn_out", new_material="hardened_steel",
    )

    assert out == refused
    assert _condition(plain_install)["state"] == "ok"


def test_an_id_naming_no_printer_here_says_so(plain_install):
    out = plain_install.tools["record_nozzle_replacement"](
        printer_id="garage", reason="upgraded", new_material="hardened_steel",
    )

    repair = out["machine_repair"]
    assert repair["marked"] is False
    assert "'garage'" in repair["next"] and "set_machine_repair" in repair["next"]
    assert _condition(plain_install)["state"] == "ok"


def test_the_helper_marks_nothing_on_kiln_servers(plain_install, monkeypatch):
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")

    assert pr.mark_nozzle_swap("shop") is None
    assert _condition(plain_install)["state"] == "ok"
