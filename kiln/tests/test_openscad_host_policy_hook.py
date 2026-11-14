"""A host's policy has its say over every OpenSCAD launch, and a plain
install has none.

``kiln.openscad_runner.set_host_policy`` is the interface: a host that runs
OpenSCAD for more than one person names one policy, and every launch through
:func:`kiln.openscad_runner.run_openscad` consults it (the source each input
file would read, then an admission that sets the time limit and may refuse).
The OpenSCAD program is replaced by a recorder; nothing here starts it.
"""

from __future__ import annotations

import contextlib
import subprocess

import pytest

from kiln import openscad_runner
from kiln.generation import openscad as provider


class Recorder:
    def __init__(self) -> None:
        self.starts: list[tuple[list[str], float | None]] = []

    def __call__(self, argv, **kwargs):
        self.starts.append((list(argv), kwargs.get("timeout")))
        return subprocess.CompletedProcess(argv, 0, "", "")


class Policy:
    def __init__(self, *, refuse_word: str | None = None, busy: bool = False, ceiling: float = 9.0) -> None:
        self.refuse_word, self.busy, self.ceiling = refuse_word, busy, ceiling
        self.sources: list[tuple[str, str | None]] = []
        self.admitted = 0

    def refuse_source(self, source, source_dir):
        self.sources.append((source, source_dir))
        return "That file is not yours." if self.refuse_word and self.refuse_word in source else None

    @contextlib.contextmanager
    def admit(self, timeout):
        if self.busy:
            raise openscad_runner.LaunchRefused("Busy; try again.", retryable=True)
        self.admitted += 1
        yield min(timeout or self.ceiling, self.ceiling)


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(openscad_runner.run_program, "run", rec)
    monkeypatch.setattr(openscad_runner, "_closed_by_the_exact_engine", lambda argv, result, **kw: result)
    previous = openscad_runner.set_host_policy(None)
    yield rec
    openscad_runner.set_host_policy(previous)


def _source(tmp_path, text: str) -> str:
    path = tmp_path / "part.scad"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_plain_install_has_no_policy_and_runs_as_before(recorder, tmp_path) -> None:
    assert openscad_runner.host_policy() is None
    scad = _source(tmp_path, 'import("/anywhere.stl");')
    result = openscad_runner.run_openscad(["openscad", "-o", "x.stl", scad], timeout=1234)
    assert result.returncode == 0 and recorder.starts[0][1] == 1234


def test_the_policy_sees_the_source_and_its_folder_and_can_refuse(recorder, tmp_path) -> None:
    policy = Policy(refuse_word="secret")
    openscad_runner.set_host_policy(policy)
    scad = _source(tmp_path, 'import("/secret.stl");')
    result = openscad_runner.run_openscad(["openscad", "-o", "x.stl", scad])
    assert result.returncode == 1 and result.refused and "not yours" in result.stderr
    assert recorder.starts == [] and policy.admitted == 0
    assert policy.sources == [('import("/secret.stl");', str(tmp_path.resolve()))]


def test_admission_sets_the_time_limit_and_can_refuse_for_now(recorder, tmp_path) -> None:
    openscad_runner.set_host_policy(Policy(ceiling=9.0))
    scad = _source(tmp_path, "cube(1);")
    openscad_runner.run_openscad(["openscad", "-o", "x.stl", scad], timeout=86_400)
    assert recorder.starts[-1][1] == 9.0
    openscad_runner.set_host_policy(Policy(busy=True))
    result = openscad_runner.run_openscad(["openscad", "-o", "x.stl", scad], timeout=5)
    assert result.returncode == 1 and result.retryable and "try again" in result.stderr
    assert len(recorder.starts) == 1


def test_a_policy_that_fails_refuses_the_launch(recorder, tmp_path) -> None:
    class Broken(Policy):
        def refuse_source(self, source, source_dir):
            raise RuntimeError("could not judge")

    openscad_runner.set_host_policy(Broken())
    result = openscad_runner.run_openscad(["openscad", "-o", "x.stl", _source(tmp_path, "cube(1);")])
    assert result.returncode == 1 and recorder.starts == []


def test_the_provider_check_gives_the_hosts_answer_and_never_asks_the_disk(recorder) -> None:
    openscad_runner.set_host_policy(Policy(refuse_word="/srv/"))
    answers = []
    for path in ("/srv/exists-or-not.stl", "/srv/never-made.stl"):
        with pytest.raises(ValueError) as raised:
            provider._check_scad_security(f'import("{path}");')
        assert raised.value.code == "SCAD_FILE_READ_NOT_ALLOWED"
        answers.append(str(raised.value))
    assert answers[0] == answers[1]
    # A path the host allows is not probed for existence here either.
    provider._check_scad_security('import("/elsewhere/never-made.stl");')


def test_without_a_host_the_provider_check_is_unchanged(recorder) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        provider._check_scad_security('import("/no/such/file.stl");')
