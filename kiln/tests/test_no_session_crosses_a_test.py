"""A sign-in left in the shared test home never reaches the next test.

The shape this guards: one CI worker answers "your session has expired" to
a signed-out wording test and SIGN_IN_AGAIN to fleet tier tests while the
other workers stay green, because an earlier test on that worker left a
refused-session stamp in ``~/.kiln/auth_tokens.json`` under the one home
the whole worker shares.  Conftest clears that file around every test.

The module fixture below plants that stamp before any test fixture runs, the
way an earlier test would have; the test proves it was cleared before the
test began.  Fails with ``_no_session_crosses_a_test`` removed from conftest.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def _a_session_an_earlier_test_left():
    shared = Path.home() / ".kiln" / "auth_tokens.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({"access_token": "x.y.z", "refresh_token": "", "refresh_rejected_at": time.time()}))
    yield shared


def test_the_test_starts_signed_out(_a_session_an_earlier_test_left):
    from kiln.auth_session import stored_session_state

    assert not _a_session_an_earlier_test_left.exists()
    assert stored_session_state() == "signed_out"
