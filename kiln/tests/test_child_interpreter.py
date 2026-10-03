"""A child interpreter imports what its parent imports, and nothing that sits beside its script.

Both failures were real (2026-10-01).  Under the test sandbox, which moves
HOME, the mesh offset's child could not find numpy -- it lives in the user's
site-packages, which a child works out from HOME.  And every child script that
lives inside the ``kiln`` package had ``kiln/queue.py`` standing in for the
standard library's ``queue``: trimesh imports it, so the child ran with a print
queue where a thread pool expected ``SimpleQueue``.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from kiln.child_interpreter import run_in_child


class ChildFailed(RuntimeError):
    pass


def _script(folder, body: str):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "child.py"
    path.write_text(textwrap.dedent(body))
    return path


_ANSWER_WHAT_IT_SEES = """
    import json, sys
    with open(sys.argv[1], encoding="utf-8") as fh:
        request = json.load(fh)
    import queue
    answer = {"stdlib_queue": hasattr(queue, "SimpleQueue"), "echo": request["echo"]}
    try:
        import kiln_child_probe_module
        answer["probe"] = kiln_child_probe_module.VALUE
    except ImportError as exc:
        answer["probe"] = repr(exc)
    print(json.dumps(answer))
"""


@pytest.fixture
def child(tmp_path, monkeypatch):
    """A script beside a ``queue.py`` of its own, and a module only the parent's path can reach."""
    script = _script(tmp_path / "package", _ANSWER_WHAT_IT_SEES)
    (tmp_path / "package" / "queue.py").write_text("PRINT_QUEUE = True\n")
    extra = tmp_path / "only_on_the_parents_path"
    extra.mkdir()
    (extra / "kiln_child_probe_module.py").write_text("VALUE = 'found'\n")
    monkeypatch.syspath_prepend(str(extra))
    return script


def test_the_child_sees_the_parents_import_path_and_the_real_stdlib(child):
    answer = run_in_child(str(child), {"echo": 7}, timeout_s=60, what="the probe", error=ChildFailed)
    assert answer == {"stdlib_queue": True, "echo": 7, "probe": "found"}


def test_a_script_run_by_path_gets_neither(child, tmp_path):
    """The launch both offset engines used before: the script's folder first,
    the parent's path re-derived.  Kept as the control that proves the
    fixture really reproduces both failures."""
    request = tmp_path / "request.json"
    request.write_text('{"echo": 7}')
    run = subprocess.run([sys.executable, str(child), str(request)], capture_output=True, text=True, timeout=60)
    import json

    answer = json.loads(run.stdout.strip().splitlines()[-1])
    assert answer["stdlib_queue"] is False
    assert "No module named" in answer["probe"]


def test_a_refusal_raises_the_callers_error_with_the_childs_sentence(tmp_path):
    script = _script(tmp_path, 'import json; print(json.dumps({"refused": "the part is not a closed solid"}))\n')
    with pytest.raises(ChildFailed, match="^the part is not a closed solid$"):
        run_in_child(str(script), {}, timeout_s=60, what="the probe", error=ChildFailed)


def test_a_crash_raises_its_last_line(tmp_path):
    script = _script(tmp_path, "raise SystemExit('kernel ran out of memory')\n")
    with pytest.raises(ChildFailed, match="kernel ran out of memory"):
        run_in_child(str(script), {}, timeout_s=60, what="the probe", error=ChildFailed)


def test_a_child_past_its_time_is_stopped(tmp_path):
    script = _script(tmp_path, "import time; time.sleep(30)\n")
    with pytest.raises(ChildFailed, match="the probe took longer than 0.5 s and was stopped"):
        run_in_child(str(script), {}, timeout_s=0.5, what="the probe", error=ChildFailed)
