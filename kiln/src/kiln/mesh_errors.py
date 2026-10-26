"""The one error a mesh reader raises for a file it cannot read as a model.

A :class:`ValueError`, like every other "this file cannot be read" answer in
Kiln, so a caller that already declines an unreadable mesh declines this one
in the same place.  Its own type lets the error envelope tell a caller's file
from Kiln's own mistake (:func:`kiln.failure_kind.refusal_code`) without
reading the message.  Kept in a module of its own so naming it costs no
mesh library import.
"""

from __future__ import annotations


class UnreadableMeshError(ValueError):
    """The file is not a model Kiln can read: not there as a mesh, empty, or
    in a shape no reader of its format accepts.  The same file fails the
    same way every time, so it is never worth retrying unchanged."""


class MeshFileMissing(UnreadableMeshError):
    """There is no file at the path a mesh read was handed.  A
    :class:`ValueError` (not a :class:`FileNotFoundError`) because that is
    what every mesh reader's callers already catch for a file they cannot
    read; :func:`kiln.failure_kind.refusal_code` still answers it as a
    missing file."""
