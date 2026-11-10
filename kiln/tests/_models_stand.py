"""Every model stands as it arrived, for tests about something else.

A door that asks a print service for a price first asks Kiln which way up
the model stands (:func:`kiln.fulfillment_service.file_to_price`), and that
judgement is Kiln's, served.  A test of the price, the order or the terms
imports :func:`models_stand` so the judgement answers "it stands" without
asking anyone; what is said when a model arrives lying down is pinned in
``test_quote_stands_the_model_up.py``.
"""

from __future__ import annotations

import pytest

#: The smallest model Kiln can read: a 10 mm tetrahedron standing on a face,
#: for a door that must measure what it prices.
TETRAHEDRON_STL = (
    b"solid test\n"
    b"facet normal 0 0 -1\n outer loop\n  vertex 0 0 0\n  vertex 0 10 0\n  vertex 10 0 0\n endloop\nendfacet\n"
    b"facet normal 0 -1 0\n outer loop\n  vertex 0 0 0\n  vertex 10 0 0\n  vertex 0 0 10\n endloop\nendfacet\n"
    b"facet normal -1 0 0\n outer loop\n  vertex 0 0 0\n  vertex 0 0 10\n  vertex 0 10 0\n endloop\nendfacet\n"
    b"facet normal 1 1 1\n outer loop\n  vertex 10 0 0\n  vertex 0 10 0\n  vertex 0 0 10\n endloop\nendfacet\n"
    b"endsolid test\n"
)


@pytest.fixture(autouse=True)
def models_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiln._pro_arrival_pose_bridge import Verdict

    monkeypatch.setattr(
        "kiln._pro_arrival_pose_bridge.judge",
        lambda facts: (Verdict("keep", "+Z", None, "It stands as it arrived."), None),
    )
