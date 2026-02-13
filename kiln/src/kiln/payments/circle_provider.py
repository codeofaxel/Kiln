"""Circle USDC payment provider — Programmable Wallets (W3S) API.

Implements :class:`~kiln.payments.base.PaymentProvider` using the
`Circle Programmable Wallets API <https://developers.circle.com/w3s/reference>`_
for developer-controlled USDC payments on Solana and Base networks.

This adapter uses Circle's W3S (Web3 Services) transfer endpoint to send
USDC payouts on-chain from a developer-controlled wallet.

Environment variables
---------------------
``KILN_CIRCLE_API_KEY``
    API key for authenticating with the Circle API.
``KILN_CIRCLE_ENTITY_SECRET``
    64-character hex string (32 bytes) used for entity secret encryption.
    Generated once via :meth:`CircleProvider.setup_entity_secret`.
``KILN_CIRCLE_WALLET_ID``
    UUID of the developer-controlled wallet to send transfers from.
    Created via :meth:`CircleProvider.setup_wallet`.
``KILN_CIRCLE_WALLET_SET_ID``
    UUID of the wallet set (needed for wallet creation).
``KILN_CIRCLE_NETWORK``
    Default blockchain network (``"solana"`` or ``"base"``).
"""

import base64
import logging
import os
import re
import secrets
import uuid
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import RequestException, Timeout

from kiln.payments.base import (
    Currency,
    PaymentError,
    PaymentProvider,
    PaymentRail,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.circle.com"

# Map W3S transaction states to PaymentStatus.
_W3S_STATUS_MAP: Dict[str, PaymentStatus] = {
    "COMPLETE": PaymentStatus.COMPLETED,
    "CONFIRMED": PaymentStatus.PROCESSING,
    "SENT": PaymentStatus.PROCESSING,
    "QUEUED": PaymentStatus.PROCESSING,
    "INITIATED": PaymentStatus.PROCESSING,
    "FAILED": PaymentStatus.FAILED,
    "CANCELLED": PaymentStatus.CANCELLED,
    "DENIED": PaymentStatus.FAILED,
}

# USDC token contract addresses per blockchain.
_USDC_ADDRESSES: Dict[str, str] = {
    "SOL": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "SOL-DEVNET": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
    "ETH": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "BASE": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "BASE-SEPOLIA": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

_RAIL_TO_CHAIN: Dict[PaymentRail, str] = {
    PaymentRail.SOLANA: "SOL",
    PaymentRail.BASE: "BASE",
}


def _encrypt_entity_secret(entity_secret_hex: str, public_key_pem: str) -> str:
    """Encrypt the entity secret with Circle's RSA public key.

    Uses RSA-OAEP with SHA-256 as required by the W3S API.

    Args:
        entity_secret_hex: 64-character hex string (32 bytes).
        public_key_pem: PEM-encoded RSA public key from Circle.

    Returns:
        Base64-encoded ciphertext.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

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


class CircleProvider(PaymentProvider):
    """Concrete :class:`PaymentProvider` backed by Circle's Programmable Wallets API.

    Routes USDC payments to Solana or Base depending on the requested
    :class:`PaymentRail`, using developer-controlled wallets.

    Args:
        api_key: Circle API key.  If not provided, reads from
            ``KILN_CIRCLE_API_KEY``.
        entity_secret: 64-character hex string for entity secret encryption.
            If not provided, reads from ``KILN_CIRCLE_ENTITY_SECRET``.
            Can be empty at construction (only needed for mutating calls).
        wallet_id: UUID of the developer-controlled wallet to send from.
            If not provided, reads from ``KILN_CIRCLE_WALLET_ID``.
            Can be empty at construction (only needed for transfers).
        default_network: Default blockchain network when the payment
            request does not specify a rail (``"solana"`` or ``"base"``).
        base_url: Base URL for the Circle API (no ``/v1`` suffix).

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        entity_secret: Optional[str] = None,
        wallet_id: Optional[str] = None,
        default_network: str = "solana",
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_CIRCLE_API_KEY", "")
        self._entity_secret = entity_secret or os.environ.get(
            "KILN_CIRCLE_ENTITY_SECRET", ""
        )
        self._wallet_id = wallet_id or os.environ.get("KILN_CIRCLE_WALLET_ID", "")
        self._wallet_set_id = os.environ.get("KILN_CIRCLE_WALLET_SET_ID", "")
        self._base_url = base_url.rstrip("/")
        self._default_network = default_network or os.environ.get(
            "KILN_CIRCLE_NETWORK", "solana"
        )

        if not self._api_key:
            raise ValueError(
                "Circle API key required. "
                "Set KILN_CIRCLE_API_KEY or pass api_key."
            )

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

        # Lazily fetched and cached RSA public key for entity secret encryption.
        self._public_key_pem: Optional[str] = None

    # -- PaymentProvider identity ---------------------------------------------

    @property
    def name(self) -> str:
        return "circle"

    @property
    def supported_currencies(self) -> list:
        return [Currency.USDC]

    @property
    def rail(self) -> PaymentRail:
        if self._default_network == "base":
            return PaymentRail.BASE
        return PaymentRail.SOLANA

    # -- Internal HTTP helpers ------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute an authenticated HTTP request to the Circle API.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path (e.g. ``/v1/w3s/developer/transactions/transfer``).
            **kwargs: Extra keyword arguments forwarded to
                :meth:`requests.Session.request`.

        Returns:
            Parsed JSON response body.

        Raises:
            PaymentError: On timeout, connection failure, or HTTP error.
        """
        url = self._url(path)
        try:
            response = self._session.request(method, url, timeout=30, **kwargs)

            if not response.ok:
                raise PaymentError(
                    f"Circle API returned HTTP {response.status_code} "
                    f"for {method} {path}: {response.text[:300]}",
                    code=f"HTTP_{response.status_code}",
                )

            try:
                return response.json()
            except ValueError:
                return {"status": "ok"}

        except Timeout as exc:
            raise PaymentError(
                "Circle API timeout",
                code="TIMEOUT",
            ) from exc
        except ReqConnectionError as exc:
            raise PaymentError(
                "Cannot reach Circle API",
                code="CONNECTION_ERROR",
            ) from exc
        except PaymentError:
            raise
        except RequestException as exc:
            raise PaymentError(
                f"Request error for {method} {path}: {exc}",
                code="REQUEST_ERROR",
            ) from exc

    # -- W3S Helpers ----------------------------------------------------------

    def _get_public_key(self) -> str:
        """Fetch (and cache) Circle's RSA public key for entity secret encryption.

        Returns:
            PEM-encoded RSA public key string.

        Raises:
            PaymentError: If the key cannot be fetched.
        """
        if self._public_key_pem is not None:
            return self._public_key_pem

        data = self._request("GET", "/v1/w3s/config/entity/publicKey")
        pem = data.get("data", {}).get("publicKey", "")
        if not pem:
            raise PaymentError(
                "Circle API did not return an entity public key.",
                code="MISSING_PUBLIC_KEY",
            )
        self._public_key_pem = pem
        return pem

    def _get_entity_secret_ciphertext(self) -> str:
        """Encrypt the entity secret for a single API call.

        Each mutating W3S request requires a freshly encrypted ciphertext.

        Returns:
            Base64-encoded RSA-OAEP ciphertext.

        Raises:
            PaymentError: If entity secret is not configured.
        """
        if not self._entity_secret:
            raise PaymentError(
                "Entity secret required for mutating W3S calls. "
                "Set KILN_CIRCLE_ENTITY_SECRET or run setup_entity_secret().",
                code="MISSING_ENTITY_SECRET",
            )
        pem = self._get_public_key()
        return _encrypt_entity_secret(self._entity_secret, pem)

    def _resolve_chain(self, rail: PaymentRail) -> str:
        """Map a :class:`PaymentRail` to a Circle W3S blockchain identifier.

        Falls back to *default_network* when the rail is not explicitly
        mapped (e.g. ``PaymentRail.CIRCLE``).
        """
        if rail in _RAIL_TO_CHAIN:
            return _RAIL_TO_CHAIN[rail]
        # Fall back to default network
        default_rail = (
            PaymentRail.SOLANA
            if self._default_network == "solana"
            else PaymentRail.BASE
        )
        return _RAIL_TO_CHAIN.get(default_rail, "SOL")

    def _get_usdc_token_address(self, blockchain: str) -> str:
        """Return the USDC token contract address for the given blockchain.

        Args:
            blockchain: Circle blockchain identifier (e.g. ``"SOL"``, ``"BASE"``).

        Returns:
            USDC token address string.

        Raises:
            PaymentError: If the blockchain is not supported.
        """
        address = _USDC_ADDRESSES.get(blockchain)
        if not address:
            raise PaymentError(
                f"No USDC token address configured for blockchain {blockchain!r}. "
                f"Supported: {list(_USDC_ADDRESSES.keys())}",
                code="UNSUPPORTED_BLOCKCHAIN",
            )
        return address

    # -- Setup methods (for initial configuration) ----------------------------

    def setup_entity_secret(self) -> Dict[str, str]:
        """Generate and register an entity secret with Circle.

        This is a one-time setup step.  The returned hex string must be saved
        as ``KILN_CIRCLE_ENTITY_SECRET`` in the environment.

        Returns:
            Dictionary with ``entity_secret`` (64-char hex) and
            ``recovery_file`` (base64-encoded recovery data).

        Raises:
            PaymentError: If registration fails.
        """
        # Generate a random 32-byte (64 hex chars) entity secret
        entity_secret_hex = secrets.token_hex(32)

        # Encrypt it with Circle's public key
        pem = self._get_public_key()
        ciphertext = _encrypt_entity_secret(entity_secret_hex, pem)

        # Register the entity secret
        payload = {
            "entitySecretCiphertext": ciphertext,
        }
        data = self._request(
            "POST", "/v1/w3s/config/entity/entitySecret", json=payload
        )

        recovery_file = data.get("data", {}).get("recoveryFile", "")

        logger.info("Entity secret registered with Circle successfully.")

        # Store it for use in this session
        self._entity_secret = entity_secret_hex

        return {
            "entity_secret": entity_secret_hex,
            "recovery_file": recovery_file,
        }

    def setup_wallet(self, blockchain: str = "SOL") -> Dict[str, str]:
        """Create a wallet set and wallet for developer-controlled transfers.

        This is a one-time setup step.  The returned wallet ID must be saved
        as ``KILN_CIRCLE_WALLET_ID`` in the environment.

        Args:
            blockchain: Blockchain to create the wallet on (default ``"SOL"``).

        Returns:
            Dictionary with ``wallet_set_id``, ``wallet_id``, and ``address``.

        Raises:
            PaymentError: If wallet creation fails or entity secret is missing.
        """
        ciphertext = self._get_entity_secret_ciphertext()

        # Step 1: Create wallet set (if we don't have one)
        wallet_set_id = self._wallet_set_id
        if not wallet_set_id:
            ws_payload = {
                "idempotencyKey": str(uuid.uuid4()),
                "entitySecretCiphertext": ciphertext,
                "name": f"kiln-{blockchain.lower()}",
            }
            ws_data = self._request(
                "POST", "/v1/w3s/developer/walletSets", json=ws_payload
            )
            wallet_set = ws_data.get("data", {}).get("walletSet", {})
            wallet_set_id = wallet_set.get("id", "")
            if not wallet_set_id:
                raise PaymentError(
                    "Circle API did not return a wallet set ID.",
                    code="MISSING_WALLET_SET_ID",
                )
            self._wallet_set_id = wallet_set_id
            logger.info("Created wallet set: %s", wallet_set_id)

        # Step 2: Create wallet — need fresh ciphertext
        ciphertext = self._get_entity_secret_ciphertext()
        w_payload = {
            "idempotencyKey": str(uuid.uuid4()),
            "entitySecretCiphertext": ciphertext,
            "blockchains": [blockchain],
            "count": 1,
            "walletSetId": wallet_set_id,
        }
        w_data = self._request(
            "POST", "/v1/w3s/developer/wallets", json=w_payload
        )
        wallets = w_data.get("data", {}).get("wallets", [])
        if not wallets:
            raise PaymentError(
                "Circle API did not return any wallets.",
                code="MISSING_WALLET",
            )

        wallet = wallets[0]
        wallet_id = wallet.get("id", "")
        address = wallet.get("address", "")

        if not wallet_id:
            raise PaymentError(
                "Circle API did not return a wallet ID.",
                code="MISSING_WALLET_ID",
            )

        self._wallet_id = wallet_id
        logger.info(
            "Created wallet %s on %s with address %s",
            wallet_id,
            blockchain,
            address,
        )

        return {
            "wallet_set_id": wallet_set_id,
            "wallet_id": wallet_id,
            "address": address,
        }

    def get_wallet_balance(self, wallet_id: Optional[str] = None) -> Dict[str, Any]:
        """Check the USDC balance of a wallet.

        Args:
            wallet_id: Wallet UUID.  Defaults to the configured wallet.

        Returns:
            Dictionary with ``wallet_id`` and ``balances`` list.

        Raises:
            PaymentError: If the wallet ID is not configured or query fails.
        """
        wid = wallet_id or self._wallet_id
        if not wid:
            raise PaymentError(
                "Wallet ID required. Set KILN_CIRCLE_WALLET_ID or pass wallet_id.",
                code="MISSING_WALLET_ID",
            )

        data = self._request("GET", f"/v1/w3s/wallets/{wid}/balances")
        token_balances = data.get("data", {}).get("tokenBalances", [])

        balances: List[Dict[str, Any]] = []
        for tb in token_balances:
            balances.append(
                {
                    "token_id": tb.get("token", {}).get("id", ""),
                    "symbol": tb.get("token", {}).get("symbol", ""),
                    "amount": tb.get("amount", "0"),
                    "blockchain": tb.get("token", {}).get("blockchain", ""),
                }
            )

        return {
            "wallet_id": wid,
            "balances": balances,
        }

    # -- PaymentProvider methods ----------------------------------------------

    def create_payment(self, request: PaymentRequest) -> PaymentResult:
        """Create a USDC transfer via the Circle W3S API.

        Sends a ``POST /v1/w3s/developer/transactions/transfer`` request and
        returns immediately with the initial transfer status (typically
        ``PROCESSING``).  Use :meth:`get_payment_status` to poll for finality.

        Args:
            request: Payment parameters including amount, rail, and job ID.

        Returns:
            Transfer outcome -- usually with ``PROCESSING`` status.
            Call ``get_payment_status`` to check for completion.

        Raises:
            PaymentError: If the transfer cannot be initiated.
        """
        if not self._wallet_id:
            raise PaymentError(
                "Wallet ID required for transfers. "
                "Set KILN_CIRCLE_WALLET_ID or run setup_wallet().",
                code="MISSING_WALLET_ID",
            )

        chain = self._resolve_chain(request.rail)

        # Validate destination address before constructing payload
        dest_address = request.metadata.get("destination_address", "")
        if not dest_address:
            return PaymentResult(
                success=False,
                payment_id="",
                status=PaymentStatus.FAILED,
                amount=request.amount,
                currency=request.currency,
                rail=request.rail,
                error="destination_address is required in metadata.",
            )
        # Basic format validation -- Ethereum addresses are 42 chars (0x + 40 hex),
        # Solana addresses are 32-44 chars base58.
        if not re.match(
            r"^(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$", dest_address
        ):
            return PaymentResult(
                success=False,
                payment_id="",
                status=PaymentStatus.FAILED,
                amount=request.amount,
                currency=request.currency,
                rail=request.rail,
                error=f"Invalid destination address format: {dest_address[:20]}...",
            )

        # Get USDC token address for the chain
        token_address = self._get_usdc_token_address(chain)

        # Encrypt entity secret (fresh ciphertext per request)
        ciphertext = self._get_entity_secret_ciphertext()

        payload = {
            "idempotencyKey": str(uuid.uuid4()),
            "entitySecretCiphertext": ciphertext,
            "walletId": self._wallet_id,
            "destinationAddress": dest_address,
            "amounts": [f"{request.amount:.2f}"],
            "tokenAddress": token_address,
            "blockchain": chain,
            "feeLevel": "MEDIUM",
        }

        logger.info(
            "Creating Circle W3S transfer: %.2f USDC on %s for job %s",
            request.amount,
            chain,
            request.job_id,
        )

        data = self._request(
            "POST", "/v1/w3s/developer/transactions/transfer", json=payload
        )

        # W3S returns transaction IDs in data.challengeId or data.transactionIds
        # For developer-controlled wallets, we get transactionIds directly.
        tx_ids = data.get("data", {}).get("transactionIds", [])
        if not tx_ids:
            # Some responses nest the transaction differently
            tx_id = data.get("data", {}).get("id", "")
            if not tx_id:
                raise PaymentError(
                    "Circle W3S API did not return a transaction ID.",
                    code="MISSING_ID",
                )
        else:
            tx_id = tx_ids[0]

        logger.info(
            "Circle W3S transfer %s created for job %s",
            tx_id,
            request.job_id,
        )

        return PaymentResult(
            success=False,  # Not yet complete
            payment_id=tx_id,
            status=PaymentStatus.PROCESSING,
            amount=request.amount,
            currency=request.currency,
            rail=request.rail,
        )

    def get_payment_status(self, payment_id: str) -> PaymentResult:
        """Check status of an existing Circle W3S transaction.

        Calls ``GET /v1/w3s/transactions/{payment_id}`` and maps the W3S
        transaction state to :class:`PaymentStatus`.

        Args:
            payment_id: The Circle W3S transaction ID.

        Returns:
            Current transaction state.

        Raises:
            PaymentError: If the transaction cannot be queried.
        """
        data = self._request("GET", f"/v1/w3s/transactions/{payment_id}")

        transaction = data.get("data", {}).get("transaction", {})
        if not transaction:
            # Fallback: some responses put data directly
            transaction = data.get("data", data)

        state = transaction.get("state", "INITIATED")
        mapped_status = _W3S_STATUS_MAP.get(state, PaymentStatus.PROCESSING)

        # Extract amount from the transaction amounts array
        amounts = transaction.get("amounts", [])
        amount_val = float(amounts[0]) if amounts else 0.0

        tx_hash = transaction.get("txHash", None)

        return PaymentResult(
            success=mapped_status == PaymentStatus.COMPLETED,
            payment_id=payment_id,
            status=mapped_status,
            amount=amount_val,
            currency=Currency.USDC,
            rail=self.rail,
            tx_hash=tx_hash,
        )

    def refund_payment(self, payment_id: str) -> PaymentResult:
        """Refund a completed Circle W3S transaction.

        Retrieves the original transaction to determine the source address,
        then creates a new transfer from our wallet back to that address.

        Args:
            payment_id: The original transaction ID to refund.

        Returns:
            Refund outcome.

        Raises:
            PaymentError: If the refund cannot be processed.
        """
        if not self._wallet_id:
            raise PaymentError(
                "Wallet ID required for refunds. "
                "Set KILN_CIRCLE_WALLET_ID or run setup_wallet().",
                code="MISSING_WALLET_ID",
            )

        # Retrieve the original transaction
        original_data = self._request(
            "GET", f"/v1/w3s/transactions/{payment_id}"
        )
        transaction = original_data.get("data", {}).get("transaction", {})
        if not transaction:
            transaction = original_data.get("data", original_data)

        # Extract refund details from the original transaction
        original_state = transaction.get("state", "")
        if original_state not in ("COMPLETE", "CONFIRMED"):
            raise PaymentError(
                f"Cannot refund transaction in state {original_state!r}. "
                "Only COMPLETE or CONFIRMED transactions can be refunded.",
                code="INVALID_STATE_FOR_REFUND",
            )

        # The destination of the original transfer is where funds went.
        # For a refund, we send back to the source address.
        # In W3S, the sourceAddress is where the transfer originated from.
        refund_dest = transaction.get("sourceAddress", "")
        if not refund_dest:
            # If source address not available, try destinationAddress
            # (for inbound transfers, source is the external address)
            raise PaymentError(
                "Cannot determine refund destination: "
                "original transaction has no source address.",
                code="MISSING_SOURCE_ADDRESS",
            )

        amounts = transaction.get("amounts", [])
        if not amounts:
            raise PaymentError(
                f"Original transaction {payment_id} has no amount information.",
                code="MISSING_AMOUNT",
            )
        refund_amount = float(amounts[0])
        if refund_amount <= 0:
            raise PaymentError(
                f"Original transaction {payment_id} has zero or negative amount.",
                code="ZERO_REFUND_AMOUNT",
            )

        blockchain = transaction.get("blockchain", self._resolve_chain(self.rail))
        token_address = self._get_usdc_token_address(blockchain)

        # Create refund transfer — fresh ciphertext
        ciphertext = self._get_entity_secret_ciphertext()

        refund_payload = {
            "idempotencyKey": str(uuid.uuid4()),
            "entitySecretCiphertext": ciphertext,
            "walletId": self._wallet_id,
            "destinationAddress": refund_dest,
            "amounts": [f"{refund_amount:.2f}"],
            "tokenAddress": token_address,
            "blockchain": blockchain,
            "feeLevel": "MEDIUM",
        }

        logger.info(
            "Creating refund for Circle W3S transaction %s: %.2f USDC to %s",
            payment_id,
            refund_amount,
            refund_dest,
        )

        data = self._request(
            "POST", "/v1/w3s/developer/transactions/transfer", json=refund_payload
        )

        tx_ids = data.get("data", {}).get("transactionIds", [])
        if not tx_ids:
            refund_id = data.get("data", {}).get("id", "")
            if not refund_id:
                raise PaymentError(
                    "Circle W3S refund response missing transaction ID.",
                    code="MISSING_REFUND_ID",
                )
        else:
            refund_id = tx_ids[0]

        logger.info(
            "Circle W3S refund %s created for original transaction %s",
            refund_id,
            payment_id,
        )

        return PaymentResult(
            success=False,  # Refund is still processing
            payment_id=refund_id,
            status=PaymentStatus.REFUNDED,
            amount=refund_amount,
            currency=Currency.USDC,
            rail=self.rail,
        )

    def __repr__(self) -> str:
        return (
            f"<CircleProvider base_url={self._base_url!r} "
            f"default_network={self._default_network!r} "
            f"wallet_id={self._wallet_id!r}>"
        )
