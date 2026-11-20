"""``register_printer`` asks a printer's address over the network once.

Registering a Moonraker printer at an address nothing answered used to hold
the call for about 100 s, and a saved printer was asked a second time by
``save_printer``.  These tests stand in for the network with
``requests.head`` and pin:

* an address that did not answer is asked once, and the person is told;
* adding a printer offers no lesson: its first print comes first;
* registering and saving asks the address once, not once per step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests


@pytest.fixture
def register(monkeypatch, tmp_path):
    from kiln import server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
    heads: list[str] = []

    def answering(answers: bool):
        def head(url, **_kw):
            heads.append(url)
            if not answers:
                raise requests.ConnectionError(f"no route to {url}")
            return object()

        monkeypatch.setattr(requests, "head", head)


    def call(*, answers: bool, persist: bool = False) -> dict:
        answering(answers)
        return server.register_printer(
            name="far",
            printer_type="moonraker",
            host="192.0.2.21",
            printer_model="neptune4_plus", printer_make="Elegoo",
            persist=persist,
            verify_connection=False,
        )

    return call, heads


def test_an_address_that_did_not_answer_is_not_asked_again_for_the_bench_offer(register):
    call, heads = register
    result = call(answers=False)
    assert result["success"] is True
    assert heads == ["http://192.0.2.21"]
    assert "bench_offer" not in result
    assert any(w.startswith("Could not reach http://192.0.2.21") for w in result["warnings"])


def test_adding_a_printer_offers_no_lesson(register, monkeypatch):
    from kiln import bench
    from tests.test_bench import _verdict

    # A printer Kiln could teach something to, at an address that answered:
    # the moment a lesson was once offered.  Its first print comes first now.
    monkeypatch.setattr(bench, "unit_of", lambda _adapter: "ab" * 16)
    monkeypatch.setattr(bench, "blanks_for", lambda _adapter, _pid: ([], _verdict(ok=True)))
    call, heads = register
    result = call(answers=True)
    assert result["success"] is True
    assert heads == ["http://192.0.2.21"]
    assert "bench_offer" not in result, "the printer's first print comes first"


def test_registering_and_saving_asks_the_address_once(register, tmp_path: Path):
    call, heads = register
    result = call(answers=False, persist=True)
    assert result["success"] is True and result["persisted"] is True
    assert (tmp_path / "config.yaml").exists()
    assert heads == ["http://192.0.2.21"]
