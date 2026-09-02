#!/usr/bin/env python3
"""Moat-comment leak gate — public Kiln must not narrate, inline, or name
the private kiln-pro tier.

Public Kiln is open source.  A comment or docstring may say a field is
"exposed for the kiln-pro overlay" (a contract note) but must NOT
describe HOW the overlay reasons — its classifiers, the signals it
combines, the verdict logic it routes through.  That hands a competitor
the methodology of the private tier for free, even though the curated
*values* stay private.  The same is true of the public TEST SUITE: it is
part of the open-source tree and (until pruned) part of the sdist on PyPI,
so a paid-tier expectation table or a private module path inlined into a
test is exactly as public as one inlined into ``src/``.

What the gate scans
-------------------
* ``kiln/src/kiln/**/*.py`` and ``kiln/tests/**/*.py`` — comments and
  docstrings (via ``tokenize``) plus every raw line.
* ``kiln/src/kiln/data/**/*.json`` and ``kiln/tests/**/*.json`` — every
  string line (shipped data has no comments to hide in).
* ``docs/``, ``README.md``, ``scripts/`` — every line of every text file
  git would commit (tracked or untracked-but-not-ignored), for the
  plain-text rules below.  Third-party OpenSCAD libraries under
  ``kiln/src/kiln/data/scad_libraries/`` are not ours to police and are
  skipped, as are the sibling leak gates, which have to spell out the
  literals they catch (``_SELF``).
* ``kiln/MANIFEST.in`` — the sdist recipe.

The rules, in plain language
----------------------------
1. **Overlay narration** (comments / docstrings, src AND tests): an
   overlay / kiln-pro mention in the same block as strategy vocabulary
   ("strut classifier", "weights tip wear", …) or datasheet provenance.
2. **Inlined Pro values** (tests only): a module-level ``_PRO_*`` name
   bound to a non-empty dict / list / set literal is the paid tier's
   answer key copied into public.  So is any comment or docstring that
   says a value "mirrors" / "matches the shipped" / "is the real
   shipped" kiln-pro data.
3. **Private paths** (all scanned text): ``kiln_pro/data/...`` or a
   ``kiln_pro/*.md`` / ``*.json`` / ``*.sql`` path is a leak wherever it
   appears.  DOTTED ``kiln_pro.<module>`` references are NOT blocked
   outright — the free-tier fallback tests legitimately stub ``kiln_pro``
   in ``sys.modules`` — but their inventory is FROZEN: every distinct
   dotted path found in ``kiln/tests`` must already be listed in
   ``scripts/public_tests_kiln_pro_paths.txt``.  A new path fails the
   gate until it is added there on purpose (``--freeze-kiln-pro-paths``
   rewrites the file from the current tree; commit the diff).
4. **Self-label** (all scanned text): the word "moat", any case,
   anywhere in public text.  There is no functional reason for the word
   in a public tree; every use points a reader at the jewels.  The only
   exceptions are the leak gates themselves and their fixture tests, which
   must carry the literal they catch (``_SELF``); a gate's own file NAME,
   quoted in prose, is never a hit.
5. **sdist prune**: ``kiln/MANIFEST.in`` must exist and ``prune tests``
   (or ``recursive-exclude tests *``) so the test suite never ships in
   the PyPI sdist.  Checked in full-tree mode, and in ``--staged`` mode
   only when the manifest or ``pyproject.toml`` is part of the commit.

Internal persona / process phrases are ``scripts/check_public_language.py``'s
rule — it scans the whole tracked tree and commit messages — not this
gate's.

The right fix for a flagged block is almost always to TRIM the private
reasoning / values / path (keep the math + the "exposed for the kiln-pro
overlay" contract), not to allowlist it.

    python3 scripts/audit_moat_comment_leak.py             # full tree; 0 clean, 2 leak
    python3 scripts/audit_moat_comment_leak.py --staged    # only the git index (commit hook)
    python3 scripts/audit_moat_comment_leak.py --files a b # only these repo-relative paths
    python3 scripts/audit_moat_comment_leak.py --sweep     # advisory broad heuristic
    python3 scripts/audit_moat_comment_leak.py --freeze-kiln-pro-paths

Runs from CI (the same job as the test matrix), from the pre-push hook,
and — via ``scripts/check_public_language.py --staged`` — from the
commit-time hook, so a leak is refused at the commit, not at the PR.
``--staged`` steps aside during a merge or rebase, where the index carries
files this commit did not author; the full-tree CI step covers those.
Stdlib only.

Pairs with the wheel-exclusion + overlay-payload gates that protect the
curated values; this one protects the *method*, the *answer key*, and
the *map* to the private tree.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _ROOT / "kiln" / "MANIFEST.in"
_FROZEN_PATHS_FILE = _ROOT / "scripts" / "public_tests_kiln_pro_paths.txt"

# Repo-relative prefixes the gate walks.  ``kiln/src/kiln`` and
# ``kiln/tests`` get the code rules; everything here gets the text rules.
_SURFACES = ("kiln/src/kiln", "kiln/tests", "docs", "README.md", "scripts")
# Third-party text inside a surface (vendored OpenSCAD libraries).
_SKIP_PREFIXES = ("kiln/src/kiln/data/scad_libraries/",)
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".venv", "dist", ".astro", "build"})
_BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".pdf", ".zip",
    ".tar", ".gz", ".tgz", ".xz", ".bz2", ".7z", ".dmg", ".so", ".dylib",
    ".a", ".o", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3",
    ".mp4", ".mov", ".wav", ".webm", ".pyc", ".stl", ".3mf", ".step", ".stp",
    ".gcode", ".svg",
})

# A leak gate has to spell out the literal it catches, so the gates and their
# fixture tests are skipped by every content rule here.  This is an exemption
# for PATTERN OWNERS, not a general allowlist: each of these files exists to
# carry the pattern, is reviewed as a gate, and is itself scanned by a sibling
# gate.  Ordinary source earns no such pass — trim the wording instead.  Add a
# file here only when its literal IS the rule (a regex, a token tuple, a
# fixture string asserting the rule fires).
_SELF = frozenset({
    "scripts/audit_moat_comment_leak.py",
    "kiln/tests/test_moat_comment_leak.py",
    # The served-surface gate carries `\bmoat\b` and a kiln_pro path regex.
    "scripts/audit_served_surface_leak.py",
})
# A gate's own file NAME is not a self-label: CI, .gitignore, and sibling gates
# have to be able to reference it in ordinary prose.
_SELF_NAME_TOKENS = (
    "audit_moat_comment_leak",
    "test_moat_comment_leak",
)

# A comment/docstring that names the private overlay surface.
_OVERLAY_MENTIONS = ("overlay", "kiln-pro", "kiln_pro")

# Language that describes HOW the overlay reasons — as opposed to a plain
# "exposed for the overlay" contract.  Co-occurrence with an overlay
# mention in the same comment/docstring block is the leak signal.  Keep
# this list tuned to *strategy narration*, not incidental vocabulary.
#
# NOTE: tuned to *strategy vocabulary*, not generic words.  Broad
# tokens like "classifies" were removed — the firmware and public-tier
# classifiers legitimately "classify", so that word is not a leak signal.
# Extend this list when a NEW private subsystem's narration vocabulary
# appears (the gate flags for review; trim the block or, rarely, allowlist).
_STRATEGY_TOKENS = (
    # lattice / strut topology routing (printability overlay)
    "strut classifier",
    "strut-specific",
    "strut semantics",
    "applying strut",
    "route lattice",
    "routes lattice",
    "lattice / scaffold",
    "lattice/scaffold",
    "lattice family",
    "lattice families",
    "lattice topology",
    "confirm lattice",
    "catches them",
    "fragment into",
    "secondary signal",
    "downgrade to advisory",
    "downgrades to advisory",
    "hole-too-small",
    # nozzle-wear attribution (device-intelligence overlay)
    "weights tip wear",
    "weights bore wear",
    "wear hypothesis",
    "bore is widening",
    "bore widening faster",
    "routes its per-component",
    "correlate flow",
    "gram-count wear",
    "wear-tracking subsystem",
)

# ── Provenance leak: naming the SOURCES the curated overlay is grounded in ──
# Public Kiln may say a value is "tuned by the kiln-pro overlay" (contract),
# but naming WHERE the private overlay's numbers came from — vendor datasheets,
# specific manufacturers, TDS docs — hands a competitor the research method.
# (Public STANDARDS — ASTM / ISO / IEC — are fine; those are textbook, not
# our research.  These tokens target vendor/datasheet provenance, not
# standards, so a "grounded in ASTM D638" note never trips.)
# DATASHEET phrasing is the precise signal: it only appears when narrating
# where the PRIVATE overlay's numbers came from.  A bare vendor name is NOT
# enough — brand names appear legitimately as material examples ("Polymaker
# PETG on brass"); only "datasheet-grounded against X" is provenance.
_PROVENANCE_TOKENS = (
    "datasheet-grounded",
    "datasheet grounded",
    "datasheet-derived",
    "datasheet derived",
    "vendor datasheet",
    "vendor datasheets",
    "tds-derived",
    "against datasheets",
    "curated against datasheet",
)

# ── Self-labeling the private tier in PUBLIC text points a competitor at
# the jewels.  A public comment describes the CONTRACT ("Pro overlay supplies
# curated values"); it must never BRAND that overlay with the word below.
# There is no other reason to write it in public text — so one token catches
# every variant ("engineering …", "… overlay", "… split", …).
_MOAT_LABEL = re.compile("moat", re.IGNORECASE)

# ── A private kiln_pro data / doc file PATH named in public text over-shares ──
# The loader references the overlay KIND ("printability_judgment") in CODE —
# the necessary public contract; the private path, the ``kiln_pro/data/``
# tree, a ``*_pro_overlay.json`` filename, or a private ``.md`` is not.
# (Module refs like ``kiln_pro/data_overlays.py`` end in .py and don't
# match — the ``/`` after ``data`` is required.)
_PRIVATE_OVERLAY_REF = re.compile(
    r"kiln_pro/data/|kiln_pro/[\w/.-]*\.(?:json|sql|md)\b|\b\w*_pro_overlay\.json\b",
    re.IGNORECASE,
)

# ── Dotted ``kiln_pro.<module>`` references: inventoried, not blocked ──
_DOTTED_PRO_PATH = re.compile(r"\bkiln_pro(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

# ── Inlined Pro values in a public test ──
# A module-level ``_PRO_*`` name bound to a NON-EMPTY dict / list / set
# literal (or constructor) is the paid tier's expectation table.  ``src``
# legitimately declares empty ``_PRO_TOOL_TIERS: dict = {}`` slots that the
# private plugin fills at runtime, so the rule is tests-only and an empty
# literal never trips.
_PRO_LITERAL_ASSIGN = re.compile(
    r"^_PRO_[A-Z0-9_]*\s*(?::[^=]*)?=\s*"
    r"(?:\{(?!\s*\})|\[(?!\s*\])|(?:dict|list|set|frozenset)\((?!\s*\)))"
)
# A comment / docstring that admits the value IS the private data.  Only
# meaningful in a block that also mentions the overlay / kiln-pro / Pro tier.
_PRO_MENTION = re.compile(
    r"overlay|kiln[-_ ]pro|\bpro[- ]tier|\bpaid[- ]tier|\bpro data|\bpro values",
    re.IGNORECASE,
)
# Value-specific admissions only.  "mirrors :func:`x` on the kiln-pro side"
# (structure) and "surface the pitch verbatim" (prose) are not leaks; the
# nouns after the verb are what make it one.
_PRO_MIRROR_NOTE = re.compile(
    r"\breal shipped\b|\bshipped (?:pro |kiln[-_ ]pro |overlay )?(?:values?|numbers?|thresholds?|weights?|table)\b|"
    r"\bis the real (?:shipped |pro |kiln[-_ ]pro |overlay )?(?:values?|numbers?|data|table|overlay)\b|"
    r"\bmirrors? (?:the )?(?:real|shipped|live|actual|curated|private|pro|kiln[-_ ]pro)"
    r"[\w' -]{0,30}?(?:values?|numbers?|thresholds?|weights?|expectations?|table|verdicts?)\b|"
    r"\b(?:same|identical) (?:values?|numbers?|thresholds?|weights?) as (?:the )?"
    r"(?:real|shipped|live|pro|kiln[-_ ]pro|private|overlay)\b|"
    r"\b(?:copied|lifted|taken) (?:verbatim )?from (?:the )?(?:kiln[-_ ]pro|pro overlay|private tier|overlay)\b|"
    r"\b(?:match(?:es)?|compared? against) the (?:real|shipped|live|actual|curated|private) "
    r"(?:pro |kiln[-_ ]pro )?(?:values?|numbers?|data|table|thresholds?|weights?)\b",
    re.IGNORECASE,
)

# ── sdist prune ──
_MANIFEST_PRUNES_TESTS = re.compile(
    r"^\s*(?:prune\s+tests\s*$|recursive-exclude\s+tests\s+\*\s*$)", re.MULTILINE
)

# ── Research-source bibliography in PUBLIC data: the sourcing playbook ──
# A public data file's _meta/sources block naming the SPECIFIC sources a curated
# knowledge base is grounded in is the research method — even when each source
# is individually public, the COMPILATION is the work.  These are research /
# material-database sources, never product data, so they're safe to flag in
# shipped JSON.  (The product-brand fields — "vendor": "Polymaker", "amazon":
# "Prusament+…" — are a different, legitimate thing: product identification.
# That's why product brands aren't in this list.)
_JSON_RESEARCH_SOURCES = (
    "cnc kitchen", "makersmuse", "hackaday", "/r/3dprinting",
    "natureworks", "basf", "stratasys", "solvay", "passive-components",
    "matweb", "ces edupack",
)
# A YEAR-STAMPED datasheet citation ("filament datasheets (2024-2025)",
# "technical data sheets (2024)") is the bibliography FORM — a specific dated
# source, not the generic provenance CLASS ("manufacturer technical datasheets",
# which carries no year and is the allowed trust signal).
_DATASHEET_CITATION = re.compile(r"data ?sheets?\s*\(20\d\d", re.IGNORECASE)

# Cheap whole-file prefilter for the line rules: a file with none of these
# substrings cannot trip any of them, so its lines are never iterated.
_LINE_PREFILTER = re.compile(r"moat|kiln_pro/|_pro_overlay\.json|^_PRO_", re.IGNORECASE | re.MULTILINE)

# Consciously reviewed blocks / lines that trip a detector but are CONTRACT /
# architecture notes (merge mechanism, public-floor fallback, upgrade nudge,
# bundle/resume wiring) — NOT narration of the private method.  Keyed by
# (filename, a stable marker phrase from the block).  Only add an entry after
# confirming it describes the boundary, not the method.
_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("design_intelligence.py", "Recommend a material using ONLY safety-floor fields"),
    ("design_intelligence.py", "Always attach the upgrade nudge when the load detector tripped"),
    ("design_versions.py", "Design version control for parametric designs"),
    ("original_design.py", "Run a harsh audit of an original design"),
    ("slicer_tools.py", "Attach Pro+ enrichment to an EXCEEDS_BED"),
    ("print_recovery.py", "Stamp gcode_path so kiln-pro's resume engine"),
)


def _allowlisted(path: Path | str, text: str) -> bool:
    name = Path(path).name
    return any(fn in name and marker in text for fn, marker in _ALLOWLIST)


def _blocks_from_bytes(data: bytes):
    """Yield ``(start_line, text)`` for each comment block and docstring."""
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(data).readline))
    except (tokenize.TokenError, SyntaxError, ValueError):
        return

    block: list[str] = []
    block_line = 0
    prev_line = -2
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            # Break the block when comment lines aren't contiguous.
            if tok.start[0] != prev_line + 1 and block:
                yield block_line, "\n".join(block)
                block = []
            if not block:
                block_line = tok.start[0]
            block.append(tok.string)
            prev_line = tok.start[0]
        elif tok.type == tokenize.STRING:
            # Docstrings / prose strings carry the same risk as comments.
            if len(tok.string) > 40:
                yield tok.start[0], tok.string
    if block:
        yield block_line, "\n".join(block)


# Beyond the curated vocabulary, catch the GENERAL narration signature:
# the overlay as the *active subject* of a reasoning verb ("the overlay
# routes / reads / flags / detects / classifies / weights X"), or a
# purpose clause that exists to serve the overlay ("exposed so the overlay
# can …").  A plain contract ("exposed for the kiln-pro overlay",
# "consumed by the overlay") has the overlay as a passive recipient — no
# trailing reasoning verb — and does NOT trip these.  This is what makes
# the gate catch novel narration, not just the phrases we already trimmed.
_OVERLAY_ACTION = re.compile(
    r"(?:overlay|kiln-pro|kiln_pro)\b[^.\n]{0,60}?\b(?:"
    r"uses?|reads?|routes?|flags?|detects?|classif\w+|confirms?|applies|"
    r"apply|weights?|correlat\w+|cross-references?|distinguish\w*|catches|"
    r"enrich\w+|infers?|derives?|downgrade\w*"
    r")\b",
    re.IGNORECASE,
)
_OVERLAY_PURPOSE = re.compile(
    r"\b(?:so (?:the |that )?)[^.\n]{0,50}?(?:overlay|kiln-pro|kiln_pro)\b"
    r"[^.\n]{0,40}?\b(?:can|to|will)\b",
    re.IGNORECASE,
)


# ── Rule 1: overlay narration in a comment / docstring block ────────────────
def _leak_reason(text: str, *, broad: bool = False) -> str | None:
    """Why a comment / docstring block leaks, or ``None`` when it doesn't."""
    low = text.lower()
    if not any(m in low for m in _OVERLAY_MENTIONS):
        return None
    if any(tok in low for tok in _STRATEGY_TOKENS):
        return "strategy"
    # Provenance: naming the curated overlay's research SOURCES via datasheet
    # phrasing — the research method is the private tier.
    if any(tok in low for tok in _PROVENANCE_TOKENS):
        return "provenance"
    # Self-labeling the overlay in public source.
    if _MOAT_LABEL.search(low):
        return "self-label"
    # A private kiln_pro data path / `*_pro_overlay.json` filename in a comment.
    if _PRIVATE_OVERLAY_REF.search(text):
        return "private path"
    # The broad subject-verb heuristic ("the overlay routes/reads/flags X")
    # catches NOVEL narration, but it also trips legitimate seam / contract /
    # marketed-feature notes — so it is advisory-only (``--sweep``), never
    # part of the hard CI gate.  Run it periodically and review by hand.
    if broad and (_OVERLAY_ACTION.search(text) or _OVERLAY_PURPOSE.search(text)):
        return "broad"
    return None


def _is_leak(text: str, *, broad: bool = False) -> bool:
    return _leak_reason(text, broad=broad) is not None


def _json_leak_reason(text: str) -> str | None:
    """Public data JSONs ship verbatim to every user but have no comments — so
    a string LINE is judged on its own.  A bare self-label in shipped data is
    always the leak (there is no functional reason for the word in a data
    file), so no overlay-context gate is needed here."""
    low = text.lower()
    if _MOAT_LABEL.search(low):
        return "self-label"
    if any(tok in low for tok in _PROVENANCE_TOKENS):
        return "provenance"
    if _PRIVATE_OVERLAY_REF.search(text):
        return "private path"
    # Research-source bibliography: named sources or a dated datasheet citation.
    if any(name in low for name in _JSON_RESEARCH_SOURCES):
        return "research source"
    if _DATASHEET_CITATION.search(text):
        return "datasheet citation"
    return None


def _is_json_leak(text: str) -> bool:
    return _json_leak_reason(text) is not None


# ── Rule 2: inlined Pro values in a public test ─────────────────────────────
def _is_inlined_pro_literal(line: str) -> bool:
    """A module-level ``_PRO_*`` name bound to a non-empty container literal."""
    return bool(_PRO_LITERAL_ASSIGN.match(line))


def _is_pro_mirror_note(block: str) -> bool:
    """A comment / docstring admitting a value IS the private tier's data."""
    return bool(_PRO_MENTION.search(block) and _PRO_MIRROR_NOTE.search(block))


# ── Rule 3: private paths ───────────────────────────────────────────────────
def _names_private_pro_path(text: str) -> bool:
    return bool(_PRIVATE_OVERLAY_REF.search(text))


def _dotted_pro_paths(text: str) -> set[str]:
    return set(_DOTTED_PRO_PATH.findall(text))


def _load_frozen_paths(path: Path = _FROZEN_PATHS_FILE) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


def _new_pro_paths(found: set[str], frozen: set[str]) -> set[str]:
    return found - frozen


# ── Rule 4: self-label ──────────────────────────────────────────────────────
def _is_moat_label(line: str) -> bool:
    if not _MOAT_LABEL.search(line):
        return False
    scrubbed = line
    for tok in _SELF_NAME_TOKENS:
        scrubbed = scrubbed.replace(tok, "")
    return bool(_MOAT_LABEL.search(scrubbed))


# ── Rule 5: sdist prune ─────────────────────────────────────────────────────
def _manifest_prunes_tests(text: str | None) -> bool:
    return bool(text) and bool(_MANIFEST_PRUNES_TESTS.search(text))


# ── File classification + the per-file scan ─────────────────────────────────
Leak = tuple[str, int, str, str]  # (repo-relative path, line, rule, text)

# Line-level rules already report these JSON reasons; the JSON pass adds
# only what the line rules cannot see.
_JSON_LINE_COVERED = frozenset({"self-label", "private path"})


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _in_scope(rel: str) -> bool:
    if rel in _SELF or rel.startswith(_SKIP_PREFIXES):
        return False
    parts = rel.split("/")
    if any(p in _SKIP_DIRS for p in parts):
        return False
    if Path(rel).suffix.lower() in _BINARY_SUFFIXES:
        return False
    return rel == "README.md" or any(rel.startswith(s + "/") for s in _SURFACES if s != "README.md")


def scan_file(rel: str, data: bytes, *, broad: bool = False) -> tuple[list[Leak], set[str]]:
    """Apply every rule that applies to one file.

    Returns ``(leaks, dotted_kiln_pro_paths)``; the dotted paths are the
    inventory input for rule 3 and are reconciled by the caller.
    """
    leaks: list[Leak] = []
    dotted: set[str] = set()
    if not _in_scope(rel):
        return leaks, dotted
    text = _decode(data)
    if text is None:
        return leaks, dotted

    name = rel.rsplit("/", 1)[-1]
    is_py = rel.endswith(".py")
    is_test = rel.startswith("kiln/tests/")
    is_src = rel.startswith("kiln/src/kiln/")
    is_shipped_json = rel.endswith(".json") and (rel.startswith("kiln/src/kiln/data/") or is_test)

    def hit(line: int, rule: str, snippet: str) -> None:
        if not _allowlisted(name, snippet):
            leaks.append((rel, line, rule, snippet.strip()))

    # Text rules — every line of every scanned file (skipped wholesale when
    # the file contains no candidate substring at all).
    if _LINE_PREFILTER.search(text):
        for i, ln in enumerate(text.splitlines(), 1):
            if _is_moat_label(ln):
                hit(i, "self-label", ln)
            if _names_private_pro_path(ln):
                hit(i, "private kiln_pro path", ln)
            if is_test and is_py and _is_inlined_pro_literal(ln):
                hit(i, "inlined Pro values", ln)

    if is_test and is_py:
        dotted = _dotted_pro_paths(text)

    # Comment / docstring rules — src and test .py, tokenized only when the
    # file mentions the private tier at all (a block can't trip otherwise).
    # Self-label and private-path reasons are already reported line by line.
    if is_py and (is_src or is_test) and (_PRO_MENTION.search(text) or broad):
        for line, block in _blocks_from_bytes(data):
            reason = _leak_reason(block, broad=broad)
            if reason in ("strategy", "provenance", "broad"):
                hit(line, "overlay narration", block)
            elif reason is None and _is_pro_mirror_note(block):
                hit(line, "Pro-data mirror note", block)

    # Shipped-data rules — data JSON (and test JSON fixtures) line by line.
    if is_shipped_json:
        for i, ln in enumerate(text.splitlines(), 1):
            reason = _json_leak_reason(ln)
            if reason and reason not in _JSON_LINE_COVERED:
                hit(i, "shipped data", ln)

    return leaks, dotted


# ── Walkers ─────────────────────────────────────────────────────────────────
# Git exports these into hook environments to pin a command to the invoking
# repository, and every one of them OUTRANKS `git -C <dir>`.  This module is
# loaded by kiln/tests/test_moat_comment_leak.py, so it runs inside the test
# suite — and the suite is started from the pre-push hook.  Inherited, they
# would make every read below resolve against whichever repo git pinned
# instead of _ROOT.
_GIT_ENV_OVERRIDES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_INDEX_VERSION", "GIT_QUARANTINE_PATH",
)


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(_ROOT), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES},
    ).stdout


def _tree_paths() -> list[str]:
    """Every public-surface file git would commit: tracked + untracked-but-
    not-ignored.  Respects .gitignore (a developer's local, ignored scripts
    are not public).  Falls back to a plain walk outside a git checkout."""
    try:
        raw = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *_SURFACES)
        names = [n.decode("utf-8", "surrogateescape") for n in raw.split(b"\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for surface in _SURFACES:
            base = _ROOT / surface
            if base.is_file():
                names.append(surface)
            elif base.is_dir():
                names.extend(p.relative_to(_ROOT).as_posix() for p in base.rglob("*") if p.is_file())
    return sorted(set(n for n in names if (_ROOT / n).is_file()))


def _staged() -> tuple[list[tuple[str, bytes]], list[str]]:
    """Staged (added/copied/modified) content from the index, plus the names
    of every staged path including deletions."""
    raw = _git("diff", "--cached", "--name-only", "--diff-filter=ACMD", "-z")
    names = [n.decode("utf-8", "surrogateescape") for n in raw.split(b"\0") if n]
    content: list[tuple[str, bytes]] = []
    for rel in names:
        try:
            content.append((rel, _git("show", f":{rel}")))
        except subprocess.CalledProcessError:
            continue  # deleted in the index — nothing to scan
    return content, names


def _merge_or_rebase_in_progress() -> bool:
    """True while git is mid-merge or mid-rebase.  The index then carries
    whole files this commit did not author (everything the other side
    touched), so a staged scan would blame the committer for the tree's
    existing state.  The secrets hook makes the same call; the full-tree CI
    step is the gate for merged content."""
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply"):
        try:
            path = _git("rev-parse", "--git-path", marker).decode().strip()
        except (OSError, subprocess.CalledProcessError):
            return False
        if path and Path(path if os.path.isabs(path) else _ROOT / path).exists():
            return True
    return False


def _read_manifest() -> str | None:
    try:
        return _MANIFEST.read_text(encoding="utf-8")
    except OSError:
        return None


# ── Main ────────────────────────────────────────────────────────────────────
def run(
    content: list[tuple[str, bytes]],
    *,
    broad: bool = False,
    check_manifest: bool = True,
    frozen: set[str] | None = None,
) -> tuple[list[Leak], dict[str, int]]:
    """Scan ``content`` and reconcile the kiln_pro path inventory."""
    leaks: list[Leak] = []
    found: set[str] = set()
    for rel, data in content:
        file_leaks, dotted = scan_file(rel, data, broad=broad)
        leaks.extend(file_leaks)
        found |= dotted

    frozen = _load_frozen_paths() if frozen is None else frozen
    new = _new_pro_paths(found, frozen)
    where = _occurrences(content, new)
    for p in sorted(new):
        rel, line = where.get(p, ("kiln/tests", 0))
        leaks.append((rel, line, "new kiln_pro path", p))

    if check_manifest and not _manifest_prunes_tests(_read_manifest()):
        leaks.append((
            "kiln/MANIFEST.in", 0, "sdist prune",
            "missing `prune tests` — the PyPI sdist would ship kiln/tests/",
        ))

    stats = {
        "kiln_pro_paths_found": len(found),
        "kiln_pro_paths_frozen": len(frozen),
        "kiln_pro_paths_new": len(new),
        "kiln_pro_paths_unused": len(frozen - found),
    }
    return leaks, stats


def _occurrences(content: list[tuple[str, bytes]], needles: set[str]) -> dict[str, tuple[str, int]]:
    """First ``(path, line)`` in the public tests where each dotted path appears."""
    where: dict[str, tuple[str, int]] = {}
    if not needles:
        return where
    for rel, data in content:
        if not rel.startswith("kiln/tests/"):
            continue
        for i, ln in enumerate((_decode(data) or "").splitlines(), 1):
            for p in _DOTTED_PRO_PATH.findall(ln):
                if p in needles and p not in where:
                    where[p] = (rel, i)
    return where


def _freeze_paths(content: list[tuple[str, bytes]]) -> int:
    found: set[str] = set()
    for rel, data in content:
        if rel.startswith("kiln/tests/") and rel.endswith(".py") and rel not in _SELF:
            found |= _dotted_pro_paths(_decode(data) or "")
    header = (
        "# Frozen inventory of dotted kiln_pro.<module> references in kiln/tests/.\n"
        "# Read by scripts/audit_moat_comment_leak.py: a path in the tests that is\n"
        "# not listed here fails the gate.  The free-tier fallback tests stub these\n"
        "# in sys.modules on purpose; this file makes GROWTH of that exposure a\n"
        "# reviewed diff rather than a side effect.  Regenerate with\n"
        "#     python3 scripts/audit_moat_comment_leak.py --freeze-kiln-pro-paths\n"
        "# and commit the diff.  Shrinking it needs no ceremony.\n"
    )
    _FROZEN_PATHS_FILE.write_text(header + "".join(f"{p}\n" for p in sorted(found)), encoding="utf-8")
    return len(found)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refuse public text that narrates, inlines, or names the private kiln-pro tier.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the git index only (commit hook)")
    mode.add_argument("--files", nargs="+", metavar="PATH", help="scan only these repo-relative paths")
    parser.add_argument("--sweep", action="store_true", help="advisory: add the broad narration heuristic; never fails")
    parser.add_argument(
        "--freeze-kiln-pro-paths", action="store_true",
        help="rewrite scripts/public_tests_kiln_pro_paths.txt from the scanned tree and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    check_manifest = True
    if args.staged:
        if _merge_or_rebase_in_progress():
            print(
                "Moat-comment audit: skipped (merge or rebase in progress — the index "
                "carries files this commit did not author; the full-tree CI step covers them)."
            )
            return 0
        content, names = _staged()
        check_manifest = any(n in ("kiln/MANIFEST.in", "kiln/pyproject.toml") for n in names)
    elif args.files:
        content = [(f, (_ROOT / f).read_bytes()) for f in args.files if (_ROOT / f).is_file()]
        check_manifest = False
    else:
        content = [(rel, (_ROOT / rel).read_bytes()) for rel in _tree_paths()]

    if args.freeze_kiln_pro_paths:
        n = _freeze_paths(content)
        print(f"Froze {n} kiln_pro path(s) into {_FROZEN_PATHS_FILE.relative_to(_ROOT)}")
        return 0

    leaks, stats = run(content, broad=args.sweep, check_manifest=check_manifest)

    inventory = (
        f"kiln_pro dotted paths in public tests: {stats['kiln_pro_paths_found']} distinct "
        f"(frozen {stats['kiln_pro_paths_frozen']}, new {stats['kiln_pro_paths_new']}, "
        f"frozen-but-unused {stats['kiln_pro_paths_unused']})"
    )

    if not leaks:
        mode = " (--sweep)" if args.sweep else " (--staged)" if args.staged else ""
        print(f"Moat-comment audit: clean.{mode}")
        print(f"  {inventory}")
        return 0

    print(
        "MOAT-COMMENT SWEEP (advisory) — review each by hand:"
        if args.sweep
        else "MOAT-COMMENT LEAK — public Kiln narrates, inlines, or names the private tier:"
    )
    by_rule: dict[str, int] = {}
    for rel, line, rule, text in leaks:
        by_rule[rule] = by_rule.get(rule, 0) + 1
        print(f"\n  [{rule}] {rel}:{line}")
        for ln in text.splitlines()[:6]:
            print(f"    {ln.strip()}")

    print(f"\n  {inventory}")
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_rule.items()))
    if args.sweep:
        print(
            f"\n{len(leaks)} finding(s) — {summary}.  Most broad-heuristic hits are "
            "legitimate contract / seam / marketed-feature notes.  Trim only the "
            "ones that narrate HOW the overlay reasons, then add the new vocabulary "
            "to _STRATEGY_TOKENS so the hard gate catches it next time."
        )
        return 0  # advisory — never fails the build

    print(
        f"\n{len(leaks)} finding(s) — {summary}.  Trim the private reasoning / "
        "values / path (keep the math + the 'exposed for the kiln-pro overlay' "
        "contract).  A NEW kiln_pro dotted path is added to "
        "scripts/public_tests_kiln_pro_paths.txt on purpose; a genuinely "
        "contract-only block gets a (filename, marker) entry in _ALLOWLIST."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
