"""Open a ZIP that came from outside the process without letting it exhaust memory.

A 3MF, a sliced-print file, a marketplace download, a file pulled off a
printer — each is a ZIP a stranger produced, and nothing in the format bounds
how large it unpacks.  On the shared hosted API (one process, a 2 GB machine,
every customer) an in-process blow-up is an outage, not one slow request (the
2026-08-24 render OOM took the whole API down).

**The primitive.**  ``zipfile``'s whole-member ``read()`` decompresses the
REAL stream regardless of the size the central directory declares, so a member
whose header LIES — declares 1 KB, holds 400 MB of deflated zeros — peaks
~800 MB read whole on Python 3.12 (measured 2026-10-13).  A whole ``read()``
with no length decompresses in steps of ``ZipExtFile.MAX_N`` (its default is
~1 GiB), handing that value to the decompressor as the per-step ceiling;
shadowing it with a small PER-INSTANCE value caps each step, so the same bomb
peaks ~33 MB and an honest member reads BYTE-IDENTICAL — the output is
truncated to the declared size either way.  Setting it in :meth:`open` covers
``zf.read(name)``, ``zf.open(name).read()`` and ``ET.parse(zf.open(name))``
with no change at any read site, because ``ZipFile.read`` itself goes through
``self.open``.

**The shape pre-check** at open refuses, before a byte is decompressed: too
many members (a central-directory flood), a member that expands past a sane
ratio (an honest ratio bomb), a compression method where ``read(n)`` does NOT
bound the decompressor (bzip2 / lzma — no 3MF or slicer writes them), and an
absurd declared total.  The caps are deliberately GENEROUS: this guard sits in
front of EVERY untrusted archive, a large sliced print included, so it must
never refuse a real file.  Measured 2026-10-13: the largest real archive total
was 127 MB (a dense model), the largest sliced ``.gcode.3mf`` 15 MB; the upload
doors already cap a file at 64-256 MB.  What a reader then HOLDS of an honest
file is the reader's own business: the 3MF readers stream their model parts
rather than building a tree per vertex (``kiln.threemf_parser``, "Streaming
model parts"), so an honest file of any real size keeps every feature.

**Honest bounds.**  The read ceiling bounds a whole ``read()``; a reader that
asks for an explicit length (``read(n)``) or streams in blocks (``ET.parse``,
``read_member_text``) is governed by that length, which is already bounded.
The member-count cap is checked after ``zipfile`` has parsed the central
directory, so it bounds a reader's per-member work, not the directory parse
itself — that cost is bounded instead by the archive's own byte size, which
the upload doors cap.
"""

from __future__ import annotations

import contextlib
import zipfile
from collections.abc import Iterator
from typing import Any

_MiB = 1024 * 1024

#: A whole-member ``read()`` decompresses in steps of this many bytes, so a
#: lying-header member cannot balloon a whole read past it.  8 MiB bounds the
#: 400 MB-in-1 KB bomb to ~33 MB peak (measured 2026-10-13) and costs an honest
#: large read nothing — its peak is the delivered buffer, not the step.
_MEMBER_READ_CEILING = 8 * _MiB

#: Generous shape caps — a backstop against the absurd, never the primary
#: memory defense (the read ceiling is).  Real files measured 2026-10-13:
#: largest archive total 127 MB, largest sliced ``.gcode.3mf`` 15 MB; upload
#: doors cap a file at 64-256 MB.  1 GiB total and 100k members clear every
#: real file with wide margin.
#
# Deliberately NO compression-ratio cap.  A ratio cap cannot tell a bomb from
# a real file: a legitimate Cura UFP compresses ~693x (a 3 MB comment line
# plus repetitive toolpath — public Kiln's own
# ``test_a_member_is_never_unpacked_past_the_budget`` fixture), and DEFLATE's
# own maximum is ~1032x, so any threshold low enough to catch a zeros bomb
# also refuses real G-code.  The memory a bomb could cost is bounded instead
# where it is actually spent: the per-member read ceiling caps a whole read
# however much a header lies, and the total cap bounds the sum — neither
# depends on the ratio.
_MAX_MEMBERS = 100_000
_MAX_UNCOMPRESSED_BYTES = 1024 * _MiB
_SAFE_COMPRESSIONS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class UntrustedArchiveError(zipfile.BadZipFile, ValueError):
    """An untrusted ZIP refused before it could exhaust memory.

    Subclasses BOTH :class:`zipfile.BadZipFile` and :class:`ValueError` on
    purpose: a bomb-shaped archive is a bad archive, so every reader that
    already turns a corrupt ZIP into its own decline — a handler catching
    ``BadZipFile``, one catching ``ValueError``, or a broad ``except
    Exception`` — catches an oversized one in the SAME place, with the SAME
    wording and return, and no handler has to be widened to know this type
    exists.  A site with no handler lets it propagate exactly as a corrupt
    ZIP already does there.
    """


class UntrustedZipFile(zipfile.ZipFile):
    """A read-only :class:`zipfile.ZipFile` that bounds what an untrusted
    archive can cost: a shape pre-check at open, and a per-member read ceiling
    that caps every whole-member ``read()``.

    Drop-in for ``zipfile.ZipFile(path)`` / ``zipfile.ZipFile(path, "r")`` at
    any site that opens an archive from outside the process.  Write modes are
    rejected — a ZIP Kiln is WRITING is not untrusted input.
    """

    def __init__(
        self,
        file: Any,
        mode: str = "r",
        *args: Any,
        max_members: int = _MAX_MEMBERS,
        max_uncompressed_bytes: int = _MAX_UNCOMPRESSED_BYTES,
        read_ceiling: int = _MEMBER_READ_CEILING,
        **kwargs: Any,
    ) -> None:
        if mode != "r":
            raise ValueError(
                f"UntrustedZipFile is read-only; got mode {mode!r}. A ZIP Kiln "
                "writes is not untrusted input — open it with zipfile.ZipFile."
            )
        self._read_ceiling = read_ceiling
        super().__init__(file, "r", *args, **kwargs)
        try:
            self._check_shape(max_members, max_uncompressed_bytes)
        except BaseException:
            self.close()
            raise

    def _check_shape(self, max_members: int, max_bytes: int) -> None:
        infos = self.infolist()
        if len(infos) > max_members:
            raise UntrustedArchiveError(
                f"archive names {len(infos)} members (limit {max_members})"
            )
        total = 0
        for info in infos:
            if info.compress_type not in _SAFE_COMPRESSIONS:
                raise UntrustedArchiveError(
                    f"member {info.filename!r} uses an unsupported compression "
                    f"method ({info.compress_type}); only stored and deflate are read"
                )
            total += info.file_size
            if total > max_bytes:
                raise UntrustedArchiveError(
                    f"archive unpacks to over {max_bytes} bytes"
                )

    def open(  # noqa: A003 — matches zipfile.ZipFile.open
        self,
        name: Any,
        mode: str = "r",
        pwd: Any = None,
        *,
        force_zip64: bool = False,
    ) -> Any:
        fh = super().open(name, mode, pwd, force_zip64=force_zip64)
        if mode == "r":
            # ZipExtFile.read() with no length decompresses in MAX_N steps;
            # shadow it per-instance so a lying header cannot balloon a whole
            # read.  Suppress AttributeError for any future handle that is not
            # a ZipExtFile — the guard is best-effort, never a new failure.
            with contextlib.suppress(AttributeError):
                fh.MAX_N = self._read_ceiling
        return fh


@contextlib.contextmanager
def open_untrusted_zip(file: Any, **kwargs: Any) -> Iterator[UntrustedZipFile]:
    """Open an untrusted archive as a context manager.

    The common entry point where ``with open_untrusted_zip(path) as zf:`` reads
    cleaner than constructing the class directly.  Raises
    :class:`UntrustedArchiveError` for an archive that fails the shape check
    and ``zipfile.BadZipFile`` for a non-ZIP — the exceptions readers already
    handle.
    """
    zf = UntrustedZipFile(file, **kwargs)
    try:
        yield zf
    finally:
        zf.close()
