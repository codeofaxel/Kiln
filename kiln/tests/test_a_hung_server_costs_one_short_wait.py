"""Servers that take a request and never answer cost one short wait.

The substitution, pairing, multi-material and adaptive slicing doors each
have a basic answer of their own.  When Kiln's servers hang, the first ask
waits a few seconds and is answered basic; every ask after it, at any of
those doors, is answered basic at once and says when Kiln asks again; once
that wait is over, the next ask goes to the servers again and a good answer
is used.  Pinned against a real socket that accepts and never replies, with
the default wait of the served door made long, so an ask that does not
bring its own short wait shows.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Any

import pytest

#: The served door's own wait for a tool that names none, made long here.
_LONG_WAIT_S = 9.0
#: The probe after a timeout, the handshake and a slow runner.
_MARGIN_S = 2.5


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def hung_server(monkeypatch):
    """A signed-in install whose servers take each request and never answer."""
    import kiln.server as srv

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    listener.settimeout(0.2)
    held: list[socket.socket] = []
    stop = threading.Event()

    def _accept() -> None:
        while not stop.is_set():
            try:
                conn, _addr = listener.accept()
            except OSError:
                continue
            held.append(conn)

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    port = listener.getsockname()[1]
    monkeypatch.setenv("KILN_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(srv, "_served_bearer", lambda: ("signed-in-account", None))
    monkeypatch.setattr(srv, "_served_wait_seconds", lambda *_a, **_k: _LONG_WAIT_S)
    yield
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    listener.close()


def _timed(call, *args, **kwargs) -> tuple[Any, float]:
    started = time.monotonic()
    out = call(*args, **kwargs)
    return out, time.monotonic() - started


def _pairing_door():
    from kiln.plugins.design_tools import plugin

    tools: dict = {}

    class _Mcp:
        def tool(self, *_a, **_k):
            def keep(fn):
                tools[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    return tools["check_multi_material_pairing"]


@pytest.mark.usefixtures("hung_server")
def test_one_short_wait_then_every_door_answers_basic_at_once(monkeypatch):
    import kiln.server as srv
    from kiln import served_answer
    from kiln.material_safety import check_material_compatibility
    from kiln.plugins.adaptive_slicing_tools import get_material_slicing_profile

    consult = getattr(served_answer, "CONSULT_TIMEOUT_S", 4.0)

    first, waited = _timed(srv.find_material_substitute, "PETG")
    assert first["basic"] is True and first["why"] == "unanswered"
    assert waited < consult + _MARGIN_S, f"the first ask waited {waited:.1f} s"

    later = [
        _timed(srv.get_best_material_substitute, "PETG"),
        _timed(_pairing_door(), "PLA", "PETG"),
        _timed(check_material_compatibility, ["PLA", "PETG"]),
        _timed(get_material_slicing_profile, "PETG", nozzle_diameter_mm=0.4),
    ]
    for out, seconds in later:
        assert out.get("basic") is True and out.get("why") == "unanswered", out
        assert seconds < 1.0, f"an ask after the first waited {seconds:.1f} s"
        # Said again, not asked: the answer says when Kiln asks again.
        assert out.get("retry_after_s", 0) > 0, out


@pytest.mark.usefixtures("hung_server")
def test_after_the_wait_kiln_asks_again_and_uses_a_good_answer(monkeypatch):
    import kiln.server as srv
    from kiln import served_answer

    first = srv.find_material_substitute("PETG")
    assert first["basic"] is True and first["why"] == "unanswered"

    asked: list[str] = []
    good = {
        "success": True,
        "status": "ok",
        "substitutes": [{
            "original_material": "petg", "substitute_material": "pla", "device_type": "fdm",
            "compatibility_score": 0.4, "reasons": ["unavailable"], "trade_offs": "stand-in",
            "cost_delta_pct": 1.0,
        }],
    }

    def answering(tool_name, _timeout=None, _asked_by_user=True, _feature=None, **_kwargs):
        asked.append(tool_name)
        return good

    monkeypatch.setattr(srv, "_pro_api_call", answering)

    # Inside the wait the servers are left alone.
    held = srv.find_material_substitute("PETG")
    assert held["basic"] is True and asked == []

    # Past it, the next ask goes to the servers and their answer is used.
    backoff = getattr(served_answer, "SERVICE_BACKOFF_S", 300.0)
    real = time.monotonic

    class _Later:
        @staticmethod
        def monotonic() -> float:
            return real() + backoff + 1.0

    monkeypatch.setattr(served_answer, "time", _Later, raising=False)
    answered = srv.find_material_substitute("PETG")
    assert asked == ["material_substitutes"]
    assert answered.get("basic") is not True
    assert [s["substitute_material"] for s in answered["substitutes"]] == ["pla"]
