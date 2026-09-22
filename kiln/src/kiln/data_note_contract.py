"""What a note in the bundled data may say, and what it may not.

The catalogues under ``data/`` ship in the public package, and the prose in
them -- a printer's notes, the motion block's per-cell notes, a material's
caveats -- is a product surface: the plan text, the doctor and the site can
quote it.  The research behind a fact (the pages fetched, the archives
opened, the hashes, the download ids, the working notes) is provenance,
and provenance lives in Kiln Pro, cell for cell, with the full text.

This module is the one definition of the line between the two, read by the
data test, by the commit-time public-language audit, and by the tooling
that writes notes.  It is deliberately a list of things a public note must
NOT contain plus, for the motion block, a length: a note that names a URL,
a file hash, a download id, a research step or a community account is
carrying the private half by another door -- which is exactly how 440
motion notes shipped on a branch on 2026-09-17 before this file existed.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: A motion note is a sentence or three, never a page.
MAX_MOTION_NOTE_CHARS = 420

#: Community accounts and client projects a fact may have been read from.
#: One list, shared with the comment-leak gate (``scripts/
#: audit_moat_comment_leak.py``), which applies it to code comments and
#: docstrings the same way this contract applies it to bundled notes.
COMMUNITY_ACCOUNTS = (
    r"\b(?:pellcorp|Guilouz|TheFeralEngineer|artillery3dlab|fpnewton|Doridian|OpenBambuAPI|"
    r"open-bamboo-networking|ha-bambulab|pybambu|bambuddy|bambino|OpenCentauri)\b"
)

#: A slicer by name, and a slicer build: the name followed by a three- or
#: four-part version.
_SLICER_APP = (
    r"(?:Bambu ?Studio|Orca ?Slicer|Prusa ?Slicer|Super ?Slicer|Creality Print|Elegoo ?Slicer|"
    r"QIDI ?Studio|Anycubic Slicer|(?:UltiMaker )?Cura)"
)
_SLICER_BUILD = _SLICER_APP + r"(?:['’]s)?\s+v?\d{1,2}\.\d{1,2}\.\d{1,3}(?:\.\d{1,3})?\b"
_RESEARCH_VERB = r"(?:captured|harvested|extracted|fetched|pulled|copied|scraped|lifted)"

#: How a vendor sequence or value was captured, as opposed to what it is.
#: A public file may say a value is the maker's own -- its source CLASS --
#: and carry the licence line; the build it was read from, the path inside
#: the slicer's profile bundle, the capture method and the date the work was
#: done are the private half.  One list, shared with the comment-leak gate,
#: which applies it to comments, docstrings, docs and G-code headers, and
#: with the commit-message audit.  Every pattern is scoped case-insensitive
#: so the list can be joined into one alternation.
CAPTURE_PROVENANCE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "a slicer build named as a source",
        r"(?i:(?:\b(?:sourced? from|captured|copied|harvested|extracted|lifted|taken from|"
        r"read (?:off|from))\b|\bsource:)[^\n]{0,70}?" + _SLICER_BUILD
        + r"|\bfrom (?:the |its |an? )?" + _SLICER_BUILD
        + r"|" + _SLICER_BUILD + r"[^\n]{0,50}?\b(?:vendor profile bundle|profile bundle|slice the|"
        r"capture|captured|read off)\b)",
    ),
    (
        "a slicer profile-bundle path",
        r"(?i:\bprofiles/[\w .<>-]+/(?:machine|process|filament)\b|\btemplate (?:machine_start_gcode|"
        r"machine_end_gcode|change_filament_gcode|layer_change_gcode|time_lapse_gcode|"
        r"machine_pause_gcode)\.json|\.app/Contents/Resources/profiles\b)",
    ),
    (
        "a capture method",
        r"(?i:\b(?:came from|captured from|captured with|taken from|captured)\b[^\n]{0,70}?"
        r"\b(?:command line|command-line|CLI|headless)\b|\bpresets?\b[^\n]{0,25}?\bflatten(?:ed|s|ing)?\b"
        r"|\bflatten(?:ed|s|ing)?\b[^\n]{0,25}?\bpresets?\b|\btemplate (?:fields?|files?)\b[^\n]{0,25}?"
        r"\bmerged\b|\bmerg(?:e|es|ed|ing)\b[^\n]{0,25}?\btemplate (?:fields?|files?)\b)",
    ),
    (
        "a research date",
        r"(?i:\b" + _RESEARCH_VERB + r"\b[^\n]{0,50}?\bfrom\b[^\n]{0,60}?\b20\d\d-\d\d-\d\d\b"
        r"|\b" + _RESEARCH_VERB + r"\b[^\n]{0,20}?\b20\d\d-\d\d-\d\d\b[^\n]{0,30}?\bfrom\b)",
    ),
)

#: Markers of research provenance or process.  Each is a thing a public note
#: has no business saying; the Kiln Pro overlay keeps the full text.
PROVENANCE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("a link", r"https?://|www\.|\b[a-z0-9-]+\.(?:com|org|io|net|dev|cc|de|cn)\b(?!\.[a-z])"),
    ("a stripped-link placeholder", r"\[vendor source\]"),
    ("a file hash", r"\b(?:sha256|sha1|md5)\b|\b[0-9a-f]{32,}\b"),
    ("a byte size", r"\b\d{1,3}(?:,\d{3}){1,3} ?B\b|\b\d+(?:\.\d+)? ?[KMG]B\b|\bbytes\b"),
    ("a download id", r"\bDrive\b|\b(?=[A-Za-z0-9_-]{25,}\b)(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b"),
    ("a research step", r"\blap[- ]?\d|\blaps?\b|\bassembler|\bthe brief\b|\bper the brief\b|\bbrief rule|\bthe report\b|\breport's\b|\bfetch(?:ed|es|ing)?\b|\bre-fetched\b|\bdownload(?:ed)?\b|\bgrep\b|\b\d+ hits?\b|\bmy reading\b|\bI \b"),
    ("an API endpoint or header", r"api\.github|raw\.githubusercontent|Content-Disposition|HEAD request|per_page"),
    ("a community account", COMMUNITY_ACCOUNTS + r"|\bdiscourse\b|\breddit\b|\bforum\b"),
    ("a repository path", r"\bgithub\b|CrealityOfficial|VoronDesign|QIDITECH|eufymake"),
    ("a fetch date", r"\bread 20\d\d-\d\d-\d\d\b"),
    ("a private path or repository", r"/Users/|scratchpad|kiln-pro|kiln_pro"),
) + CAPTURE_PROVENANCE_PATTERNS

#: The one place a bundled catalogue carries links on purpose: where a
#: material can be bought, keyed under ``sources`` in the material catalogue.
_LINK_BEARING = (("material_catalog.json", "sources"),)


def provenance_findings(text: str, *, allow_links: bool = False) -> list[str]:
    """Every provenance marker *text* carries; empty when it is a public note."""
    findings: list[str] = []
    for name, pattern in PROVENANCE_PATTERNS:
        if allow_links and name == "a link":
            continue
        match = re.search(pattern, text)
        if match:
            findings.append(f"{name}: {match.group(0)!r}")
    return findings


def motion_note_findings(note: str) -> list[str]:
    """The motion block's stricter contract: the markers, and a length."""
    findings = provenance_findings(note)
    if len(note) > MAX_MOTION_NOTE_CHARS:
        findings.append(f"{len(note)} chars, over {MAX_MOTION_NOTE_CHARS}")
    return findings


def _strings(node: Any, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], str]]:
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _strings(child, path + (str(key),))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _strings(child, path + (str(index),))
    elif isinstance(node, str):
        yield path, node


def data_file_findings(
    relative_path: str, text: str, *, vendored_dirs: tuple[str, ...] | None = None
) -> dict[str, list[str]]:
    """Every string in one bundled JSON that breaks the contract, by JSON path.

    ``_meta`` is skipped (it names Kiln's own pricing page); a motion
    block's ``_sources`` notes get the stricter motion contract; the
    material catalogue's ``sources`` block may carry links; a vendored
    library's JSON is upstream tooling config, not Kiln's prose.  A caller
    that loaded this file by path (the commit hook) passes the vendored
    directories from the same tree, so nothing here resolves through an
    installed ``kiln`` that may be another checkout.
    """
    if vendored_dirs is None:
        from kiln.data_manifest import VENDORED_DATA_DIRS

        vendored_dirs = VENDORED_DATA_DIRS
    if relative_path.split("/", 1)[0] in vendored_dirs:
        return {}
    broken: dict[str, list[str]] = {}
    for path, value in _strings(json.loads(text), ()):
        if not path or path[0] == "_meta":
            continue
        where = f"{relative_path}:{'.'.join(path)}"
        if relative_path == "printer_intelligence.json" and "_sources" in path and path[-1] == "note":
            findings = motion_note_findings(value)
        else:
            allow = any(relative_path == file and key in path for file, key in _LINK_BEARING)
            findings = provenance_findings(value, allow_links=allow)
        if findings:
            broken[where] = findings
    return broken


def bundled_data_findings(root: Path | None = None) -> dict[str, list[str]]:
    """Every contract break across every bundled data file."""
    from kiln.data_manifest import DATA_ROOT, discover_bundled_data_files

    base = DATA_ROOT if root is None else Path(root)
    broken: dict[str, list[str]] = {}
    for relative in sorted(discover_bundled_data_files(base)):
        broken.update(data_file_findings(relative, (base / relative).read_text(encoding="utf-8")))
    return broken
