"""Whether something moved out of this package has come back, by digest.

A guard that spells a moved name or sentence out keeps that name or sentence
in the package it guards.  These compare SHA-256 digests instead: a test
holds the digests, and the names a module carries, the package's file names
and the word runs in its strings are hashed and looked up.  A sentence is
compared as its case-folded words (runs of letters, digits and underscores)
joined by single spaces.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: The package these guards read.
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "kiln"


def digest(text: str) -> str:
    """The digest a guard holds for a name or a file name, as written."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def words_digest(text: str) -> str:
    """The digest a guard holds for a sentence: its case-folded words."""
    return digest(" ".join(_words(text)))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.casefold())


def attributes_back(obj: Any, digests: Iterable[str]) -> list[str]:
    """The digest of every name *obj* carries that is one of *digests*."""
    wanted = set(digests)
    return sorted(digest(name) for name in dir(obj) if digest(name) in wanted)


@functools.lru_cache(maxsize=4)
def _strings(package: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Every string constant in *package*'s modules, as words, by file."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(package))
        out.extend(
            (rel, tuple(_words(node.value)))
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    return tuple(out)


def strings_back(sentences: dict[str, int], package: Path = PACKAGE) -> list[str]:
    """``file: digest`` for every string constant in *package* holding a
    sentence of *sentences* (its :func:`words_digest` mapped to its number of
    words)."""
    sizes = sorted(set(sentences.values()))
    found: list[str] = []
    for rel, words in _strings(package):
        for size in sizes:
            hits = {
                key
                for start in range(len(words) - size + 1)
                if sentences.get(key := digest(" ".join(words[start:start + size]))) == size
            }
            found.extend(f"{rel}: {key}" for key in sorted(hits))
    return found


def tokens_back(digests: Iterable[str], package: Path = PACKAGE) -> list[str]:
    """``file: digest`` for every Python file in *package* whose text holds a
    name (a run of letters, digits and underscores, anywhere: code, a string,
    a comment) whose digest is one of *digests*."""
    wanted = set(digests)
    found: list[str] = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = {key for token in set(re.findall(r"[A-Za-z0-9_]+", text)) if (key := digest(token)) in wanted}
        found.extend(f"{path.relative_to(package)}: {key}" for key in sorted(hits))
    return found


def _identifiers(tree: ast.AST) -> set[str]:
    """Every name *tree*'s code uses or defines (not its strings or comments)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.arg, ast.keyword)) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.update(n for n in (node.name, node.asname) if n)
    return names


def names_back(digests: Iterable[str], package: Path = PACKAGE) -> list[str]:
    """``file: digest`` for every Python file in *package* whose code uses or
    defines a name (a variable, field, function, class, argument or keyword)
    whose digest is one of *digests*.  The same word as a string, a dict key
    for one, is not a name."""
    wanted = set(digests)
    found: list[str] = []
    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits = {key for name in _identifiers(tree) if (key := digest(name)) in wanted}
        found.extend(f"{path.relative_to(package)}: {key}" for key in sorted(hits))
    return found


def files_back(digests: Iterable[str], package: Path = PACKAGE) -> list[str]:
    """Every file in *package* whose name's digest is one of *digests*."""
    wanted = set(digests)
    return sorted(str(p.relative_to(package)) for p in package.rglob("*") if p.is_file() and digest(p.name) in wanted)


def quoted_back(digests: Iterable[str], suffixes: tuple[str, ...], package: Path = PACKAGE) -> list[str]:
    """``file: digest`` for every file in *package* with one of *suffixes*
    that quotes a name (a data file's key, for one) whose digest is one of
    *digests*."""
    wanted = set(digests)
    found: list[str] = []
    for path in sorted(package.rglob("*")):
        if path.is_file() and path.suffix in suffixes:
            text = path.read_text(encoding="utf-8", errors="replace")
            for quoted in sorted(set(re.findall(r"[\"']([A-Za-z0-9_]+)[\"']", text))):
                if digest(quoted) in wanted:
                    found.append(f"{path.relative_to(package)}: {digest(quoted)}")
    return found
