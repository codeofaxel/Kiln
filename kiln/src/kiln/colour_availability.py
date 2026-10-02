"""Whether the colours a person chose are on their printer — and nothing more.

When someone colours or prints a model in red, there are four honest
answers, and only two of them are worth a word:

* red is **loaded** on their printer — say nothing about filament;
* Kiln has a red spool on record in their inventory (``add_spool``) but it
  is not loaded — say so, and that they can load it;
* Kiln knows of no red they own and none is loaded — ask, once, whether
  they want help finding a filament that suits the print.  No names, no
  links, no list until they say yes;
* Kiln cannot see what is loaded (no printer, no reading of the loaded
  colours, the hosted server) — say nothing at all.

Kiln never reads as if it is selling filament: what to buy is offered only
when the person asks or has nothing that will do, and then only as a
question.  A yes is answered by ``find_closest_filaments``, a separate tool
the person reaches by asking; nothing here names a filament they do not
own.

**Loaded** is :func:`kiln.server._spool_advisory`, the printer read and
the colour matcher the print gate uses (:func:`kiln.ams_routing.advise_colours`
at :data:`kiln.ams_routing.MATCH_DELTA_E`), so this never disagrees with
what the print will do.  **Owned** is a spool on record whose colour is a
code, or a colour name with one standard code (a CSS colour name, read from
Pillow's table, never guessed from a word inside a longer name: "Galaxy
Black" is not resolved), within the same distance; a spool already
standing in for a loaded tray is not on the shelf, an empty spool is not
owned (:mod:`kiln.spool_usage` counts a spool down as it prints, and the
person saying they have run out empties it), and a spool whose material
clearly differs from the print's is not owned for this print.

The colouring doors attach the answer through
:func:`attach_colour_availability`; the print doors word a colour the
loaded spools cannot supply through :func:`not_loaded_say`.  Both speak
with the one composer below, so a person hears the same sentence at the
moment they choose a colour and at the moment a print would need it.
Nothing here raises.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from kiln.ams_routing import MATCH_DELTA_E, _colour_name
from kiln.colour_distance import delta_e_76, hex_to_lab, normalize_hex

logger = logging.getLogger(__name__)

__all__ = [
    "LOADED",
    "MISSING",
    "OWNED",
    "attach_colour_availability",
    "colour_availability",
    "not_loaded_say",
]

#: The three states a requested colour can be in, when Kiln can see.
LOADED = "loaded"
OWNED = "owned"
MISSING = "missing"

#: The advisory verdicts that are a reading of the loaded colours.  Every
#: other answer (no printer, a failed read, colours the printer did not
#: report, a unit with nothing reported loaded) is Kiln not seeing.
_SEEN_VERDICTS = frozenset({"true", "mismatch"})

_NAME_SEPARATORS = re.compile(r"[\s_\-]+")


@dataclass(frozen=True)
class _Colour:
    """One requested colour: its code, the name it was given, the print's material."""

    hex6: str
    given_name: str | None = None
    material: str | None = None

    @property
    def code(self) -> str:
        return f"#{self.hex6}"

    @property
    def words(self) -> str:
        """The colour as a person names it: as given, else a known name, else
        the plain name the print gate's own messages use for this code."""
        return self.given_name or _known_name(self.hex6) or _colour_name(self.hex6)


# ---------------------------------------------------------------------------
# The two doors
# ---------------------------------------------------------------------------


def colour_availability(
    colours: list[str | None],
    *,
    printer_name: str | None = None,
    adapter: Any = None,
    material: str | None = None,
) -> dict[str, Any] | None:
    """Whether each of *colours* is loaded on the printer, owned, or neither.

    ``None`` when Kiln cannot see what is loaded on that printer (no
    printer, no reading of its loaded colours, the hosted server) or when
    *colours* holds no colour: the caller then says nothing.  Otherwise::

        {"printer": "<as a person calls it>",
         "colours": [{"colour": "#RRGGBB", "state": "loaded", "slot": "slot A2"},
                     {"colour": "#RRGGBB", "state": "owned", "spool": "<as recorded>"},
                     {"colour": "#RRGGBB", "state": "missing"}],
         "say": "<'' when every colour is loaded, else the sentences to relay as written>"}

    One entry per distinct usable colour, in the order asked.  A colour may
    be a code or a CSS colour name.  *printer_name* may be a registered
    printer or a printer model id (an unregistered name asks the default
    printer); *adapter*, when given, is the printer read.  *material* is
    the print's material when the caller knows it: a recorded spool of a
    clearly different material is not counted as owned for this print.
    """
    try:
        if _hosted():
            return None
        wanted = _requested(colours, material)
        if not wanted:
            return None
        from kiln import server

        advisory = server._spool_advisory(
            [c.code for c in wanted],
            printer_name=printer_name,
            adapter=adapter,
        )
        if not isinstance(advisory, dict) or advisory.get("verdict") not in _SEEN_VERDICTS:
            return None
        loaded = {entry.get("color"): entry for entry in advisory.get("matched") or () if isinstance(entry, dict)}
        printer = _printer_words(advisory.get("printer"))
        not_loaded = [c for c in wanted if c.code not in loaded]
        tray_codes = [entry.get("nearest_color") for entry in loaded.values()]
        owned = _owned_spools(not_loaded, tray_codes)

        entries: list[dict[str, Any]] = []
        for colour in wanted:
            if colour.code in loaded:
                entries.append({"colour": colour.code, "state": LOADED, "slot": _where(loaded[colour.code])})
            elif colour.code in owned:
                entries.append({"colour": colour.code, "state": OWNED, "spool": _spool_words(owned[colour.code])})
            else:
                entries.append({"colour": colour.code, "state": MISSING})
        return {
            "printer": printer,
            "colours": entries,
            "say": _say(
                [(c, owned[c.code]) for c in not_loaded if c.code in owned],
                [c for c in not_loaded if c.code not in owned],
                printer,
            ),
        }
    except Exception:  # noqa: BLE001 -- a colouring is never failed by this
        logger.debug("colour availability not worked out", exc_info=True)
        return None


def attach_colour_availability(
    response: dict[str, Any],
    colours: Iterable[str | None] | None,
    *,
    printer_name: str | None = None,
    adapter: Any = None,
    material: str | None = None,
) -> None:
    """Set ``response["colour_availability"]``, or leave *response* alone.

    The one call every colouring door makes once its response is built.
    Nothing is set when :func:`colour_availability` has nothing to say.
    Never raises: a good colouring is never failed by it.
    """
    try:
        answer = colour_availability(
            list(colours or ()),
            printer_name=printer_name,
            adapter=adapter,
            material=material,
        )
    except Exception:  # noqa: BLE001 -- belt and braces; the helper never raises
        logger.debug("colour availability not attached", exc_info=True)
        return
    if answer is not None:
        response["colour_availability"] = answer


def not_loaded_say(
    filaments: Sequence[Any],
    *,
    printer_name: str | None = None,
    loaded: Iterable[str | None] = (),
    offer: bool = True,
) -> str:
    """What to tell a person about colours the loaded spools cannot supply.

    For the print doors, which have already matched the file against the
    printer: *filaments* are the ones nothing loaded can supply (each with
    ``hex6`` and ``material``, as :class:`kiln.ams_routing.Filament`), and
    *loaded* the colour codes of the spools the print uses.  Worded exactly
    as a colouring would word them.  *offer* False leaves the question off,
    for a surface where nobody can answer one.  ``""`` when there is
    nothing to say: no colour among *filaments*, or the hosted server.
    """
    try:
        if _hosted():
            return ""
        wanted: list[_Colour] = []
        for filament in filaments:
            hex6 = normalize_hex(getattr(filament, "hex6", None))
            if hex6 and all(c.hex6 != hex6 for c in wanted):
                wanted.append(_Colour(hex6, None, getattr(filament, "material", None)))
        if not wanted:
            return ""
        owned = _owned_spools(wanted, list(loaded))
        return _say(
            [(c, owned[c.code]) for c in wanted if c.code in owned],
            [c for c in wanted if c.code not in owned],
            _printer_words(printer_name),
            offer=offer,
        )
    except Exception:  # noqa: BLE001 -- the print door words its own fallback
        logger.debug("not-loaded sentence not worked out", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# The sentences
# ---------------------------------------------------------------------------


def _say(
    owned: list[tuple[_Colour, Any]],
    missing: list[_Colour],
    printer: str,
    *,
    offer: bool = True,
) -> str:
    """At most two short sentences.  ``""`` when nothing is unloaded."""
    if not owned and not missing:
        return ""
    sentences: list[str] = []
    if owned:
        one = len(owned) == 1
        # One spool is named as the person recorded it; several are named
        # by colour and material only, so a sentence never becomes a list
        # of brands.
        spools = _listed([_spool_words(spool, brand=one) for _c, spool in owned], "and")
        isnt, it = ("it isn't", "it") if one else ("they aren't", "them")
        if missing:
            sentences.append(f"You have {spools} but {isnt} loaded on {printer}, so load {it} before printing.")
        else:
            sentences.append(f"You have {spools} but {isnt} loaded on {printer}. Load {it} before printing.")
    if missing:
        # Two near shades share a plain name; a person hears it once.
        colours = _listed(list(dict.fromkeys(c.words for c in missing)), "or")
        where = "there either" if owned else f"on {printer}"
        question = (
            "want help finding a filament that suits this print?"
            if len(missing) == 1
            else "want help finding filaments that suit this print?"
        )
        if not offer:
            sentences.append(f"I don't see {colours} loaded {where}.")
        elif owned:
            sentences.append(f"I don't see {colours} loaded {where}; {question}")
        else:
            sentences.append(f"I don't see {colours} loaded {where}. {question[0].upper()}{question[1:]}")
    return " ".join(sentences)


def _listed(words: list[str], conjunction: str) -> str:
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} {conjunction} {words[-1]}"


def _spool_words(spool: Any, *, brand: bool = True) -> str:
    """A spool as the person recorded it: brand, colour, material."""
    parts = [
        getattr(spool, "brand", None) if brand else None,
        getattr(spool, "color", None),
        getattr(spool, "material_type", None),
    ]
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def _where(entry: dict[str, Any]) -> str:
    """Where a loaded colour is, in the printer's own words for the slot."""
    where = entry.get("where")
    if isinstance(where, str) and where:
        return where
    name = entry.get("slot_name")
    return f"slot {name}" if name else "a loaded slot"


def _printer_words(registered_name: str | None) -> str:
    """The printer as a person calls it (never Kiln's ``default`` alias)."""
    try:
        from kiln.server import _printer_label_for_a_person

        return _printer_label_for_a_person(registered_name)
    except Exception:  # noqa: BLE001
        name = str(registered_name or "").strip()
        return name if name and name.lower() != "default" else "your printer"


# ---------------------------------------------------------------------------
# Colours and spools
# ---------------------------------------------------------------------------


def _requested(colours: Any, material: str | None) -> list[_Colour]:
    """The distinct usable colours in *colours*, in order; others dropped."""
    if isinstance(colours, str):
        colours = [colours]
    out: list[_Colour] = []
    try:
        items = list(colours or ())
    except TypeError:
        return out
    for raw in items:
        resolved = _resolve_colour(raw)
        if resolved is None:
            continue
        hex6, name = resolved
        if all(c.hex6 != hex6 for c in out):
            out.append(_Colour(hex6, name, material))
    return out


def _resolve_colour(value: Any) -> tuple[str, str | None] | None:
    """``(RRGGBB, name)`` for a colour code or a CSS colour name; else ``None``.

    A name resolves only when the whole name is one CSS colour name
    (spacing, hyphens and case aside).  Nothing is guessed from a word
    inside a longer name.
    """
    if not isinstance(value, str):
        return None
    hex6 = normalize_hex(value)
    if hex6 is not None:
        return hex6, None
    key = _NAME_SEPARATORS.sub("", value).lower()
    if not key.isalpha():
        return None
    try:
        from PIL import ImageColor

        if key not in ImageColor.colormap:
            return None
        r, g, b = ImageColor.getrgb(key)[:3]
    except Exception:  # noqa: BLE001 -- without the table, a name does not resolve
        return None
    return f"{r:02X}{g:02X}{b:02X}", " ".join(value.split()).lower()


def _known_name(hex6: str) -> str | None:
    """The name Kiln's colouring tools give this exact code, or ``None``."""
    try:
        from kiln.plugins.color_tools import _hex_to_color_name

        name = _hex_to_color_name(f"#{hex6}")
    except Exception:  # noqa: BLE001
        return None
    return None if not name or name.startswith("#") else name


def _distance(a: str, b: str) -> float:
    """The planner's measure (CIE76), so owned and loaded share one edge."""
    return delta_e_76(hex_to_lab(a), hex_to_lab(b))


def _material_family(raw: Any) -> str | None:
    """A material Kiln can name (``PLA``, ``PETG`` ...) from what was written, or ``None``.

    The whole string first, then its first word ("PLA Basic" is PLA,
    "PETG-CF" is PETG).  ``None`` means "cannot judge", never "differs".
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    from kiln.materials import normalise_material_type

    family = normalise_material_type(raw)
    if family is None:
        first = re.split(r"[^A-Za-z0-9]+", raw.strip())[0]
        family = normalise_material_type(first)
    return family


def _clearly_differs(print_material: str | None, spool_material: str | None) -> bool:
    ours, theirs = _material_family(print_material), _material_family(spool_material)
    return ours is not None and theirs is not None and ours != theirs


def _recorded_spools() -> list[tuple[Any, str]]:
    """Spools on record that have filament left and a colour that resolves."""
    try:
        from kiln.server import _get_material_tracker

        spools = _get_material_tracker().list_spools()
    except Exception:  # noqa: BLE001 -- no inventory is no owned spool
        logger.debug("spool inventory not readable", exc_info=True)
        return []
    out: list[tuple[Any, str]] = []
    for spool in spools:
        remaining = getattr(spool, "remaining_grams", None)
        if isinstance(remaining, (int, float)) and remaining <= 0:
            continue
        resolved = _resolve_colour(getattr(spool, "color", None))
        if resolved is not None:
            out.append((spool, resolved[0]))
    return out


def _owned_spools(colours: list[_Colour], loaded_codes: Sequence[str | None]) -> dict[str, Any]:
    """``{"#RRGGBB": spool}`` for each of *colours* a spool on the shelf covers.

    Spools are paired best-first, each spool once, as the print gate pairs
    trays.  The spools already loaded (closest to *loaded_codes*) are taken
    off the shelf first, so a red spool in the printer is never also "a red
    you have but haven't loaded".
    """
    if not colours:
        return {}
    spools = _recorded_spools()
    if not spools:
        return {}
    taken: set[int] = set()
    in_use = [code for code in (normalize_hex(c) for c in loaded_codes) if code]
    _pair_best_first(in_use, spools, taken)
    paired = _pair_best_first(
        [c.hex6 for c in colours],
        spools,
        taken,
        fits=lambda i, spool: not _clearly_differs(colours[i].material, getattr(spool, "material_type", None)),
    )
    return {colours[i].code: spools[j][0] for i, j in paired.items()}


def _pair_best_first(
    targets: list[str],
    spools: list[tuple[Any, str]],
    taken: set[int],
    *,
    fits: Callable[[int, Any], bool] = lambda _i, _spool: True,
) -> dict[int, int]:
    """Pair targets with untaken spools within the match distance, closest first."""
    scored = sorted(
        (delta, i, j)
        for i, target in enumerate(targets)
        for j, (spool, code) in enumerate(spools)
        if j not in taken and fits(i, spool) and (delta := _distance(target, code)) <= MATCH_DELTA_E
    )
    chosen: dict[int, int] = {}
    for _delta, i, j in scored:
        if i in chosen or j in taken:
            continue
        chosen[i] = j
        taken.add(j)
    return chosen


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001 -- unsure is treated as hosted: say nothing
        return True
