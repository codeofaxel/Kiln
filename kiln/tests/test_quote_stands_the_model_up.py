"""A model a print service is asked to price stands up first, and the
person's own file is never touched.

A model exported with up along Y reaches a quote lying on its back, and a
price on a model lying down is a price on a different print: more support,
a different face on the bed.  Pinned here:

* :func:`kiln.arrival_pose.stand_up` with ``into`` turns a COPY and only
  reads the file it was handed: nothing is written beside that file, the
  copy carries its note (where it came from, its goal) and the pose;
* :func:`kiln.fulfillment_service.file_to_price` is what every pricing door
  sends: the standing copy when Kiln is sure of a turn, the file as named
  otherwise or when the person asks for it as sent;
* the person is told first, in one sentence every door says the same way;
* every pricing door prices the standing copy: the agent tool on both paths
  (Kiln's servers, and an install's own order service), ``compare_print_options``,
  ``kiln order quote`` (and ``--as-sent``), ``kiln compare-cost``; a served
  quote asks the servers to price exactly the file sent, so the file this
  computer previews is the file priced, and the quote is found again by the
  file the person named;
* a model prepared for print from an AI download says what was done to the
  copy, and keeps where it came from;
* a static check that any new door asking a print service for a price
  settles the pose first.

The judgement itself is Kiln's and lives on its servers; here it is stood in
for, so what is pinned is the floor every install runs.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from kiln import _pro_order_bridge as bridge
from kiln import arrival, arrival_pose
from kiln._pro_arrival_pose_bridge import Verdict
from kiln.fulfillment_service import file_to_price, quote_pose_block, quote_pose_line
from kiln.served_answer import Miss
from tests import _order_service as order_service
from tests.test_arrival_pose import _extents, _figure_standing, _on_its_back, _write

trimesh = pytest.importorskip("trimesh")

#: ``rotate_model``'s turn that stands a file written with up along Y.
_STAND = {"x": 90.0, "y": 0.0, "z": 0.0}
_STOOD = "It arrived on its back; Kiln stood it on its base (90° front over back). rotate_model(rotation_x=-90) puts it back."
_LINE = "It arrived on its back; Kiln stood it on its base (90° front over back). The price is for it standing up."


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from kiln import output_paths

    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)
    # Kiln's own temp folder, so every standing copy is found under it.
    token = output_paths.REQUEST_OUTPUT_ROOT.set(str(tmp_path / "kiln-temp"))
    os.makedirs(tmp_path / "kiln-temp", exist_ok=True)
    yield
    output_paths.REQUEST_OUTPUT_ROOT.reset(token)


@pytest.fixture()
def judge(monkeypatch):
    """Kiln's judgement, stood in for: ``judge.verdict`` is what it answers,
    ``judge.miss`` the miss instead, ``judge.asked`` each set of facts."""

    class _Judge:
        verdict: Verdict | None = Verdict("turn", "+Y", dict(_STAND), _STOOD, advice="It is shown as it arrived.")
        miss: Miss | None = None
        asked: list[dict] = []

    _Judge.asked = []

    def fake(facts):
        _Judge.asked.append(facts)
        if _Judge.miss is not None:
            return None, _Judge.miss
        return _Judge.verdict, None

    monkeypatch.setattr("kiln._pro_arrival_pose_bridge.judge", fake)
    return _Judge


def _lying(tmp_path: Path, name: str = "knight.stl") -> str:
    path = _write(tmp_path / name, _on_its_back(_figure_standing()))
    arrival.record(path, arrival.Arrival(kind=arrival.DOWNLOADED, by="MakerWorld", name="Knight", url="https://makerworld.com/en/models/1"))
    return path


def _stands(path: str) -> bool:
    _x, y, z = _extents(path)
    return z > y


def _tools(module: str) -> dict:
    tools: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn

            return decorator

    __import__(module, fromlist=["plugin"]).plugin.register(_MCP())
    return tools


def _served_answer() -> bridge.Answer:
    return bridge.Answer(body={"quote": {"quote_id": "q-9", "total_price": 10.5}, "tell_the_person": ["1 copy: 10.50 USD."]})


class _Asked:
    """A stand-in for the served quote that keeps what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _served_answer()


def _quote_record(file_path: str) -> SimpleNamespace:
    return SimpleNamespace(
        quote_id="q-1", provider="craftcloud", material="pla", quantity=1, total_price=10.0,
        currency="USD", lead_time_days=5, shipping_options=[], to_dict=lambda: {"quote_id": "q-1", "material": "pla", "unit_price": 10.0, "total_price": 10.0},
    )


class _Provider:
    """A print service on this install that keeps the files it was asked to price."""

    name = "craftcloud"

    def __init__(self) -> None:
        self.priced: list[str] = []

    def get_quote(self, request):
        self.priced.append(str(request.file_path))
        return _quote_record(str(request.file_path))


# ---------------------------------------------------------------------------
# the copy
# ---------------------------------------------------------------------------


class TestStandingACopy:
    def test_a_turn_goes_to_the_copy_and_the_file_handed_in_is_only_read(self, tmp_path, judge):
        src = _lying(tmp_path)
        before = Path(src).read_bytes()
        note_before = Path(arrival.note_path_for(src)).read_text()
        into = str(tmp_path / "copy.stl")

        record = arrival_pose.stand_up(src, into=into, source="quote")

        assert record["status"] == arrival_pose.TURNED and record["said"] == _STOOD
        assert record["undo"] == {"rotation_x": -90.0}
        assert Path(src).read_bytes() == before and not _stands(src)
        assert Path(arrival.note_path_for(src)).read_text() == note_before, "nothing is written beside the file handed in"
        assert _stands(into)
        assert arrival.read(into).by == "MakerWorld", "the copy keeps where the model came from"
        assert arrival.read_pose(into)["status"] == arrival_pose.TURNED

    def test_a_model_that_stands_writes_nothing_anywhere(self, tmp_path, judge):
        judge.verdict = Verdict("keep", "+Z", None, "It stands as it arrived.")
        src = _write(tmp_path / "standing.stl", _figure_standing())
        into = tmp_path / "copy.stl"

        record = arrival_pose.stand_up(src, into=str(into))

        assert record["status"] == arrival_pose.STANDING
        assert not into.exists() and not Path(arrival.note_path_for(src)).exists()

    def test_a_judgement_that_did_not_arrive_writes_nothing_beside_the_file(self, tmp_path, judge):
        judge.miss = Miss("unanswered")
        src = _write(tmp_path / "standing.stl", _on_its_back(_figure_standing()))

        record = arrival_pose.stand_up(src, into=str(tmp_path / "copy.stl"))

        assert record["status"] == arrival_pose.UNJUDGED
        assert not (tmp_path / "copy.stl").exists() and not Path(arrival.note_path_for(src)).exists()

    def test_a_copy_that_cannot_be_turned_is_removed_and_the_part_is_shown_as_it_arrived(self, tmp_path, judge, monkeypatch):
        def broken(*_a, **_k):
            raise ValueError("cannot rewrite")

        monkeypatch.setattr(arrival_pose, "turn_file", broken)
        src = _lying(tmp_path)

        record = arrival_pose.stand_up(src, into=str(tmp_path / "copy.stl"))

        assert record["status"] == arrival_pose.ADVISED and "Kiln could not write a turned copy." in record["said"]
        assert "undo" not in record
        assert not (tmp_path / "copy.stl").exists()


# ---------------------------------------------------------------------------
# the file a door sends, and what the person is told
# ---------------------------------------------------------------------------


class TestTheFileToPrice:
    def test_a_model_lying_down_is_priced_from_a_standing_copy_in_kilns_temp_folder(self, tmp_path, judge):
        src = _lying(tmp_path)
        before = Path(src).read_bytes()

        priced = file_to_price(src)

        assert priced.sent == src and priced.stood_up
        assert Path(priced.path).name == "knight.stl", "the print service sees the model's own name"
        assert Path(priced.path).parent.parent == tmp_path / "kiln-temp" / "standing-for-quote"
        assert _stands(priced.path) and Path(src).read_bytes() == before
        assert priced.line() == _LINE
        assert priced.block() == {
            "status": arrival_pose.TURNED, "said": _LINE, "turn": _STAND, "priced_file": priced.path,
        }

    def test_the_same_model_quoted_twice_is_priced_from_the_same_copy(self, tmp_path, judge):
        src = _lying(tmp_path)
        assert file_to_price(src).path == file_to_price(src).path

    def test_a_model_asked_for_as_sent_is_not_judged_or_copied(self, tmp_path, judge):
        src = _lying(tmp_path)

        priced = file_to_price(src, keep_as_sent=True)

        assert (priced.path, priced.pose, priced.line()) == (src, {"status": "as_sent"}, "")
        assert judge.asked == []

    def test_a_model_that_stands_is_priced_as_named_and_nothing_is_said(self, tmp_path, judge):
        judge.verdict = Verdict("keep", "+Z", None, "It stands as it arrived.")
        src = _write(tmp_path / "standing.stl", _figure_standing())

        priced = file_to_price(src)

        assert (priced.path, priced.stood_up, priced.line()) == (src, False, "")

    def test_a_file_that_is_not_a_model_is_sent_as_named(self, tmp_path, judge):
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")

        priced = file_to_price(str(gcode))

        assert (priced.path, priced.pose) == (str(gcode), {}) and judge.asked == []


class TestWhatThePersonIsTold:
    def test_the_undo_of_kilns_own_copy_is_never_said(self):
        record = {"status": "turned", "said": _STOOD, "turn": _STAND}
        assert quote_pose_line(record) == _LINE

    def test_a_model_kiln_cannot_place_is_priced_as_sent_and_says_so(self):
        unsure = {"status": "unsure", "said": "Kiln could not tell which way up it goes."}
        assert quote_pose_line(unsure) == "Kiln could not tell which way up it goes. The price is for the file exactly as sent."
        assert quote_pose_line({"status": "unjudged"}) == (
            "Kiln could not check which way up this model stands, so the price is for the file exactly as sent."
        )

    def test_nothing_is_said_for_a_model_that_stands_or_one_asked_for_as_sent(self):
        assert quote_pose_line({"status": "standing", "said": "It stands as it arrived."}) == ""
        assert quote_pose_line({"status": "as_sent"}) == ""

    def test_the_block_never_names_a_file(self):
        block = quote_pose_block({"status": "turned", "said": _STOOD, "turn": _STAND, "undo": {"rotation_x": -90.0}})
        assert block == {"status": "turned", "said": _LINE, "turn": _STAND}


# ---------------------------------------------------------------------------
# every door that asks a print service for a price
# ---------------------------------------------------------------------------


class TestEveryPricingDoor:
    def test_the_served_quote_prices_the_standing_copy_and_says_so_first(self, tmp_path, judge, monkeypatch):
        order_service.remove(monkeypatch)
        asked = _Asked()
        monkeypatch.setattr(bridge, "quote", asked)
        src = _lying(tmp_path)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=src, material_id="pla")

        assert reply["success"] is True, reply
        (args, kwargs), = asked.calls
        priced = reply["pose"]["priced_file"]
        assert args[0] == priced != src and _stands(priced)
        assert kwargs["as_sent"] is True and kwargs["sent_file"] == src
        assert reply["tell_the_person"][0] == _LINE
        assert reply["tell_the_person"][1] == "1 copy: 10.50 USD."
        height = reply["priced_size_mm"]["height_mm"]
        assert height == pytest.approx(max(_extents(src)), abs=0.5), "the size said is the standing copy's"

    def test_the_served_quote_of_a_model_asked_for_as_sent_sends_the_file_named(self, tmp_path, judge, monkeypatch):
        order_service.remove(monkeypatch)
        asked = _Asked()
        monkeypatch.setattr(bridge, "quote", asked)
        src = _lying(tmp_path)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=src, material_id="pla", keep_as_sent=True)

        (args, kwargs), = asked.calls
        assert args[0] == src and kwargs["as_sent"] is True
        assert reply["pose"] == {"status": "as_sent"} and reply["tell_the_person"][0] == "1 copy: 10.50 USD."

    def test_an_installs_own_order_service_prices_the_standing_copy_and_finds_it_by_either_file(self, tmp_path, judge, monkeypatch):
        import kiln.server as server
        from kiln.quote_cache import get_quote_cache

        order_service.provide(monkeypatch)
        provider = _Provider()
        monkeypatch.setattr(server, "_get_fulfillment", lambda: provider)
        monkeypatch.setattr(server, "_get_billing", lambda: None)
        monkeypatch.setattr(server, "_get_payment_mgr", lambda: SimpleNamespace(available_rails=[]))
        monkeypatch.setattr(server, "_provider_routing_metadata", lambda name, **_kw: {"provider": name})
        src = _lying(tmp_path)

        reply = _tools("kiln.plugins.fulfillment_tools")["fulfillment_quote"](file_path=src, material_id="pla")

        assert reply["success"] is True, reply
        assert provider.priced == [reply["pose"]["priced_file"]] and _stands(provider.priced[0])
        assert reply["tell_the_person"][0] == _LINE
        cache = get_quote_cache()
        assert cache.get_for_file(src) is not None, "the quote is found by the file the person named"
        assert cache.get_for_file(provider.priced[0]) is not None, "and by the file that was priced"

    def test_compare_print_options_prices_the_standing_copy_on_both_paths(self, tmp_path, judge, monkeypatch):
        import kiln.server as server

        src = _lying(tmp_path)
        order_service.remove(monkeypatch)
        asked = _Asked()
        monkeypatch.setattr(bridge, "quote", asked)

        served = server.compare_print_options(file_path=src, fulfillment_material_id="pla")

        (args, kwargs), = asked.calls
        assert args[0] != src and _stands(args[0]) and kwargs["as_sent"] is True
        assert served["fulfillment"]["pose"]["said"] == _LINE

        order_service.provide(monkeypatch)
        provider = _Provider()
        monkeypatch.setattr(server, "_get_fulfillment", lambda: provider)
        monkeypatch.setattr(server, "_get_billing", lambda: SimpleNamespace(calculate_fee=lambda *a, **k: SimpleNamespace(to_dict=dict, total_cost=10.0)))

        local = server.compare_print_options(file_path=src, fulfillment_material_id="pla")

        assert provider.priced == [args[0]]
        assert local["fulfillment"]["pose"]["priced_file"] == args[0]

    def test_kiln_order_quote_prices_the_standing_copy_and_says_so_first(self, tmp_path, judge, monkeypatch):
        import kiln.cli.main as cli_main

        provider = _Provider()
        service = SimpleNamespace(QuoteRequest=SimpleNamespace, get_provider=lambda *a: provider)
        monkeypatch.setattr(cli_main, "_order_service_or_served", lambda: service)
        monkeypatch.setattr(cli_main, "_order_routing", lambda name, **_kw: {})
        monkeypatch.setattr(cli_main, "format_quote", lambda data, json_mode=False: "QUOTE")
        src = _lying(tmp_path)

        out = CliRunner().invoke(cli_main.cli, ["order", "quote", src, "-m", "pla"])

        assert out.exit_code == 0, out.output
        assert out.output.splitlines()[0] == _LINE
        assert len(provider.priced) == 1 and provider.priced[0] != src and _stands(provider.priced[0])

        as_sent = CliRunner().invoke(cli_main.cli, ["order", "quote", src, "-m", "pla", "--as-sent"])

        assert as_sent.exit_code == 0, as_sent.output
        assert provider.priced[1] == src and _LINE not in as_sent.output

    def test_kiln_compare_cost_prices_the_standing_copy(self, tmp_path, judge, monkeypatch):
        import kiln.cli.main as cli_main

        provider = _Provider()
        service = SimpleNamespace(QuoteRequest=SimpleNamespace, get_provider=lambda *a: provider)
        monkeypatch.setattr(cli_main, "_order_service_or_served", lambda: service)
        src = _lying(tmp_path)

        out = CliRunner().invoke(cli_main.cli, ["compare-cost", src, "--fulfillment-material", "pla-0000-uuid-like-id", "--json"])

        assert out.exit_code == 0, out.output
        fulfillment = json.loads(out.output)["data"]["fulfillment"]
        assert fulfillment["pose"]["said"] == _LINE
        assert provider.priced == [fulfillment["pose"]["priced_file"]] and _stands(provider.priced[0])

    def test_the_served_order_service_asks_the_servers_to_price_exactly_the_file_handed_in(self, monkeypatch):
        asked = _Asked()
        monkeypatch.setattr(bridge, "quote", asked)

        bridge.ServedProvider().get_quote(SimpleNamespace(file_path="/models/knight.stl", material_id="pla", quantity=1, shipping_country="US"))

        (_args, kwargs), = asked.calls
        assert kwargs["as_sent"] is True

    def test_the_servers_are_told_only_when_the_file_was_chosen(self, tmp_path, monkeypatch):
        sent: list[dict] = []

        def ask(method, path, **kwargs):
            sent.append(dict(kwargs.get("data") or {}))
            return bridge.Answer(body={})

        monkeypatch.setattr(bridge, "_ask", ask)
        model = _write(tmp_path / "knight.stl", _figure_standing())

        bridge.quote(model, "pla", as_sent=True)
        bridge.quote(model, "pla")

        assert sent[0]["price_as_sent"] == "1" and "price_as_sent" not in sent[1]


# ---------------------------------------------------------------------------
# a model prepared for print
# ---------------------------------------------------------------------------


class TestAModelPreparedForPrint:
    def test_the_person_is_told_what_was_done_to_the_copy_and_it_keeps_where_it_came_from(self, tmp_path, judge):
        from kiln.plugins.validation_pipeline_tools import _stood_up

        src = _lying(tmp_path)
        before = Path(src).read_bytes()

        record = _stood_up(src, src)

        assert record["status"] == arrival_pose.TURNED and record["path"] != src
        assert record["said"] == _STOOD, "what WAS done, not advice to do it"
        assert record["undo"] == {"rotation_x": -90.0}
        assert _stands(record["path"]) and Path(src).read_bytes() == before
        assert arrival.read(record["path"]).by == "MakerWorld"

    def test_a_model_that_stands_leaves_no_empty_copy_behind(self, tmp_path, judge, monkeypatch):
        from kiln.plugins import validation_pipeline_tools as pipeline

        judge.verdict = Verdict("keep", "+Z", None, "It stands as it arrived.")
        made: list[str] = []
        real = pipeline._scaled_copy_path

        def watched(path):
            made.append(real(path))
            return made[-1]

        monkeypatch.setattr(pipeline, "_scaled_copy_path", watched)
        src = _write(tmp_path / "standing.stl", _figure_standing())

        record = pipeline._stood_up(src, src)

        assert record["status"] == arrival_pose.STANDING and record["path"] == src
        assert made and not Path(made[0]).exists()


# ---------------------------------------------------------------------------
# no door prices a model without settling its pose
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"
#: The print-service providers themselves: they price exactly what they are
#: handed, which the door chose.
_PROVIDERS = {("_pro_order_bridge.py", "get_quote")}


def _asks_for_a_price(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "get_quote":
        return True
    return (
        isinstance(func, ast.Attribute) and func.attr == "quote"
        and isinstance(func.value, ast.Name) and func.value.id in {"bridge", "served", "_served"}
    )


def _settles(fn: ast.AST) -> bool:
    """Calls file_to_price, or is handed the PricedFile a door made."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "file_to_price":
                return True
    args = getattr(fn, "args", None)
    return any(
        isinstance(a.annotation, ast.Name) and a.annotation.id == "PricedFile"
        for a in (args.args + args.kwonlyargs if args else [])
    )


def _unsettled_doors(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _asks_for_a_price(node)):
                continue
            chain, cur = [], parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chain.append(cur)
                cur = parents.get(cur)
            if not chain or (path.name, chain[0].name) in _PROVIDERS:
                continue
            if not any(_settles(fn) for fn in chain):
                found.append(f"{path.relative_to(root)}::{chain[0].name}")
    return found


def test_every_door_that_asks_a_print_service_for_a_price_settles_the_pose_first():
    assert _unsettled_doors(_SRC) == []


def test_the_check_finds_a_door_that_does_not(tmp_path):
    (tmp_path / "door.py").write_text(
        "def quote_it(provider, path):\n    return provider.get_quote(path)\n", encoding="utf-8"
    )
    assert _unsettled_doors(tmp_path) == ["door.py::quote_it"]
