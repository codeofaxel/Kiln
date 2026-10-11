"""``kiln register`` makes no key and sends nothing: it points at sign-in.

Until 2026-10-11 the command traded an email for a free licence key on any
install with kiln-pro's licence manager present -- no account behind it, so
the person had nothing to sign in to, and the address was kept beside the key
in a table the privacy policy described as holding only a hash.  One person
ever used it.  A free account is made by signing in, and every route that took
that key also takes the account.  The command stays, hidden, so an old habit
or guide still leads somewhere.
"""

from __future__ import annotations

import json
from unittest import mock

from click.testing import CliRunner

from kiln.cli import main as cli_main


class _Manager:
    """A licence manager, so the command reaches what used to be its minting
    branch.  Activating anything is a failure of the test."""

    def get_info(self):
        return mock.MagicMock(is_valid=False, tier=mock.MagicMock(value="free"), license_key_hint="")

    def activate_license(self, key):  # pragma: no cover - reaching here is the bug
        raise AssertionError("kiln register activated a key")


def _run(monkeypatch, *args):
    monkeypatch.setattr(cli_main, "_license_manager_or_none", lambda: _Manager())
    with mock.patch("requests.post", side_effect=AssertionError("kiln register sent a request")) as post:
        result = CliRunner().invoke(cli_main.cli, ["register", *args], input="maker@example.com\n")
    return result, post


def test_it_sends_nothing_and_points_at_sign_in(monkeypatch):
    result, post = _run(monkeypatch, "--email", "maker@example.com")
    assert post.call_count == 0
    assert result.exit_code == 1, result.output
    assert "kiln signin" in result.output and "SIGN_IN_INSTEAD" in result.output
    assert "Registered!" not in result.output and "Email address" not in result.output


def test_json_says_the_same_thing(monkeypatch):
    result, post = _run(monkeypatch, "--json")
    assert post.call_count == 0 and result.exit_code == 1
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["error"]["code"] == "SIGN_IN_INSTEAD"
    assert "kiln signin" in payload["error"]["message"]


def test_new_people_never_see_it_in_help():
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    listed = {line.split()[0] for line in result.output.splitlines() if line.startswith("  ") and line.split()}
    assert "register" not in listed
    assert "signin" in listed
