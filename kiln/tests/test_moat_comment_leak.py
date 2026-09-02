"""CI backstop for the moat-comment leak gate.

Public Kiln must not, in comments / docstrings, raw source, tests, docs,
scripts OR shipped data JSON:
- narrate HOW the kiln-pro overlay reasons (strategy),
- name the overlay's research PROVENANCE (vendor datasheets),
- brand the private tier "the moat",
- name a private overlay file PATH (``kiln_pro/data/…``, ``*.md``),
- inline a paid-tier answer key (``_PRO_* = {…}`` in a public test),
- grow the set of dotted ``kiln_pro.<module>`` paths the tests reference
  without a reviewed diff to the frozen inventory,
- carry an internal persona / process name ("judges' verdict", "war-room"),
- ship ``kiln/tests`` in the PyPI sdist.

A green run is the proof we didn't ship the method.  The detection tests
below also prove the gate CAN fail (a gate that can't fail is theatre) and
that it stays precise (no false positives on brand examples / public
standards / plain contract notes / "judges" as a verb / MCP Apps panels).
Each rule has an inline positive AND negative fixture.  See
``scripts/audit_moat_comment_leak.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_gate():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "audit_moat_comment_leak", root / "scripts" / "audit_moat_comment_leak.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()


def _rules(rel: str, text: str, **kw) -> list[str]:
    """Rule names the gate raises for one in-memory file at ``rel``."""
    leaks, _ = _GATE.scan_file(rel, text.encode("utf-8"), **kw)
    return [rule for _path, _line, rule, _text in leaks]


# ── The live gate stays green (the in-suite backstop) ───────────────────────

# TODO(fix/public-tests-close-doors): today's tree carries the findings this
# gate was extended to catch — paid-tier ``_PRO_*`` tables, ``kiln_pro/data``
# paths, self-labels, and one persona docstring in kiln/tests, plus three
# self-label comments in src/ and two scripts.  The sibling branch removes
# them.  ``strict=True`` means this test FAILS the moment the tree is clean,
# which forces the marker (and the matching ``continue-on-error`` in
# .github/workflows/ci.yml) to be removed in the same commit — the interim
# can't silently outlive the cleanup.
@pytest.mark.xfail(
    strict=True,
    reason="pending fix/public-tests-close-doors: the tree still carries the "
    "leaks this gate now catches; remove this marker with the cleanup",
)
def test_no_moat_comment_leak() -> None:
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "audit_moat_comment_leak.py"
    assert script.exists(), f"gate script missing: {script}"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, (
        "Moat-comment leak — public Kiln narrates, inlines, or names the "
        "private tier:\n\n" + result.stdout
    )


def test_gate_runs_and_reports_inventory() -> None:
    """Whatever the verdict, the gate must run to completion and emit the
    frozen-path inventory line — a crash is not a pass."""
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "audit_moat_comment_leak.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert result.returncode in (0, 2), result.stdout + result.stderr
    assert "kiln_pro dotted paths in public tests:" in result.stdout


# ── The gate CATCHES each leak class (proves it can fail) ───────────────────

def test_catches_datasheet_provenance() -> None:
    assert _GATE._is_leak("# the kiln-pro overlay values are datasheet-grounded")
    assert _GATE._is_leak("# Overlay supplies per-material curves curated against datasheets")


def test_catches_moat_self_label() -> None:
    assert _GATE._is_leak("# the kiln-pro engineering-moat overlay")
    assert _GATE._is_leak("# Curated content is the engineering moat in kiln-pro")


def test_catches_private_overlay_path() -> None:
    assert _GATE._is_leak("# tuned by kiln_pro/data/foo_pro_overlay.json")


def test_json_catches_moat_label() -> None:
    assert _GATE._is_json_leak('"methodology": "these values are the engineering moat"')


def test_json_catches_datasheet_provenance() -> None:
    assert _GATE._is_json_leak('"note": "datasheet-grounded per-material values"')


# ── Rule 1: scope — the public TEST SUITE is scanned like src ───────────────

def test_scope_covers_public_tests() -> None:
    # A strategy-narrating docstring in a test trips exactly as it would in src.
    leak = '"""The kiln-pro overlay routes lattice parts through the strut classifier."""\n'
    assert _rules("kiln/tests/test_anything.py", leak) == ["overlay narration"]
    assert _rules("kiln/src/kiln/anything.py", leak) == ["overlay narration"]
    # A plain contract note in a test does not.
    assert _rules("kiln/tests/test_anything.py", '"""Exposed for the kiln-pro overlay; public floor only."""\n') == []
    # Test JSON fixtures are shipped-data surfaces too.
    assert _rules("kiln/tests/fixtures/thing.json", '{"note": "datasheet-grounded"}\n') == ["shipped data"]
    assert _rules("kiln/tests/fixtures/thing.json", '{"note": "a drink coaster"}\n') == []
    # The gate's own test may spell out its patterns.
    assert _rules("kiln/tests/test_moat_comment_leak.py", "# moat moat kiln_pro/data/x.json judges' verdict\n") == []


# ── Rule 2: inlined Pro values ──────────────────────────────────────────────

def test_catches_inlined_pro_literal() -> None:
    for line in (
        "_PRO_OVERLAY = {",
        "_PRO_EXPECTED: dict[str, str] = {",
        "_PRO_TIER_EXPECTED: dict[str, str] = {",
        "_PRO_SCORECARD_OVERLAY = {",
        "_PRO_ORIENTATION_OVERLAY = {",
        "_PRO_CASES = [",
        "_PRO_WEIGHTS = dict(",
        "_PRO_FLAGS = frozenset({",
    ):
        assert _GATE._is_inlined_pro_literal(line), line
    for line in (
        "_PRO_TOOL_TIERS: dict[str, str] = {}",  # empty slot the plugin fills at runtime
        "_PRO_TOOL_QUOTA: dict[str, dict] = {}",
        '_PRO_UPGRADE_URL = "https://kiln3d.com/pricing"',  # a string, not a table
        "    _PRO_LOCAL = {",  # not module level
        "_PUBLIC_EXPECTED = {",  # public-tier table
        "_PRO_ENABLED = True",
    ):
        assert not _GATE._is_inlined_pro_literal(line), line
    # Tests-only: src legitimately declares _PRO_* slots and sentinel tables.
    table = "_PRO_EXPECTED = {\n    'benchy': 'low',\n}\n"
    assert _rules("kiln/tests/test_calibration.py", table) == ["inlined Pro values"]
    assert _rules("kiln/src/kiln/server.py", table) == []


def test_catches_pro_data_mirror_note() -> None:
    for block in (
        "# Kept here so the tier tests below compare against the real shipped values (overlay).",
        "# mirrors the shipped kiln-pro thresholds",
        "# same values as the kiln_pro overlay",
        '"""Pro-tier table; identical thresholds as the private overlay."""',
        "# copied verbatim from the kiln-pro overlay",
    ):
        assert _GATE._is_pro_mirror_note(block), block
    for block in (
        "# mirrors :func:`_screw_hole_detail_for` on the kiln-pro side",  # structure, not values
        "# agents can surface the upgrade pitch verbatim (Kiln Pro link)",
        "# ``kiln pair`` is the web-initiated mirror of ``kiln signin`` (kiln-pro tier)",
        "# behavior is then identical to today when the overlay is absent",
        "# compare against the real shipped values",  # no private-tier mention at all
    ):
        assert not _GATE._is_pro_mirror_note(block), block
    note = "# Pro-tier calibrated values; mirrors the shipped kiln-pro thresholds\nX = 1\n"
    assert _rules("kiln/tests/test_reasoning.py", note) == ["Pro-data mirror note"]


# ── Rule 3: private paths — blocked outright, dotted paths frozen ───────────

def test_catches_private_kiln_pro_path_anywhere() -> None:
    for text in (
        "see kiln_pro/data/BRIDGING_KNOWLEDGE.md",
        "kiln_pro/data/printability_pro_overlay.json",
        "kiln_pro/docs/PLAYBOOK.md on the private side",
        "the warping_factor schedule in printability_pro_overlay.json",
        "kiln_pro/migrations/001.sql",
    ):
        assert _GATE._names_private_pro_path(text), text
    for text in (
        "kiln_pro/data_overlays.py",  # module ref: the public contract
        "kiln_pro/printability_overlay/data_loader.py",
        "kiln_pro/__init__.py",
        "from kiln_pro.bridge import pro_features",
    ):
        assert not _GATE._names_private_pro_path(text), text
    # Applies to every public surface, not only comments in src.
    for rel in ("kiln/tests/test_bridging.py", "docs/guide.md", "README.md", "scripts/tool.py"):
        assert _rules(rel, "See kiln_pro/data/BRIDGING_KNOWLEDGE.md\n") == ["private kiln_pro path"], rel


def test_freezes_dotted_kiln_pro_paths() -> None:
    text = (
        "import kiln_pro.bridge\n"
        "sys.modules['kiln_pro.brand_new.thing'] = stub\n"
        "# kiln_pro/data/x.json is a path, not a dotted module\n"
    )
    found = _GATE._dotted_pro_paths(text)
    assert found == {"kiln_pro.bridge", "kiln_pro.brand_new.thing"}
    assert _GATE._new_pro_paths(found, {"kiln_pro.bridge"}) == {"kiln_pro.brand_new.thing"}
    assert _GATE._new_pro_paths(found, found) == set()

    content = [("kiln/tests/test_fallback.py", text.encode())]
    leaks, stats = _GATE.run(content, check_manifest=False, frozen={"kiln_pro.bridge"})
    assert [(r, t) for _p, _l, r, t in leaks if r == "new kiln_pro path"] == [
        ("new kiln_pro path", "kiln_pro.brand_new.thing")
    ]
    new = [l for l in leaks if l[2] == "new kiln_pro path"][0]
    assert new[0] == "kiln/tests/test_fallback.py" and new[1] == 2
    assert stats["kiln_pro_paths_new"] == 1

    leaks, stats = _GATE.run(content, check_manifest=False, frozen=found)
    assert [l for l in leaks if l[2] == "new kiln_pro path"] == []
    assert stats["kiln_pro_paths_new"] == 0

    # A dotted path in src is the public contract, not test exposure.
    leaks, _ = _GATE.run(
        [("kiln/src/kiln/x.py", b"import kiln_pro.brand_new\n")], check_manifest=False, frozen=set()
    )
    assert leaks == []

    # The checked-in freeze file exists and is non-empty.
    frozen = _GATE._load_frozen_paths()
    assert frozen and all(p.startswith("kiln_pro.") for p in frozen)


# ── Rule 4: the self-label, anywhere in public text ─────────────────────────

def test_catches_moat_label_in_every_public_surface() -> None:
    assert _GATE._is_moat_label("The MOAT (real engineering math the upgrade nudge points to)")
    assert _GATE._is_moat_label('"note": "engineering moat"')
    assert _GATE._is_moat_label("# not moat, just a floor")  # negation is still the label
    # The gate's own file name is not a hit.
    assert not _GATE._is_moat_label("run scripts/audit_moat_comment_leak.py first")
    assert not _GATE._is_moat_label("!scripts/audit_moat_comment_leak.py")
    assert not _GATE._is_moat_label("kiln/tests/test_moat_comment_leak.py backstops it")
    assert not _GATE._is_moat_label("a drink coaster")
    for rel in (
        "kiln/src/kiln/anything.py",
        "kiln/src/kiln/data/materials.json",
        "kiln/tests/test_anything.py",
        "kiln/tests/conftest.py",
        "docs/guide.md",
        "README.md",
        "scripts/some_tool.py",
    ):
        assert "self-label" in _rules(rel, "the private moat\n"), rel
        assert _rules(rel, "the private tier\n") == [], rel
    # Only the gate and its test may say the word.
    assert _rules("scripts/audit_moat_comment_leak.py", "moat\n") == []
    assert _rules("kiln/tests/test_moat_comment_leak.py", "moat\n") == []
    # Surfaces outside the public tree are not this gate's job.
    assert _rules(".github/workflows/ci.yml", "moat\n") == []


# ── Rule 5: internal persona / process names, phrases not bare words ────────

def test_catches_persona_phrases_not_bare_words() -> None:
    for line in (
        "Judges' verdict on placement: wire into the 4 canonical entry points",
        "the judges asked for a smaller diff",
        "judges panel pending",
        "war-room notes from the outage",
        "ship gate passed",
        "panel verdict: ship it",
        "Judges: keep the seam",
    ):
        assert _GATE._is_persona_phrase(line), line
    for line in (
        "a new print judges its heaters afresh",  # verb
        "the composer re-centres a group it judges off ITS plate",
        "the MCP Apps panel renders the mesh inline",  # bare "panel"
        "the band-height warning judges the divide",
        "a judge of character",
        "shipping the gate",
    ):
        assert not _GATE._is_persona_phrase(line), line
    assert _rules("kiln/tests/test_safety_gap_warning.py", '"""Judges\' verdict on placement."""\n') == ["internal persona"]
    assert _rules("kiln/tests/test_safety_gap_warning.py", '"""Wire into the four canonical entry points."""\n') == []
    # The MCP Apps stage panel note in local_stage.py is consciously allowlisted…
    panel = "``renders`` is the panel verdict, not the geometry verdict\n"
    assert _rules("kiln/src/kiln/local_stage.py", panel) == []
    # …by file, not by phrase.
    assert _rules("kiln/src/kiln/other.py", panel) == ["internal persona"]
    # The public-language gate spells out the phrases it catches; exempt from this rule only.
    assert _rules("scripts/check_public_language.py", 'r"judges panel|war-room"\n') == []
    assert _rules("scripts/check_public_language.py", "# the moat\n") == ["self-label"]


# ── Rule 6: the sdist must not ship the test suite ──────────────────────────

def test_manifest_must_prune_tests(tmp_path, monkeypatch) -> None:
    assert _GATE._manifest_prunes_tests("include README.md\nprune tests\n")
    assert _GATE._manifest_prunes_tests("recursive-exclude tests *\n")
    assert not _GATE._manifest_prunes_tests("include README.md\n")
    assert not _GATE._manifest_prunes_tests("# prune tests\n")  # commented out
    assert not _GATE._manifest_prunes_tests("prune tests/fixtures\n")  # only a subtree
    assert not _GATE._manifest_prunes_tests("")
    assert not _GATE._manifest_prunes_tests(None)  # no MANIFEST.in at all

    manifest = tmp_path / "MANIFEST.in"
    monkeypatch.setattr(_GATE, "_MANIFEST", manifest)
    # Missing file → finding.
    leaks, _ = _GATE.run([], check_manifest=True, frozen=set())
    assert [r for _p, _l, r, _t in leaks] == ["sdist prune"]
    # Present without the prune → finding.
    manifest.write_text("include README.md\n")
    leaks, _ = _GATE.run([], check_manifest=True, frozen=set())
    assert [r for _p, _l, r, _t in leaks] == ["sdist prune"]
    # Pruned → clean.
    manifest.write_text("include README.md\nprune tests\n")
    leaks, _ = _GATE.run([], check_manifest=True, frozen=set())
    assert leaks == []


# ── The gate does NOT flag legitimate notes (precision, no false positives) ──

def test_allows_plain_overlay_contract() -> None:
    assert not _GATE._is_leak("# exposed for the kiln-pro overlay")
    assert not _GATE._is_leak("# the Pro overlay supplies curated per-material values")


def test_allows_material_brand_example() -> None:
    # A brand used as a MATERIAL example is not provenance (the false positive
    # that was removed) — only "datasheet-grounded" phrasing is.
    assert not _GATE._is_leak("# the kiln-pro overlay tunes Polymaker PETG on brass")


def test_allows_public_standard_reference() -> None:
    # Public STANDARDS (ASTM / ISO) are textbook, not datasheet provenance.
    assert not _GATE._is_leak("# the kiln-pro overlay values follow ASTM D638")


def test_json_allows_clean_line() -> None:
    assert not _GATE._is_json_leak('"description": "A drink coaster"')


def test_allows_fake_secrets_and_local_hosts() -> None:
    # Placeholder secrets and mock hostnames are a different gate's business;
    # nothing here trips on them.
    fixture = (
        'STRIPE_KEY = "sk_live_abc123"\n'
        'API_KEY = "sk-ant-placeholder-not-real"\n'
        'HOST = "octopi.local"\n'
        'sys.modules["kiln_pro"] = types.ModuleType("kiln_pro")\n'
    )
    assert _rules("kiln/tests/test_redaction.py", fixture) == []
    assert _rules("kiln/tests/test_stripe_checkout.py", fixture) == []
