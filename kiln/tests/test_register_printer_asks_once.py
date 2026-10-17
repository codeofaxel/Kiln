"""``register_printer`` asks a printer's address over the network once.

Registering a Moonraker printer at an address nothing answered used to hold
the call for about 100 s: the address check waited 5 s for its HEAD request,
then the one-time bench offer asked the printer for its motion settings and
waited out the adapter's three 30 s attempts to learn nothing.  A saved
printer was asked a second time by ``save_printer``.  These tests stand in
for the network with ``requests.head`` and pin both:

* an address that did not answer is not asked again for the bench offer,
  and the person is told it did not answer;
* an address that answered still gets the offer;
* registering and saving asks the address once, not once per step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests


@pytest.fixture
def register(monkeypatch, tmp_path):
    from kiln import bench, server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
    heads: list[str] = []
    offers: list[str] = []

    def answering(answers: bool):
        def head(url, **_kw):
            heads.append(url)
            if not answers:
                raise requests.ConnectionError(f"no route to {url}")
            return object()

        monkeypatch.setattr(requests, "head", head)

    monkeypatch.setattr(bench, "offer_after_registration", lambda _adapter, name: offers.append(name))

    def call(*, answers: bool, persist: bool = False) -> dict:
        answering(answers)
        return server.register_printer(
            name="far",
            printer_type="moonraker",
            host="192.0.2.21",
            printer_model="neptune4_plus",
            persist=persist,
            verify_connection=False,
        )

    return call, heads, offers


def test_an_address_that_did_not_answer_is_not_asked_again_for_the_bench_offer(register):
    call, heads, offers = register
    result = call(answers=False)
    assert result["success"] is True
    assert heads == ["http://192.0.2.21"]
    assert offers == []
    assert any(w.startswith("Could not reach http://192.0.2.21") for w in result["warnings"])


def test_an_address_that_answered_still_gets_the_offer(register):
    call, heads, offers = register
    result = call(answers=True)
    assert result["success"] is True
    assert heads == ["http://192.0.2.21"]
    assert offers == ["far"]


def test_registering_and_saving_asks_the_address_once(register, tmp_path: Path):
    call, heads, _offers = register
    result = call(answers=False, persist=True)
    assert result["success"] is True and result["persisted"] is True
    assert (tmp_path / "config.yaml").exists()
    assert heads == ["http://192.0.2.21"]
