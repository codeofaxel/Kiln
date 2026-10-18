"""Kiln's frame: Z is up, the way a part stands on the printer bed.

STL and OBJ carry no up axis of their own and 3MF names Z, which is already
Kiln's, so those are read as written.  glTF (``.glb`` / ``.gltf``), the
format AI model generators and most model sites hand back, is different:
the glTF 2.0 specification defines +Y as up and says the front of an asset
faces +Z.  Read as written, every such model lies on its side.  The letter
a prompt asked for on top lands on a wall, and the height Kiln reports is
really the depth.

:func:`load_mesh` is the door every mesh read in Kiln goes through.  For
glTF it places each part where the file's scene graph puts it, then stands
the whole model up with one rigid turn, (x, y, z) -> (x, -z, y): up becomes
+Z and the front faces -Y, the side Kiln's front view looks at.  Every
other format comes back exactly as ``trimesh.load`` returns it.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any

import trimesh

#: File types whose own specification puts up on +Y.
Y_UP_TYPES = frozenset({"glb", "gltf"})

#: glTF -> Kiln: a +90 degree turn about X.  A rotation, never a mirror,
#: so a part's handedness and its threads survive the turn.
GLTF_TO_KILN = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, -1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)

#: The most one companion file may be.  A mesh file names its own companion
#: files and whoever wrote the mesh chose those names, so the read has to be
#: bounded by something other than trust.  Deliberately generous: an MTL is
#: kilobytes and the largest thing an MTL names is a texture, which the
#: upload doors already cap at 10 MB, so 16 MB clears every real companion
#: file and still bounds a request.
MAX_ASSET_BYTES = 16 * 1024 * 1024


#: Mesh entries -- vertices and triangles together -- one read may hold.
#: Read from the reader that already reasoned about it against real files
#: (``threemf_parser``: the largest real model measured 2026-10-13 holds
#: 2.2 M, and six million clears it about three times over), never copied,
#: so the two readers of one file cannot drift apart again.
try:
    from kiln.threemf_parser import _MAX_KEPT_MESH_ENTRIES as MAX_MESH_ENTRIES
except ImportError:  # pragma: no cover - the parser is part of this package
    MAX_MESH_ENTRIES = 6_000_000

#: Bytes the census reads at a time.  It only counts, so what it holds is
#: this, not the file.
_CENSUS_CHUNK = 1 << 20

#: How much of a 3MF's model part the census reads before it stops counting.
#: Past the budget the answer is already "too large"; reading further only
#: spends time.  An honest model's whole part is far inside it.
_CENSUS_MAX_MODEL_BYTES = 512 * 1024 * 1024


class MeshTooLargeError(ValueError):
    """A mesh file declares more mesh than one read may hold.

    A :class:`ValueError`, like every other "this file cannot be read"
    answer in Kiln, so a caller that already declines an unreadable mesh
    declines this one in the same place, with the same wording.
    """


#: A 3MF's vertex or triangle element, whatever namespace prefix it carries.
#: Anchored on the character immediately BEFORE the element's own name --
#: ``<`` when it has no prefix, ``:`` when it has one -- so the prefix may be
#: any length and the match stays ten bytes.  Anchoring on ``<`` instead
#: meant bounding the prefix, and a longer one than the bound walked past.
_MESH_ELEMENT_RE = re.compile(rb"[<:](?:vertex|triangle)[\s/>]")

#: Longest run :data:`_MESH_ELEMENT_RE` can match, so a chunk boundary
#: cannot hide one.
_MESH_ELEMENT_MAX = 1 + len("triangle") + 1


#: How much of an XML part is read to see what it is written in.
_ENCODING_WINDOW = 256

#: Byte orders a BOM announces that this census cannot read.
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")

#: The ``encoding="..."`` of an XML declaration.
_XML_ENCODING_RE = re.compile(rb"""encoding\s*=\s*["']([\w.\-]+)["']""", re.I)

#: What the byte census can read: ASCII's own spellings, where an element
#: name is the same bytes as its text.
_BYTE_ENCODINGS = frozenset({"utf-8", "utf8", "us-ascii", "ascii"})


def _counts_as_utf8(opening: bytes) -> bool:
    """Whether *opening* is XML this census can count by its bytes.

    A part in UTF-16 spells ``<vertex`` as ``<\\x00v\\x00e\\x00...``, which a
    byte pattern cannot match while the reader decodes it and reads every
    element -- so the census has to know it cannot count, rather than
    counting nothing and calling that none.
    """
    if opening.startswith(_WIDE_BOMS):
        return False
    if opening.startswith(b"\xef\xbb\xbf"):  # a UTF-8 BOM is still UTF-8
        return True
    body = opening[3:] if opening.startswith(b"\xef\xbb\xbf") else opening
    found = _XML_ENCODING_RE.search(body)
    if found is not None:
        return found.group(1).decode("ascii", "ignore").lower() in _BYTE_ENCODINGS
    # No declaration, so XML's own default is UTF-8 -- but only say so when
    # the opening POSITIVELY reads as ASCII-compatible XML.  BOTH halves are
    # load-bearing, and each was tried alone first: asking only whether it
    # lacks a NUL answered "countable" for anything unfamiliar, and asking
    # only whether it starts with "<" answered "countable" for UTF-16 with
    # no BOM, whose first byte IS "<" and whose second is NUL.
    return b"\x00" not in body and body.lstrip()[:1] == b"<"


def _count_matches(fh: Any, pattern: re.Pattern[bytes], ceiling: int,
                   overlap: int, opening: bytes = b"") -> int:
    """How many times *pattern* matches in the rest of *fh*, counted a chunk
    at a time and stopped once *ceiling* is passed.

    Chunks overlap by *overlap* bytes so a match split across a boundary is
    found, and the overlap is excluded from the next count so it is found
    exactly once.
    """
    total = 0
    tail = b""
    first = opening
    while True:
        chunk, first = (first, b"") if first else (fh.read(_CENSUS_CHUNK), b"")
        if not chunk:
            return total
        window = tail + chunk
        # Matches wholly inside the carried-over tail were counted last
        # round; count only those reaching into the new bytes.
        start = max(0, len(tail) - overlap)
        total += sum(1 for _ in pattern.finditer(window, start))
        if total > ceiling:
            return total
        tail = window[-overlap:] if overlap else b""


#: Bytes the OBJ census reads at a time.  Smaller than
#: :data:`_CENSUS_CHUNK` on purpose: the census splits a chunk on
#: whitespace, which turns it into one small object per word and costs
#: several times the chunk itself, so the chunk is sized for what the split
#: holds rather than for the read.
_LINE_CHUNK = 1 << 16

#: Letters that make a longer OBJ keyword out of a leading ``v`` or ``f``
#: (``vn``, ``vt``, ``vp``).  Anything else after them is a separator to
#: some reader, so the census counts the line.
_OBJ_WORD_TAIL = re.compile(rb"[A-Za-z]")


def _obj_line_kind(head: bytes) -> bytes:
    """What an OBJ line beginning with *head* declares: ``b"v"`` a vertex,
    ``b"f"`` a face, ``b"?"`` neither.

    A leading ``v`` or ``f`` followed by anything that is not a letter is
    the keyword itself however it is separated -- a space, a tab, a form
    feed.  Looking for a space or a tab alone meant a reader and this census
    disagreed about what a face line was, which is the whole bug class.
    """
    first = head[:1]
    if first not in (b"v", b"f"):
        return b"?"
    if _OBJ_WORD_TAIL.match(head[1:2] or b" "):
        return b"?"  # vn, vt, vp -- not a geometric vertex
    return first


def _entries_for_line(kind: bytes, corners: int) -> int:
    """What one finished OBJ line adds to the census."""
    if kind == b"v":
        return 1
    if kind == b"f" and corners:
        # A reader fans an n-corner face into n-2 triangles.
        return max(1, corners - 2)
    return 0


def _census_obj(fh: Any, ceiling: int) -> int:
    """Vertices and triangles an OBJ declares, counted as it streams.

    Deliberately never holds a LINE: an OBJ may put its whole mesh on one
    face line, and a census that accumulated bytes until a newline arrived
    would copy its growing buffer once per chunk -- quadratic in the file,
    on exactly the file it exists to refuse.  So corners are counted per
    chunk and only a word's worth of state is carried across the boundary,
    and a face already past the ceiling stops the count mid-line.
    """
    total = 0
    kind: bytes | None = None   # the line in progress
    head = b""                  # its first bytes, when a chunk split them
    corners = 0                 # words seen so far on a face line
    mid_word = False            # the last chunk ended inside a word
    line_last = b""             # last byte of the line in progress

    while True:
        chunk = fh.read(_LINE_CHUNK)
        if not chunk:
            break
        pieces = chunk.split(b"\n")
        for index, piece in enumerate(pieces):
            if index:
                # A newline ended the line before this one -- unless that
                # line ended in a backslash, which continues it.  A reader
                # joins those, so one face may be spread over a thousand
                # lines (measured 2026-10-18: trimesh reads a
                # backslash-continued face as the whole face), and
                # finishing the line here counted only its first part.
                #
                # Decided from the last byte of the line WHEREVER it fell:
                # reading it off this chunk's pieces meant a line split
                # across a chunk boundary was judged on half of itself.
                if line_last == b"\\":
                    mid_word = False  # the mark separates the two halves
                else:
                    total += _entries_for_line(kind or b"?", corners)
                    if total > ceiling:
                        return total
                    kind, head, corners, mid_word = None, b"", 0, False
                line_last = b""
            stripped = piece.rstrip(b"\r")
            line_last = stripped[-1:] or line_last
            if kind is None:
                probe = head + piece
                if len(probe) < 2 and index == len(pieces) - 1:
                    head = probe  # not enough yet to tell; carry it
                    continue
                kind, head, piece = _obj_line_kind(probe), b"", probe[1:]
                if kind != b"f":
                    continue
            elif kind != b"f":
                continue
            # What is left of this piece after the keyword, without the
            # trailing backslash, which is the continuation mark and not a
            # corner.  Taken from ``piece`` and not ``stripped``, because a
            # face's FIRST piece has had its keyword removed above.
            counted = piece.rstrip(b"\r")
            if counted.endswith(b"\\"):
                counted = counted[:-1]
            words = len(counted.split())
            if mid_word and counted[:1] and not counted[:1].isspace():
                words -= 1  # one word straddled the chunk boundary
            corners += max(0, words)
            mid_word = bool(counted) and not counted[-1:].isspace()
            if corners - 2 > ceiling:
                return corners
    return total + _entries_for_line(kind or b"?", corners)


def _census_3mf(path: str, ceiling: int) -> int | None:
    """Vertices and triangles a 3MF's model parts declare, read through the
    archive guard without holding the mesh."""
    try:
        from kiln.threemf_parser import open_untrusted_zip
        from kiln.untrusted_zip import UntrustedArchiveError
    except ImportError:  # pragma: no cover - part of this package
        return None
    try:
        with open_untrusted_zip(path) as zf:
            members = [
                info for info in zf.infolist()
                if info.filename.lower().endswith(".model")
            ]
            if not members:
                return None
            total = 0
            for info in members:
                if info.file_size > _CENSUS_MAX_MODEL_BYTES:
                    # It declares more than the census will read, so it
                    # declares more than the budget either way.
                    return ceiling + 1
                with zf.open(info) as fh:
                    opening = fh.read(_ENCODING_WINDOW)
                    if not _counts_as_utf8(opening):
                        # The census reads bytes; a part in some other
                        # encoding spells its elements in bytes this cannot
                        # match (``<\x00v\x00`` in UTF-16), while the
                        # reader decodes it and reads every one.  Refuse
                        # rather than count nothing: a real 3MF is UTF-8.
                        return ceiling + 1
                    total += _count_matches(
                        fh, _MESH_ELEMENT_RE, ceiling, _MESH_ELEMENT_MAX,
                        opening=opening,
                    )
                if total > ceiling:
                    return total
            return total
    except UntrustedArchiveError:
        # Kiln's archive guard will not open it at all -- too many members,
        # or it declares more than the guard unpacks.  trimesh reads a 3MF
        # with plain ``zipfile``, which has no such guard, so answering "not
        # counted" here would hand the reader exactly the archive Kiln's own
        # guard just turned away.
        return ceiling + 1
    except Exception:  # noqa: BLE001 - an unreadable archive is the reader's to report
        return None


#: A facet of an ASCII STL, followed by any whitespace.
_STL_FACET_RE = re.compile(rb"facet\s")


def declared_mesh_entries(path: str, file_type: str | None = None) -> int | None:
    """How many vertices and triangles the mesh file at *path* declares, or
    ``None`` when this format has no cheap census.

    *file_type* wins when given, as it does for ``trimesh.load``; otherwise
    the path's suffix decides.  It has to win: a reader told the type reads
    the file as that type whatever the name says, so censusing by the name
    alone would count nothing for ``load_mesh(tmp, file_type="obj")``.

    ``None`` means "not counted", never "none": a format the census cannot
    read cheaply is read exactly as it was before.  The counts are what the
    FILE claims, which is the point -- a claim is what a reader is about to
    allocate for, and reading the claim costs a header or a byte count
    rather than the mesh.
    """
    if file_type is None:
        try:
            kind = os.path.splitext(path)[1]
        except (AttributeError, TypeError):
            return None
    else:
        kind = file_type
    kind = str(kind).lower().lstrip(".")
    ceiling = MAX_MESH_ENTRIES
    try:
        if kind == "stl":
            with open(path, "rb") as fh:
                head = fh.read(84)
                if len(head) == 84 and not head.lstrip()[:5].lower().startswith(b"solid"):
                    # Binary: the triangle count is in the header, so this
                    # costs 84 bytes however large the claim.
                    return int.from_bytes(head[80:84], "little", signed=False)
                fh.seek(0)
                return _count_matches(fh, _STL_FACET_RE, ceiling, len("facet") + 1)
        if kind == "obj":
            with open(path, "rb") as fh:
                return _census_obj(fh, ceiling)
        if kind == "3mf":
            return _census_3mf(path, ceiling)
    except OSError:
        return None
    return None


def refuse_an_oversized_mesh(path: Any, file_type: str | None = None) -> None:
    """Refuse *path* when it declares more mesh than one read may hold.

    :raises MeshTooLargeError: past :data:`MAX_MESH_ENTRIES`.
    """
    try:
        text = os.fspath(path)
    except TypeError:
        return
    if not isinstance(text, str):
        return
    declared = declared_mesh_entries(text, file_type)
    if declared is not None and declared > MAX_MESH_ENTRIES:
        raise MeshTooLargeError(
            f"{os.path.basename(text)} declares {declared} vertices and "
            f"triangles, more than the {MAX_MESH_ENTRIES} Kiln reads at once. "
            "Simplify it (simplify_mesh_model), or split it into parts."
        )


class BesideTheFileResolver(trimesh.resolvers.FilePathResolver):
    """Reads a mesh's companion files from the mesh's OWN folder, and no
    further.

    A mesh file names the files it needs: an OBJ names its MTL
    (``mtllib``), an MTL names its texture (``map_Kd``), a ``.gltf`` names
    its buffers.  Those names are written inside the file by whoever made
    it, and the reader opens them while it reads the mesh.  When the mesh
    came up an upload door, the names are a stranger's: ``mtllib
    /etc/passwd``, a walk up out of the folder, a device that never ends.
    trimesh's own resolver joins the name onto the folder, and
    :func:`os.path.join` with an absolute right-hand side returns that
    absolute path -- so the name decided what was read (measured
    2026-10-18).

    This resolver answers a name only with a real file inside the folder
    the mesh was read from, at most :data:`MAX_ASSET_BYTES`.  Anything else
    raises :class:`OSError`, which is what every reader already treats as
    "that companion file is not there": the mesh still loads, with its
    triangles and its shape, and only the decoration that pointed outside
    is missing.

    Real files keep working, which is the other half of the job.  A name
    may point into a subfolder of the mesh's folder (``materials/part.mtl``).
    And where the name does not resolve as written, its last component is
    tried inside the folder, split on BOTH separators -- a real exporter
    writes the path the file had on its own machine
    (``mtllib C:\\Users\\sam\\part.obj.mtl``) while the file itself arrives
    beside the mesh.
    """

    def _confined(self, candidate: str) -> str | None:
        """*candidate* resolved inside this resolver's folder, or ``None``.

        Resolved with :func:`os.path.realpath` before the comparison, so a
        link that sits inside the folder and lands outside it is outside.
        """
        root = os.path.realpath(self.parent)
        full = os.path.realpath(os.path.join(root, candidate))
        if full != root and not full.startswith(root + os.sep):
            return None
        return full

    def _candidates(self, name: str) -> list[str]:
        text = str(name).strip()
        if not text:
            return []
        out = [text, text.lstrip("/\\")]
        last = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if last:
            out.append(last)
        return out

    def get(self, name: str) -> bytes:
        """The bytes of the companion file *name*, read from this folder.

        :raises OSError: when *name* names nothing in this folder, lands
            outside it, is not a regular file, or is larger than
            :data:`MAX_ASSET_BYTES`.
        """
        for candidate in self._candidates(name):
            # A candidate that is absolute, or walks up, resolves outside
            # the folder and is simply not a candidate.
            full = self._confined(candidate)
            if full is None or not os.path.isfile(full):
                continue
            if os.path.getsize(full) > MAX_ASSET_BYTES:
                raise OSError(
                    f"{os.path.basename(full)} is larger than the "
                    f"{MAX_ASSET_BYTES // (1024 * 1024)} MB Kiln reads for a "
                    "file a model names"
                )
            with open(full, "rb") as fh:
                # Bounded even though the size was just checked: the size
                # and the read are two moments, and this one is the one
                # that spends the memory.
                return fh.read(MAX_ASSET_BYTES)
        raise FileNotFoundError(
            f"the model names {str(name)[:120]!r}, which is not beside it"
        )

    def namespaced(self, namespace: str) -> "BesideTheFileResolver":
        """A resolver for a subfolder of this one -- confined in its turn,
        so the guard does not end one level down."""
        inner = self._confined(str(namespace)) if str(namespace).strip() else None
        if inner is None or not os.path.isdir(inner):
            # Nothing to descend into: a resolver on this folder answers
            # every name the same way, which is the honest fallback.
            return BesideTheFileResolver(self.parent)
        return BesideTheFileResolver(inner)


def _confined_resolver(source: Any) -> BesideTheFileResolver | None:
    """A :class:`BesideTheFileResolver` for *source*, or ``None`` when
    *source* is not a path on this disk (a file object carries no folder, so
    there is nothing for a reader to open beside it)."""
    try:
        path = os.fspath(source)
    except TypeError:
        return None
    if not isinstance(path, str) or not os.path.exists(path):
        return None
    try:
        return BesideTheFileResolver(path)
    except (OSError, ValueError):
        return None


def is_y_up(source: Any, file_type: str | None = None) -> bool:
    """Whether *source* is a format that puts up on +Y.

    *file_type* wins when given, as it does for ``trimesh.load``; otherwise
    the path's suffix decides.  A file object with no *file_type* is not one.
    """
    if file_type is None:
        try:
            file_type = os.path.splitext(os.fspath(source))[1]
        except TypeError:
            return False
    return file_type.lower().lstrip(".") in Y_UP_TYPES


def load_mesh(source: Any, **kwargs: Any) -> Any:
    """``trimesh.load`` in Kiln's frame: same arguments, same return types.

    A glTF file comes back with its parts where its scene graph places them,
    turned Z-up.  A glTF file whose scene places none of its meshes (some
    minimal writers leave the node list out) has them read where they are
    written, rather than coming back as an empty model.

    The companion files the mesh names -- its MTL, its textures, its
    buffers -- are read from the mesh's own folder and nowhere else, because
    those names were written by whoever made the file
    (:class:`BesideTheFileResolver`).  A caller that passes its own
    ``resolver`` keeps it.

    A file declaring more mesh than one read may hold is refused before any
    of that memory is spent (:func:`refuse_an_oversized_mesh`), at the limit
    Kiln's own 3MF reader already uses.

    :raises MeshTooLargeError: past :data:`MAX_MESH_ENTRIES`.
    """
    refuse_an_oversized_mesh(source, kwargs.get("file_type"))
    if "resolver" not in kwargs:
        resolver = _confined_resolver(source)
        if resolver is not None:
            kwargs["resolver"] = resolver
    if not is_y_up(source, kwargs.get("file_type")):
        return trimesh.load(source, **kwargs)
    force = kwargs.pop("force", None)
    scene = trimesh.load_scene(source, **kwargs)
    if scene.geometry and not scene.graph.nodes_geometry:
        for name in scene.geometry:
            scene.graph.update(frame_to=name, geometry=name)
    scene.apply_transform(GLTF_TO_KILN)
    return scene.to_mesh() if force == "mesh" else scene


#: trimesh's file readers.  Calling one directly skips the turn above.
_READERS = frozenset({"load", "load_mesh", "load_scene"})


def unrouted_loads(source: str) -> list[int]:
    """Lines in *source* that open a mesh with trimesh directly.

    Each one reads a glTF file lying on its side unless it goes through
    :func:`load_mesh`.  A line that must see the file exactly as written (a
    fingerprint of its bytes, say) says why with a ``raw frame:`` comment
    and is not listed.  Both repositories' tests run this over their trees.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    aliases = {"trimesh"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "trimesh" and alias.asname
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            hit = node.module == "trimesh" and any(a.name in _READERS for a in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            root = node.func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            hit = node.func.attr in _READERS and isinstance(root, ast.Name) and root.id in aliases
        else:
            continue
        if hit and "raw frame:" not in lines[node.lineno - 1]:
            found.append(node.lineno)
    return sorted(found)
