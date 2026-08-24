"""Every surface that names Kiln's version gives the same real answer.

Two ways to get this wrong, both of which shipped.

**The wrong name.**  The distribution is ``kiln3d``; the import package
is ``kiln``.  ``skill_manifest.get_version`` asked
``importlib.metadata.version("kiln")``, which raises
``PackageNotFoundError`` on every ordinary install, and a bare
``except`` turned that into ``"unknown"``.  That string was the
``version`` field of the skill manifest — the thing an agent reads to
find out what it is talking to — so Kiln answered "unknown" to every
agent that asked, on every install, while a perfectly good version sat
in ``kiln.__version__``.

**The stale answer.**  Package metadata is written at INSTALL time.  On
an editable install it keeps reporting whatever was current when ``pip
install -e`` last ran, however far the source tree has moved since.
Measured on 2026-08-14: ``pip show kiln-pro`` reported 1.1.3 for a
source tree at 1.3.2.  Nothing errors, nothing warns; the check simply
answers wrongly with total confidence, which is the failure mode that
costs the most to discover.

``kiln.__version__`` already handles both — source tree first, then
each distribution name in turn.  So the rule this file pins is not "get
the version right" in several places; it is that there is ONE resolver
and the other doors call it.  A new surface that reaches for
``importlib.metadata`` on its own reintroduces whichever of the two
defects it happens to hit.
"""

from __future__ import annotations

import kiln
from kiln.skill_manifest import generate_manifest, get_version


def test_the_version_is_a_version_and_not_a_shrug():
    """``"unknown"`` is what this reported for its whole shipped life."""
    resolved = get_version()

    assert resolved != "unknown", (
        "Kiln cannot name its own version. The manifest reports this "
        "string to every agent that asks what it is talking to."
    )
    assert resolved and resolved[0].isdigit(), (
        f"version {resolved!r} does not begin with a digit — this is the "
        "field agents and support read as a release number."
    )


def test_every_door_gives_the_same_answer():
    """One resolver, so the doors cannot disagree about what is running."""
    assert get_version() == kiln.__version__
    assert generate_manifest().version == kiln.__version__


def test_the_manifest_carries_it_all_the_way_out():
    """The value has to survive into the payload agents actually receive.

    ``generate_manifest`` is what ``get_skill_manifest()`` answers with,
    so a version that is correct in the helper and lost on the way into
    the dict is the same defect with a longer path.
    """
    payload = generate_manifest().to_dict()

    assert payload["version"] == kiln.__version__
    assert payload["version"] != "unknown"


def test_the_resolver_does_not_depend_on_installed_metadata():
    """Source tree first — an editable install must not answer stale.

    Written against the mechanism rather than the symptom: with metadata
    lookups made to fail outright, a correct resolver still answers from
    the source tree.  If this starts failing, some door has gone back to
    trusting what ``pip`` recorded at install time, and it will be right
    exactly until the next version bump that nobody reinstalls.
    """
    import kiln as kiln_module

    def _explode(_name):
        raise AssertionError(
            "resolved the version through installed metadata; an editable "
            "install answers stale that way"
        )

    original = kiln_module.version
    try:
        kiln_module.version = _explode
        resolved = kiln_module._resolve_version()
    finally:
        kiln_module.version = original

    assert resolved != "unknown"
    assert resolved == kiln.__version__
