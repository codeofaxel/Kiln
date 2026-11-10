"""Whether this install can order prints from a print service.

Ordering a print from a print service (its materials, a quote, placing,
tracking or cancelling an order, the order history) runs through Kiln's
order service.  It ships with kiln-pro (https://kiln3d.com), which makes it
importable as ``kiln.fulfillment``; a plain ``pip install kiln3d`` has none.

Every ordering door -- the ``kiln order`` commands, ``kiln
fulfillment-materials``, the outsourced half of ``kiln compare-cost``, and
the ``fulfillment_*`` and ``compare_print_options`` agent tools -- asks
:func:`order_service` first.  When the answer is ``None`` the door orders
THROUGH Kiln's servers instead (:mod:`kiln._pro_order_bridge`, over the
same routes Kiln's hosted connector uses), and says a miss in one of the
four causes.  :data:`NOT_INCLUDED` is said only when the servers the install
asks offer no ordering at all (an operator's own server address without
the routes).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

#: Where ordering is set up for an install without the order service: the
#: page for Kiln's hosted connector, which prices, places and tracks orders.
CONNECTOR_DOCS_URL = "https://kiln3d.com/docs/connector"

#: The code shown beside :data:`NOT_INCLUDED`.
NOT_INCLUDED_CODE = "NOT_AVAILABLE"

#: The one sentence every ordering door says on an install without the
#: order service.  It says where ordering lives; it does not promise that an
#: order placed there will go through.
NOT_INCLUDED = (
    "This install of Kiln doesn't include ordering prints from a print service, "
    "so nothing was sent to one. For quotes and orders, see Kiln's hosted "
    f"connector: {CONNECTOR_DOCS_URL}"
)


#: Cancel refusals the order service words for the person in full: there is
#: nothing to cancel (an unpaid order is never made), or the print service
#: cancels only on a written request and the refusal says where to write.
#: Every door says them as they are, never wrapped in "Failed to cancel".
WORDED_CANCEL_REFUSALS = frozenset({"NOTHING_TO_CANCEL", "CANCEL_NOT_SUPPORTED"})


def order_service() -> ModuleType | None:
    """The order service when this install has one, else ``None``."""
    try:
        import kiln.fulfillment  # noqa: F401
    except ImportError:
        return None
    return sys.modules["kiln.fulfillment"]


def fee_basis(priced: object) -> float | None:
    """The amount Kiln's fee is charged on: the printing, and nothing else.

    Kiln's fee on an order is charged on the production amount (the printing,
    plus any print shop's minimum per order), never on shipping or tax, at
    every door that quotes or charges it.  Quotes and placed orders both state
    that amount as ``production_price``.  A placed order also states its
    checkout total, which carries shipping and tax; that total is never read
    here.  ``None`` when the print service did not state a production amount:
    the fee is then not worked out, rather than worked out on the wrong
    amount.
    """
    value = getattr(priced, "production_price", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def server_takes_fee(provider: object) -> bool:
    """Whether orders placed through ``provider`` have Kiln's fee charged by
    Kiln's hosted server, so nothing on this machine may record or charge it.

    The provider says so itself (``charges_kiln_fee``): Kiln's hosted
    connector charges the fee on the server when it places the order.  This
    is the provider's own contract, never a guess from which keys are set.
    A door that charges before placing (the agent order tool) asks this; the
    order it gets back then confirms it (:func:`fee_taken_by_server`).
    """
    return getattr(provider, "charges_kiln_fee", False) is True


def fee_taken_by_server(order: object) -> dict | None:
    """Kiln's fee as the hosted server already charged it, or ``None``.

    An order placed through Kiln's hosted connector comes back saying what
    the server charged (``kiln_fee_charged``).  Every door that records or
    charges Kiln's fee on this machine asks this first: when it answers, the
    door shows that fee, once, and records and charges nothing itself, so
    the fee is never taken twice.  ``None`` means no server took it, and the
    door settles the fee as it always has.
    """
    fee = getattr(order, "kiln_fee_charged", None)
    return dict(fee) if isinstance(fee, dict) else None


# ---------------------------------------------------------------------------
# which way up the model a print service prices stands
# ---------------------------------------------------------------------------

#: The folder, in Kiln's own temp folder, that keeps the standing copy of a
#: model a print service is asked to price.
STANDING_COPIES = "standing-for-quote"


@dataclass(frozen=True)
class PricedFile:
    """The file a print service is asked to price for a model a door was given.

    ``path`` is the file to send: the one named (``sent``), or Kiln's
    standing copy of it when the model arrived lying down.  The file named
    is never rewritten.  ``pose`` is Kiln's record of which way up it stands
    (:func:`kiln.arrival_pose.stand_up`); ``{"status": "as_sent"}`` when the
    person asked for the file exactly as sent.
    """

    path: str
    sent: str
    pose: dict[str, Any] = field(default_factory=dict)

    @property
    def stood_up(self) -> bool:
        """Whether the file priced is Kiln's standing copy."""
        return self.path != self.sent

    def line(self) -> str:
        """The first thing the person is told about the quote, or ``""``."""
        return quote_pose_line(self.pose)

    def block(self) -> dict[str, Any]:
        """The pose on a quote's reply (:func:`quote_pose_block`), with
        ``priced_file`` when the file priced is the standing copy."""
        block = quote_pose_block(self.pose)
        if self.stood_up:
            block["priced_file"] = self.path
        return block


def file_to_price(file_path: str, *, keep_as_sent: bool = False) -> PricedFile:
    """The file every door that asks a print service for a price sends.

    A model that arrived lying down is priced standing: Kiln judges which
    way up it goes (:func:`kiln.arrival_pose.stand_up`) and, when it is sure
    of a turn, prices a turned copy kept in Kiln's own temp folder, which
    carries the file's note.  The file named is only read, whatever the
    answer.  *keep_as_sent* is the person's own call to have the file priced
    exactly as it is.  Never raises: a file Kiln cannot read or judge is
    priced as sent, and the record says why.
    """
    from kiln.arrival_pose import TURNABLE_TYPES, TURNED, UNJUDGED, stand_up

    sent = os.path.abspath(os.path.expanduser(str(file_path or "")))
    if keep_as_sent:
        return PricedFile(sent, sent, {"status": "as_sent"})
    kind = os.path.splitext(sent)[1].lower().lstrip(".")
    if kind not in TURNABLE_TYPES or not os.path.isfile(sent):
        return PricedFile(sent, sent, {})
    try:
        from kiln.output_paths import kiln_output_dir
        from kiln.preview_gate import hash_file

        # One folder per model's bytes, so the copy keeps the model's own
        # file name for the print service and a second quote of the same
        # model writes the same copy again.
        folder = os.path.join(kiln_output_dir(STANDING_COPIES), hash_file(sent)[:16])
        os.makedirs(folder, mode=0o700, exist_ok=True)
        pose = stand_up(sent, into=os.path.join(folder, os.path.basename(sent)), source="quote")
    except Exception:  # noqa: BLE001 -- a quote never fails for its pose
        logger.debug("no pose judged for %s", sent, exc_info=True)
        pose = {"status": UNJUDGED}
    if pose.get("status") == TURNED:
        return PricedFile(os.path.join(folder, os.path.basename(sent)), sent, pose)
    return PricedFile(sent, sent, pose)


def quote_pose_block(pose: dict[str, Any]) -> dict[str, Any]:
    """Which way up a quote's model stands, as its reply carries it:
    ``status``, the ``said`` line (:func:`quote_pose_line`), and the ``turn``
    and ``candidates`` when there are any.  Never a file's place."""
    block: dict[str, Any] = {"status": str(pose.get("status") or "")}
    if said := quote_pose_line(pose):
        block["said"] = said
    for key in ("turn", "candidates"):
        if pose.get(key):
            block[key] = pose[key]
    return block


def quote_pose_line(pose: dict[str, Any]) -> str:
    """What a person is told first about the model a quote priced, or ``""``.

    Kiln's servers word their own quotes with it too, so a person hears one
    sentence for one thing whichever door priced the model.  A model that
    stands as it arrived, or that the person asked to have priced exactly as
    sent, needs no sentence.
    """
    from kiln.arrival_pose import ADVISED, TURNED, UNJUDGED, UNSURE, undo_of

    status = str(pose.get("status") or "")
    said = str(pose.get("said") or "").strip()
    if status == TURNED and said:
        # The person's own file was never turned, so the judgement's
        # "rotate_model(...) puts it back" is about Kiln's copy, not theirs.
        undo = ", ".join(f"{k}={v:g}" for k, v in undo_of(pose.get("turn") or {}).items())
        said = said.replace(f"rotate_model({undo}) puts it back.", "").strip()
        return f"{said} The price is for it standing up."
    if status in (UNSURE, ADVISED) and said:
        return f"{said} The price is for the file exactly as sent."
    if status == UNJUDGED:
        return "Kiln could not check which way up this model stands, so the price is for the file exactly as sent."
    return ""

