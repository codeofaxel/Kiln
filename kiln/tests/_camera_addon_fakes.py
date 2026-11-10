"""Stand-ins for the camera add-on tests: a computer with nothing on it, and an installer that installs nothing real."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kiln import camera_addon

MODULE = "imageio_ffmpeg"
PROTOCOLS_OK = "Supported file protocols:\nInput:\n  file\n  rtsp\n  tls\nOutput:\n  file\n"
PROTOCOLS_PLAIN = "Supported file protocols:\nInput:\n  file\n  rtsp\nOutput:\n  file\n"


def write_package(folder: Path) -> str:
    """A tiny bundled package in *folder*; returns the program it names."""
    package = folder / MODULE
    (package / "binaries").mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        "import os\n\n\n"
        "def get_ffmpeg_exe():\n"
        "    return os.path.join(os.path.dirname(__file__), 'binaries', 'standin')\n",
        encoding="utf-8",
    )
    program = package / "binaries" / "standin"
    program.write_text("stand-in\n", encoding="utf-8")
    return str(program)


def forget_package() -> None:
    for name in [n for n in sys.modules if n == MODULE or n.startswith(f"{MODULE}.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


class _HideEnvironmentCopy:
    """Hides a real copy in the test environment, and nothing else."""

    def __init__(self, hidden: set[str]) -> None:
        self._hidden = hidden

    def find_spec(self, name, path=None, target=None):
        if name != MODULE:
            return None
        allowed = [p for p in sys.path if p not in self._hidden]
        spec = importlib.machinery.PathFinder.find_spec(name, allowed)
        if spec is None:
            raise ModuleNotFoundError(name)
        return spec


def bare_computer(monkeypatch, tmp_path: Path) -> Path:
    """Nothing that finds the program is present; returns Kiln's (empty) home.

    Nothing learned earlier in this process carries in, and the computer's
    own system is unknown until a test writes one with :func:`a_system`.
    """
    home = tmp_path / "kiln-home"
    monkeypatch.setenv("KILN_HOME", str(home))
    monkeypatch.setenv("KILN_CLI", "kiln")
    for name in ("KILN_UPDATE_COMMAND", "KILN_HOSTED_MULTITENANT", "UV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(camera_addon, "_system_program", lambda: None)
    monkeypatch.setattr(camera_addon, "_LEARNED", {}, raising=False)
    monkeypatch.setattr(camera_addon, "_OS_RELEASE_FILES", (tmp_path / "no-os-release",), raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    forget_package()
    real = importlib.util.find_spec(MODULE)
    if real is not None and real.origin:
        hidden = {str(Path(real.origin).parent.parent)}
        sys.meta_path.insert(0, _HideEnvironmentCopy(hidden))
    return home


@dataclass
class _Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeInstaller:
    """A runner that answers the installer and the program check without running either."""

    returncode: int = 0
    stderr: str = ""
    writes: bool = True
    protocols: str = PROTOCOLS_OK
    check_returncode: int = 0
    raises: BaseException | None = None
    calls: list[list[str]] = field(default_factory=list)

    @property
    def installs(self) -> list[list[str]]:
        return [c for c in self.calls if "--target" in c]

    @property
    def checks(self) -> list[list[str]]:
        return [c for c in self.calls if "--target" not in c]

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "--target" not in argv:
            return _Proc(self.check_returncode, self.protocols)
        if self.raises is not None:
            raise self.raises
        if self.returncode == 0 and self.writes:
            write_package(Path(argv[argv.index("--target") + 1]))
        return _Proc(self.returncode, "", self.stderr)


def has_pip(name: str):
    return object() if name == "pip" else None


def no_installer(name: str):
    return None


def program_on_this_computer(monkeypatch, path: str, *, secure: bool = True) -> None:
    """A camera program already on this computer, which can (or cannot) open a secure stream."""
    monkeypatch.setattr(camera_addon, "_system_program", lambda: path)
    monkeypatch.setattr(camera_addon, "_reads_secure_streams", lambda _program: secure, raising=False)


def a_system(monkeypatch, tmp_path: Path, os_release: str) -> Path:
    """This computer's /etc/os-release says *os_release*."""
    release = tmp_path / "os-release"
    release.write_text(os_release, encoding="utf-8")
    monkeypatch.setattr(camera_addon, "_OS_RELEASE_FILES", (release,), raising=False)
    return release


#: Each installer's words for "no prebuilt package fits this computer".
NO_BUILD_ANSWERS = {
    "pip": (
        "ERROR: Could not find a version that satisfies the requirement imageio-ffmpeg<1,>=0.6 "
        "(from versions: none)\nERROR: No matching distribution found for imageio-ffmpeg<1,>=0.6"
    ),
    "pip-resolver": (
        "Additionally, some packages in these conflicts have no matching distributions "
        "available for your environment:\n    imageio-ffmpeg"
    ),
    "uv-platform-tag": (
        "Because imageio-ffmpeg==0.8.1 has no wheels with a matching platform tag "
        "(e.g., `manylinux_2_36_armv7l`) and you require imageio-ffmpeg>=0.6,<1, we can "
        "conclude that your requirements are unsatisfiable."
    ),
    "uv-current-platform": (
        "error: Distribution `imageio-ffmpeg==0.8.1 @ registry+https://pypi.org/simple` can't be "
        "installed because it doesn't have a wheel for the current platform"
    ),
}
