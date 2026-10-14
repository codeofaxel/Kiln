"""The untrusted-archive guard bounds what a hostile ZIP can cost.

A 3MF / sliced-print / marketplace file is a ZIP a stranger produced, and
``zipfile``'s whole-member ``read()`` decompresses the real stream however
small the central directory says the member is — so a member whose header
lies (declares 1 KB, holds hundreds of MB of deflated zeros) balloons the
process when read whole.  On the shared 2 GB hosted machine that is an outage.

:mod:`kiln.untrusted_zip` closes it with a per-instance read ceiling applied
in ``open()`` (so ``zf.read()`` and ``zf.open().read()`` are both bounded with
no change at any read site) plus a shape pre-check at open.  These tests prove
the ceiling bounds a bomb, an honest file reads byte-identical, the shape
check refuses the absurd, and three representative hosted readers that take an
uploaded 3MF stay bounded on a bomb — the regression proof measured in a
subprocess (A/B: on the pre-guard raw opens the same bomb peaked ~0.8 GB).
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kiln.untrusted_zip import (
    _MEMBER_READ_CEILING,
    UntrustedArchiveError,
    UntrustedZipFile,
    open_untrusted_zip,
)
from tests._child_memory import CHILD_PEAK_MB_FUNCTION

_TINY_MODEL = b'<?xml version="1.0"?><model xmlns="x"><resources/></model>'

def _zip(path: Path, members: dict[str, bytes], *, method_overrides=None) -> str:
    overrides = method_overrides or {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            if name in overrides:
                zf.writestr(name, data, compress_type=overrides[name])
            else:
                zf.writestr(name, data)
    return str(path)


def _lying_header_3mf(path: Path, *, real_mb: int = 300, declared: int = 1024) -> str:
    """A decompression-bomb fixture: a member that DECLARES *declared* bytes
    but really holds *real_mb* MB of deflated zeros.

    ``zipfile`` writes honest headers, so this rewrites the one
    uncompressed-size field in the single central-directory record down to the
    small value — the shape a hostile uploader produces by hand.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf, zf.open("3D/3dmodel.model", "w") as fh:
        chunk = b"\0" * (1 << 20)
        for _ in range(real_mb):
            fh.write(chunk)
    raw = bytearray(path.read_bytes())
    cd = raw.rfind(b"PK\x01\x02")  # the single central-directory record
    struct.pack_into("<I", raw, cd + 24, declared)  # its uncompressed-size field
    path.write_bytes(bytes(raw))
    return str(path)


class TestOrdinaryArchivesPass:
    def test_open_and_read_a_normal_member(self, tmp_path):
        path = _zip(tmp_path / "ok.3mf", {"3D/3dmodel.model": _TINY_MODEL})
        with open_untrusted_zip(path) as zf:
            assert zf.read("3D/3dmodel.model") == _TINY_MODEL

    def test_honest_member_reads_byte_identical_to_stock(self, tmp_path):
        # Incompressible body (ratio ~1) so the member exercises the read
        # path, not the ratio cap.
        body = b"<model/>" + os.urandom(100_000)
        path = _zip(tmp_path / "h.3mf", {"3D/3dmodel.model": body})
        stock = zipfile.ZipFile(path).read("3D/3dmodel.model")
        guarded = UntrustedZipFile(path).read("3D/3dmodel.model")
        assert guarded == stock == body

    def test_open_sets_the_read_ceiling_on_every_member(self, tmp_path):
        path = _zip(tmp_path / "c.3mf", {"3D/3dmodel.model": _TINY_MODEL})
        with open_untrusted_zip(path) as zf, zf.open("3D/3dmodel.model") as fh:
            assert fh.MAX_N == _MEMBER_READ_CEILING


class TestHostileArchivesRefused:
    def test_write_mode_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="read-only"):
            UntrustedZipFile(tmp_path / "x.zip", "w")

    def test_unsupported_compression_method_is_refused(self, tmp_path):
        path = _zip(
            tmp_path / "bz2.3mf",
            {"3D/3dmodel.model": _TINY_MODEL},
            method_overrides={"3D/3dmodel.model": zipfile.ZIP_BZIP2},
        )
        with pytest.raises(UntrustedArchiveError, match="compression method"):
            UntrustedZipFile(path)

    def test_a_highly_compressible_but_honest_member_is_NOT_refused(self, tmp_path):
        # No ratio cap: a legit Cura UFP compresses ~693x, so refusing by ratio
        # would turn away real G-code.  The read ceiling bounds the memory, not
        # the ratio — this honestly-declared high-ratio member opens fine and
        # its whole read stays bounded (its delivered size is its honest size).
        body = b"; cura\n" + b"G1 X1 Y1 E.1\n" * 500_000  # compresses hundreds of x
        path = _zip(tmp_path / "ufp.gcode.3mf", {"Metadata/plate_1.gcode": body})
        with open_untrusted_zip(path) as zf:
            assert zf.read("Metadata/plate_1.gcode") == body

    def test_too_many_members_is_refused(self, tmp_path):
        path = _zip(tmp_path / "flood.3mf", {f"m{i}.model": b"x" for i in range(20)})
        with pytest.raises(UntrustedArchiveError, match="members"):
            UntrustedZipFile(path, max_members=10)

    def test_declared_total_over_cap_is_refused(self, tmp_path):
        # Incompressible so the TOTAL cap is what refuses it.
        path = _zip(tmp_path / "huge.3mf", {"big.gcode": os.urandom(2 * 1024 * 1024)})
        with pytest.raises(UntrustedArchiveError, match="unpacks to over"):
            UntrustedZipFile(path, max_uncompressed_bytes=1024 * 1024)

    def test_a_lying_header_bomb_is_bounded_not_refused(self, tmp_path):
        # It PASSES the shape check (declares 1 KB) — the read ceiling is what
        # saves us.  Reading it raises BadZipFile (the CRC fails on the
        # truncated-to-declared output), which UntrustedArchiveError also is,
        # so every reader's decline path catches either.
        bomb = _lying_header_3mf(tmp_path / "bomb.3mf", real_mb=200)
        with open_untrusted_zip(bomb) as zf, pytest.raises(zipfile.BadZipFile):
            zf.read("3D/3dmodel.model")

    def test_untrusted_archive_error_is_caught_as_badzipfile_and_valueerror(self):
        err = UntrustedArchiveError("x")
        assert isinstance(err, zipfile.BadZipFile)
        assert isinstance(err, ValueError)


def test_hosted_readers_stay_bounded_on_a_bomb(tmp_path):
    """Three representative hosted readers that take an uploaded 3MF must not
    balloon on a lying-header bomb.

    A/B measured 2026-10-13 on Python 3.12: on the pre-guard raw
    ``zipfile.ZipFile`` opens, reading this bomb whole peaked ~0.8 GB; through
    the guard each reader peaks near its import baseline.  The child uses the
    readers' own public entry points (each swallows a bad archive into its
    decline), so it runs on BOTH the pre-guard and post-guard trees: pre-guard
    it exceeds the bound and fails, post-guard it stays bounded and passes.
    The bound is generous (400 MB) so CI variance never flakes it — the class
    caught is an order of magnitude.
    """
    bomb = _lying_header_3mf(tmp_path / "bomb.3mf", real_mb=400)
    script = (
        "import sys, resource\n"
        f"sys.path[:0] = {list(sys.path)!r}\n"
        "from kiln.multicolor_3mf import detect_3mf_multicolor\n"
        "from kiln.model_metadata import extract_3mf_metadata\n"
        "from kiln.file_metadata import _extract_3mf_metadata\n"
        f"p = {bomb!r}\n"
        "for fn in (detect_3mf_multicolor, extract_3mf_metadata, _extract_3mf_metadata):\n"
        "    try:\n"
        "        fn(p)\n"
        "    except Exception:\n"
        "        pass\n"
        + CHILD_PEAK_MB_FUNCTION
        + "print(_peak_mb())\n"
    )
    env = {"PATH": os.environ.get("PATH", "")}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        timeout=180, env=env,
    )
    assert out.returncode == 0, out.stderr[-1000:]
    peak_mb = float(out.stdout.strip().splitlines()[-1])
    assert peak_mb < 400, f"hosted readers peaked at {peak_mb:.0f} MB on a bomb"
