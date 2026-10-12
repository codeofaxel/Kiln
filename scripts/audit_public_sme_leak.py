#!/usr/bin/env python3
"""Public-source SME-table leak gate — public Kiln must not ship a curated
cross-vendor capability / SME table.

The private tier is curated intelligence: per-vendor, per-model behaviour
compiled from many sources over a long time.  Three gates already protect
it — the wheel-exclusion and design-knowledge-split gates guard *where a
file lives* (user disk / public repo), and ``audit_moat_comment_leak.py``
guards the *method* narrated in prose.  They all miss one shape: a curated
DATA TABLE — a registry keyed by printer brand / firmware carrying
``limitations``, ``recovery_methods``, capability matrices, ``failure_modes``,
``design_rules`` — sitting in PUBLIC source as a ``.py`` module or ``.json``
file.

On 2026-06-23 a 459-line cross-vendor power-loss capability registry
(``resume_capabilities.py``) was found doing exactly this: world-visible,
consumed by no live code path, a free head-start for any competitor trying
to compile the same multi-vendor SME and catch up.  It fell in the blind
spot between "code" (so the data gates ignored it) and "prose" (so the
comment gate ignored it).

This gate closes that blind spot.  It flags any public-source file carrying
the signature of a compiled cross-vendor SME table — several distinct
printer-vendor names AND multiple curated-intelligence data fields — unless
the file is consciously allowlisted as a public floor.  The right fix for
a flagged file is to MOVE the table to the private tier (kiln-pro) or a pro
overlay, never to allowlist it.

The bundled data files need a second, structural reading.  The text scan
above knows SME fields by name, and a per-printer catalogue names its
fields after what they hold: a hotend ceiling, a build volume, a feedrate.
Every printer's limits, variants and spec sheet sat in ``data/`` in that
form, and the text scan read those files clean.  So every JSON under
``kiln/src/kiln/data/`` is also judged as tables: a table whose rows name
several printer makers (in the row key or the row's display name) AND
carry limit-shaped fields (temperature ceilings, build volume or bed
shape, speed / acceleration / flow, hotend or extruder type, enclosure,
chamber, per-material recipes, variants, notes) FAILS unless
``scripts/public_data_allowed.yaml`` classifies it, with a reason:

    roster              ids and display names, plus the fields the row names
                        (each row may carry only those; ``full_rows`` names
                        the generic rows that stay whole)
    safety_floor        a free floor every install needs offline (names the
                        fields it may carry, enforced the same way)
    standard_reference  published standard / catalogue facts anyone can look
                        up (names its fields, enforced the same way)
    pending_move        a per-model dataset that moves to kiln-pro; needs
                        ``why`` and an ISO ``by`` date, FAILS once past it
    not_printer_keyed   the maker names are a coincidence of keying (say why)

A ledger row whose file is gone, or whose file no longer carries the shape,
is stale and FAILS: a row survives only while its subject does.  Detection
reads field SHAPES, never a list of file names, so a new per-model file
fails the day it lands.

    python3 scripts/audit_public_sme_leak.py          # exit 0 clean, 2 leak
    python3 scripts/audit_public_sme_leak.py --json   # CI / machine format
    python3 scripts/audit_public_sme_leak.py --data-root <dir>  # judge another data tree

Pairs with ``audit_moat_comment_leak.py`` (protects the method in prose) and
the wheel / overlay gates (protect the curated values); this one protects
against compiled SME *data tables* shipping in public source.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "kiln" / "src" / "kiln"
_DATA = _SRC / "data"
_LEDGER = Path(__file__).resolve().parent / "public_data_allowed.yaml"
_REPO = _SRC.parent.parent.parent

# Printer vendors / firmwares / adapters.  A compiled cross-vendor table
# names many of these; ordinary code that happens to dispatch on printer
# type names them too, which is why the vendor count ALONE is not the
# signal — it must co-occur with curated data fields (below).
_VENDORS: tuple[str, ...] = (
    "bambu", "bambulab", "prusa", "prusalink", "klipper", "moonraker",
    "octoprint", "marlin", "elegoo", "creality", "anycubic", "sovol",
    "voron", "ratrig", "flsun", "qidi", "raise3d", "ultimaker", "snapmaker",
)

# Curated-intelligence DATA fields — the hallmark of a compiled SME table.
# Matched ONLY as an assignment (``field=``) or a mapping key
# (``"field":``), never as an incidental word in prose, so a dispatcher
# that merely branches on printer type does not trip the gate.  Extend this
# list whenever a new curated-field type is introduced (it is the same
# private-tier field vocabulary the design-knowledge-split gate uses).
_SME_FIELDS: tuple[str, ...] = (
    "limitations",
    "recovery_methods",
    "failure_modes",
    "capabilities",
    "agent_guidance",
    "design_rules",
    "related_patterns",
    "holding_torque_n_m",
    "cycle_count_estimates",
    "insert_table",
    "ip_rating_guidance",
    "risk_thresholds",
    "score_deductions",
    "recommendation_rules",
    "geometry_score_rules",
    "captured_ball_variant",
    "latch_variants",
    "anti_walkout_lock_options",
    "pin_retention_variants",
    "supports_firmware_recovery",
    "supports_layer_resume",
    "supports_z_offset_resume",
)

_VENDOR_MIN = 3   # distinct vendor names → looks like a cross-vendor table
_FIELD_MIN = 2    # distinct curated data fields → looks like an SME table

# Structural exoneration: a file that routes through the design-knowledge
# split — it declares the private-tier fields as empty-by-default dataclass fields
# and pulls the curated VALUES from the private kiln-pro overlay at runtime
# (``_merge_pro_overlay_if_available`` / ``load_overlay``).  By construction
# such a file holds the public safety-floor only; the curated values live in the
# overlay.  This is the SANCTIONED pattern (``design_intelligence.py``,
# ``printer_intelligence.py``) and the opposite of a standalone hardcoded
# table like the ``resume_capabilities.py`` leak, which had no overlay merge.
_OVERLAY_LOADER_MARKERS: tuple[str, ...] = (
    "_merge_pro_overlay_if_available",
    "load_overlay(",
)

# Reviewed public-safe files: (relative-path substring, reason).  A file
# here carries the signature but is genuinely NOT private-tier data — a datasheet floor,
# schema boilerplate, or the tool-manifest mirror.  Adding an entry is a
# conscious tier decision, reviewed the same way the other leak-gate
# allowlists are.  Default to MOVING the table private instead.
_ALLOWLIST: tuple[tuple[str, str], ...] = ()


def _iter_sources():
    """Yield every public-source ``.py`` / ``.json`` file under kiln/src."""
    for ext in ("*.py", "*.json"):
        yield from _SRC.rglob(ext)


def _allowlisted(rel: str) -> bool:
    return any(sub in rel for sub, _reason in _ALLOWLIST)


def _is_overlay_loader(text: str) -> bool:
    """True when the file routes its private-tier fields through the pro-overlay
    split — a sanctioned loader holding only the public floor."""
    return any(m in text for m in _OVERLAY_LOADER_MARKERS)


def _distinct_vendors(text: str) -> set[str]:
    lowered = text.lower()
    return {v for v in _VENDORS if re.search(rf"\b{re.escape(v)}\b", lowered)}


def _distinct_fields(text: str) -> set[str]:
    """Curated fields present as an assignment or a mapping key."""
    found: set[str] = set()
    for f in _SME_FIELDS:
        # ``field =`` (python) or ``"field":`` / ``'field':`` (json/dict)
        if re.search(rf"(^|[^.\w]){re.escape(f)}\s*=", text) or re.search(
            rf"""["']{re.escape(f)}["']\s*:""", text
        ):
            found.add(f)
    return found


def scan() -> list[dict]:
    """Return one record per flagged file (compiled-SME-table signature)."""
    findings: list[dict] = []
    for path in sorted(_iter_sources()):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        vendors = _distinct_vendors(text)
        if len(vendors) < _VENDOR_MIN:
            continue
        fields = _distinct_fields(text)
        if len(fields) < _FIELD_MIN:
            continue
        rel = str(path.relative_to(_SRC.parent.parent))
        findings.append(
            {
                "file": rel,
                "vendors": sorted(vendors),
                "fields": sorted(fields),
                "overlay_loader": _is_overlay_loader(text),
                "allowlisted": _allowlisted(rel),
            }
        )
    return findings


def _is_leak(finding: dict) -> bool:
    return not finding["overlay_loader"] and not finding["allowlisted"]


# ---------------------------------------------------------------------------
# Bundled data files, judged as tables
# ---------------------------------------------------------------------------

#: One maker spelled two ways in ``_VENDORS`` counts once.
_VENDOR_ALIASES: dict[str, str] = {"bambulab": "bambu", "prusalink": "prusa"}

#: Row fields that carry a row's name.  Many printer ids name no maker
#: (``k1``, ``ender3``); the display name beside them does.
_NAME_FIELDS: tuple[str, ...] = (
    "display_name",
    "name",
    "manufacturer",
    "vendor",
    "brand",
    "model",
)

#: Limit-shaped field names, by what they hold.  Read against every key at
#: any depth inside a row, so a figure tucked into a sub-block still counts.
#: A field is reported under the first label it matches.
_LIMIT_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern))
    for label, pattern in (
        ("temperature", r"temp"),
        (
            "build volume / bed",
            (
                r"build_volume|build_area|build_plate|bed_shape|bed_size"
                r"|print_height|printable_area|plate_size"
            ),
        ),
        ("speed / acceleration / flow", r"feedrate|speed|accel|jerk|volumetric|flow"),
        ("hotend / extruder / nozzle", r"hotend|extruder|nozzle|direct_drive"),
        ("enclosure / chamber", r"enclos|chamber"),
        (
            "per-material recipe",
            r"^materials?$|recipe|supported_materials|compatible_materials",
        ),
        ("variants", r"variant"),
        ("notes", r"(^|_)notes?$|quirk|caveat|warnings?$"),
        ("ceiling / floor", r"^(max|min)_|limit"),
        ("capability flag", r"^(has|supports|can|requires)_"),
        ("firmware", r"firmware"),
        ("tolerance", r"tolerance"),
        ("upgrade", r"upgrade"),
    )
)
_SME_FIELD_LABEL = "curated SME field"

#: Base-polymer words.  A mapping keyed by several of them is a table per
#: material -- a recipe or a verdict for each -- whatever its key is called.
_MATERIAL_WORDS: frozenset[str] = frozenset(
    {
        "pla",
        "petg",
        "pet",
        "abs",
        "asa",
        "tpu",
        "tpe",
        "pa",
        "pa6",
        "pa12",
        "nylon",
        "pc",
        "polycarbonate",
        "peek",
        "pekk",
        "pei",
        "pps",
        "ppsu",
        "pp",
        "pva",
        "pvb",
        "hips",
    }
)
_MATERIAL_TABLE_MIN = 3

# A row that is a bare value carries its one field in the TABLE's name, so
# there is no field name to read.  A number or a note per printer is the
# per-model shape outright, and such a table is judged without the field
# count.
_PER_MATERIAL = "<a table keyed by material>"
_NUMBER_PER_PRINTER = "<a number per printer>"
_NOTE_PER_PRINTER = "<a note per printer>"

_FIELD_CLASSES = frozenset({"roster", "safety_floor", "standard_reference"})
_CLASSES = _FIELD_CLASSES | {"pending_move", "not_printer_keyed"}
_LEDGER_KEYS = frozenset({"classification", "why", "fields", "full_rows", "by"})

_manifest_module: Any = None


def _manifest() -> Any:
    """``kiln/data_manifest.py``, read from this tree by path.

    It owns the prose rule and the printer-keyed file list; the gate reads
    both rather than restating them.  Loaded by path so the gate judges this
    checkout, never an installed copy of Kiln.
    """
    global _manifest_module
    if _manifest_module is None:
        spec = importlib.util.spec_from_file_location(
            "_sme_gate_data_manifest", _SRC / "data_manifest.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _manifest_module = module
    return _manifest_module


def _name_tokens(text: str) -> set[str]:
    """Lowercase words, plus each adjacent pair joined ("rat rig" -> "ratrig")."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return set(words) | {a + b for a, b in itertools.pairwise(words)}


def _row_vendors(key: str, row: Any) -> set[str]:
    """Makers named by a row's key or its name fields, aliases collapsed."""
    parts = [key]
    if isinstance(row, dict):
        parts += [row[f] for f in _NAME_FIELDS if isinstance(row.get(f), str)]
    named = _name_tokens(" ".join(parts)) & set(_VENDORS)
    return {_VENDOR_ALIASES.get(v, v) for v in named}


def _tables(doc: Any) -> list[tuple[str, list[tuple[str, Any]]]]:
    """The tables a data file holds: the file itself, and each mapping or
    list one level down.  An underscore key names metadata, not a row."""

    def rows_of(node: Any) -> list[tuple[str, Any]]:
        if isinstance(node, dict):
            return [(str(k), v) for k, v in node.items() if not str(k).startswith("_")]
        if isinstance(node, list):
            return [("", v) for v in node]
        return []

    tables = [("<file>", rows_of(doc))]
    if isinstance(doc, dict):
        tables += [
            (str(k), rows_of(v)) for k, v in doc.items() if isinstance(v, (dict, list))
        ]
    return tables


def _keys_within(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            out.add(str(key))
            _keys_within(value, out)
    elif isinstance(node, list):
        for value in node:
            _keys_within(value, out)


def _holds_material_table(node: Any) -> bool:
    if isinstance(node, dict):
        keyed = sum(1 for k in node if _name_tokens(str(k)) & _MATERIAL_WORDS)
        if keyed >= _MATERIAL_TABLE_MIN:
            return True
        return any(_holds_material_table(v) for v in node.values())
    if isinstance(node, list):
        return any(_holds_material_table(v) for v in node)
    return False


def _bare_value_field(value: Any) -> str | None:
    """The pseudo-field a non-mapping row carries, or None for a name."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _NUMBER_PER_PRINTER
    if (
        isinstance(value, list)
        and value
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        return _NUMBER_PER_PRINTER
    if isinstance(value, str):
        prose, _figures = _manifest().scan_entry(value)
        return _NOTE_PER_PRINTER if prose else None
    return None


def _limit_label(field: str) -> str | None:
    if field in _SME_FIELDS:
        return _SME_FIELD_LABEL
    lowered = field.lower()
    for label, pattern in _LIMIT_SHAPES:
        if pattern.search(lowered):
            return label
    return None


def _judge_table(name: str, rows: list[tuple[str, Any]]) -> dict | None:
    """What the gate saw in one table, or None when it is not a cross-vendor
    per-printer limit table."""
    vendors: set[str] = set()
    for key, row in rows:
        vendors |= _row_vendors(key, row)
    if len(vendors) < _VENDOR_MIN:
        return None

    row_fields: set[str] = set()
    within: set[str] = set()
    pseudo: set[str] = set()
    for _key, row in rows:
        if isinstance(row, dict):
            row_fields.update(str(k) for k in row)
            _keys_within(row, within)
            if _holds_material_table(row):
                pseudo.add(_PER_MATERIAL)
        else:
            bare = _bare_value_field(row)
            if bare:
                pseudo.add(bare)

    limit: dict[str, list[str]] = {}
    for field in sorted(within):
        label = _limit_label(field)
        if label:
            limit.setdefault(label, []).append(field)
    if _PER_MATERIAL in pseudo:
        limit.setdefault("per-material recipe", []).append(_PER_MATERIAL)
    bare_values = sorted(pseudo - {_PER_MATERIAL})
    if bare_values:
        limit.setdefault("value per printer", []).extend(bare_values)

    count = sum(len(names) for names in limit.values())
    if count < _FIELD_MIN and not bare_values:
        return None
    return {
        "table": name,
        "rows": len(rows),
        "vendors": sorted(vendors),
        "row_fields": sorted(row_fields),
        "limit_fields": limit,
    }


def _flagged(doc: Any) -> list[tuple[dict, list[tuple[str, Any]]]]:
    """Each per-printer limit table in a parsed file, with its rows."""
    out = []
    for name, rows in _tables(doc):
        record = _judge_table(name, rows)
        if record is not None:
            out.append((record, rows))
    return out


def _read_data(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Every JSON under ``root`` parsed, and the ones that would not parse."""
    docs: dict[str, Any] = {}
    unreadable: dict[str, str] = {}
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root).as_posix()
        try:
            docs[rel] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            unreadable[rel] = f"{type(exc).__name__}: {exc}"
    return docs, unreadable


def load_ledger(path: Path = _LEDGER) -> dict[str, dict]:
    """The classified files, keyed by path relative to the data root."""
    import yaml  # a Kiln dependency; imported here so the text scan never needs it

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict):
        raise TypeError(f"{path.name} has no 'files' mapping")
    return files


def _as_date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _row_problems(row: Any) -> list[str]:
    """Why a ledger row cannot be judged, or [] when it is well formed."""
    if not isinstance(row, dict):
        return ["the row is not a mapping"]
    problems = []
    unknown = sorted(set(row) - _LEDGER_KEYS)
    if unknown:
        problems.append(f"unknown keys {unknown}")
    cls = row.get("classification")
    if cls not in _CLASSES:
        problems.append(f"classification {cls!r} is not one of {sorted(_CLASSES)}")
    if not (isinstance(row.get("why"), str) and row["why"].strip()):
        problems.append("no 'why'")
    fields = row.get("fields")
    if cls in _FIELD_CLASSES:
        if not (
            isinstance(fields, list)
            and fields
            and all(isinstance(f, str) for f in fields)
        ):
            problems.append(f"a {cls} row names its 'fields' (a list of field names)")
    elif fields is not None:
        problems.append(f"'fields' means nothing on a {cls} row")
    full_rows = row.get("full_rows")
    if full_rows is not None and (
        cls not in _FIELD_CLASSES
        or not isinstance(full_rows, list)
        or not all(isinstance(r, str) for r in full_rows)
    ):
        problems.append(
            "'full_rows' is a list of row keys on a roster / floor / reference row"
        )
    if cls == "pending_move":
        if _as_date(row.get("by")) is None:
            problems.append("a pending_move row needs an ISO 'by' date")
    elif row.get("by") is not None:
        problems.append(f"'by' means nothing on a {cls} row")
    return problems


def _field_class_failures(
    rel: str, row: dict, tables: list[tuple[dict, list[tuple[str, Any]]]]
) -> tuple[list[dict], set[str]]:
    """A roster / floor / reference row: each row carries only the named
    fields, the named full rows exist and are not printers, and every named
    field is still carried somewhere.  Also returns the fields the rows
    outside ``full_rows`` actually carry, for the report."""
    allowed = set(row["fields"])
    full = set(row.get("full_rows") or [])
    failures: list[dict] = []
    extra: dict[str, list[str]] = {}
    seen_keys: set[str] = set()
    carried: set[str] = set()
    for _record, rows in tables:
        for key, value in rows:
            seen_keys.add(key)
            if key in full:
                if _row_vendors(key, value):
                    failures.append(
                        {
                            "file": rel,
                            "kind": "full_row_is_a_printer",
                            "detail": f"full row {key!r} names a printer maker; only "
                            "the generic rows may stay whole",
                        }
                    )
                continue
            if isinstance(value, dict):
                carried |= set(value)
                over = sorted(set(value) - allowed)
            else:
                over = [_bare_value_field(value) or "<a bare value>"]
            if over:
                extra[key] = over
    if extra:
        fields = sorted({f for over in extra.values() for f in over})
        failures.append(
            {
                "file": rel,
                "kind": "extra_fields",
                "detail": f"{len(extra)} row(s) carry fields the ledger does not name: "
                f"{', '.join(fields)}",
                "rows": {k: extra[k] for k in sorted(extra)},
            }
        )
    missing_full = sorted(full - seen_keys)
    if missing_full:
        failures.append(
            {
                "file": rel,
                "kind": "stale",
                "detail": f"full_rows names rows the file no longer has: {missing_full}",
            }
        )
    unused = sorted(allowed - carried)
    if unused and carried:
        failures.append(
            {
                "file": rel,
                "kind": "stale",
                "detail": f"fields names fields no row carries: {unused}",
            }
        )
    return failures, carried


def judge_data(
    root: Path = _DATA,
    ledger: dict[str, dict] | None = None,
    *,
    ledger_path: Path = _LEDGER,
    today: datetime.date | None = None,
) -> dict:
    """Judge every data file under ``root`` against the ledger.

    Returns ``failures`` (each fails the gate), ``pending`` (pending_move
    rows inside their date, said every run), ``classified`` (every flagged
    file with its classification and what the gate saw) and ``notes``.
    """
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    failures: list[dict] = []
    if ledger is None:
        try:
            ledger = load_ledger(ledger_path)
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger judges nothing
            failures.append(
                {
                    "file": _shown(ledger_path),
                    "kind": "ledger",
                    "detail": f"cannot read the ledger ({type(exc).__name__}: {exc}); "
                    "every table below is judged unclassified",
                }
            )
            ledger = {}

    docs, unreadable = _read_data(root)
    for rel, why in unreadable.items():
        failures.append({"file": rel, "kind": "unreadable", "detail": why})

    flagged = {rel: _flagged(doc) for rel, doc in docs.items()}
    flagged = {rel: tables for rel, tables in flagged.items() if tables}

    pending: list[dict] = []
    classified: list[dict] = []
    for rel, tables in flagged.items():
        seen = [record for record, _rows in tables]
        row = ledger.get(rel)
        if row is None:
            failures.append(
                {
                    "file": rel,
                    "kind": "unclassified",
                    "detail": "a table keyed by several printer makers, carrying "
                    "limit-shaped fields, with no ledger row",
                    "tables": seen,
                }
            )
            continue
        problems = _row_problems(row)
        if problems:
            failures.append(
                {"file": rel, "kind": "malformed", "detail": "; ".join(problems)}
            )
            continue
        cls = row["classification"]
        entry = {"file": rel, "classification": cls, "why": row["why"], "tables": seen}
        if cls in _FIELD_CLASSES:
            entry["fields"] = list(row["fields"])
            entry["full_rows"] = list(row.get("full_rows") or [])
            found, carried = _field_class_failures(rel, row, tables)
            entry["rows_carry"] = sorted(carried)
            failures += found
        elif cls == "pending_move":
            by = _as_date(row["by"])
            assert by is not None  # _row_problems checked it
            entry["by"] = by.isoformat()
            entry["days_left"] = (by - today).days
            if today > by:
                failures.append(
                    {
                        "file": rel,
                        "kind": "past_due",
                        "detail": f"pending_move was due {by.isoformat()}: move the "
                        "data to kiln-pro, or re-decide the row",
                    }
                )
            else:
                pending.append(
                    {
                        "file": rel,
                        "by": by.isoformat(),
                        "days_left": (by - today).days,
                        "why": row["why"],
                    }
                )
        classified.append(entry)

    for rel in ledger:
        if rel in flagged or rel in unreadable:
            continue
        gone = not (root / rel).is_file()
        failures.append(
            {
                "file": rel,
                "kind": "stale",
                "detail": "the file is gone"
                if gone
                else "the file no longer carries a per-printer limit table; remove the row",
            }
        )

    return {
        "root": _shown(root),
        "failures": failures,
        "pending": pending,
        "classified": classified,
        "notes": _manifest_notes(flagged, ledger),
    }


def _manifest_notes(flagged: dict[str, Any], ledger: dict[str, dict]) -> list[str]:
    """Where this gate and ``data_manifest.PRINTER_KEYED_FILES`` disagree.

    The two ask different questions (keyed by printer id, versus carrying
    per-printer limits), so a disagreement is said, never failed.
    """
    keyed = set(_manifest().PRINTER_KEYED_FILES)
    notes = []
    for rel in sorted(flagged):
        cls = (ledger.get(rel) or {}).get("classification")
        if cls == "not_printer_keyed" and rel in keyed:
            notes.append(
                f"{rel}: classified not_printer_keyed here, but data_manifest "
                "lists it in PRINTER_KEYED_FILES"
            )
        elif cls in _CLASSES - {"not_printer_keyed"} and rel not in keyed:
            notes.append(
                f"{rel}: classified {cls} here (a per-printer table), but "
                "data_manifest does not list it in PRINTER_KEYED_FILES"
            )
    for rel in sorted(keyed - set(flagged)):
        notes.append(
            f"{rel}: data_manifest keys it by printer; this gate sees no "
            "cross-vendor per-printer limit table in it"
        )
    return notes


def _shown(path: Path) -> str:
    """A path as a reader of this repo would type it."""
    try:
        return Path(path).resolve().relative_to(_REPO.resolve()).as_posix()
    except ValueError:
        return str(path)


def _names(items: list[str], limit: int = 6) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit})" if len(items) > limit else "")


def _print_tables(tables: list[dict], indent: str = "      ") -> None:
    for t in tables:
        where = "" if t["table"] == "<file>" else f" [{t['table']}]"
        print(
            f"{indent}rows{where}: {t['rows']}, makers: {_names(t['vendors'])}; "
            f"row fields: {_names(t['row_fields'], 8)}"
        )
        for label, names in t["limit_fields"].items():
            print(f"{indent}  {label}: {_names(names)}")


def _print_data_report(result: dict) -> None:
    failures = result["failures"]
    classified = result["classified"]
    print(
        f"\npublic data tables ({result['root']}): {len(classified)} classified, "
        f"{len(result['pending'])} pending a move, {len(failures)} failing."
    )
    for entry in classified:
        cls = entry["classification"]
        if cls in _FIELD_CLASSES:
            extra = (
                f" -- full rows: {', '.join(entry['full_rows'])}"
                if entry["full_rows"]
                else ""
            )
            print(
                f"  {cls:<19} {entry['file']}  fields: {', '.join(entry['fields'])}{extra}"
            )
            print(f"      the other rows carry: {', '.join(entry['rows_carry']) or 'nothing'}")
        elif cls == "pending_move":
            print(
                f"  PENDING MOVE        {entry['file']}  to kiln-pro by {entry['by']} "
                f"({entry['days_left']} days): {entry['why']}"
            )
        else:
            print(f"  {cls:<19} {entry['file']}: {entry['why']}")
        _print_tables(entry["tables"])
    for note in result["notes"]:
        print(f"  NOTE {note}")
    if failures:
        print("\nPUBLIC DATA TABLE FAILURES:")
        for f in failures:
            print(f"  [{f['kind']}] {f['file']}: {f['detail']}")
            if f.get("rows"):
                for key, over in list(f["rows"].items())[:5]:
                    print(f"      {key}: {', '.join(over)}")
                if len(f["rows"]) > 5:
                    print(f"      ... and {len(f['rows']) - 5} more row(s)")
            if f.get("tables"):
                _print_tables(f["tables"])
        print(
            "\nFix: move a per-printer limit table to kiln-pro (one record per "
            "printer, served).  If the file is genuinely a roster, a safety "
            "floor or standard reference data, classify it in "
            "scripts/public_data_allowed.yaml with a reason and the fields it "
            "may carry; a dataset that should move gets pending_move with a date."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine output")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=_DATA,
        help="data tree to judge as tables (default: this checkout's kiln/src/kiln/data)",
    )
    parser.add_argument(
        "--ledger", type=Path, default=_LEDGER, help="classification ledger (YAML)"
    )
    args = parser.parse_args(argv)

    findings = scan()
    leaks = [f for f in findings if _is_leak(f)]
    exonerated = [f for f in findings if not _is_leak(f)]
    data = judge_data(args.data_root, ledger_path=args.ledger)
    failing = bool(leaks) or bool(data["failures"])

    if args.json:
        print(json.dumps({"leaks": leaks, "all": findings, "data": data}, indent=2))
        return 2 if failing else 0

    if not leaks:
        print(
            "public SME-table leak gate: clean — no compiled cross-vendor "
            f"SME tables in public source ({len(exonerated)} exonerated as "
            "overlay-split loaders / allowlisted)."
        )
    else:
        print(
            "PUBLIC SME-TABLE LEAK — compiled cross-vendor curated data in "
            "public source:\n"
        )
        for f in leaks:
            print(f"  {f['file']}")
            print(f"      vendors ({len(f['vendors'])}): {', '.join(f['vendors'])}")
            print(f"      fields  ({len(f['fields'])}): {', '.join(f['fields'])}")
        print(
            "\nFix: MOVE the table to the private tier (kiln-pro) or a pro "
            "overlay — do not allowlist it.  A curated cross-vendor capability / "
            "SME table is private-tier data; public source must not ship it.  If the file is "
            "genuinely a public floor (datasheet, schema, manifest mirror), add "
            "a reviewed (path, reason) entry to _ALLOWLIST."
        )
    _print_data_report(data)
    return 2 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
