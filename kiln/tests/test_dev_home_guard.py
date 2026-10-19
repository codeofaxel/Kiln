"""A test or a sandboxed run never writes the developer's real ``~/.kiln``.

The suite's own ``conftest.py`` moves ``HOME`` for every test it collects;
these tests are about the runs that skip it: a Kiln test run some other way, a
one-off probe with ``KILN_SANDBOX=1``.  The behavioural half starts a real
child interpreter with ``HOME`` set to the account's REAL home and only
imports Kiln, then reads where its home ended up.  Importing is all it does,
so nothing can be written even on the old code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiln import dev_home_guard as guard

SRC = Path(guard.__file__).resolve().parents[1]
REAL = guard.account_home()


def _decide(**over):
    args = dict(home=REAL, real_home=REAL, kiln_home=None, pytest_running=False,
                sandbox_requested=False, source_checkout=True, allow_real_home=False)
    args.update(over)
    return guard.decide(**args)


class TestTheRule:
    def test_a_test_of_a_source_checkout_never_uses_the_real_home(self):
        assert _decide(pytest_running=True) == "test"

    def test_a_run_that_asked_for_a_sandbox_never_uses_it_either(self):
        assert _decide(sandbox_requested=True) == "sandbox"
        assert _decide(sandbox_requested=True, source_checkout=False) == "sandbox"

    def test_kiln_home_pointed_at_the_real_folder_counts_too(self, tmp_path):
        assert _decide(home=str(tmp_path), kiln_home=os.path.join(REAL, ".kiln"), pytest_running=True) == "test"

    def test_normal_use_is_never_moved(self):
        assert _decide() is None

    def test_an_installed_kiln_never_moves_a_strangers_tests(self):
        assert _decide(pytest_running=True, source_checkout=False) is None

    def test_a_run_already_somewhere_else_is_left_alone(self, tmp_path):
        assert _decide(home=str(tmp_path), pytest_running=True, sandbox_requested=True) is None

    def test_the_escape_uses_the_real_records_on_purpose(self):
        assert _decide(pytest_running=True, sandbox_requested=True, allow_real_home=True) is None

    def test_this_tree_reads_as_a_source_checkout_and_site_packages_does_not(self, tmp_path):
        assert guard.is_source_checkout()
        installed = tmp_path / "lib" / "python3.12" / "site-packages" / "kiln" / "__init__.py"
        installed.parent.mkdir(parents=True)
        installed.write_text("")
        assert not guard.is_source_checkout(str(installed))


def _child(**env_over) -> dict:
    """Import Kiln in a fresh interpreter whose HOME is the REAL home."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTEST_CURRENT_TEST", guard.SANDBOX_ENV, guard.ALLOW_REAL_HOME_ENV,
                        guard.MOVED_ENV, "KILN_HOME")}
    env.update(HOME=REAL, PYTHONPATH=str(SRC))
    env.update(env_over)
    code = (
        "import json, os, kiln; "
        "print(json.dumps({'home': os.environ['HOME'], 'moved': os.environ.get('KILN_SANDBOX_HOME'), "
        "'kiln': kiln.__file__}))"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not os.path.isdir(REAL), reason="no account home to point a child at")
class TestARealChildProcess:
    def test_a_test_run_without_the_suites_setup_is_moved_off_the_real_home(self):
        said = _child(PYTEST_CURRENT_TEST="tests/test_x.py::test_y (call)")
        assert said["kiln"].startswith(str(SRC))  # this tree, not an installed copy
        assert not said["home"].startswith(REAL + os.sep) and said["home"] != REAL
        assert said["moved"] == said["home"] and "kiln-test-home-" in said["home"]

    def test_a_sandboxed_run_is_moved_off_it_too(self):
        said = _child(**{guard.SANDBOX_ENV: "1"})
        assert said["home"] != REAL and "kiln-sandbox-home-" in said["home"]

    def test_normal_use_keeps_the_real_home(self):
        said = _child()
        assert said["home"] == REAL and said["moved"] is None

    def test_the_escape_keeps_it_on_purpose(self):
        said = _child(**{guard.SANDBOX_ENV: "1", guard.ALLOW_REAL_HOME_ENV: "1"})
        assert said["home"] == REAL and said["moved"] is None
