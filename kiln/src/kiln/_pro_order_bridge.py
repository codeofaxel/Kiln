"""Public Kiln → Kiln's servers: ordering a print from a print service.

Ordering (the print service's materials, a quote for a part, placing an
order, tracking and cancelling it, the order history) is worked out by
Kiln's servers: they hold the print service's account, the fee allowance,
the spend caps and the quote the order is placed against.  A plain
``pip install kiln3d`` has no order service of its own
(:func:`kiln.fulfillment_service.order_service` is ``None`` there), so every
ordering door on such an install -- the ``fulfillment_*`` agent tools,
``compare_print_options``, the ``kiln order`` commands, ``kiln
fulfillment-materials`` and the outsourced half of ``kiln compare-cost`` --
orders THROUGH this module, over the same ``/api/fulfillment/*`` routes
Kiln's hosted connector uses.  Nothing about ordering is worked out on this
computer; it sends the part and the person's choices, and shows what came
back, worded by the servers for the person (``tell_the_person``).

**What a miss is.**  A quote that was not given, an order that was not placed,
a status that did not come back is a :class:`~kiln.served_answer.Miss` with
one of the four causes (offline, signed out, no answer, refused), worded
through :func:`kiln.served_answer.sentence` with ordering's own words
(:data:`WORDS`): an order not placed is SAID to be not placed, never read as
placed, and a quote not given is no quote.  The servers' own refusal (a card
to set up first, a spend cap, nothing to cancel) rides whole, with its code
and its ``details`` (the card setup link, the approval link), because the
refusal is the next step.  The one address that has no ordering routes at
all (a self-hosted ``KILN_API_URL`` without them) answers a bare 404, and
that alone reads as :data:`NOT_OFFERED_CODE`, the case the sentence
:data:`~kiln.fulfillment_service.NOT_INCLUDED` was written for.

**The quote token.**  The servers answer a quote with a ``quote_token`` that
every later step (the two confirmations, the order) is keyed on.  It is kept
beside the quote, with the file that was quoted, in this computer's own
quote cache (:mod:`kiln.quote_cache`, under the quote id the print service
gave), so ``fulfillment_order(quote_id=...)`` and ``kiln order place
QUOTE_ID`` keep their contract.  An order is placed only from a quote this
computer took: the record is what binds the order to the token the servers
issued and to the file the person previewed.  A door handed the token too
checks it against the record and refuses a disagreement; a token for a
quote taken elsewhere is refused with "quote again here", never trusted.

**Two confirmations, kept.**  The local ordering doors already refuse an
order without the person's approval of the rendered preview
(``issue_preview_token``) and of the shipping details
(``issue_shipping_confirmation_token``).  Those stay, checked on this
computer as before; then the servers mint their own two confirmation tokens
for the quote (``/preview-confirm``, ``/shipping-confirm``) and the order is
placed with them (:func:`place`).  Nothing here charges a fee: the servers
charge Kiln's fee when they place the order and say what they charged
(``kiln_fee``), which every door shows and none records.

Nothing here raises for want of an answer; a bad local input (a model file
that is not there) raises as the door's own validation would.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiln.fulfillment_service import NOT_INCLUDED, NOT_INCLUDED_CODE
from kiln.served_answer import (
    SESSION_EXPIRED_CODE,
    Miss,
    classify_answer,
    classify_transport_error,
    fields,
    sentence,
)

logger = logging.getLogger(__name__)

#: Where ordering is served from when ``KILN_API_URL`` names nothing else
#: (the same default as every served door, ``kiln.server._pro_api_call``).
HOSTED_API_URL = "https://api.kiln3d.com"
#: The address this install asks has no ordering routes at all: the servers
#: answered a bare 404 with no code of their own.  The one miss the doors
#: word as :data:`~kiln.fulfillment_service.NOT_INCLUDED`.
NOT_OFFERED_CODE = "ORDERING_NOT_OFFERED"
#: The door was asked to order against a quote whose token this computer no
#: longer holds (quoted elsewhere, or the cache has gone): quote again.
QUOTE_TOKEN_UNKNOWN_CODE = "QUOTE_TOKEN_UNKNOWN"
#: Kiln's servers quote and order through this print service when a door
#: names none.
DEFAULT_PROVIDER = "craftcloud"
#: The model files a quote may send.  The servers hold the authoritative list
#: and refuse anything else; this one stops a path that is plainly not a
#: model (a key, a config, a log) before a byte of it is read, since the
#: path comes from an agent.  Judged on the file the path resolves to.
QUOTE_FILE_SUFFIXES = frozenset({".stl", ".obj", ".3mf", ".step", ".stp", ".iges", ".igs"})

#: A quote uploads the model and waits on the print service's own pricing;
#: everything else is one round trip.
_QUOTE_TIMEOUT_S = 300.0
_TIMEOUT_S = 90.0

#: What each ordering door puts on the line, what Kiln can't do without the
#: servers, and what it therefore won't do -- the three anchors of
#: :func:`kiln.served_answer.sentence`.  Ordering's kind is ``action``: a
#: step not taken is said so, never read as taken.
WORDS: dict[str, tuple[str, str, str]] = {
    "materials": (
        "The print service's materials are listed by Kiln's servers",
        "list them",
        "has no list",
    ),
    "quote": (
        "A print-service quote for this part comes from Kiln's servers",
        "get a quote",
        "has no quote, and nothing was sent to a print service",
    ),
    "order": (
        "Placing a print-service order goes through Kiln's servers",
        "place this order",
        "placed no order and charged nothing",
    ),
    "status": (
        "Where a print-service order is comes from Kiln's servers",
        "check this order",
        "has no answer about it",
    ),
    "cancel": (
        "Cancelling a print-service order goes through Kiln's servers",
        "cancel this order",
        "cancelled nothing",
    ),
    "orders": (
        "Your print-service orders are kept by Kiln's servers",
        "list them",
        "has no list",
    ),
}


@dataclass(frozen=True)
class Answer:
    """What one route gave back: the servers' answer, or why there is none.

    ``details`` is the structured part of a refusal the servers attached (a
    card setup link, a spend-cap approval link): it is the next step, so it
    rides beside the sentence rather than being lost in it.
    """

    body: dict[str, Any] | None = None
    miss: Miss | None = None
    details: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.body is not None


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------


def api_base() -> str:
    """Kiln's servers: ``KILN_API_URL`` (an operator's own address), else the
    hosted default -- the same resolution as every served door."""
    return (os.environ.get("KILN_API_URL") or "").strip().rstrip("/") or HOSTED_API_URL


def _bearer() -> tuple[str, Miss | None]:
    """This install's sign-in for the servers, or the miss that says it has none."""
    try:
        from kiln.server import _served_bearer

        bearer, expired = _served_bearer()
    except Exception:  # noqa: BLE001 -- a broken install is a sign-in nobody can read
        logger.debug("order bridge: the sign-in could not be read", exc_info=True)
        bearer, expired = "", None
    if expired is not None:
        return "", Miss("signed_out", SESSION_EXPIRED_CODE, str(expired))
    if not bearer:
        return "", Miss("signed_out", "KILN_ACCOUNT_NOT_PAIRED")
    return bearer, None


def _headers(bearer: str) -> dict[str, str]:
    """The headers every served call carries: the sign-in, this device's
    name (the activation cap counts devices), this client's version (the
    servers may hold a floor)."""
    headers = {"Authorization": f"Bearer {bearer}"}
    try:
        from kiln.api_device import device_fingerprint_headers

        headers.update(device_fingerprint_headers())
    except Exception:  # noqa: BLE001 -- the call stands without the device name
        pass
    try:
        from kiln.version_check import _current_version

        headers["X-Kiln-Client-Version"] = _current_version()
    except Exception:  # noqa: BLE001
        pass
    return headers


def _miss_for_http(status: int, body: Any) -> tuple[Miss, dict[str, Any] | None]:
    """Why an HTTP error answer is no answer, and the refusal's details."""
    code, text = "", ""
    details = None
    if isinstance(body, dict):
        code = str(body.get("code") or "")
        error = body.get("error")
        text = str((error.get("message") if isinstance(error, dict) else error) or body.get("detail") or "")
        if isinstance(body.get("details"), dict):
            details = dict(body["details"])
    if status == 401:
        return Miss("signed_out", code or "KILN_AUTH_REJECTED", text), details
    if status == 404 and not code:
        # The route itself is not there: this address serves no ordering.
        return Miss("refused", NOT_OFFERED_CODE, NOT_INCLUDED), None
    if isinstance(body, dict):
        miss = classify_answer(body)
        if miss is not None:
            return miss, details
    if status >= 500:
        return Miss("unanswered", "KILN_API_HTTP_ERROR", text), details
    return Miss("refused", code or "KILN_API_HTTP_ERROR", text), details


def _ask(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT_S,
) -> Answer:
    """One request to ``/api/fulfillment/*`` as this install's sign-in.

    Never raises: no HTTP answer is classified by :mod:`kiln.served_answer`
    (offline or unanswered), an HTTP error by its status and body, and a 200
    that says ``success: false`` is the servers' own refusal.
    """
    bearer, miss = _bearer()
    if miss is not None:
        return Answer(miss=miss)
    base = api_base()
    try:
        import httpx

        resp = httpx.request(
            method, f"{base}{path}", json=json, data=data, files=files, params=params,
            headers=_headers(bearer), timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("order bridge: %s %s got no answer", method, path, exc_info=True)
        return Answer(miss=classify_transport_error(exc, host=base))
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 400:
        miss, details = _miss_for_http(resp.status_code, body)
        return Answer(miss=miss, details=details)
    if not isinstance(body, dict):
        return Answer(miss=Miss("unanswered", "KILN_API_HTTP_ERROR", "not an answer"))
    refused = classify_answer(body)
    if refused is not None:
        details = body.get("details") if isinstance(body.get("details"), dict) else None
        return Answer(miss=refused, details=details)
    return Answer(body=body)


# ---------------------------------------------------------------------------
# the routes, one function each
# ---------------------------------------------------------------------------


def materials(provider: str = DEFAULT_PROVIDER) -> Answer:
    """The print service's materials: ``{"materials": [...], "provider": ...}``."""
    return _ask("GET", "/api/fulfillment/materials", params={"provider": provider or DEFAULT_PROVIDER})


def quote(
    file_path: str,
    material_id: str,
    quantity: int = 1,
    shipping_country: str = "US",
    *,
    provider: str = DEFAULT_PROVIDER,
    jurisdiction: str = "",
    business_tax_id: str = "",
) -> Answer:
    """A quote for one part, priced by the print service through Kiln's servers.

    The model goes up as a file from this computer: that file is what the
    person previews and approves, so the order step can bind the two.  (The
    hosted connector also quotes a make by its server-side artifact token;
    no public door takes one, so this bridge does not either -- a path saved
    beside a quote made from a token would not be the file that was quoted.)
    A path that is not a file raises :class:`FileNotFoundError`; one that
    resolves (symlinks followed) to something other than a model file
    (:data:`QUOTE_FILE_SUFFIXES`) raises :class:`ValueError` before it is
    opened -- both the door's own validation.  The servers judge the type
    again and refuse what they do not take.  On an answer the quote token
    and the file are remembered beside the quote (:func:`remember_quote`).
    """
    form = {
        "material_id": str(material_id or ""),
        "quantity": str(int(quantity or 1)),
        "shipping_country": str(shipping_country or "US"),
        "provider": str(provider or DEFAULT_PROVIDER),
        "jurisdiction": str(jurisdiction or ""),
        "business_tax_id": str(business_tax_id or ""),
    }
    path = Path(str(file_path or "")).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Model file not found: {path}")
    real = path.resolve()
    if path.suffix.lower() not in QUOTE_FILE_SUFFIXES or real.suffix.lower() not in QUOTE_FILE_SUFFIXES:
        raise ValueError(
            f"Not a model file: {path.name}. A quote takes "
            f"{', '.join(sorted(QUOTE_FILE_SUFFIXES))}; nothing was sent."
        )
    with real.open("rb") as fh:
        answer = _ask(
            "POST", "/api/fulfillment/quote", data=form,
            files={"file": (path.name, fh, "application/octet-stream")},
            timeout=_QUOTE_TIMEOUT_S,
        )
    if answer.ok:
        remember_quote(answer.body, file_path=str(real), material_id=material_id,
                       quantity=int(quantity or 1), shipping_country=shipping_country, provider=provider)
    return answer


def preview_confirm(quote_token: str) -> Answer:
    """The servers' record that the person approved the rendered preview."""
    return _ask("POST", "/api/fulfillment/preview-confirm", json={"quote_token": quote_token})


def shipping_confirm(quote_token: str, shipping_option_id: str, shipping_address: dict[str, Any]) -> Answer:
    """The servers' record that the person approved these exact shipping details."""
    return _ask(
        "POST", "/api/fulfillment/shipping-confirm",
        json={
            "quote_token": quote_token,
            "shipping_option_id": str(shipping_option_id or ""),
            "shipping_address": dict(shipping_address or {}),
        },
    )


def order(
    quote_token: str,
    quote_id: str,
    shipping_option_id: str,
    shipping_address: dict[str, Any],
    preview_confirmation_token: str,
    shipping_confirmation_token: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    notes: str = "",
    approval_token: str = "",
) -> Answer:
    """Place the order the servers quoted, with their two confirmations."""
    body: dict[str, Any] = {
        "quote_token": quote_token,
        "quote_id": str(quote_id or ""),
        "shipping_option_id": str(shipping_option_id or ""),
        "shipping_address": dict(shipping_address or {}),
        "preview_confirmation_token": preview_confirmation_token,
        "shipping_confirmation_token": shipping_confirmation_token,
        "provider": str(provider or DEFAULT_PROVIDER),
        "notes": str(notes or ""),
    }
    if approval_token:
        body["approval_token"] = str(approval_token)
    return _ask("POST", "/api/fulfillment/order", json=body, timeout=_QUOTE_TIMEOUT_S)


def place(
    quote_token: str,
    quote_id: str,
    shipping_option_id: str,
    shipping_address: dict[str, Any],
    *,
    provider: str = DEFAULT_PROVIDER,
    notes: str = "",
    approval_token: str = "",
) -> Answer:
    """The whole placing step: the servers' preview confirmation, their
    shipping confirmation, then the order.  A miss at any step is the order
    not placed, said as such; the quote token is forgotten once an order
    exists (it is single-use on the servers too)."""
    confirmed = preview_confirm(quote_token)
    if not confirmed.ok:
        return confirmed
    preview_token = str(confirmed.body.get("preview_confirmation_token") or "")
    shipped = shipping_confirm(quote_token, shipping_option_id, shipping_address)
    if not shipped.ok:
        return shipped
    shipping_token = str(shipped.body.get("shipping_confirmation_token") or "")
    if not preview_token or not shipping_token:
        return Answer(miss=Miss("unanswered", "KILN_API_HTTP_ERROR", "a confirmation with no token in it"))
    placed = order(
        quote_token, quote_id, shipping_option_id, shipping_address, preview_token, shipping_token,
        provider=provider, notes=notes, approval_token=approval_token,
    )
    if placed.ok:
        forget_quote_token(quote_id)
    return placed


def orders(limit: int = 10) -> Answer:
    """The person's own orders, newest first, with where each is now."""
    return _ask("GET", "/api/fulfillment/orders", params={"limit": int(limit or 10)})


def order_status(order_id: str, provider: str = DEFAULT_PROVIDER) -> Answer:
    """One order's state: ``{"tell_the_person": [...], "order": {...}}``."""
    from urllib.parse import quote as _urlquote

    return _ask(
        "GET", f"/api/fulfillment/order/{_urlquote(str(order_id), safe='')}/status",
        params={"provider": provider or DEFAULT_PROVIDER},
    )


def cancel(order_id: str, provider: str = DEFAULT_PROVIDER) -> Answer:
    """Cancel one order, as far as the print service still allows."""
    from urllib.parse import quote as _urlquote

    return _ask(
        "POST", f"/api/fulfillment/order/{_urlquote(str(order_id), safe='')}/cancel",
        json={"provider": provider or DEFAULT_PROVIDER},
    )


# ---------------------------------------------------------------------------
# the quote token, kept beside the quote
# ---------------------------------------------------------------------------

_TOKEN_KEY = "quote_token"


def remember_quote(
    body: dict[str, Any], *, file_path: str = "", material_id: str = "", quantity: int = 1,
    shipping_country: str = "", provider: str = DEFAULT_PROVIDER,
) -> None:
    """Keep the servers' quote token beside the quote, under the print
    service's quote id, in this computer's quote cache -- the record the
    order step reads when it is handed only a quote id.  Best-effort: a
    cache that cannot be written never costs the person the quote."""
    quoted = body.get("quote") if isinstance(body.get("quote"), dict) else {}
    quote_id = str(quoted.get("quote_id") or body.get("provider_quote_id") or "")
    token = str(body.get(_TOKEN_KEY) or "")
    if not quote_id or not token:
        return
    try:
        from kiln.quote_cache import cache_quote

        cache_quote(
            str(quoted.get("provider") or provider or DEFAULT_PROVIDER),
            str(quoted.get("material") or material_id or ""),
            str(material_id or quoted.get("material") or ""),
            int(quoted.get("quantity") or quantity or 1),
            float(quoted.get("total_price") or 0.0),
            str(quoted.get("currency") or "USD"),
            int(quoted.get("lead_time_days") or 0),
            quote_id=quote_id,
            metadata={
                _TOKEN_KEY: token,
                "file_path": file_path,
                "shipping_country": shipping_country,
                "shipping_options": list(quoted.get("shipping_options") or []),
                "served": True,
            },
        )
    except Exception:  # noqa: BLE001 -- the quote stands without the record
        logger.debug("order bridge: quote %s not remembered", quote_id, exc_info=True)


def quote_token_for(quote_id: str) -> str:
    """The servers' token for a quote this computer remembers, else ``""``."""
    try:
        from kiln.quote_cache import get_cached_quote_by_id

        cached = get_cached_quote_by_id(str(quote_id or ""))
    except Exception:  # noqa: BLE001
        return ""
    metadata = getattr(cached, "metadata", None) or {}
    return str(metadata.get(_TOKEN_KEY) or "") if isinstance(metadata, dict) else ""


def quoted_file_for(quote_id: str) -> str:
    """The file this computer quoted under *quote_id*, else ``""``: what the
    order step binds the approved preview to."""
    try:
        from kiln.quote_cache import get_cached_quote_by_id

        cached = get_cached_quote_by_id(str(quote_id or ""))
    except Exception:  # noqa: BLE001
        return ""
    metadata = getattr(cached, "metadata", None) or {}
    return str(metadata.get("file_path") or "") if isinstance(metadata, dict) else ""


def forget_quote_token(quote_id: str) -> None:
    """Drop the token once the servers have consumed it, so a second place
    with the same quote id is told to quote again rather than refused."""
    try:
        from kiln.quote_cache import get_cached_quote_by_id

        cached = get_cached_quote_by_id(str(quote_id or ""))
        if cached is not None and isinstance(getattr(cached, "metadata", None), dict):
            cached.metadata.pop(_TOKEN_KEY, None)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# wording a miss
# ---------------------------------------------------------------------------


#: What an order or a cancel that got NO ANSWER is: the request may have left
#: this computer and reached the servers, so Kiln cannot say it placed (or
#: cancelled) nothing.  It says it does not know, and sends the person to the
#: order's status before they try again; the servers' single-use quote token
#: is what stops a second placing, never this sentence.
_UNANSWERED_ACTION: dict[str, tuple[str, str]] = {
    "order": ("doesn't know whether the order was placed", "check the order's status before ordering again"),
    "cancel": ("doesn't know whether it was cancelled", "check the order's status before trying again"),
}


def words(miss: Miss, door: str) -> str:
    """The one sentence for *miss* at *door* (a key of :data:`WORDS`)."""
    on_the_line, cannot, wont = WORDS[door]
    then = "try again"
    if miss.cause == "unanswered" and door in _UNANSWERED_ACTION:
        wont, then = _UNANSWERED_ACTION[door]
    return sentence(miss, feature="servers", on_the_line=on_the_line, cannot=cannot, wont=wont, then=then)


def not_offered(miss: Miss | None) -> bool:
    """Whether *miss* is the servers having no ordering routes at all."""
    return miss is not None and miss.code == NOT_OFFERED_CODE


def refusal_fields(answer: Answer, door: str) -> dict[str, Any]:
    """The machine-readable twin of a miss, to splat beside a door's error:
    ``why`` / ``why_code`` / ``why_detail``, the refusal's ``details`` when the
    servers attached any, and the sign-in hint when a sign-in is the fix."""
    out: dict[str, Any] = dict(fields(answer.miss))
    if answer.details:
        out["details"] = dict(answer.details)
    if answer.miss is not None and answer.miss.cause == "signed_out":
        from kiln.tiers_and_terms import signin_hint_fields

        out.update(signin_hint_fields())
    return out


# ---------------------------------------------------------------------------
# folding a served reply into the shape the local doors answer with
# ---------------------------------------------------------------------------

_ENVELOPE_KEYS = frozenset({"success", "tell_the_person"})


def folded(body: dict[str, Any], inner: str) -> dict[str, Any]:
    """The servers' reply with everything beside *inner* (``quote`` /
    ``order``) folded into it -- the fee, the price sheet, the print
    service's terms, the quote token -- which is where the local doors have
    always put them.  The reply's ``tell_the_person`` stays outside."""
    record = dict(body.get(inner) or {}) if isinstance(body.get(inner), dict) else {}
    for key, value in body.items():
        if key in _ENVELOPE_KEYS or key == inner:
            continue
        record.setdefault(key, value)
    return record


# ---------------------------------------------------------------------------
# the order service a plain install's terminal commands run with
# ---------------------------------------------------------------------------


class ServedRefusal(Exception):
    """The order service's error type on a plain install: a miss, worded.

    Carries the miss's ``code`` and the refusal's ``details`` so the
    terminal's one error formatter (:func:`kiln.cli.output.format_error`)
    can answer with the sentence and code as they are, rather than the
    command's own wrapping ("verify the material ID"), which would be the
    wrong remedy for an install that is offline or signed out.
    """

    def __init__(self, message: str, code: str = "", *, miss: Miss | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code or (miss.code if miss is not None else "") or "KILN_API_HTTP_ERROR"
        self.miss = miss
        self.details = details

    @classmethod
    def of(cls, answer: Answer, door: str) -> ServedRefusal:
        miss = answer.miss or Miss("unanswered", "KILN_API_HTTP_ERROR", "not an answer")
        if not_offered(miss):
            return cls(NOT_INCLUDED, NOT_INCLUDED_CODE, miss=miss)
        return cls(words(miss, door), miss.code, miss=miss, details=answer.details)


def refusal_in_flight(exc: BaseException | None = None) -> ServedRefusal | None:
    """The served refusal behind the exception being handled, or ``None``.

    Follows the causes a failure names (``raise ... from``), so a command
    that re-wraps the refusal still answers with it.  The same shape as the
    one-printer refusal's reader (:func:`kiln.printers.engagement.refusal_in_flight`).
    """
    if exc is None:
        exc = sys.exc_info()[1]
    seen: set[int] = set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ServedRefusal):
            return exc
        exc = exc.__cause__
    return None


#: Fields the terminal reads as text (``.lower()``, a width format): a
#: record the servers left empty answers ``""`` for them, never ``None``.
_TEXT_FIELDS = frozenset({
    "id", "name", "technology", "color", "finish", "currency", "material", "material_id",
    "material_name", "provider", "status", "order_id", "order_number", "vendor",
    "tracking_number", "tracking_url", "checkout_url", "status_url", "payment_state",
})


class Record(dict):
    """A served record the terminal reads the way it reads the order
    service's own objects: by attribute, and as a dict (``to_dict``)."""

    reply: dict[str, Any]

    def __init__(self, data: dict[str, Any] | None = None, *, reply: dict[str, Any] | None = None) -> None:
        super().__init__(data or {})
        object.__setattr__(self, "reply", reply if reply is not None else {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = self.get(name)
        if value is None and name in _TEXT_FIELDS:
            return ""
        return value

    def to_dict(self) -> dict[str, Any]:
        return dict(self)


class _Request:
    """``QuoteRequest`` / ``OrderRequest`` as the terminal builds them: a bag
    of the fields it names.  Nothing here copies the order service's field
    list, so there is none to drift."""

    def __init__(self, **fields_: Any) -> None:
        self.__dict__.update(fields_)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return None


class ServedProvider:
    """The print service, reached through Kiln's servers.

    ``charges_kiln_fee`` is ``True``: the servers charge Kiln's fee when they
    place the order and say what they charged, so no door on this computer
    records or charges it (:func:`kiln.fulfillment_service.server_takes_fee`).
    """

    charges_kiln_fee = True

    def __init__(self, provider: str = DEFAULT_PROVIDER) -> None:
        self._provider = provider or DEFAULT_PROVIDER

    @property
    def name(self) -> str:
        return self._provider

    @property
    def display_name(self) -> str:
        return self._provider.capitalize()

    def list_materials(self) -> list[Record]:
        answer = materials(self._provider)
        if not answer.ok:
            raise ServedRefusal.of(answer, "materials")
        return [Record(m) for m in answer.body.get("materials") or [] if isinstance(m, dict)]

    def get_quote(self, request: Any) -> Record:
        answer = quote(
            str(request.file_path or ""), str(request.material_id or ""),
            int(request.quantity or 1), str(request.shipping_country or "US"),
            provider=self._provider,
        )
        if not answer.ok:
            raise ServedRefusal.of(answer, "quote")
        return Record(folded(answer.body, "quote"), reply=answer.body)

    def place_order(self, request: Any) -> Record:
        quote_id = str(request.quote_id or "")
        token = quote_token_for(quote_id)
        if not token:
            raise ServedRefusal(quote_token_unknown_message(quote_id), QUOTE_TOKEN_UNKNOWN_CODE)
        handed = str(getattr(request, "quote_token", "") or "")
        if handed and handed != token:
            raise ServedRefusal(
                f"The quote_token handed in is not the one Kiln holds for quote {quote_id!r}, so the "
                "order was not placed.", "QUOTE_TOKEN_MISMATCH",
            )
        answer = place(
            token, quote_id, str(request.shipping_option_id or ""), dict(request.shipping_address or {}),
            provider=self._provider, notes=str(request.notes or ""),
            approval_token=str(getattr(request, "approval_token", "") or ""),
        )
        if not answer.ok:
            raise ServedRefusal.of(answer, "order")
        placed = Record(folded(answer.body, "order"), reply=answer.body)
        charged = answer.body.get("kiln_fee")
        if isinstance(charged, dict):
            placed["kiln_fee_charged"] = dict(charged)
        return placed

    def get_order_status(self, order_id: str, **_ignored: Any) -> Record:
        answer = order_status(order_id, self._provider)
        if not answer.ok:
            raise ServedRefusal.of(answer, "status")
        return Record(folded(answer.body, "order"), reply=answer.body)

    def cancel_order(self, order_id: str, **_ignored: Any) -> Record:
        answer = cancel(order_id, self._provider)
        if not answer.ok:
            raise ServedRefusal.of(answer, "cancel")
        return Record(folded(answer.body, "order"), reply=answer.body)


def quote_token_unknown_message(quote_id: str) -> str:
    return (
        f"Kiln has no record of quote {quote_id!r} on this computer, so the order was not placed. "
        "An order is placed from a quote taken here (its token and its file are what the order "
        "is bound to): get the quote again (fulfillment_quote, or `kiln order quote`) and place "
        "the order from that quote."
    )


class _ServedHistory:
    """The order history as the terminal reads it, from the servers' list."""

    def list_orders(self, limit: int = 20, provider: str | None = None) -> list[Record]:
        answer = orders(limit)
        if not answer.ok:
            raise ServedRefusal.of(answer, "orders")
        rows = []
        for entry in answer.body.get("orders") or []:
            if not isinstance(entry, dict):
                continue
            if provider and str(entry.get("provider") or "") != provider:
                continue
            rows.append(Record(history_row(entry)))
        return rows


def history_row(entry: dict[str, Any]) -> dict[str, Any]:
    """One order of the servers' list in the row shape the terminal prints."""
    now = entry.get("now") if isinstance(entry.get("now"), dict) else {}
    contents = entry.get("contents") if isinstance(entry.get("contents"), dict) else {}
    total = entry.get("checkout_total_at_order")
    if not isinstance(total, (int, float)):
        total = entry.get("print_price")
    return {
        **entry,
        "status": str(now.get("status") or entry.get("recorded_status") or "unknown"),
        "total_price": float(total) if isinstance(total, (int, float)) else 0.0,
        "material_id": str(contents.get("material") or contents.get("material_id") or ""),
    }


class ServedOrderService:
    """The order service a plain install runs with: the same names the
    ``kiln order`` commands read from the order service kiln-pro installs
    (``get_provider``, ``QuoteRequest``, ``OrderRequest``,
    ``FulfillmentError``, ``get_order_history``, the wording functions),
    each answered by Kiln's servers.  The wording functions hand back the
    servers' own ``tell_the_person`` lines, so a quote or an order reads the
    same at the terminal as through the agent tools."""

    FulfillmentError = ServedRefusal
    QuoteRequest = _Request
    OrderRequest = _Request
    #: Said once by every door that reads it: this service orders through the servers.
    served = True

    def get_provider(self, name: str | None = None) -> ServedProvider:
        return ServedProvider(name or DEFAULT_PROVIDER)

    def get_order_history(self) -> _ServedHistory:
        return _ServedHistory()

    def get_insurance_options(self, order_value: float) -> list[Any]:
        raise ServedRefusal(
            "Shipping insurance options aren't listed on this install; the print service's "
            "checkout shows any it offers for the order.",
            "NOT_SERVED_HERE",
        )

    @staticmethod
    def price_sheet(quoted: Any, *, kiln_fee: float = 0.0) -> Any:
        reply = getattr(quoted, "reply", None) or {}
        return reply.get("price_sheet") if isinstance(reply, dict) else None

    @staticmethod
    def quote_message(quoted: Any, _sheet: Any = None, _routing: Any = None, **_kw: Any) -> list[str]:
        return _tell(quoted)

    @staticmethod
    def order_message(placed: Any, _routing: Any = None, **_kw: Any) -> list[str]:
        return _tell(placed)

    @staticmethod
    def status_message(status: Any, **_kw: Any) -> list[str]:
        return _tell(status)


def _tell(record: Any) -> list[str]:
    reply = getattr(record, "reply", None) or {}
    lines = reply.get("tell_the_person") if isinstance(reply, dict) else None
    return [str(line) for line in lines] if isinstance(lines, list) else []


def served_service() -> ServedOrderService:
    """The order service for an install without one (every plain install)."""
    return ServedOrderService()


__all__ = [
    "DEFAULT_PROVIDER",
    "HOSTED_API_URL",
    "NOT_OFFERED_CODE",
    "QUOTE_FILE_SUFFIXES",
    "QUOTE_TOKEN_UNKNOWN_CODE",
    "WORDS",
    "Answer",
    "Record",
    "ServedOrderService",
    "ServedProvider",
    "ServedRefusal",
    "api_base",
    "cancel",
    "folded",
    "forget_quote_token",
    "materials",
    "not_offered",
    "order",
    "order_status",
    "orders",
    "place",
    "preview_confirm",
    "quote",
    "quote_token_for",
    "quote_token_unknown_message",
    "quoted_file_for",
    "refusal_fields",
    "refusal_in_flight",
    "remember_quote",
    "served_service",
    "shipping_confirm",
    "words",
]
