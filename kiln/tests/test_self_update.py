"""The self-update action: safe defer, honest reporting, graceful fallback."""

from __future__ import annotations

from dataclasses import dataclass

from kiln import self_update


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _runner(returncode=0, stdout="", stderr=""):
    captured = {}

    def run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _Proc(returncode, stdout, stderr)

    run.captured = captured  # type: ignore[attr-defined]
    return run


def test_defers_while_a_print_is_active():
    out = self_update.perform_upgrade(
        runner=_runner(0, "Successfully installed kiln3d-9.9.9"),
        print_active=lambda: True,
    )
    assert out["ok"] is False
    assert out["status"] == "deferred_active_print"
    assert out["restart_required"] is False
    assert "finishes" in out["message"].lower()


def test_force_overrides_the_active_print_defer():
    out = self_update.perform_upgrade(
        runner=_runner(0, "Successfully installed kiln3d-1.4.0"),
        print_active=lambda: True,
        force=True,
    )
    assert out["status"] == "updated"
    assert out["installed"] == "1.4.0"


def test_successful_upgrade_reports_versions_and_restart():
    run = _runner(0, "Collecting kiln3d\nSuccessfully installed kiln3d-1.4.0")
    out = self_update.perform_upgrade(runner=run)
    assert out["ok"] is True
    assert out["status"] == "updated"
    assert out["installed"] == "1.4.0"
    assert out["restart_required"] is True
    assert "restart" in out["message"].lower()
    # targets THIS interpreter's pip, not a bare "pip".
    assert run.captured["cmd"][1:] == ["-m", "pip", "install", "--upgrade", "kiln3d"]


def test_clean_run_with_no_install_line_means_already_latest():
    out = self_update.perform_upgrade(
        runner=_runner(0, "Requirement already satisfied: kiln3d")
    )
    assert out["status"] == "already_latest"
    assert out["restart_required"] is False


def test_pip_failure_hands_back_the_one_liner():
    out = self_update.perform_upgrade(
        runner=_runner(1, "", "ERROR: externally-managed-environment")
    )
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert out["command"] == self_update.UPGRADE_COMMAND
    assert "pip install --upgrade kiln3d" in out["message"]


def test_runner_exception_is_reported_not_raised():
    def boom(cmd, **kw):
        raise OSError("no pip here")

    out = self_update.perform_upgrade(runner=boom)
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "pip install --upgrade kiln3d" in out["message"]


def test_parse_installed_version_takes_the_last_match():
    assert self_update._parse_installed_version(
        "Successfully installed dep-1.0.0 kiln3d-2.3.4"
    ) == "2.3.4"
    assert self_update._parse_installed_version("nothing here") is None


# --- installers that write into a folder ------------------------------------


def _argvs(tmp_path, monkeypatch, *, pip: bool, uv_env: str = "", uv_on_path: str | None = None):
    if uv_env:
        monkeypatch.setenv(self_update.UV_ENV, uv_env)
    else:
        monkeypatch.delenv(self_update.UV_ENV, raising=False)
    return self_update.installer_argvs(
        "thing>=1",
        target=tmp_path / "into",
        find_spec=lambda name: object() if pip and name == "pip" else None,
        which=lambda name: uv_on_path if name == "uv" else None,
    )


def test_installer_argvs_lists_this_interpreters_pip_first_then_uv(tmp_path, monkeypatch):
    import sys

    target = str(tmp_path / "into")
    argvs = _argvs(tmp_path, monkeypatch, pip=True, uv_on_path="/somewhere/uv")
    assert argvs == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", "--target", target, "thing>=1"],
        ["/somewhere/uv", "pip", "install", "--no-deps", "--target", target, "--python", sys.executable, "thing>=1"],
    ]


def test_installer_argvs_without_pip_is_uv_alone_and_the_uv_that_started_kiln_wins(tmp_path, monkeypatch):
    argvs = _argvs(tmp_path, monkeypatch, pip=False, uv_env="/started/uv", uv_on_path="/path/uv")
    assert [argv[0] for argv in argvs] == ["/started/uv"]


def test_installer_argvs_is_empty_when_this_computer_has_no_installer(tmp_path, monkeypatch):
    assert _argvs(tmp_path, monkeypatch, pip=False) == []


def test_run_installer_tries_the_next_command_after_a_failure():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Proc(1, "", "no such option") if cmd == ["first"] else _Proc(0, "done\n", "")

    out = self_update.run_installer([["first"], ["second"]], runner=run)
    assert calls == [["first"], ["second"]]
    assert (out.ok, out.argv, out.stdout) == (True, ["second"], "done\n")


def test_run_installer_stops_at_no_connection_and_says_so():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Proc(1, "", "a\nb\nc\nFailed to establish a new connection: [Errno 8]")

    out = self_update.run_installer([["first"], ["second"]], runner=run)
    assert calls == [["first"]]
    assert (out.ok, out.offline, out.argv) == (False, True, ["first"])
    assert out.tail.splitlines()[0] == "b" and len(out.tail.splitlines()) == 3


def test_run_installer_reports_a_command_that_cannot_start_and_an_empty_list():
    def boom(cmd, **kw):
        raise OSError("not found")

    out = self_update.run_installer([["gone"]], runner=boom)
    assert (out.ok, out.argv, out.error, out.offline) == (False, ["gone"], "not found", False)
    nothing = self_update.run_installer([], runner=boom)
    assert (nothing.ok, nothing.argv) == (False, None)
