"""Whether the plan saved with a sign-in is one Kiln's servers stated.

Kiln's servers sign the plan they tell this computer, and the proof is saved
beside the plan in the sign-in file.  A paid plan counts only while its proof
verifies against the public keys below, so a plan typed into the file, or
answered by anything other than Kiln's servers, reads as Free.

A proof covers the account, the plan, when it was issued, when it stops
counting, and the machine sign-in it was issued to (empty for a person's
session).  It is checked against the sign-in it sits beside:

* the signature verifies under one of :data:`PUBLIC_KEYS`;
* it names the plan the file claims and the account that is signed in;
* a proof issued to a machine sign-in is used by that machine sign-in;
* the time is inside its window (the window is the offline allowance).

A sign-in saved with no proof at all is honoured until
:data:`UNPROVEN_PLAN_HONOURED_UNTIL` and asked about as soon as the account
can be reached; after that moment it reads as Free until the account answers
with a proof.  A proof that is present and does not verify never counts.

Nothing here deletes or rewrites the sign-in file, and nothing here is asked
by a safety command.
"""

from __future__ import annotations

import base64
import calendar
import logging

logger = logging.getLogger(__name__)

__all__ = [
    "PROOF_FIELD",
    "PUBLIC_KEYS",
    "UNPROVEN_PLAN_HONOURED_UNTIL",
    "plan_is_proven",
    "proof_fields",
    "proof_present",
    "unproven_plan_still_honoured",
]

PROOF_FIELD = "plan_proof"
_PROOF_VERSION = 1
_DOMAIN = "kiln-plan-proof-v1"

#: The public halves of the keys Kiln's servers sign plans with (base64 of
#: the 32 raw Ed25519 bytes).  More than one only while a key is replaced.
PUBLIC_KEYS: tuple[str, ...] = (
    "C0gJD2uoVWAdRFT2c16AGLC0bwBlUn2apqzwmvGzAdQ=",
)

#: Until this moment (unix seconds, UTC) a paid plan saved with no proof is
#: honoured under the rules that held before proofs.  A fixed moment, never an
#: age read from the file: the file's own stamps are the person's to edit.
UNPROVEN_PLAN_HONOURED_UNTIL = float(calendar.timegm((2026, 12, 15, 0, 0, 0)))

#: A proof may be dated this far ahead of this computer's clock.
_CLOCK_SLACK_S = 24 * 3600.0


def proof_present(stored: dict) -> bool:
    """Whether the sign-in carries a proof at all, valid or not."""
    return bool(stored.get(PROOF_FIELD))


def proof_fields(answer: object) -> dict:
    """The proof an answer from Kiln's servers carries, as the fields to save
    with the sign-in; ``{}`` when it carries none."""
    proof = answer.get(PROOF_FIELD) if isinstance(answer, dict) else None
    return {PROOF_FIELD: proof} if isinstance(proof, dict) and proof else {}


def unproven_plan_still_honoured(now: float) -> bool:
    """Whether a plan saved with no proof still counts at *now*."""
    return now < UNPROVEN_PLAN_HONOURED_UNTIL


def _account_of(stored: dict) -> str:
    """The account the saved sign-in belongs to, or ``""``."""
    from kiln.auth_session import _jwt_claims

    subject = str(_jwt_claims(str(stored.get("access_token") or "")).get("sub") or "")
    return subject.strip() or str(stored.get("auth_uid") or "").strip()


def _message(proof: dict) -> bytes:
    return "\n".join(
        (
            _DOMAIN,
            str(proof["account"]),
            str(proof["tier"]),
            str(int(proof["issued_at"])),
            str(int(proof["expires_at"])),
            str(proof.get("machine") or ""),
        )
    ).encode("utf-8")


def _signature_verifies(proof: dict) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = base64.b64decode(str(proof["sig"]), validate=True)
    message = _message(proof)
    for encoded in PUBLIC_KEYS:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded)).verify(
                signature, message
            )
            return True
        except (InvalidSignature, ValueError):
            continue
    return False


def plan_is_proven(stored: dict, tier: str, now: float) -> bool:
    """Whether *stored* proves its account holds *tier* at *now*.

    ``False`` for a missing, malformed, altered, expired or misplaced proof,
    and for any fault while checking.  Never raises.
    """
    try:
        proof = stored.get(PROOF_FIELD)
        if not isinstance(proof, dict) or proof.get("v") != _PROOF_VERSION:
            return False
        if str(proof.get("tier") or "") != str(tier or "").strip().lower():
            return False
        account = _account_of(stored)
        if not account or str(proof.get("account") or "") != account:
            return False
        machine = str(proof.get("machine") or "")
        if machine and machine != str(stored.get("machine_id") or ""):
            return False
        issued, expires = float(proof["issued_at"]), float(proof["expires_at"])
        if issued > now + _CLOCK_SLACK_S or now > expires:
            return False
        return _signature_verifies(proof)
    except Exception:  # noqa: BLE001 - a proof that cannot be checked proves nothing
        logger.debug("plan proof: could not be checked", exc_info=True)
        return False
