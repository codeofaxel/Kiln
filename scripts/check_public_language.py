#!/usr/bin/env python3
"""Reject private editorial context from public repository surfaces.

In ``--staged`` mode (the commit-time hook) this also runs
``scripts/audit_moat_comment_leak.py --staged`` over the same index, so a
paid-tier table, a private ``kiln_pro`` path, a self-label, or an internal
persona name in a staged test / doc / script is refused at the commit, not
at the PR.  Full-tree runs of that gate stay with its own CI step and the
pre-push hook; this file only closes the commit-time door.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SELF = Path(__file__).relative_to(_ROOT).as_posix()
_LEAK_GATE = _ROOT / "scripts" / "audit_moat_comment_leak.py"
# Files that must spell out the phrases they catch: this checker, the
# private-tier leak gate, and that gate's fixture test.  Everything else in
# the tree is fair game.
_PATTERN_OWNERS = frozenset({
    _SELF,
    "scripts/audit_moat_comment_leak.py",
    "kiln/tests/test_moat_comment_leak.py",
})
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


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


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
    Rule(
        "internal review process",
        re.compile(
            r"\b(?:judges?[- ]panel|war[- ]room|panel-approved|judge-voted|"
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


def find_violations(
    text: str,
    *,
    source: str,
    commit_message: bool = False,
) -> list[Finding]:
    """Return public-language violations in ``text``."""
    findings: list[Finding] = []
    rules = _PUBLIC_RULES + (_COMMIT_RULES if commit_message else ())

    for line_number, line in enumerate(text.splitlines(), 1):
        if _RETIRED_PROVIDER in line.lower():
            findings.append(
                Finding(source, line_number, "retired public provider", line.strip())
            )
        for rule in rules:
            if rule.pattern.search(line):
                findings.append(
                    Finding(source, line_number, rule.name, line.strip())
                )
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
    if relative_path in _PATTERN_OWNERS:
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
