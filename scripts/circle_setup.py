#!/usr/bin/env python3
"""One-time Circle Programmable Wallets setup script.

Generates an entity secret, registers it with Circle, creates a wallet set
and wallet, then prints all values needed for .env configuration.

Usage:
    pip install requests cryptography python-dotenv
    python scripts/circle_setup.py

Reads KILN_CIRCLE_API_KEY from /Users/adamarreola/Kiln/.env
"""

import base64
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
RECOVERY_PATH = Path(__file__).resolve().parent.parent / ".circle-recovery.txt"
BASE_URL = "https://api.circle.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_api_key() -> str:
    """Load KILN_CIRCLE_API_KEY from .env file."""
    if not ENV_PATH.exists():
        print(f"ERROR: .env file not found at {ENV_PATH}")
        sys.exit(1)

    load_dotenv(ENV_PATH)
    api_key = os.environ.get("KILN_CIRCLE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: KILN_CIRCLE_API_KEY not set in .env")
        sys.exit(1)

    return api_key


def make_session(api_key: str) -> requests.Session:
    """Create an authenticated requests session matching CircleProvider."""
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    return session


def api_request(
    session: requests.Session, method: str, path: str, **kwargs
) -> dict:
    """Execute an HTTP request against the Circle API.

    Mirrors CircleProvider._request — raises on non-2xx status.
    """
    url = f"{BASE_URL}{path}"
    response = session.request(method, url, timeout=30, **kwargs)
    if not response.ok:
        print(f"ERROR: Circle API returned HTTP {response.status_code}")
        print(f"  {method} {path}")
        print(f"  {response.text[:500]}")
        sys.exit(1)
    try:
        return response.json()
    except ValueError:
        return {"status": "ok"}


def encrypt_entity_secret(entity_secret_hex: str, public_key_pem: str) -> str:
    """Encrypt the entity secret with Circle's RSA public key.

    Uses RSA-OAEP with SHA-256 -- identical to
    ``circle_provider._encrypt_entity_secret``.
    """
    entity_secret_bytes = bytes.fromhex(entity_secret_hex)
    public_key = serialization.load_pem_public_key(public_key_pem.encode())
    encrypted = public_key.encrypt(
        entity_secret_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(encrypted).decode()


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------


def fetch_public_key(session: requests.Session) -> str:
    """Step 1: Fetch Circle's RSA public key for entity secret encryption."""
    print("[1/5] Fetching Circle RSA public key...")
    data = api_request(session, "GET", "/v1/w3s/config/entity/publicKey")
    pem = data.get("data", {}).get("publicKey", "")
    if not pem:
        print("ERROR: Circle API did not return an entity public key.")
        sys.exit(1)
    print("  -> Public key retrieved successfully.")
    return pem


def generate_and_register_entity_secret(
    session: requests.Session, public_key_pem: str
) -> dict:
    """Step 2-3: Generate a 32-byte entity secret, encrypt it, and register."""
    print("[2/5] Generating 32-byte random entity secret...")
    entity_secret_hex = secrets.token_hex(32)
    print(f"  -> Entity secret generated ({len(entity_secret_hex)} hex chars).")

    print("[3/5] Encrypting and registering entity secret with Circle...")
    ciphertext = encrypt_entity_secret(entity_secret_hex, public_key_pem)

    payload = {
        "entitySecretCiphertext": ciphertext,
    }
    data = api_request(
        session, "POST", "/v1/w3s/config/entity/entitySecret", json=payload
    )

    recovery_file = data.get("data", {}).get("recoveryFile", "")
    print("  -> Entity secret registered successfully.")

    return {
        "entity_secret": entity_secret_hex,
        "recovery_file": recovery_file,
    }


def create_wallet_set(
    session: requests.Session,
    entity_secret_hex: str,
    public_key_pem: str,
) -> str:
    """Step 4: Create a wallet set named 'Kiln3D Payments'."""
    print("[4/5] Creating wallet set 'Kiln3D Payments'...")
    ciphertext = encrypt_entity_secret(entity_secret_hex, public_key_pem)

    payload = {
        "idempotencyKey": str(uuid.uuid4()),
        "entitySecretCiphertext": ciphertext,
        "name": "Kiln3D Payments",
    }
    data = api_request(
        session, "POST", "/v1/w3s/developer/walletSets", json=payload
    )

    wallet_set = data.get("data", {}).get("walletSet", {})
    wallet_set_id = wallet_set.get("id", "")
    if not wallet_set_id:
        print("ERROR: Circle API did not return a wallet set ID.")
        sys.exit(1)

    print(f"  -> Wallet set created: {wallet_set_id}")
    return wallet_set_id


def create_wallet(
    session: requests.Session,
    entity_secret_hex: str,
    public_key_pem: str,
    wallet_set_id: str,
) -> dict:
    """Step 5: Create a SOL wallet inside the wallet set."""
    print("[5/5] Creating SOL wallet in wallet set...")
    # Fresh ciphertext for this request (matches CircleProvider pattern)
    ciphertext = encrypt_entity_secret(entity_secret_hex, public_key_pem)

    payload = {
        "idempotencyKey": str(uuid.uuid4()),
        "entitySecretCiphertext": ciphertext,
        "blockchains": ["SOL"],
        "count": 1,
        "walletSetId": wallet_set_id,
    }
    data = api_request(
        session, "POST", "/v1/w3s/developer/wallets", json=payload
    )

    wallets = data.get("data", {}).get("wallets", [])
    if not wallets:
        print("ERROR: Circle API did not return any wallets.")
        sys.exit(1)

    wallet = wallets[0]
    wallet_id = wallet.get("id", "")
    address = wallet.get("address", "")

    if not wallet_id:
        print("ERROR: Circle API did not return a wallet ID.")
        sys.exit(1)

    print(f"  -> Wallet created: {wallet_id}")
    print(f"  -> Blockchain address: {address}")

    return {
        "wallet_id": wallet_id,
        "address": address,
    }


def save_recovery_file(recovery_data: str, entity_secret_hex: str) -> None:
    """Save the recovery file with entity secret and recovery data."""
    timestamp = datetime.now(timezone.utc).isoformat()
    contents = (
        f"# Circle Programmable Wallets Recovery File\n"
        f"# Generated: {timestamp}\n"
        f"#\n"
        f"# IMPORTANT: Store this file securely. It is needed to recover\n"
        f"# your wallets if you lose access to your entity secret.\n"
        f"#\n"
        f"# Entity Secret (also store in KILN_CIRCLE_ENTITY_SECRET):\n"
        f"{entity_secret_hex}\n"
        f"#\n"
        f"# Recovery File Data (base64):\n"
        f"{recovery_data}\n"
    )
    RECOVERY_PATH.write_text(contents)
    print(f"\nRecovery file saved to: {RECOVERY_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 60)
    print("Circle Programmable Wallets - One-Time Setup")
    print("=" * 60)
    print()

    # Load API key
    api_key = load_api_key()
    masked = api_key[:20] + "..." if len(api_key) > 20 else api_key
    print(f"Using API key: {masked}")
    print()

    session = make_session(api_key)

    # Step 1: Fetch public key
    public_key_pem = fetch_public_key(session)

    # Steps 2-3: Generate and register entity secret
    secret_result = generate_and_register_entity_secret(session, public_key_pem)
    entity_secret_hex = secret_result["entity_secret"]
    recovery_data = secret_result["recovery_file"]

    # Step 4: Create wallet set
    wallet_set_id = create_wallet_set(
        session, entity_secret_hex, public_key_pem
    )

    # Step 5: Create wallet
    wallet_result = create_wallet(
        session, entity_secret_hex, public_key_pem, wallet_set_id
    )

    # Save recovery file
    save_recovery_file(recovery_data, entity_secret_hex)

    # Print summary
    print()
    print("=" * 60)
    print("Setup Complete! Add these to your .env file:")
    print("=" * 60)
    print()
    print(f"KILN_CIRCLE_ENTITY_SECRET={entity_secret_hex}")
    print(f"KILN_CIRCLE_WALLET_SET_ID={wallet_set_id}")
    print(f"KILN_CIRCLE_WALLET_ID={wallet_result['wallet_id']}")
    print()
    print(f"Wallet blockchain address (SOL): {wallet_result['address']}")
    print()
    print(
        "IMPORTANT: The recovery file at "
        f"{RECOVERY_PATH} contains sensitive data."
    )
    print("Store it securely and do NOT commit it to version control.")
    print()


if __name__ == "__main__":
    main()
