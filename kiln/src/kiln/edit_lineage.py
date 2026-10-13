"""An edit of a part Kiln already holds is kept as that part's next version.

With kiln-pro installed (https://kiln3d.com), a tool that edits a mesh --
repair, rescale, texture, decorate and the rest -- keeps its result in the
version history of the part it edited, naming the tool that changed it, and
the result carries a ``saved_version`` block saying where it was kept.  A
part with no history yet gets one.  Without kiln-pro this module does
nothing, and every tool answers exactly as it always has.

Two entry points:

* :func:`install` -- run once by ``kiln serve`` after every tool has
  registered;
* :func:`around` -- for a door outside the tool registry that edits one file
  (a CLI command)::

      with edit_lineage.around("kiln repair", path) as edit:
          result = edit.finish(repair_stl(path, output_path=out))
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

#: The key a result names where an edit was kept.
SAVED_VERSION_KEY = "saved_version"


def install(mcp: Any) -> dict[str, Any]:
    """Keep every registered edit tool's result in its part's history.

    Idempotent and never raises.  Returns ``{"installed": False}`` on an
    install without kiln-pro.
    """
    try:
        from kiln_pro.design_versions.edit_lineage import install as _install
    except ImportError:
        return {"installed": False}
    try:
        return _install(mcp)
    except Exception:  # noqa: BLE001 -- a server that will not start is worse
        logger.warning("edit lineage: not installed", exc_info=True)
        return {"installed": False}


class Edit:
    """One edit of one file, in progress (see :func:`around`)."""

    def __init__(self, door: str, path: str) -> None:
        self._pending: Any = None
        try:
            from kiln_pro.design_versions.edit_lineage import begin_file, discard, finish
        except ImportError:
            return
        self._finish, self._discard = finish, discard
        self._pending = begin_file(door, path)

    def finish(self, result: Any) -> Any:
        """*result* -- the edit's answer, a dict naming the edited file under
        ``path`` or ``output_path`` -- plus ``saved_version`` when the edit was
        kept.  Returns *result* unchanged without kiln-pro."""
        pending, self._pending = self._pending, None
        if pending is None:
            return result
        return self._finish(pending, result)

    def drop(self) -> None:
        """The edit did not happen: keep nothing."""
        pending, self._pending = self._pending, None
        if pending is not None:
            self._discard(pending)


@contextlib.contextmanager
def around(door: str, path: str) -> Iterator[Edit]:
    """An :class:`Edit` of *path* by the door named *door*.  Read before the
    door writes, since it may overwrite *path*; call :meth:`Edit.finish` with
    its answer.  An exception, or no call to ``finish``, keeps nothing."""
    edit = Edit(door, path)
    try:
        yield edit
    finally:
        edit.drop()
