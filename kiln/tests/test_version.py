"""Version metadata tests."""

from __future__ import annotations

import kiln


def test_version_is_not_stale_literal() -> None:
    """Version should not regress to the old hardcoded value."""
    assert kiln.__version__ != "0.1.0"
    assert kiln.__version__ not in {"", "unknown"}
