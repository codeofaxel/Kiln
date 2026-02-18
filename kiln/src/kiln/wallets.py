"""Kiln project wallet addresses and ENS/SNS domain configuration.

Centralizes crypto wallet addresses used for:
- Receiving platform fees via Circle USDC transfers
- Accepting open-source donations / tips

Environment variable overrides
------------------------------
``KILN_WALLET_SOLANA``
    Override the default Solana receiving address.
``KILN_WALLET_ETHEREUM``
    Override the default Ethereum receiving address.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WalletInfo:
    """A blockchain wallet address with optional ENS/SNS domain."""

    address: str
    domain: str | None = None
    chain: str = ""

    def display_name(self) -> str:
        """Return the ENS/SNS domain if available, otherwise the address."""
        return self.domain or self.address

    def to_dict(self) -> dict:
        d: dict = {"address": self.address, "chain": self.chain}
        if self.domain:
            d["domain"] = self.domain
        return d


# -- Hardcoded project wallets ------------------------------------------------
# These are Kiln's official receiving wallets.  Env vars override for testing.

_DEFAULT_SOLANA_ADDRESS = "2jJUNvsDWGUrFqSVXokjS6253MPcMXGAhcvZsM5G55TS"
_DEFAULT_SOLANA_DOMAIN = "kiln3d.sol"

_DEFAULT_ETHEREUM_ADDRESS = "0xe46D8557C3d93632e2D519Ebe9e42daff869217a"
_DEFAULT_ETHEREUM_DOMAIN = "kiln3d.eth"


def get_solana_wallet() -> WalletInfo:
    """Return the Solana receiving wallet."""
    address = os.environ.get("KILN_WALLET_SOLANA", _DEFAULT_SOLANA_ADDRESS)
    domain = _DEFAULT_SOLANA_DOMAIN if address == _DEFAULT_SOLANA_ADDRESS else None
    return WalletInfo(address=address, domain=domain, chain="solana")


def get_ethereum_wallet() -> WalletInfo:
    """Return the Ethereum receiving wallet."""
    address = os.environ.get("KILN_WALLET_ETHEREUM", _DEFAULT_ETHEREUM_ADDRESS)
    domain = _DEFAULT_ETHEREUM_DOMAIN if address == _DEFAULT_ETHEREUM_ADDRESS else None
    return WalletInfo(address=address, domain=domain, chain="ethereum")


def get_donation_info() -> dict:
    """Return donation/tip info with all supported wallets.

    Suitable for displaying to users via MCP tool or CLI.
    """
    sol = get_solana_wallet()
    eth = get_ethereum_wallet()

    return {
        "message": ("Kiln is free, open-source software. If you find it useful, consider sending a tip!"),
        "wallets": {
            "solana": {
                "address": sol.address,
                "domain": sol.domain,
                "accepts": ["SOL", "USDC", "SPL tokens"],
            },
            "ethereum": {
                "address": eth.address,
                "domain": eth.domain,
                "accepts": ["ETH", "USDC", "ERC-20 tokens"],
            },
        },
        "note": ("You can send to the ENS/SNS domain names directly from any wallet that supports them."),
    }
