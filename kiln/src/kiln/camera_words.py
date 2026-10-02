"""The words Kiln uses for whether a printer model has a camera of its own.

One vocabulary, stated once.  The catalogue answer about a MODEL is one of
these four words; public Kiln's doors read it (:mod:`kiln._pro_camera_bridge`)
and the service that answers imports the words from here, so the two sides
cannot spell them differently.

A word is about the model as it leaves its maker.  It says nothing about a
camera its owner stood beside it, and nothing about whether a camera is
answering right now; :func:`kiln.plate_state.knows_a_camera` weighs those.
"""

from __future__ import annotations

#: Every unit of the model ships with a camera of its own.
FITTED = "fitted"
#: Not fitted as shipped; its maker sells or documents one the owner adds.
ADD_ON = "add_on"
#: No camera, and its maker offers none for it.
NONE = "none"
#: Not settled: the catalogue does not know the model, units of it differ,
#: or the maker's own pages do not say.
UNKNOWN = "unknown"

WORDS: tuple[str, ...] = (FITTED, ADD_ON, NONE, UNKNOWN)

__all__ = ["ADD_ON", "FITTED", "NONE", "UNKNOWN", "WORDS"]
