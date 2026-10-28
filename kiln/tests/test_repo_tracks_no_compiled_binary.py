"""The repository tracks no compiled binary.

A native executable or library of any operating system is never committed.
It is told by its first bytes, never by its name: ELF, Mach-O (thin or
universal), PE/MZ and WebAssembly.  A reader of this repository can then
trust that everything it holds is source they can read.

Kiln's one native component, the Mac helper that posts print codes as Kiln
(``kiln/native/notifier/main.swift``), is built from that source by the
release workflow (``.github/workflows/notifier.yml``) and carries a GitHub
build attestation for the commit it was built from::

    gh attestation verify <the helper> --repo codeofaxel/Kiln --source-digest <commit>

``kiln/scripts/build_notifier.py`` builds the same helper on a Mac for local
work, into a path the repository ignores.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_HELPER = "kiln/src/kiln/data/notifier/Kiln.app/Contents/MacOS/kiln-notifier"

#: First bytes of a native executable or library, as a file-type check reads them.
_NATIVE_HEADERS = {
    b"\x7fELF": "ELF",
    b"\xcf\xfa\xed\xfe": "Mach-O 64-bit",
    b"\xce\xfa\xed\xfe": "Mach-O 32-bit",
    b"\xfe\xed\xfa\xcf": "Mach-O 64-bit (big-endian)",
    b"\xfe\xed\xfa\xce": "Mach-O 32-bit (big-endian)",
    b"\xca\xfe\xba\xbe": "Mach-O universal",
    b"\xbe\xba\xfe\xca": "Mach-O universal (little-endian)",
    b"\x00asm": "WebAssembly",
}


def _native_kind(head: bytes) -> str | None:
    if head[:4] in _NATIVE_HEADERS:
        return _NATIVE_HEADERS[head[:4]]
    if head[:2] == b"MZ":
        return "PE/MZ"
    return None


def _tracked_files() -> list[str]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"], cwd=_REPO_ROOT, capture_output=True, check=False,
        )
    except OSError:
        listed = None
    if listed is None or listed.returncode != 0:
        pytest.skip("not a git checkout: there is no tracked tree to read")
    return [n for n in listed.stdout.decode("utf-8", "surrogateescape").split("\0") if n]


def test_no_tracked_file_is_a_native_binary():
    tracked = _tracked_files()
    found = []
    for name in tracked:
        path = _REPO_ROOT / name
        if path.is_symlink() or not path.is_file():
            continue
        with open(path, "rb") as fh:
            kind = _native_kind(fh.read(4))
        if kind:
            found.append(f"{name} ({kind})")
    assert found == [], (
        "compiled binaries are built by a workflow from source in this "
        "repository and attested, never committed:\n  " + "\n  ".join(found)
    )
    # The read was not vacuous: the tree, and the helper's source, were seen.
    assert len(tracked) > 500, len(tracked)
    assert "kiln/native/notifier/main.swift" in tracked


def test_the_header_check_knows_each_kind():
    assert _native_kind(b"\x7fELF") == "ELF"
    assert _native_kind(b"\xca\xfe\xba\xbe") == "Mach-O universal"
    assert _native_kind(b"\xcf\xfa\xed\xfe") == "Mach-O 64-bit"
    assert _native_kind(b"MZ\x90\x00") == "PE/MZ"
    assert _native_kind(b"\x00asm") == "WebAssembly"
    assert _native_kind(b"\x89PNG") is None
    assert _native_kind(bytes([0, 0, 1, 0])) is None  # a Windows icon file


def test_a_locally_built_helper_cannot_be_committed_by_accident():
    """``build_notifier.py`` writes the helper into the source tree; the
    repository ignores it there, so ``git add -A`` leaves it out."""
    _tracked_files()  # skips outside a checkout
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", _LOCAL_HELPER], cwd=_REPO_ROOT, check=False,
    )
    assert ignored.returncode == 0, f"{_LOCAL_HELPER} is not ignored"
