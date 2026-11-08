"""A signed-in install, for tests: the one bearer an install holds.

There are no license keys, so a test that needs "this machine has a
bearer" signs the install in the way ``kiln signin`` does: a session
file where :func:`kiln.auth_session._tokens_path` reads it.  The session
carries *token* as its access token and no refresh token, so every reader
(:func:`kiln.auth_session.resolve_api_bearer`, the raw token readers) hands
back exactly *token* without touching the network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: The bearer a test's signed-in install presents unless it names its own.
SIGNED_IN_TOKEN = "signed-in-account"


def sign_in(monkeypatch, tmp_path: Path, token: str = SIGNED_IN_TOKEN) -> str:
    """Sign this install in with *token* and return it.

    Writes under ``KILN_AUTH_HOME`` when the test already pointed it
    somewhere, otherwise under *tmp_path*, never the session-wide test HOME,
    so one test's sign-in never reaches the next."""
    home = os.environ.get("KILN_AUTH_HOME")
    if not home:
        home = str(tmp_path / "signed-in-home")
        monkeypatch.setenv("KILN_AUTH_HOME", home)
    path = Path(home) / ".kiln" / "auth_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": token}), encoding="utf-8")
    return token


def sign_out() -> None:
    """Remove the sign-in :func:`sign_in` wrote, leaving the install signed out."""
    home = os.environ.get("KILN_AUTH_HOME")
    if home:
        (Path(home) / ".kiln" / "auth_tokens.json").unlink(missing_ok=True)
