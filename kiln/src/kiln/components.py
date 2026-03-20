"""Component catalog — maps user intent to OpenSCAD library functions.

When a user describes what they want, this module identifies which
pre-built library components can be used instead of generating geometry
from scratch.  The agent never needs to expose library names to users.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Component:
    component_id: str
    display_name: str
    library: str
    import_line: str
    example_call: str
    key_params: dict[str, Any]
    agent_guidance: str
    printability_notes: str
    category: str
    user_intents: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComponentMatch:
    component: Component
    score: float
    matched_intents: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component.to_dict(),
            "score": self.score,
            "matched_intents": self.matched_intents,
        }


# ---------------------------------------------------------------------------
# Singleton catalog loader
# ---------------------------------------------------------------------------

_catalog: dict[str, Component] | None = None
_catalog_meta: dict[str, Any] = {}


def _get_catalog() -> dict[str, Component]:
    global _catalog, _catalog_meta
    if _catalog is not None:
        return _catalog

    data_dir = Path(__file__).parent / "data"
    catalog_path = data_dir / "component_catalog.json"

    if not catalog_path.exists():
        _logger.warning("Component catalog not found at %s", catalog_path)
        _catalog = {}
        return _catalog

    try:
        with open(catalog_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _logger.warning("Failed to load component catalog: %s", exc)
        _catalog = {}
        return _catalog

    _catalog_meta = raw.get("_meta", {})
    _catalog = {}
    for key, val in raw.items():
        if key.startswith("_"):
            continue
        try:
            _catalog[key] = Component(
                component_id=key,
                display_name=val["display_name"],
                library=val["library"],
                import_line=val["import_line"],
                example_call=val["example_call"],
                key_params=val.get("key_params", {}),
                agent_guidance=val.get("agent_guidance", ""),
                printability_notes=val.get("printability_notes", ""),
                category=val.get("category", "general"),
                user_intents=val.get("user_intents", []),
            )
        except (KeyError, TypeError) as exc:
            _logger.warning("Skipping malformed component %r: %s", key, exc)

    _logger.debug("Loaded %d components from catalog", len(_catalog))
    return _catalog


def _reset_catalog() -> None:
    """Reset singleton for test isolation."""
    global _catalog, _catalog_meta
    _catalog = None
    _catalog_meta = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)


def match_components(description: str) -> list[ComponentMatch]:
    """Return catalog components whose intents match *description*.

    Matching rules
    --------------
    * Tokenise *description* into lowercase words (strip punctuation).
    * For each component, check every token against every ``user_intent``:
      - exact match  (token == intent)
      - token is a substring of the intent
      - intent is a substring of the token  (handles plurals like
        "gears" matching "gear")
    * Multi-word intents: if an intent has >1 word, check whether **all**
      words appear somewhere in the description.
    * Score = number of unique matched intents.
    * Results are sorted by descending score.
    """
    catalog = _get_catalog()
    if not catalog:
        return []

    desc_lower = description.lower()
    tokens = _PUNCTUATION_RE.sub("", desc_lower).split()

    results: list[ComponentMatch] = []

    for comp in catalog.values():
        matched_intents: list[str] = []

        for intent in comp.user_intents:
            intent_lower = intent.lower()
            intent_words = intent_lower.split()

            # Multi-word intent: all words must appear in description
            if len(intent_words) > 1:
                if all(
                    w in tokens or any(
                        (len(t) >= 3 and (t in w or w in t))
                        for t in tokens
                    )
                    for w in intent_words
                ):
                    matched_intents.append(intent)
                continue

            # Single-word intent: check against each token
            for token in tokens:
                if token == intent_lower:
                    matched_intents.append(intent)
                    break
                # Substring matching only for tokens >= 3 chars to
                # avoid false positives from short words like "a", "i"
                if len(token) >= 3 and (
                    token in intent_lower or intent_lower in token
                ):
                    matched_intents.append(intent)
                    break

        if matched_intents:
            results.append(
                ComponentMatch(
                    component=comp,
                    score=float(len(matched_intents)),
                    matched_intents=matched_intents,
                )
            )

    results.sort(key=lambda m: m.score, reverse=True)
    return results


def get_component(component_id: str) -> Component | None:
    """Look up a single component by its ID."""
    return _get_catalog().get(component_id)


def list_components(category: str | None = None) -> list[Component]:
    """Return all components, optionally filtered by *category*.

    Results are sorted alphabetically by ``display_name``.
    """
    catalog = _get_catalog()
    comps = list(catalog.values())
    if category is not None:
        comps = [c for c in comps if c.category == category]
    comps.sort(key=lambda c: c.display_name)
    return comps


def get_library_path(library: str) -> str:
    """Return the absolute path to a bundled OpenSCAD library directory.

    Raises :class:`ValueError` if *library* is not listed in the catalog
    ``_meta``.
    """
    _get_catalog()  # ensure meta is loaded
    libs = _catalog_meta.get("libraries", {})
    if library not in libs:
        raise ValueError(
            f"Unknown library {library!r}. "
            f"Available: {', '.join(sorted(libs))}"
        )
    rel_path = libs[library]["path"]
    data_dir = Path(__file__).parent / "data"
    return str((data_dir / rel_path).resolve())


def get_available_libraries() -> list[dict[str, Any]]:
    """Return metadata for every bundled OpenSCAD library.

    Each dict has keys: ``name``, ``license``, ``path``, ``available``.
    """
    _get_catalog()  # ensure meta is loaded
    libs = _catalog_meta.get("libraries", {})
    data_dir = Path(__file__).parent / "data"
    result: list[dict[str, Any]] = []
    for name, info in sorted(libs.items()):
        abs_path = (data_dir / info["path"]).resolve()
        result.append(
            {
                "name": name,
                "license": info.get("license", "unknown"),
                "path": str(abs_path),
                "available": abs_path.exists(),
            }
        )
    return result
