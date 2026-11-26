"""The published .gitignore holds patterns only.

Visitors can open the root .gitignore on GitHub, so it carries no comments:
every non-blank line is a pattern.  Patterns that only matter on one
maintainer's machine belong in that checkout's local exclude file.
"""

from __future__ import annotations

from pathlib import Path

_GITIGNORE = Path(__file__).resolve().parents[2] / ".gitignore"


def test_root_gitignore_has_no_comment_lines():
    lines = _GITIGNORE.read_text(encoding="utf-8").splitlines()
    comments = [n for n, line in enumerate(lines, 1) if line.lstrip().startswith("#")]
    assert not comments, f".gitignore lines {comments} are comments; keep it to patterns"


def test_root_gitignore_still_keeps_secrets_out():
    patterns = set(_GITIGNORE.read_text(encoding="utf-8").split())
    for must in (".env", ".env.*", "*.pem", "*.key", ".kiln/"):
        assert must in patterns, must
