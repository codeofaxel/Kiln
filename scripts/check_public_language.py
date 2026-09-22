#!/usr/bin/env python3
"""Reject private editorial context from public repository surfaces.

In ``--staged`` mode (the commit-time hook) this also runs
``scripts/audit_moat_comment_leak.py --staged`` over the same index, so a
paid-tier table, a private ``kiln_pro`` path, a self-label, or an internal
persona name in a staged test / doc / script is refused at the commit, not
at the PR.  (The gate skips itself during a merge or rebase, where the index
carries files the committer did not author; the full-tree CI step covers
those.)  Full-tree runs of that gate stay with its own CI step and the
pre-push hook; this file only closes the commit-time door.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SELF = Path(__file__).relative_to(_ROOT).as_posix()
_LEAK_GATE = _ROOT / "scripts" / "audit_moat_comment_leak.py"
_SKIP_PREFIXES = (
    "kiln/src/kiln/data/scad_libraries/",
)
_BINARY_SUFFIXES = {
    ".a",
    ".bin",
    ".dmg",
    ".eot",
    ".gif",
    ".gz",
    ".icns",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".mov",
    ".o",
    ".otf",
    ".pdf",
    ".png",
    ".so",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".xz",
    ".zip",
}

# Keep the retired name out of the public tree while still preventing it from
# returning. Splitting the token lets a repository-wide literal search stay
# empty and makes accidental reintroduction fail this gate.
_RETIRED_PROVIDER = "".join(("sculp", "teo"))

# The tier above enterprise, and the internal dashboard named after it, are
# not words a customer may meet.  Assembled from parts for the same reason the
# retired provider above is: this file is public source, so a repository-wide
# search for the word itself has to come back empty.
_INTERNAL_TIER = "".join(("found", "er"))


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    #: File suffixes this rule judges; empty means every file.  The internal
    #: tier name is an ordinary English word in prose — the terms page uses it
    #: for the person who answers support — so the bare-word rule is scoped to
    #: source, where the word can only mean the tier or the surface named
    #: after it.  Commit messages carry no suffix and are judged by every rule.
    suffixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    rule: str
    text: str


_PUBLIC_RULES = (
    Rule(
        "review-persona attribution",
        re.compile(
            r"\b(?:jobs?\s*[/,+&]\s*ive|ive\s*[/,+&]\s*antirez|"
            r"antirez(?:'s)?|andreessen|jony|steve says)\b",
            re.IGNORECASE,
        ),
    ),
    # Phrases, never bare words: "panel" alone is an MCP Apps panel and
    # "judges" alone is a verb ("the composer judges the layout"); only the
    # review-persona forms — possessive, "the judges", the panel / room /
    # gate phrases — are private editorial context.
    Rule(
        "internal review process",
        re.compile(
            r"\bjudges['’]|\bjudges\s*:|"
            r"\b(?:the|our|three|four|from the|per the) judges\b|"
            r"\b(?:judges?[- ]panel|war[- ]room|ship[- ]gate|panel(?:['’]s)? verdicts?|"
            r"panel-approved|judge-voted|"
            r"gap analysis|sme flagged|internal thinking|session shorthand)\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "numbered internal review round",
        re.compile(
            r"(?:\b(?:review|audit|panel|board)\s+round\s+\d+\b|"
            r"\bround\s+\d+\s+(?:of\s+the\s+)?(?:panel|review|audit)\b|"
            r"\(\s*round\s+\d+\s*\))",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "unannounced relationship status",
        re.compile(
            r"\b(?:not confirmed as (?:a )?partner|pending (?:partner )?credentials|"
            r"pending api access|until credentials are received|"
            r"partnership materializes|ready to re-enable|"
            r"internal (?:adapter )?code (?:retained|untouched))\b",
            re.IGNORECASE,
        ),
    ),
    # Public Kiln's tier ladder stops at enterprise.  The tier above it must
    # not appear in public source at all.  kiln-pro's suite already forbids
    # it, but that suite reads this repository AFTER a push, so the word was
    # public before anything said so; this is the same rule at the door it
    # actually leaves by (2026-09-15: four files, two of them shipped source).
    Rule(
        "internal tier name",
        re.compile(rf"\b{_INTERNAL_TIER}\b", re.IGNORECASE),
        suffixes=(".py", ".pyi"),
    ),
    # Any file, including prose and commit messages: naming the internal
    # surface, or whose machine a measurement came from, tells a stranger
    # more than the bare word does.
    Rule(
        "internal tier surface",
        re.compile(
            rf"\b{_INTERNAL_TIER}(?:['’]s)?[- ](?:dashboard|tier|seat|account|licen[cs]e)\b|"
            rf"\b{_INTERNAL_TIER}['’]s\s+\w+",
            re.IGNORECASE,
        ),
    ),
)

_COMMIT_RULES = (
    Rule(
        "agent-work metadata",
        re.compile(
            r"(?:^|\n)\s*(?:co-authored-by:|handoff:|wip:)|"
            r"\b(?:parallel agent session|stashed agent work|overnight feature sprint|"
            r"judges panel pending|not yet (?:verified|validated))\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "numbered review commit",
        re.compile(
            r"(?:^|\n)\s*(?:board review|review round|round)\s+\d+\b",
            re.IGNORECASE,
        ),
    ),
)


def _commit_provenance_rule() -> Rule:
    """Research provenance in a commit message: a source file:line pin, a
    wiki or forum page, a community account or client, a fetch date, and
    how a vendor sequence was captured -- the slicer build it was read
    from, the path inside the slicer's profile bundle, the capture method,
    the date the work was done.

    The public repository's history is as public as its tree; a pin in a
    message is the same trail a comment is refused for.  A repository link
    is NOT judged here -- dependency bumps cite the dependency's own repo
    -- so the account vocabulary carries the research projects by name.
    The vocabulary is ``kiln.data_note_contract``'s, read from this tree.
    """
    accounts = r"\b(?:pellcorp|Guilouz|TheFeralEngineer|artillery3dlab|fpnewton|Doridian|OpenBambuAPI)\b"
    capture = ""
    try:
        contract = _load_module(
            "kiln_data_note_contract_for_commit", _ROOT / "kiln" / "src" / "kiln" / "data_note_contract.py",
        )
        accounts = str(contract.COMMUNITY_ACCOUNTS)
        capture = "".join("|" + pattern for _name, pattern in contract.CAPTURE_PROVENANCE_PATTERNS)
    except Exception:  # noqa: BLE001 -- a tree without the contract has nothing to judge by
        pass
    return Rule(
        "research provenance",
        re.compile(
            r"\b[\w./-]+\.(?:cpp|hpp|cc|c|h)\b(?::\d+|[`'\")]*\s*\(?\s*(?:L|lines?\s+)\d+|[^\n]{0,40}@ \d+\.\d+)"
            r"|\b(?:wiki|forum|forums|community|discuss)\.[\w.-]+\.(?:com|org|io|net|dev|cn)/[\w./#?=%-]+"
            r"|reddit\.com/r/[\w/]+"
            r"|\bread 20\d\d-\d\d-\d\d\b"
            r"|" + accounts + capture,
        ),
    )


def _wrapped_findings(text: str, source: str, rule: Rule, found: list[Finding]) -> list[Finding]:
    """*rule* read a paragraph at a time.  A commit body wraps at 72 columns,
    and a slicer build split over two lines is still one sentence; a match
    the line-by-line pass already reported is not reported twice."""
    seen = {(f.line, f.rule) for f in found}
    lines = text.splitlines()
    extra: list[Finding] = []
    start = 0
    for end in range(len(lines) + 1):
        if end < len(lines) and lines[end].strip():
            continue
        if end - start > 1:
            joined, offsets = "", []
            for line in lines[start:end]:
                offsets.append(len(joined))
                joined += line.strip() + " "
            for match in rule.pattern.finditer(joined):
                line_number = start + bisect.bisect_right(offsets, match.start())
                if (line_number, rule.name) not in seen:
                    seen.add((line_number, rule.name))
                    extra.append(Finding(source, line_number, rule.name, match.group(0)))
        start = end + 1
    return extra


def find_violations(
    text: str,
    *,
    source: str,
    commit_message: bool = False,
) -> list[Finding]:
    """Return public-language violations in ``text``."""
    findings: list[Finding] = []
    provenance = _commit_provenance_rule() if commit_message else None
    rules = _PUBLIC_RULES + (_COMMIT_RULES + (provenance,) if provenance is not None else ())

    suffix = Path(source).suffix.lower()

    for line_number, line in enumerate(text.splitlines(), 1):
        if _RETIRED_PROVIDER in line.lower():
            findings.append(
                Finding(source, line_number, "retired public provider", line.strip())
            )
        for rule in rules:
            if rule.suffixes and not commit_message and suffix not in rule.suffixes:
                continue
            if rule.pattern.search(line):
                findings.append(
                    Finding(source, line_number, rule.name, line.strip())
                )
    if provenance is not None:
        findings.extend(_wrapped_findings(text, source, provenance, findings))
    return findings


# Git exports these into hook environments to pin a command to the invoking
# repository, and every one of them OUTRANKS `git -C <dir>`.  This module is
# imported by kiln/tests/test_public_language.py, so it runs inside the test
# suite — and the suite is started from the pre-push hook.  Inherited, they
# would make every read below resolve against whichever repo git pinned
# instead of _ROOT: the checks would silently grade the wrong tree.
_GIT_ENV_OVERRIDES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_INDEX_VERSION", "GIT_QUARANTINE_PATH",
)


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(_ROOT), *args],
        check=True,
        stdout=subprocess.PIPE,
        env={k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES},
    )
    return result.stdout


def _eligible(relative_path: str) -> bool:
    if relative_path == _SELF:
        return False
    if relative_path.startswith(_SKIP_PREFIXES):
        return False
    return Path(relative_path).suffix.lower() not in _BINARY_SUFFIXES


def _decode(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _tracked_content(*, staged: bool) -> list[tuple[str, str]]:
    if staged:
        names = _git(
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACM",
            "-z",
        ).split(b"\0")
    else:
        names = _git("ls-files", "-z").split(b"\0")

    content: list[tuple[str, str]] = []
    for raw_name in names:
        if not raw_name:
            continue
        relative_path = raw_name.decode("utf-8", errors="surrogateescape")
        if not _eligible(relative_path):
            continue
        if staged:
            data = _git("show", f":{relative_path}")
        else:
            path = _ROOT / relative_path
            if not path.is_file():
                continue
            data = path.read_bytes()
        text = _decode(data)
        if text is not None:
            content.append((relative_path, text))
    return content


_DATA_PREFIX = "kiln/src/kiln/data/"


def _load_module(name: str, path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def data_note_findings(source: str, text: str) -> list[Finding]:
    """The bundled-data half of the audit: a catalogue string that carries
    research provenance (a link, a hash, a download id, a research step, a
    community account, a repository path, a fetch date, a private path).

    One definition, ``kiln.data_note_contract``, shared with the data test
    -- imported from THIS tree, so the rule the commit is judged by is the
    rule the commit ships.  A JSON that does not parse is left to the test
    suite; this door judges content, not syntax.
    """
    if not source.startswith(_DATA_PREFIX) or not source.endswith(".json"):
        return []
    # Loaded by FILE PATH from this tree, never through ``import kiln``: an
    # editable install resolves that name to whichever checkout it was made
    # from, and a hook judging one tree by another tree's rule is no gate.
    package = _ROOT / "kiln" / "src" / "kiln"
    try:
        manifest = _load_module("kiln_data_manifest_for_hook", package / "data_manifest.py")
        contract = _load_module("kiln_data_note_contract_for_hook", package / "data_note_contract.py")
    except Exception:  # noqa: BLE001 -- a tree without the contract has nothing to judge by
        return []
    try:
        broken = contract.data_file_findings(
            source[len(_DATA_PREFIX):], text, vendored_dirs=manifest.VENDORED_DATA_DIRS
        )
    except ValueError:
        return []
    return [
        Finding(source=source, line=0, rule=f"bundled-data provenance ({why[0]})", text=where)
        for where, why in broken.items()
    ]


def _staged_leak_gate() -> tuple[int, str]:
    """Run the private-tier leak gate over the staged index.

    Returns ``(exit_code, report)``.  A missing gate script is not an error —
    this checker also runs in trees that never carried it.
    """
    if not _LEAK_GATE.is_file():
        return 0, ""
    result = subprocess.run(
        [sys.executable, str(_LEAK_GATE), "--staged"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES},
    )
    return result.returncode, result.stdout


def _range_messages(revision_range: str) -> list[tuple[str, str]]:
    raw = _git("log", "--format=%H%x00%B%x00", revision_range)
    fields = raw.decode("utf-8", errors="replace").split("\0")
    messages: list[tuple[str, str]] = []
    for index in range(0, len(fields) - 1, 2):
        commit_hash = fields[index].strip()
        message = fields[index + 1]
        if commit_hash:
            messages.append((f"commit {commit_hash[:12]}", message))
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="scan staged file content instead of the full tracked tree",
    )
    parser.add_argument(
        "--message-file",
        type=Path,
        help="also scan a commit-message file",
    )
    parser.add_argument(
        "--range",
        dest="revision_range",
        help="also scan commit messages in a Git revision range",
    )
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for source, text in _tracked_content(staged=args.staged):
        findings.extend(find_violations(text, source=source))
        findings.extend(data_note_findings(source, text))

    if args.message_file is not None:
        message = args.message_file.read_text(encoding="utf-8", errors="replace")
        findings.extend(
            find_violations(
                message,
                source=str(args.message_file),
                commit_message=True,
            )
        )

    if args.revision_range:
        for source, message in _range_messages(args.revision_range):
            findings.extend(
                find_violations(message, source=source, commit_message=True)
            )

    leak_rc, leak_report = _staged_leak_gate() if args.staged else (0, "")

    if not findings and leak_rc == 0:
        print("Public-language audit: clean.")
        return 0

    if findings:
        print("PUBLIC-LANGUAGE VIOLATION — private editorial context in public output:")
        for finding in findings:
            print(
                f"  {finding.source}:{finding.line}: {finding.rule}\n"
                f"    {finding.text}"
            )
    if leak_rc != 0:
        print(leak_report.rstrip() or f"leak gate exited {leak_rc} with no output")
    return 2


if __name__ == "__main__":
    sys.exit(main())
