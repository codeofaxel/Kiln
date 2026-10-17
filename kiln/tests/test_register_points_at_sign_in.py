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
import re
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from kiln.cli import main as cli_main

#: The version the release process bumps (the packaging metadata a running
#: install reports can belong to another checkout; this file cannot).
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

#: Decided 2026-10-11: the command is deleted in the first release after this.
LAST_RELEASE_WITH_THE_POINTER = (1, 5, 0)


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


# ---------------------------------------------------------------------------
# The pointer is temporary
# ---------------------------------------------------------------------------


def _version(pyproject: Path = PYPROJECT) -> tuple[int, ...]:
    found = re.search(r'^version = "([0-9.]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
    assert found, f"no version line in {pyproject}"
    return tuple(int(part) for part in found.group(1).split("."))


def _pointer_overstays(version: tuple[int, ...]) -> bool:
    return version > LAST_RELEASE_WITH_THE_POINTER and "register" in cli_main.cli.commands


def test_the_pointer_is_deleted_after_1_5_0():
    """The release after 1.5.0 deletes ``kiln register`` outright (decided
    2026-10-11).  When this fails: delete the ``register`` command from
    kiln/cli/main.py and this file's first three tests with it.  The hosted
    API's answer to the old route stays: installs that never upgrade still
    call it."""
    assert not _pointer_overstays(_version()), (
        f"kiln {'.'.join(map(str, _version()))} still ships `kiln register`; it was to be deleted "
        "in the first release after 1.5.0 (decided 2026-10-11)."
    )


@pytest.mark.parametrize(("version", "fires"), [("1.4.1.1", False), ("1.5.0", False), ("1.5.0.1", True), ("1.6.0", True)])
def test_the_tripwire_fires_on_the_first_release_after_1_5_0(tmp_path, version, fires):
    fake = tmp_path / "pyproject.toml"
    fake.write_text(f'[project]\nname = "kiln3d"\nversion = "{version}"\n', encoding="utf-8")
    assert _pointer_overstays(_version(fake)) is fires
