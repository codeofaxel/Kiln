"""How a Bambu names an AMS tray — one rule, one place, with the evidence.

A Bambu printer names a tray by ONE integer everywhere Kiln meets it: the
``tray_now`` / ``tray_pre`` / ``tray_tar`` status fields, the ``target`` of
the ``ams_change_filament`` command, and each entry of ``ams_mapping`` on
the ``project_file`` print command.  The rule is fixed by the UNIT TYPE that
holds the tray, never by the printer model.  This module is the only place
Kiln computes it, in either direction, and the only place it turns one into
the name a person sees in Bambu Studio.

Sources
=======

* **Studio** — Bambu Studio ``v02.06.00.51`` (github.com/bambulab/BambuStudio,
  tag ``v02.06.00.51``, commit ``b506005``), the version installed as
  ``/Applications/BambuStudio.app`` beside this checkout.  Paths are under
  ``src/slic3r/GUI/``.
* **mqtt.md** — github.com/Doridian/OpenBambuAPI ``mqtt.md`` at commit
  ``cc383a2c`` (community LAN-protocol notes).
* **obn** — github.com/ClusterM/open-bamboo-networking ``src/print_job.cpp``
  at commit ``6aca2cb1`` (a drop-in replacement for the closed networking
  plugin, kept byte-compatible with the stock one on the wire).
* **wiki** — wiki.bambulab.com, read 2026-09-20: ``ams/manual/
  multi-model-AMS-compatibility-guide``, ``a1/manual/ams-connection-guide``,
  ``ams-lite/manual/faq``.
* **bundle** — the installed app's ``Resources/printers/<model>.json``.
* **bench** — the owner's A1 with ONE AMS Lite.  Nothing with a second unit,
  an AMS HT, an AMS 2 Pro or a second nozzle has been bench-verified.

Per unit type: how the printer numbers a tray
=============================================

==============  ==========  ===========================  =====================================================
Unit type       Unit ids    Tray id (the printer's)      Verified from
==============  ==========  ===========================  =====================================================
AMS (gen 1)     0-3 (0-15   ``unit * 4 + slot``,         Studio ``DeviceCore/DevMapping.cpp:127`` (``AMS``,
                accepted)   slot 0-3                     ``AMS_LITE``, ``N3F`` share one branch);
                                                         ``DeviceCore/DevFilaSystem.cpp:344-371``
                                                         (``GetTrayIndexMap``); ``DeviceManager.cpp:1542-1543``
                                                         (load target, ``ams_id < 16``); mqtt.md:251
                                                         ("ams 2 tray 2 would be (1*4)+1 = 5").
AMS Lite        0           same rule, unit 0 only:      the same three Studio sites; bench (A1: trays 0-3,
                            trays 0-3                    ``tray_now`` 0-3 / 254 / 255).
AMS 2 Pro       0-3         same rule                    Studio ``DevMapping.cpp:127`` names ``N3F`` ("AMS
(``n3f``)                                                2PRO", ``DevDefs.h:59``) in the ``* 4`` branch;
                                                         ``DeviceManager.cpp:863-877`` gives ``n3f`` ids 0-7.
AMS HT          128-135     ``unit`` itself (one slot,   Studio ``DevMapping.cpp:131`` (``N3S`` = "AMS HT",
(``n3s``)                   slot 0): HT-A is tray 128    ``DevDefs.h:60``: ``ams_id + tray_id``);
                                                         ``DeviceManager.cpp:880`` (``n3s_start_id = 128``);
                                                         ``DeviceCore/DevExtruderSystem.cpp:289-291``
                                                         (``tray_now`` 0x80-0x87 is the unit id);
                                                         ``DeviceManager.cpp:1542-1558`` (load target =
                                                         ``ams_id`` once ``ams_id >= 16``).
External spool  --          254 in ``tray_now`` and as   Studio ``DevDefs.h:86`` (``VIRTUAL_TRAY_DEPUTY_ID``);
                            the load target; NEVER in    ``DevExtruderSystem.cpp:282-286`` ("254 means loading
                            ``ams_mapping`` (there it    ext spool"); ``StatusPanel.cpp:4285`` (load target
                            is ``-1``)                   254); ``SelectMachine.cpp:1410`` (-1 in the flat
                                                         list); mqtt.md:251, :366 (``vt_tray.id`` 254).
No tray         --          255 in ``tray_now``; 255 as  Studio ``DevDefs.h:85`` (``VIRTUAL_TRAY_MAIN_ID``);
                            the load target = unload     ``DevExtruderSystem.cpp:277-281``;
                                                         ``DeviceManager.cpp:1552-1554`` (unload: target 255,
                                                         ``slot_id`` 255); bench (A1 idle reads 255).
==============  ==========  ===========================  =====================================================

Studio also keeps a SECOND number for an AMS HT, ``16 + (unit - 128)``
(``DevFilaSystem.cpp:240-259``, ``DevAms::GetTrayId``): it is the HT's bit in
``tray_exist_bits`` (``DevFilaSystem.cpp:799``; ``ams_exist_bits`` puts an HT
at bit ``4 + (unit - 128)``, ``:618``) and feeds one display path.  It never
goes on the wire; Kiln does not use it.

Per printer model: which units, hence which ids
===============================================

The arithmetic above has no model branch anywhere in Studio, so a model only
decides WHICH unit types it can carry and how many (all counts from the wiki
pages named above; the resulting id ranges follow from the table).

======================  =========================================  =================================  ===========
Model (Kiln id)         Units the vendor allows                    Tray ids that can appear           Verified
======================  =========================================  =================================  ===========
A1, A1 mini             ONE AMS Lite ("Each A series printer       0-3; or via the A1 AMS Hub 0-15    wiki (counts);
(``bambu_a1``,          supports only one AMS lite"; it cannot     and 128-131                        bench (one Lite,
``bambu_a1_mini``)      chain and cannot mix with the others);                                        unit 0);
                        OR, via the A1-series AMS Hub, up to 4                                        bundle (``N1``,
                        of AMS / AMS 2 Pro / AMS HT in any mix                                        ``N2S``:
                        (the type is switched on the screen)                                          ``use_ams_type
                                                                                                      "f1"``)
A2L (``bambu_a2l``)     "up to 4 AMS units and 1 AMS lite"         0-15 and 128-131                   wiki (one line;
                        (the A2L connection guide was not read)                                       unverified
                                                                                                      beyond it)
P1P, P1S, X1, X1C, X1E  up to 4 units of AMS / AMS 2 Pro / AMS HT   0-15 and 128-131                   wiki; Studio
                        via the AMS Hub ("4 units in total")                                          (unit ids 0-3
                                                                                                      = A-D)
P2S (``bambu_p2s``)     up to 4 AMS 2 Pro + 4 AMS HT ("8 units in  0-15 and 128-131                   wiki; no
                        total with 20 slots"); one nozzle                                             P2S-specific
                                                                                                      branch in Studio
H2S (``bambu_h2s``)     AMS 2 Pro + AMS HT, one nozzle             0-15 and 128-135                   wiki (H2 guide);
                                                                                                      unverified
H2D, H2D Pro, H2C,      up to 4 AMS 2 Pro + 8 AMS HT + an           0-15 and 128-135; TWO external     wiki; Studio
X2D (``bambu_h2d``,     external spool per nozzle; two nozzles     spools: 255 = right/main, 254 =    (``DevDefs.h``,
``bambu_h2d_pro``,                                                 left/deputy in ``ams_mapping2``    ``DeviceManager
``bambu_h2c``,                                                     and ``vir_slot``                   .cpp:3368-3396``);
``bambu_x2d``)                                                                                        unverified
======================  =========================================  =================================  ===========

Where each field carries the id
===============================

``tray_now`` / ``tray_pre`` / ``tray_tar`` (status, one-nozzle machines)
    The tray id as above; Studio reads it in ``DevExtruderSystem.cpp:224-300``
    (``ParseV1_0``): 255 → nothing, 254 → external spool, 0x80-0x87 → that AMS
    HT, anything else → ``>> 2`` / ``& 3``.  Kiln mirrors that exactly, except
    that 64-127 and 136-253 (ids no unit can own) read as "not a tray"
    rather than as a unit.
``extruder.info[].snow`` / ``star`` / ``spre`` (status, two-nozzle machines)
    One integer per nozzle, ``(unit << 8) | slot`` (``DevExtruderSystem.cpp:
    313-363``, ``ParseV2_0``); the external spool is unit 255 (right) or 254
    (left), slot 0.  Studio ignores ``ams.tray_now`` once a machine reports
    more than one extruder (``ParseV1_0`` returns early).  Whether H2-series
    firmware still fills ``ams.tray_now`` is unverified; Kiln has no H2
    adapter path that reads ``extruder.info``.
``ams_change_filament.target`` (the load / unload command)
    The tray id as above (``DeviceManager.cpp:1537-1573``).  Studio sends
    ``ams_id`` and ``slot_id`` beside ``target`` (and ``extruder_id`` on a
    two-nozzle machine); on firmware that sets ``flag3`` bit 9
    (``is_enable_ams_np``, ``DeviceManager.cpp:2973``) the external spool is
    asked for as ``target 255 + slot_id 0`` and unload as ``target 255 +
    slot_id 255``, while legacy firmware takes ``target 254`` for the spool
    (``StatusPanel.cpp:4281-4286``).  Kiln sends ``target`` alone: 254 for
    the external spool (bench-verified on the A1), 255 to unload; the
    new-protocol spool form is unverified and not sent.
``ams_mapping`` (print command, flat list)
    One entry per filament of the project, the tray id as above; ``-1`` for
    an unmapped filament AND for either external spool
    (``SelectMachine.cpp:1348-1432``; obn:369-370; mqtt.md:859-905).  254 and
    255 never appear here.
``ams_mapping2`` (print command, list of objects)
    One ``{"ams_id": unit, "slot_id": slot}`` per filament; ``{255, 255}`` for
    an unmapped one, ``{255, 0}`` / ``{254, 0}`` for the right / left external
    spool (``SelectMachine.cpp:1386-1420``).  Sent by the stock plugin on
    every print, as ``[]`` when unused (obn:373-391).  Kiln does not send it.
Unit type on the wire
    ``ams[].info`` bits 0-3: 1 AMS, 2 AMS Lite, 3 AMS 2 Pro, 4 AMS HT; bits
    8-11 the nozzle the unit feeds (``DevFilaSystem.cpp:556-583``).  Older
    firmware omits ``info``; Studio then calls a unit on an A-series printer
    (bundle ``use_ams_type "f1"``) an AMS Lite.  Kiln keys on the unit id
    alone, which the type ranges above make sufficient.
Names people see
    ``A1``-``D4`` (unit letter + slot number), ``HT-A``-``HT-H``, ``Ext`` /
    ``Ext-L`` / ``Ext-R`` (``GUI_App.cpp:4325-4356``, ``transition_tridid``);
    the printer's screen calls the units ``AMS-A``… and ``HT-A``… (wiki, A1
    AMS connection guide, "AMS ID Assignment").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "AMS_HT_UNIT_IDS",
    "CHAINED_UNIT_IDS",
    "EXTERNAL_SPOOL_TRAY",
    "NO_TRAY",
    "TRAYS_PER_UNIT",
    "TrayRef",
    "describe_tray_id",
    "read_tray_id",
    "tray_id",
    "tray_name",
    "unit_name",
]

#: Slots on a chained unit (AMS, AMS Lite, AMS 2 Pro).  An AMS HT has one.
TRAYS_PER_UNIT = 4
#: ``tray_now`` / load ``target`` for the external spool holder.
EXTERNAL_SPOOL_TRAY = 254
#: ``tray_now`` for "nothing feeding"; the load ``target`` that unloads.
NO_TRAY = 255
#: Unit ids a chained unit can hold: Studio's ``ams_id < 16`` guard on the
#: ``* 4`` rule.  The vendor allows four per printer (A-D).
CHAINED_UNIT_IDS = range(0, 16)
#: Unit ids an AMS HT holds (``n3s_start_id`` 128, eight of them).
AMS_HT_UNIT_IDS = range(128, 136)

_LETTERS = "ABCDEFGHIJKLMNOP"


def tray_id(unit: int, slot: int) -> int:
    """The printer's id for *slot* of *unit*.

    ``unit * 4 + slot`` on a chained unit (ids 0-15); the unit id itself on
    an AMS HT (128-135), whose one slot is 0.  Any other pair is not a tray
    a Bambu can name, and is refused rather than turned into a number.
    """
    unit = int(unit)
    slot = int(slot)
    if unit in AMS_HT_UNIT_IDS:
        if slot != 0:
            raise ValueError(f"an AMS HT (unit {unit}) has one slot, 0; slot {slot} does not exist")
        return unit
    if unit in CHAINED_UNIT_IDS and slot >= 0:
        return unit * TRAYS_PER_UNIT + slot
    raise ValueError(f"no Bambu AMS unit has id {unit} (chained units are 0-15, AMS HT 128-135)")


@dataclass(frozen=True)
class TrayRef:
    """A tray id read off the wire, resolved to what it names.

    ``unit`` / ``slot`` are set for a real tray; both are ``None`` for the
    external spool (254) and for "no tray" (255).
    """

    tray_id: int
    unit: int | None
    slot: int | None

    @property
    def external(self) -> bool:
        return self.tray_id == EXTERNAL_SPOOL_TRAY

    @property
    def none(self) -> bool:
        return self.tray_id == NO_TRAY

    @property
    def loaded_tray(self) -> bool:
        """A tray on a unit — something ``ams_mapping`` and the load command can name."""
        return self.unit is not None

    @property
    def name(self) -> str:
        """Studio's own name: ``B2``, ``HT-A``, ``Ext``; empty for "no tray"."""
        if self.unit is not None and self.slot is not None:
            return tray_name(self.unit, self.slot)
        return "Ext" if self.external else ""


def read_tray_id(value: Any) -> TrayRef | None:
    """Resolve a ``tray_now`` / ``target`` / ``ams_mapping`` value; ``None`` when it is not one.

    Studio's reading (``ParseV1_0``): 255 is nothing, 254 the external
    spool, 128-135 an AMS HT, and everything else ``>> 2`` / ``& 3``.  Ids
    that no unit the vendor sells can own (64-127, 136-253, anything
    negative or past 255) come back ``None`` instead of a made-up unit.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        value = int(text)
    if not isinstance(value, int):
        return None
    if value in (EXTERNAL_SPOOL_TRAY, NO_TRAY):
        return TrayRef(value, None, None)
    if value in AMS_HT_UNIT_IDS:
        return TrayRef(value, value, 0)
    if 0 <= value < len(CHAINED_UNIT_IDS) * TRAYS_PER_UNIT:
        unit, slot = divmod(value, TRAYS_PER_UNIT)
        return TrayRef(value, unit, slot)
    return None


def unit_name(unit: int) -> str:
    """``A``-``P`` for a chained unit, ``HT-A``-``HT-H`` for an AMS HT."""
    unit = int(unit)
    if unit in AMS_HT_UNIT_IDS:
        return "HT-" + _LETTERS[unit - AMS_HT_UNIT_IDS.start]
    if unit in CHAINED_UNIT_IDS:
        return _LETTERS[unit]
    raise ValueError(f"no Bambu AMS unit has id {unit}")


def tray_name(unit: int, slot: int) -> str:
    """The name Studio shows for a tray: ``A1``…``D4``, or ``HT-A`` (one slot)."""
    tray_id(unit, slot)  # refuse an impossible pair the same way
    unit = int(unit)
    if unit in AMS_HT_UNIT_IDS:
        return unit_name(unit)
    return f"{unit_name(unit)}{int(slot) + 1}"


def describe_tray_id(value: Any) -> str:
    """A tray id for a sentence: ``tray 5 (slot B2)``, ``the external spool``, ``no tray``."""
    ref = read_tray_id(value)
    if ref is None:
        return f"tray {value!r} (not an id this printer uses)"
    if ref.external:
        return "the external spool"
    if ref.none:
        return "no tray"
    return f"tray {ref.tray_id} (slot {ref.name})"
