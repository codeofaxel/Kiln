"""Material catalog — brands, variants, sources, and compatibility for
popular 3D printing filaments and resins.

Ships a curated JSON database of material data that agents query
to recommend filaments, generate purchase links, and match spool
metadata to known products.

Usage::

    from kiln.material_catalog import get_material_entry, search_catalog

    entry = get_material_entry("hatchbox_pla")
    print(entry.vendor, entry.material_type)   # "Hatchbox" "PLA"
    print(entry.sources.amazon)                # search query template

    results = search_catalog("polymaker petg")
    for r in results:
        print(r.id, r.vendor, r.material_type)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "material_catalog.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialSource:
    """Purchase source URLs / search templates for a material."""

    amazon: str
    manufacturer: str


@dataclass(frozen=True)
class MaterialCatalogEntry:
    """A single material product in the catalog.

    Attributes:
        id: Unique identifier matching the JSON key.
        vendor: Brand name (``None`` for generic profiles).
        material_type: Specific product name (e.g. ``"PLA+"``, ``"PolyTerra PLA"``).
        material_family: Lowercase family (``"pla"``, ``"petg"``, ``"resin"``).
        diameter_mm: Filament diameter in mm (``None`` for resins).
        weight_options_kg: Available spool weights.
        price_range_usd: ``(min, max)`` price in USD.
        variants: Available color/finish options.
        sources: Purchase source templates.
        compatible_with: IDs of compatible generic/branded materials.
        notes: Brief description of quality and reputation.
    """

    id: str
    vendor: str | None
    material_type: str
    material_family: str
    diameter_mm: float | None
    weight_options_kg: tuple[float, ...]
    price_range_usd: tuple[float, float]
    variants: tuple[str, ...]
    sources: MaterialSource
    compatible_with: tuple[str, ...]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for MCP responses."""
        return {
            "id": self.id,
            "vendor": self.vendor,
            "material_type": self.material_type,
            "material_family": self.material_family,
            "diameter_mm": self.diameter_mm,
            "weight_options_kg": list(self.weight_options_kg),
            "price_range_usd": list(self.price_range_usd),
            "variants": list(self.variants),
            "sources": {
                "amazon": self.sources.amazon,
                "manufacturer": self.sources.manufacturer,
            },
            "compatible_with": list(self.compatible_with),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Singleton cache
# ---------------------------------------------------------------------------

_catalog: dict[str, MaterialCatalogEntry] | None = None


def _load() -> dict[str, MaterialCatalogEntry]:
    global _catalog
    if _catalog is not None:
        return _catalog

    _catalog = {}

    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load material catalog: %s", exc)
        return _catalog

    for key, data in raw.items():
        if key.startswith("_"):
            continue
        try:
            sources_raw = data.get("sources", {})
            sources = MaterialSource(
                amazon=sources_raw.get("amazon", ""),
                manufacturer=sources_raw.get("manufacturer", ""),
            )

            price_raw = data.get("price_range_usd", [0, 0])
            weight_raw = data.get("weight_options_kg", [])

            _catalog[key] = MaterialCatalogEntry(
                id=key,
                vendor=data.get("vendor"),
                material_type=data.get("material_type", ""),
                material_family=data.get("material_family", ""),
                diameter_mm=data.get("diameter_mm"),
                weight_options_kg=tuple(float(w) for w in weight_raw),
                price_range_usd=(float(price_raw[0]), float(price_raw[1])),
                variants=tuple(data.get("variants", [])),
                sources=sources,
                compatible_with=tuple(data.get("compatible_with", [])),
                notes=data.get("notes", ""),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            logger.warning("Skipping malformed catalog entry '%s': %s", key, exc)

    logger.debug("Loaded %d material catalog entries from %s", len(_catalog), _DATA_FILE)
    return _catalog


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_material_entry(material_id: str) -> MaterialCatalogEntry | None:
    """Return the catalog entry for *material_id*, or ``None`` if not found."""
    catalog = _load()
    return catalog.get(material_id.lower().strip())


def list_catalog_ids() -> list[str]:
    """Return all material IDs in the catalog, sorted alphabetically."""
    catalog = _load()
    return sorted(catalog.keys())


def search_catalog(query: str) -> list[MaterialCatalogEntry]:
    """Case-insensitive search across vendor, material_type, and notes.

    Each whitespace-separated token must appear in at least one of
    the searchable fields for an entry to match.
    """
    catalog = _load()
    tokens = query.lower().split()
    if not tokens:
        return []

    results: list[MaterialCatalogEntry] = []
    for entry in catalog.values():
        searchable = " ".join(
            filter(None, [
                (entry.vendor or "").lower(),
                entry.material_type.lower(),
                entry.notes.lower(),
            ])
        )
        if all(token in searchable for token in tokens):
            results.append(entry)
    return results


def get_compatible_materials(material_family: str) -> list[MaterialCatalogEntry]:
    """Return all entries sharing the given *material_family* (e.g. ``"pla"``)."""
    catalog = _load()
    family = material_family.lower().strip()
    return [e for e in catalog.values() if e.material_family == family]


def get_purchase_urls(material_id: str, *, color: str | None = None) -> dict[str, str]:
    """Return purchase URLs for *material_id*.

    Amazon URL uses the search query template with ``{color}`` replaced.
    If *color* is ``None``, the ``{color}`` placeholder is removed from the
    search query.

    Returns an empty dict if the material is not found.
    """
    entry = get_material_entry(material_id)
    if entry is None:
        return {}

    urls: dict[str, str] = {}

    amazon_query = entry.sources.amazon
    if amazon_query:
        if color is not None:
            amazon_query = amazon_query.replace("{color}", color.replace(" ", "+"))
        else:
            amazon_query = amazon_query.replace("+{color}", "").replace("{color}+", "").replace("{color}", "")
        urls["amazon"] = f"https://www.amazon.com/s?k={amazon_query}"

    if entry.sources.manufacturer:
        urls["manufacturer"] = entry.sources.manufacturer

    return urls


def find_matching_entry(
    *,
    vendor: str | None = None,
    material_type: str | None = None,
    color: str | None = None,
) -> MaterialCatalogEntry | None:
    """Fuzzy match a catalog entry from spool metadata.

    Matches against vendor and material_type (case-insensitive substring).
    If *color* is provided and multiple entries match vendor+material_type,
    prefers entries whose variants include the color.

    Returns the best match or ``None``.
    """
    catalog = _load()
    if not vendor and not material_type:
        return None

    candidates: list[MaterialCatalogEntry] = []
    for entry in catalog.values():
        if entry.vendor is None:
            continue  # skip generic profiles

        vendor_match = True
        if vendor:
            vendor_match = vendor.lower() in (entry.vendor or "").lower()

        type_match = True
        if material_type:
            type_match = material_type.lower() in entry.material_type.lower()

        if vendor_match and type_match:
            candidates.append(entry)

    if not candidates:
        return None

    if color and len(candidates) > 1:
        color_lower = color.lower()
        color_matches = [
            c for c in candidates
            if any(color_lower in v.lower() for v in c.variants)
        ]
        if color_matches:
            return color_matches[0]

    return candidates[0]
