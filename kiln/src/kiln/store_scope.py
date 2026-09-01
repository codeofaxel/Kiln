"""Scope disclosure for tool answers that read a LOCAL store.

A tool that reads ``~/.kiln/...`` and answers ``{"count": 1, ...}`` has
told the caller two things, and only one of them is true.  The count is
true about the store it read.  The *shape* of the answer — a clean
success, a confident number, no mention of a boundary — reads as a claim
about everything the user owns.  An agent that believes the second thing
says "you have no saved Kiln logo"; so does a person reading the same
JSON.  That has already happened.

Kiln's saved-artifact stores come in two halves:

* the **local** half — files under ``~/.kiln`` on THIS machine, which
  every tier has;
* the **cloud** half — the user's library at ``app.kiln3d.com``, which
  paid tiers have.  It is a kiln-pro feature; public Kiln owns no cloud
  code and reaches it only through the bridge seam described below.

So the honest answer depends on the caller's tier:

* **free** — local is the whole library.  Say which store was read.
* **paid, cloud readable** — return the union, with every item saying
  which half it came from.
* **paid, cloud NOT readable** — this is the dangerous state, and it is
  the one this module exists for.  The local read succeeded, so the
  response still looks healthy; the answer is nonetheless a fragment of
  the caller's library.  It is marked ``incomplete`` and carries a
  top-level ``warning``, so neither an agent nor a person can mistake it
  for a complete listing.

A second way to be a fragment
-----------------------------
The split above is about tiers.  A door can also fail to read its OWN
local store: it answers out of an in-process cache that holds less than
the file on disk, and the shortfall has nothing to do with licences.
``job_history`` is that case — the queue reloads only non-terminal rows
at startup, so every job that FINISHED before this server process began
is in ``~/.kiln/queue.db`` and missing from the answer.  Restart the
server and a full history reports ``{"success": true, "count": 0}``.

Such a door passes a :class:`LocalRead` saying so.  The response is then
marked ``incomplete`` for that reason on its own, free tier included,
and the two shortfalls are listed separately in ``scope`` because they
are different failures with different fixes.

Stores that are one MACHINE's record of events rather than a curated
library (print history, job history) additionally set
``per_machine=True``.  Kiln has no cross-install read at any tier, so
that flag does not make an answer incomplete — an unmeasurable gap is
not a countable one — it only forbids the "this is all of it" sentence,
which for a fleet spread across machines is false.

Usage — one call, at the end of the tool, over the response it already
built::

    result = {"success": True, "count": len(rows), "decorations": rows}
    return scoped_store_response(
        result, store=DECORATION_LIBRARY, items_key="decorations",
    )

The helper owns tier resolution, the cloud read, item tagging, the
count, and the wording.  A door only names WHICH store it read — never
its own branch of the logic, because per-door branches are how the two
answers drift apart.

``tests/test_store_scope.py`` pins both the helper and the wiring: a
shared helper nobody calls is the same bug with extra steps.

The kiln-pro seam
-----------------
The cloud half is read through one optional method on the pro bridge::

    pro_features.list_cloud_store(capability, filters={...}) -> {
        "status": "ok" | "unauthenticated" | "unavailable" | "error",
        "items": [ {...}, ... ],   # required when status == "ok"
        "detail": "...",           # optional, human-readable
    }

``filters`` carries the same narrowing the local read applied (a
``design_id``, a ``content_type``, a search ``query``).  The
implementation must apply every filter it understands and return
``"unavailable"`` for any it cannot, rather than returning a wider set:
an unfiltered cloud half merged into a filtered local one is a different
wrong answer, not a better one.

Public Kiln never implements it, never approximates it, and treats its
absence as a fact about the answer rather than a reason to stay quiet:
a paid caller whose bridge has no such method gets the loud
``incomplete`` disclosure, which is the truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)

#: The optional bridge method public Kiln calls for the cloud half.
CLOUD_SEAM_ATTR = "list_cloud_store"

#: Cloud-read outcomes that still leave the caller with their whole
#: library.  Everything else means the answer is a fragment.
_COMPLETE_STATUSES = frozenset({"ok", "no_cloud_half", "tier_local_only"})

_PRICING_URL = "https://kiln3d.com/pricing"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalStore:
    """One local store a tool can read.

    :param id: Stable identifier, used in the disclosure block.
    :param label: Human name, used in the sentences ("decoration library").
    :param location: Where the local half lives, for a person who wants
        to go look at it.
    :param cloud_capability: The seam key naming this store's cloud half,
        or ``None`` when the store has no cloud counterpart at ANY tier
        (a machine-local cache).  ``None`` is a claim: it says a paid
        caller is missing nothing, so only set it when that is true.
    :param per_machine: True when the store records what happened on
        THIS machine, so a second Kiln install keeps a second one and no
        local read can see it.  It does not make an answer incomplete —
        a gap that cannot be measured is not a gap that can be counted —
        but it does forbid the "this is all of it" sentence a curated
        library gets, because for a fleet spread across machines that
        sentence is false.
    """

    id: str
    label: str
    location: str
    cloud_capability: str | None = None
    per_machine: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "location": self.location,
            "has_cloud_half": self.cloud_capability is not None,
            "per_machine": self.per_machine,
        }


@dataclass
class LocalRead:
    """How much of the LOCAL half the tool's own read actually covered.

    Most doors read their local store directly, so the local half is
    whole by construction and this defaults to ``whole()``.  Some can
    fail to: a door may fall back to an in-process cache when the
    durable store on disk cannot be read, and that answer is a fragment
    for a reason that has nothing to do with tiers or the cloud.
    ``job_history`` is the shape this exists for — it once served
    finished jobs from the queue's in-memory dict alone, which reloads
    only NON-terminal rows on startup, so after every restart a full
    history returned ``{"success": true, "count": 0}``.  The door now
    reads ``~/.kiln/queue.db`` directly and passes ``partial`` only
    when that read fails.

    A listing the caller truncated with ``limit`` is NOT partial — a
    page of a store the door fully read is pagination, not a gap, and
    crying incomplete over it teaches callers to ignore the flag.  The
    door discloses the store's total via ``local_total`` instead.

    :param status: ``"whole"`` (the read covered the local store) or
        ``"partial"`` (it did not).
    :param detail: Plain sentence naming what the read missed and where
        it still lives, so the caller can go and get it.
    :param missing: How many local records are known to be absent, when
        that can be counted.  ``None`` means "some, count unknown" —
        never round it to zero.
    """

    status: str = "whole"
    detail: str = ""
    missing: int | None = None

    @property
    def complete(self) -> bool:
        """Did this read cover the local store it answers for?"""
        return self.status == "whole"

    @classmethod
    def whole(cls) -> LocalRead:
        """The read covered the local store — the ordinary case."""
        return cls()

    @classmethod
    def partial(cls, detail: str, missing: int | None = None) -> LocalRead:
        """The read did NOT cover the local store; say so and where the rest is."""
        return cls(status="partial", detail=detail, missing=missing)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.missing is not None:
            d["records_not_listed"] = self.missing
        return d


@dataclass
class CloudRead:
    """What came back when the cloud half was asked for.

    :param status: ``"ok"`` (items merged), ``"no_cloud_half"`` (this
        store has none), ``"tier_local_only"`` (free tier), or one of the
        degraded states — ``"unavailable"``, ``"unauthenticated"``,
        ``"error"``.
    :param items: Cloud-side items, only meaningful for ``"ok"``.
    :param detail: Plain sentence naming what happened, for the degraded
        states.
    """

    status: str
    items: list[Any] = field(default_factory=list)
    detail: str = ""

    @property
    def complete(self) -> bool:
        """Does this outcome leave the caller with their whole library?"""
        return self.status in _COMPLETE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status}
        if self.detail:
            d["detail"] = self.detail
        if self.status == "ok":
            d["items_returned"] = len(self.items)
        return d


# ---------------------------------------------------------------------------
# The stores public Kiln reads
#
# Named once here and imported by every door that reads them, so two
# doors onto one store cannot describe it two different ways.
# ---------------------------------------------------------------------------

DECORATION_LIBRARY = LocalStore(
    id="decoration_library",
    label="decoration library",
    location="~/.kiln/decorations/",
    cloud_capability="decoration_library",
)

DESIGN_VERSIONS = LocalStore(
    id="design_versions",
    label="design version history",
    location="~/.kiln/designs/",
    cloud_capability="design_versions",
)

# Its own capability key rather than sharing the design library's: a
# cached-design row and a design-VERSION row are different shapes, and
# merging one list into the other would be a new wrong answer rather
# than a completed one.
DESIGN_CACHE = LocalStore(
    id="design_cache",
    label="cached design library",
    location="~/.kiln/design_cache/",
    cloud_capability="design_cache",
)

# A download/generation cache, not a library the user curates: nothing on
# the cloud side corresponds to it, so a paid caller is missing nothing
# and saying otherwise would invent a library that does not exist.
MODEL_CACHE = LocalStore(
    id="model_cache",
    label="local model cache",
    location="~/.kiln/model_cache/",
    cloud_capability=None,
)


# What this machine recorded about prints it ran.  A second Kiln
# install keeps a second one and neither can see the other, so this is
# marked per-machine: "your print history" spans the fleet, and one
# machine's database is a slice of it.
#
# No cloud capability, and that is a verified claim, not an assumption:
# cloud_sync pushes COPIES of job and event rows (the local rows are
# only marked synced, never removed) to a user-configured endpoint that
# nothing in Kiln or kiln-pro can read back — kiln-pro has no ingest
# for those pushes and no history library behind the seam.  So a cloud
# sink may hold duplicates of rows this answer already includes, and
# rows from OTHER machines, which is exactly the boundary per_machine
# already names.  The one real gap — rows retention pruned locally
# after a year that survive in a user's own sink — is unmeasurable
# from here and belongs to the endpoint's owner, not to this answer.
PRINT_HISTORY = LocalStore(
    id="print_history",
    label="print history",
    location="~/.kiln/kiln.db",
    per_machine=True,
)

# The queue's record of finished jobs.  Same machine boundary and the
# same verified no-cloud-half reasoning as the print history.  Its own
# hazard is local: the queue's in-memory dict reloads only non-terminal
# rows at startup, so a door serving history from memory alone answers
# "none" after a restart while ~/.kiln/queue.db holds every row.  The
# door reads the durable store; LocalRead exists for the day that read
# fails.
JOB_HISTORY = LocalStore(
    id="job_history",
    label="print job history",
    location="~/.kiln/queue.db",
    per_machine=True,
)


# What this machine learned from prints that went wrong.  Same verified
# no-cloud-half reasoning as the print history: nothing pushes failure
# records anywhere readable.
FAILURE_HISTORY = LocalStore(
    id="failure_history",
    label="print failure history",
    location="~/.kiln/kiln.db",
    per_machine=True,
)

# This machine's print attempts per model (print DNA).  The community
# registry exchanges anonymised aggregates, not these rows — a user's
# own attempt log has no readable cloud half, and another install's
# attempts on the same model are exactly the per-machine boundary.
PRINT_DNA = LocalStore(
    id="print_dna",
    label="model print history",
    location="~/.kiln/kiln.db",
    per_machine=True,
)

# ---------------------------------------------------------------------------
# Tier
# ---------------------------------------------------------------------------


def _tier_from_licensing() -> str | None:
    """Read the tier from ``kiln.licensing``, or None if it will not answer."""
    try:
        from kiln.licensing import get_tier
    except Exception:  # noqa: BLE001 — module absent or not yet registered
        return None
    try:
        tier = get_tier()
    except Exception:  # noqa: BLE001 — a broken resolver is not a free tier
        return None
    value = str(getattr(tier, "value", tier) or "").strip().lower()
    return value or None


def current_tier() -> str:
    """Return the caller's licence tier as a lowercase string.

    ``kiln.licensing`` is a shim kiln-pro registers when it is imported,
    so a plain ``from kiln.licensing import ...`` answers only in a
    process that has already touched kiln-pro.  Resolving straight to
    ``"free"`` on that miss is how a founder-key holder gets told a
    local store is their whole library, so the miss is retried after
    importing kiln-pro, and only a genuinely absent kiln-pro means free:

    * kiln-pro absent → ``"free"``.  There is no cloud half to miss.
    * tier resolved → that tier.
    * kiln-pro present but the tier will not resolve → ``"unknown"``,
      which is treated as possibly-paid.  It grants nothing; it only
      means the answer discloses that a cloud half may be missing,
      rather than asserting the local store is everything.
    """
    resolved = _tier_from_licensing()
    if resolved:
        return resolved

    try:
        import kiln_pro  # noqa: F401 — importing registers the licensing shim
    except ImportError:
        return "free"
    except Exception:  # noqa: BLE001 — present but unhealthy; do not claim free
        return "unknown"

    return _tier_from_licensing() or "unknown"


def is_paid_tier(tier: str) -> bool:
    """Return True when *tier* may have a cloud library.

    Stated as "local-only is the FREE-tier behaviour" rather than as a
    list of paid tiers: a tier added later gets its cloud half without
    anyone remembering to extend a list here, and a tier customers
    cannot buy is not named in a package they can install.  The same
    rule, worded the same way, gates metering in ``decoration_quota``.

    ``"unknown"`` lands on this side deliberately.  The question here is
    not "may this caller have a paid feature" — nothing is handed out on
    the strength of the answer — it is "could this caller be missing
    something".  Guessing free there is the failure being fixed.
    """
    return (tier or "").strip().lower() not in ("", "free")


# ---------------------------------------------------------------------------
# The cloud half
# ---------------------------------------------------------------------------


def read_cloud_half(
    store: LocalStore,
    *,
    tier: str | None = None,
    filters: dict[str, Any] | None = None,
) -> CloudRead:
    """Ask kiln-pro for *store*'s cloud items on behalf of this caller.

    :param filters: The same narrowing the local read applied, passed
        through so the two halves describe the same question.

    Never raises.  Every way of not getting the items is reported as a
    distinct status, because "there are none" and "I could not look" are
    different answers and only one of them is safe to round down to a
    count.
    """
    if store.cloud_capability is None:
        return CloudRead("no_cloud_half")

    resolved = current_tier() if tier is None else tier
    if not is_paid_tier(resolved):
        return CloudRead("tier_local_only")

    try:
        from kiln_pro.bridge import pro_features
    except ImportError:
        return CloudRead(
            "unavailable",
            detail=(
                "kiln-pro is not installed on this machine, so the cloud "
                "library cannot be read from here."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — a broken bridge is a degraded read
        return CloudRead(
            "unavailable",
            detail=f"the kiln-pro bridge could not be loaded ({type(exc).__name__}).",
        )

    reader = getattr(pro_features, CLOUD_SEAM_ATTR, None)
    if not callable(reader):
        return CloudRead(
            "unavailable",
            detail=(
                "the installed kiln-pro does not expose "
                f"{CLOUD_SEAM_ATTR}(), so the cloud library cannot be read "
                "from here."
            ),
        )

    try:
        raw = reader(store.cloud_capability, filters=dict(filters or {}))
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        _logger.warning(
            "cloud read failed for store=%s: %s", store.id, exc, exc_info=True
        )
        return CloudRead(
            "error",
            detail=f"the cloud library read failed ({type(exc).__name__}).",
        )

    return _normalise_cloud_result(raw, store)


def _normalise_cloud_result(raw: Any, store: LocalStore) -> CloudRead:
    """Turn whatever the seam returned into a :class:`CloudRead`.

    An unrecognised shape is an ERROR, not an empty library: guessing
    "nothing there" from a response we cannot parse is the exact
    substitution this module exists to prevent.
    """
    if not isinstance(raw, dict):
        return CloudRead(
            "error",
            detail="the cloud library returned an unrecognised response.",
        )

    status = str(raw.get("status") or "").strip().lower()
    detail = str(raw.get("detail") or "").strip()

    if status == "ok":
        items = raw.get("items")
        if not isinstance(items, list):
            return CloudRead(
                "error",
                detail="the cloud library reported success but returned no list.",
            )
        return CloudRead("ok", items=list(items), detail=detail)

    if status == "unauthenticated":
        return CloudRead(
            "unauthenticated",
            detail=detail
            or "this machine is not signed in, so the cloud library was not read.",
        )
    if status in ("unavailable", "error"):
        return CloudRead(
            status,
            detail=detail or "the cloud library could not be read from here.",
        )

    _logger.warning(
        "cloud read for store=%s returned unknown status %r", store.id, status
    )
    return CloudRead(
        "error",
        detail=(
            detail
            or f"the cloud library returned an unknown status ({status or 'missing'})."
        ),
    )


# ---------------------------------------------------------------------------
# The disclosure
# ---------------------------------------------------------------------------


def _local_sentence(store: LocalStore, local: LocalRead, complete: bool) -> str:
    """What the tool's own read covered, and whether that is the whole local half."""
    where = f"your local {store.label} on this machine ({store.location})"

    if not local.complete:
        counted = ""
        if local.missing:
            plural = "" if local.missing == 1 else "s"
            verb = "is" if local.missing == 1 else "are"
            counted = (
                f" {local.missing} record{plural} held there {verb} not in "
                "this list."
            )
        return (
            f"INCOMPLETE — this did not even read all of {where}: "
            f"{local.detail}{counted}"
        )
    if not complete:
        return f"INCOMPLETE — read from {where}, and nowhere else."
    return f"Read from {where}."


def _cloud_sentence(store: LocalStore, cloud: CloudRead) -> str:
    """What happened to the cloud half."""
    if cloud.status == "ok":
        return (
            f"Your cloud {store.label} was read too; each item says which one "
            "it came from."
        )
    if cloud.status == "no_cloud_half":
        return "This store has no cloud half Kiln can read."
    if cloud.status == "tier_local_only":
        return (
            f"Cloud libraries are a kiln-pro feature ({_PRICING_URL}); on the "
            "free tier there is no cloud half to read."
        )
    return (
        f"Your cloud {store.label} was not read: {cloud.detail} Anything saved "
        "there is missing from this list, and the count is not your whole "
        "library."
    )


def _summary(
    store: LocalStore,
    local: LocalRead,
    cloud: CloudRead,
    *,
    local_total: int | None = None,
    local_listed: int = 0,
) -> str:
    """Plain sentences saying exactly what this answer covers.

    Assembled from three independent facts — how much of the local half
    was read, what happened to the cloud half, and whether the store is
    one machine's record of events rather than a library — rather than
    from one branch per combination.  A door that gains a new kind of
    gap gains a sentence, not a new matrix of wordings.
    """
    complete = local.complete and cloud.complete
    sentences = [
        _local_sentence(store, local, complete),
        _cloud_sentence(store, cloud),
    ]

    if local_total is not None and local_total > local_listed:
        # A page, not a gap: the store was read in full and the caller's
        # limit chose how much of it to carry.  Said plainly so a count
        # of 20 is never mistaken for a library of 20.
        sentences.append(
            f"{local_total} matching records are stored on this machine; "
            f"this answer lists {local_listed} of them."
        )

    if store.per_machine:
        # Never claim totality for a store that only ever held one
        # machine's events.  Kiln has no cross-install read at any tier,
        # so the honest move is to name the boundary, not to count past
        # it.  A cloud half that DID answer may already span machines —
        # public Kiln cannot tell — so the boundary is stated about the
        # local half rather than about the whole answer.
        if cloud.status == "ok":
            sentences.append(
                f"The local half is only THIS machine's {store.label}; "
                "whether the cloud half covers your other machines is up to "
                "what it stores."
            )
        else:
            sentences.append(
                f"It covers THIS machine only: any other computer running "
                f"Kiln keeps its own {store.label}, which no local read can "
                "see."
            )
    elif complete:
        sentences.append(f"That is all of your {store.label}.")

    return " ".join(sentences)


def _tag_items(items: list[Any], source: str, source_key: str) -> list[Any]:
    """Stamp *source* onto every dict item that does not already say."""
    tagged: list[Any] = []
    for item in items:
        if isinstance(item, dict) and source_key not in item:
            item = {**item, source_key: source}
        tagged.append(item)
    return tagged


def scoped_store_response(
    response: dict[str, Any],
    *,
    store: LocalStore,
    items_key: str,
    count_key: str = "count",
    source_key: str = "store",
    filters: dict[str, Any] | None = None,
    local: LocalRead | None = None,
    local_total: int | None = None,
) -> dict[str, Any]:
    """Merge in the cloud half and disclose what this answer covers.

    Reads the LOCAL items already in ``response[items_key]``, adds the
    caller's cloud items when their tier has them and they can be read,
    stamps ``source_key`` onto every item, rewrites ``count_key``, and
    attaches the ``scope`` block.  When the answer is a fragment it also
    sets ``incomplete`` and a top-level ``warning``, so a caller
    skimming for ``success`` and a number cannot miss the boundary.

    :param response: The response the tool already built, mutated and
        returned.  Its existing keys are left alone.
    :param store: Which store was read — one of the module constants.
    :param items_key: Key holding the local items list.
    :param count_key: Key holding the count, rewritten to match.
    :param source_key: Per-item key stamped with ``"local"``/``"cloud"``.
    :param filters: The narrowing the local read applied (``design_id``,
        ``content_type``, a search ``query``), forwarded so the cloud
        half answers the same question.  Drop a filter here and the two
        halves stop describing the same list.
    :param local: How much of the LOCAL half the caller's own read
        covered.  Defaults to :meth:`LocalRead.whole` — a door that
        reads its store directly needs nothing here.  A door whose
        durable read failed and fell back to a cache passes
        :meth:`LocalRead.partial`, and the answer is marked incomplete
        for that reason alone, at any tier, with or without a cloud
        half.
    :param local_total: How many records match in the local store as a
        whole, when the door measured it.  A listing capped by the
        caller's own ``limit`` is a page, not a fragment — the total is
        disclosed in ``scope.local.total_records`` and in the summary,
        and the answer stays complete.  ``None`` means unmeasured, and
        an unmeasured total is simply not stated.
    :returns: The same dict, annotated.
    """
    if not isinstance(response, dict):
        return response

    try:
        local_items = response.get(items_key)
        if not isinstance(local_items, list):
            local_items = []

        tier = current_tier()
        local_read = local or LocalRead.whole()
        cloud = read_cloud_half(store, tier=tier, filters=filters)
        complete = local_read.complete and cloud.complete

        items = _tag_items(local_items, "local", source_key)
        if cloud.status == "ok":
            items = items + _tag_items(cloud.items, "cloud", source_key)

        response[items_key] = items
        if count_key in response:
            response[count_key] = len(items)

        stores_read = ["local"] + (["cloud"] if cloud.status == "ok" else [])
        local_dict = local_read.to_dict()
        if local_total is not None:
            local_dict["total_records"] = local_total
        scope: dict[str, Any] = {
            "complete": complete,
            "store": store.to_dict(),
            "stores_read": stores_read,
            "tier": tier,
            "local": local_dict,
            "cloud": cloud.to_dict(),
            "count_covers": ", ".join(stores_read),
            "summary": _summary(
                store,
                local_read,
                cloud,
                local_total=local_total,
                local_listed=len(local_items),
            ),
        }
        # Named separately because they are different failures with
        # different fixes: the local half is missing because this
        # process never loaded it, the cloud half because it could not
        # be reached.  Collapsing them would hide the one a restart
        # cannot explain.
        missing = (["local (durable)"] if not local_read.complete else []) + (
            ["cloud"] if not cloud.complete else []
        )
        if missing:
            scope["stores_missing"] = missing
        response["scope"] = scope

        if not complete:
            response["incomplete"] = True
            response["warning"] = scope["summary"]
        return response
    except Exception as exc:  # noqa: BLE001 — a broken disclosure is not silence
        _logger.exception("store scope disclosure failed for store=%s", store.id)
        response["scope"] = {
            "complete": False,
            "store": store.to_dict(),
            "summary": (
                f"Scope could not be determined ({type(exc).__name__}); treat "
                "this list as possibly incomplete."
            ),
        }
        response["incomplete"] = True
        response["warning"] = response["scope"]["summary"]
        return response
