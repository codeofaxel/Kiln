"""An interpreter a test starts must find the packages this one found.

The suite moves HOME to a scratch directory (see the top of ``conftest``).
Python works out the per-user package directory from HOME when an
interpreter starts, so a child started after the move looked for it under
the scratch home and lost every package installed with ``pip install
--user``.  Tests that spawn ``sys.executable`` each worked around that their
own way, or did not, and one of the workarounds read ``site.getuserbase()``
-- which in a pytest-xdist worker is already wrong, because the worker
itself was started after the controller had moved HOME.  The same test
passed serially and died in the child with ``ModuleNotFoundError`` in a
parallel run.

``conftest`` now names the directory in the environment before it moves
HOME.  These tests hold that in both run modes.
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys

_SCRATCH_HOME_MARK = "kiln-test-home-"

_PROBE = (
    "import json, site, sys; "
    "print(json.dumps({'base': site.getuserbase(), "
    "'user_site': site.getusersitepackages(), 'path': sys.path}))"
)


def _child(env: dict[str, str]) -> dict:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_this_interpreter_knows_the_user_site_it_was_started_with():
    """True in a worker too, which starts after the controller moved HOME."""
    base = site.getuserbase()
    assert _SCRATCH_HOME_MARK not in base, base
    assert not base.startswith(os.environ["HOME"]), base
    assert os.environ.get("PYTHONUSERBASE") == base


def test_a_child_with_the_inherited_environment_keeps_the_user_site():
    child = _child(dict(os.environ))
    assert child["base"] == site.getuserbase()
    assert _SCRATCH_HOME_MARK not in child["base"], child["base"]


def test_a_child_given_its_own_scratch_home_keeps_the_user_site(tmp_path):
    """The shape that failed: a fresh interpreter with HOME moved again."""
    child = _child({**os.environ, "HOME": str(tmp_path)})
    assert child["base"] == site.getuserbase()
    assert not child["base"].startswith(str(tmp_path)), child["base"]

    # Where this interpreter imports from a user site, the child does too.
    here = site.getusersitepackages()
    if here in sys.path:
        assert child["user_site"] == here
        assert here in child["path"]
