"""A refusal from Kiln's servers is shown in words and acted on by its code.

An answer's ``error`` may hold a sentence or a code word (``invalid_bearer``),
and which one depends on the route and on the Kiln release the servers think
they are answering.  Two things must hold whatever ``error`` holds:

* a person is never shown a bare code word, and
* a door that acts on a refusal (a revoked grant, a plan wall, the Terms)
  reads its code, so it keeps working when ``error`` holds the sentence and
  the word rides in ``code`` or ``error_word``.

The one reader is :func:`kiln.served_answer.read_refusal`.  These pin it, the
doors that show or act on a refusal, and a guard that finds, from source,
every read of ``error`` off an answer from Kiln's servers that goes around
the reader (:mod:`_answer_reads`): a new one fails here, and so does a
planted one.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from kiln import served_answer as sa
from tests import _answer_reads as reads

#: The raw reads of ``error`` that stay raw, each with why.  Frozen: a new
#: raw read fails, an entry no read matches fails as stale, and the list is
#: never longer than it was frozen at.
KEPT_RAW_READS: dict[tuple[str, str], str] = {
    ("kiln._pro_assembly_bridge", "_refuses_unknown"): (
        "the servers' refusal of an argument a tool does not take names the arguments in its sentence "
        "and in no code; the names are read back from those words, nothing is shown"
    ),
    ("kiln._pro_fleet_fill_bridge", "ask"): (
        "a ruling built in this door's own shape (an error object carrying its code) is recognised by "
        "that shape and relayed whole; nothing in it is read or shown here"
    ),
    ("kiln.plugins.fleet_tools", "_served_route"): (
        "a routing ruling built in this door's own shape (an error object carrying its code) is "
        "recognised by that shape and relayed whole; nothing in it is read or shown here"
    ),
    ("kiln.plugins.fleet_tools", "_with_runs_in_fleet"): (
        "reads the code inside a routing ruling's own error object, where the route puts it, to add "
        "each printer's filaments beside it; the ruling's words are left as they are"
    ),
    ("kiln.history_courier", "_diverged"): (
        "a push the account refused because its branch moved on is also told by the 409 sentence "
        "servers without the code send; the code is compared first and the sentence is the fallback"
    ),
}
KEPT_FROZEN_AT = 5


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return reads.package_sources()


@pytest.fixture(scope="module")
def scan(sources):
    return reads.Scan(sources)


class _Resp:
    """A stand-in HTTP answer for httpx and requests."""

    def __init__(self, status: int, body: Any = None, content: bytes = b"", headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.content = content or (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode(errors="replace")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


#: A code word, spelled out here so a check of the doors does not lean on
#: the reader it checks.
_CODE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*")


def _is_code_word(text: str) -> bool:
    return bool(_CODE_WORD.fullmatch(str(text)))


# ---------------------------------------------------------------------------
# the reader
# ---------------------------------------------------------------------------


class TestTheReader:
    def test_a_code_word_in_error_is_the_code_and_never_the_words(self):
        refusal = sa.read_refusal({"success": False, "error": "mfa_required", "message": "Enter your code."})
        assert refusal.code == "mfa_required"
        assert refusal.said == "Enter your code."

    def test_the_code_is_read_from_code_then_error_word_then_error(self):
        assert sa.read_refusal({"code": "TERMS_REQUIRED", "error": "terms_required"}).code == "TERMS_REQUIRED"
        assert sa.read_refusal({"error": "Agree to the Terms first.", "error_word": "terms_required"}).code == (
            "terms_required"
        )
        assert sa.read_refusal({"error": {"code": "NO_ROOM", "message": "No plate has room."}}).code == "NO_ROOM"
        assert sa.read_refusal({"error": "Something went wrong here."}).code == ""

    def test_a_code_with_no_words_of_its_own_gets_kilns_words(self):
        refusal = sa.read_refusal({"success": False, "error": "invalid_bearer"})
        assert refusal.said == ""
        assert refusal.words("The door's own sentence.") == sa.WORDS_FOR_CODE["invalid_bearer"]
        unknown = sa.read_refusal({"success": False, "error": "some_new_code"})
        assert unknown.said == ""
        assert unknown.words("The door's own sentence.") == "The door's own sentence."
        assert unknown.words() == sa.NO_REASON_GIVEN

    def test_words_inside_an_error_object_or_a_detail_object_are_found(self):
        assert sa.read_refusal({"error": {"code": "X", "message": "Nothing fits."}}).said == "Nothing fits."
        nested = sa.read_refusal({"detail": {"error": "plan_unavailable"}})
        assert nested.code == "plan_unavailable"
        assert nested.words() == sa.WORDS_FOR_CODE["plan_unavailable"]

    def test_in_words_puts_the_sentence_in_error_and_keeps_the_word(self):
        answer = {"success": False, "error": "delegation_revoked", "message": "That yes was closed."}
        worded = sa.in_words(answer)
        assert worded["error"] == "That yes was closed."
        assert worded["error_word"] == "delegation_revoked"
        assert sa.read_refusal(worded).code == "delegation_revoked"
        assert answer["error"] == "delegation_revoked"  # a copy, never a change


class TestTheServedDoor:
    def test_an_http_refusal_whose_error_is_a_code_word_comes_back_in_words(self):
        out = sa.envelope_for_http("keep_design", 401, {"error": "invalid_bearer"}, kind="made")
        assert not _is_code_word(out["error"])
        assert out["error"] == sa.WORDS_FOR_CODE["invalid_bearer"]
        assert out["error_word"] == "invalid_bearer"

    def test_a_code_word_with_no_words_gets_the_tools_own_sentence(self):
        out = sa.envelope_for_http("keep_design", 409, {"error": "some_new_code"}, kind="made")
        assert not _is_code_word(out["error"])
        assert "Kiln can't" in out["error"]
        assert out["error_word"] == "some_new_code"

    def test_a_tools_answer_with_a_code_word_comes_back_in_words(self, monkeypatch):
        import urllib.request

        from kiln import server

        class _Opened:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"success": False, "error": "plan_unavailable"}).encode()

        monkeypatch.setattr(server, "_served_bearer", lambda: ("tok", None))
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Opened())
        out = server._pro_api_call("verify_design_release")
        assert not _is_code_word(out["error"])
        assert out["error"] == sa.WORDS_FOR_CODE["plan_unavailable"]
        assert out["error_word"] == "plan_unavailable"

    def test_the_terms_are_handed_over_when_error_holds_the_sentence(self, monkeypatch):
        from kiln import server, terms

        handed: list[str] = []
        monkeypatch.setattr(terms, "sync_to_account", lambda *, bearer, version: handed.append(version) or True)
        body = {"error": "Agree to Kiln's Terms of Use first.", "error_word": "terms_required", "version": "7"}
        assert server._terms_agreement_handed_over(403, body, "tok") is True
        assert handed == ["7"]


# ---------------------------------------------------------------------------
# the doors that show or act on a refusal
# ---------------------------------------------------------------------------


class TestKeepAndUpload:
    @pytest.fixture(autouse=True)
    def _signed_in(self, monkeypatch, tmp_path):
        from kiln import served_makes

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
        monkeypatch.setattr(served_makes, "_bearer", lambda: "tok")

    def test_keeping_a_make_says_a_refusal_in_words(self, monkeypatch):
        import httpx

        from kiln import served_makes

        monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(401, {"error": "invalid_bearer"}))
        out = served_makes.keep("a" * 32)
        assert not _is_code_word(out["error"])
        assert out["error"] == sa.WORDS_FOR_CODE["invalid_bearer"]
        assert sa.read_refusal(out).code == "invalid_bearer"

    def test_an_upload_refused_with_a_code_word_says_words(self, monkeypatch, tmp_path):
        import httpx

        from kiln import served_makes

        part = tmp_path / "part.stl"
        part.write_bytes(b"solid x\nendsolid x\n")
        monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(400, {"error": "file_unreadable"}))
        token, said = served_makes._upload_file(part)
        assert token is None
        assert "file_unreadable" not in said
        assert not _is_code_word(said)


class TestPrintApprovals:
    @pytest.fixture(autouse=True)
    def _signed_in(self, monkeypatch):
        from kiln import bridge_client

        monkeypatch.setattr(bridge_client, "account_bearer", lambda: "tok")
        monkeypatch.setattr(bridge_client, "_machine_id", lambda: "machine-1")

    def _answer(self, monkeypatch, status: int, body: dict) -> None:
        import requests

        monkeypatch.setattr(requests, "request", lambda method, url, **kw: _Resp(status, body))

    def test_a_grant_already_closed_is_closed_when_error_holds_the_sentence(self, monkeypatch):
        from kiln import bridge_client

        self._answer(monkeypatch, 409, {
            "success": False, "error": "That standing yes was already closed.", "error_word": "delegation_revoked",
        })
        assert bridge_client.revoke_on_account("grant-1") is True

    def test_the_on_screen_yes_reads_the_refusals_code(self, monkeypatch):
        from kiln import bridge_client

        self._answer(monkeypatch, 403, {
            "success": False, "error": "This computer is not switched on to say yes.", "error_word": "screen_yes_off",
        })
        ok, code, said = bridge_client.confirm_on_screen("challenge-1")
        assert (ok, code) == (False, "screen_yes_off")
        assert said == "This computer is not switched on to say yes."

    def test_a_rename_the_account_refused_is_said_in_words(self, monkeypatch):
        import requests

        from kiln import bridge_client

        monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(409, {"success": False, "error": "name_taken"}))
        done, why = bridge_client.rename_on_account("old", "new")
        assert done is False
        assert "name_taken" not in why


class TestSpendCaps:
    def test_the_tool_reads_the_code_when_error_holds_the_sentence(self):
        from kiln.plugins import spend_cap_tools

        out = spend_cap_tools._refusal(
            {"success": False, "error": "Changing caps from chat is turned off.", "error_word": "OPT_IN_REQUIRED"},
            default="Could not request a cap change.",
        )
        assert out["code"] == "OPT_IN_REQUIRED"
        assert out["error"].startswith("Changing caps from chat is turned off.")

    def test_the_tool_never_shows_a_bare_code_word(self):
        from kiln.plugins import spend_cap_tools

        out = spend_cap_tools._refusal(
            {"success": False, "error": "OPT_IN_REQUIRED", "enable_url": "https://app.kiln3d.com/x"},
            default="Could not request a cap change.",
        )
        assert out["code"] == "OPT_IN_REQUIRED"
        assert not out["error"].startswith("OPT_IN_REQUIRED")

    def test_showing_caps_refused_says_words(self, monkeypatch):
        from kiln.plugins import spend_cap_tools

        tools: dict[str, Any] = {}

        class _Mcp:
            def tool(self, **_kw):
                def keep(fn):
                    tools[fn.__name__] = fn
                    return fn

                return keep

        monkeypatch.setattr(spend_cap_tools, "_ask", lambda *a, **k: ({"success": False, "error": "unauthorized"}, None))
        spend_cap_tools.plugin.register(_Mcp())
        out = tools["kiln_spend_caps_show"]()
        assert out["code"] == "UNAUTHORIZED"
        assert not _is_code_word(out["error"])
        assert out["error"] == sa.WORDS_FOR_CODE["unauthorized"]

    def test_the_command_reads_the_code_when_error_holds_the_sentence(self):
        from kiln.cli.spend_caps_commands import _surface_envelope_error

        refused = _surface_envelope_error(
            {"success": False, "error": "Too many tries today.", "error_word": "RATE_LIMITED",
             "retry_after_seconds": 7200},
            default="Cap change denied.",
        )
        assert "paused for 24 hours" in refused.message


class TestHistory:
    def test_an_account_refusal_with_a_code_word_comes_back_in_words(self, monkeypatch):
        import httpx

        from kiln import auth_session, history_courier

        monkeypatch.setattr(
            auth_session, "resolve_api_bearer", lambda *a, **k: SimpleNamespace(token="tok", state="ok", detail=""),
        )
        monkeypatch.setattr(httpx, "request", lambda method, url, **kw: _Resp(403, {"error": "terms_required"}))
        out = history_courier._request("cloud_push_branch", "GET", "/api/cloud/designs")
        assert out["success"] is False
        assert not _is_code_word(out["error"])
        assert out["error"] == sa.WORDS_FOR_CODE["terms_required"]
        assert sa.read_refusal(out).code in ("KILN_API_HTTP_ERROR", "terms_required")
        assert out["error_word"] == "terms_required"


# ---------------------------------------------------------------------------
# the guard: no read of error goes around the reader
# ---------------------------------------------------------------------------


class TestTheGuard:
    def test_no_door_reads_error_around_the_reader(self, scan):
        found = {(module, fn) for module, fn, _line in scan.raw_reads()}
        new = sorted(found - set(KEPT_RAW_READS))
        assert not new, (
            "these read `error` off an answer from Kiln's servers without kiln.served_answer.read_refusal: "
            "show `read_refusal(answer).words(...)` and compare `read_refusal(answer).code` instead: "
            + ", ".join(f"{m}.{f}" for m, f in new)
        )

    def test_the_kept_list_only_shrinks(self, scan):
        found = {(module, fn) for module, fn, _line in scan.raw_reads()}
        stale = sorted(set(KEPT_RAW_READS) - found)
        assert not stale, f"kept raw reads that are gone: delete their entries: {stale}"
        assert len(KEPT_RAW_READS) <= KEPT_FROZEN_AT, "the kept raw reads only shrink; read through the reader"
        for key, why in KEPT_RAW_READS.items():
            assert len(why.split()) >= 12, key

    def test_no_comparison_of_error_with_a_code_word(self, sources):
        assert reads.code_comparisons(sources) == []

    def test_a_raw_read_planted_in_a_door_is_caught(self, sources):
        planted = dict(sources)
        planted["kiln.served_makes"] = sources["kiln.served_makes"] + (
            "\n\ndef _planted_door(url):\n"
            "    import httpx\n\n"
            "    resp = httpx.post(url)\n"
            "    return resp.json().get(\"error\")\n"
        )
        found = {(m, f) for m, f, _ in reads.Scan(planted).raw_reads()}
        assert ("kiln.served_makes", "_planted_door") in found

    def test_a_raw_read_planted_downstream_of_a_door_is_caught(self, sources):
        planted = dict(sources)
        planted["kiln._planted_reader"] = (
            "from kiln.history_courier import _request\n"
            "from kiln.server import _pro_api_call\n\n\n"
            "def shown():\n"
            "    answer = _pro_api_call(\"some_tool\")\n"
            "    return answer[\"error\"]\n\n\n"
            "def also_shown():\n"
            "    got = _request(\"cloud_x\", \"GET\", \"/api/cloud/designs\")\n"
            "    return got.get(\"error\") or \"nothing\"\n"
        )
        found = {(m, f) for m, f, _ in reads.Scan(planted).raw_reads()}
        assert {("kiln._planted_reader", "shown"), ("kiln._planted_reader", "also_shown")} <= found

    def test_a_comparison_of_error_with_a_code_word_planted_anywhere_is_caught(self, sources):
        planted = dict(sources)
        planted["kiln._planted_compare"] = (
            "def closed(data):\n"
            "    return data.get(\"error\") in (\"delegation_revoked\", \"delegation_unknown\")\n"
        )
        assert ("kiln._planted_compare", "closed", 2) in reads.code_comparisons(planted)


def test_every_kept_entry_names_a_function_that_exists(sources):
    for module, fn in KEPT_RAW_READS:
        assert f"def {fn}(" in sources[module], (module, fn)
