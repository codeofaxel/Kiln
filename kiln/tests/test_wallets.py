"""Tests for kiln.wallets — wallet address configuration and donation info."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from kiln.wallets import (
    WalletInfo,
    get_donation_info,
    get_ethereum_wallet,
    get_solana_wallet,
)


class TestWalletInfo:
    """WalletInfo dataclass behaviour."""

    def test_display_name_with_domain(self):
        w = WalletInfo(address="0xabc", domain="kiln3d.eth", chain="ethereum")
        assert w.display_name() == "kiln3d.eth"

    def test_display_name_without_domain(self):
        w = WalletInfo(address="0xabc", chain="ethereum")
        assert w.display_name() == "0xabc"

    def test_to_dict_with_domain(self):
        w = WalletInfo(address="0xabc", domain="kiln3d.eth", chain="ethereum")
        d = w.to_dict()
        assert d == {"address": "0xabc", "domain": "kiln3d.eth", "chain": "ethereum"}

    def test_to_dict_without_domain(self):
        w = WalletInfo(address="0xabc", chain="ethereum")
        d = w.to_dict()
        assert d == {"address": "0xabc", "chain": "ethereum"}
        assert "domain" not in d

    def test_frozen(self):
        w = WalletInfo(address="0xabc", chain="ethereum")
        with pytest.raises(AttributeError):
            w.address = "0xdef"  # type: ignore[misc]


class TestGetSolanaWallet:
    """get_solana_wallet() returns correct defaults and env overrides."""

    def test_default_address(self):
        wallet = get_solana_wallet()
        assert wallet.address == "2jJUNvsDWGUrFqSVXokjS6253MPcMXGAhcvZsM5G55TS"
        assert wallet.domain == "kiln3d.sol"
        assert wallet.chain == "solana"

    def test_env_override(self):
        with patch.dict(os.environ, {"KILN_WALLET_SOLANA": "CustomSolAddr123"}):
            wallet = get_solana_wallet()
            assert wallet.address == "CustomSolAddr123"
            assert wallet.domain is None  # domain cleared for non-default address

    def test_env_override_clears_domain(self):
        with patch.dict(os.environ, {"KILN_WALLET_SOLANA": "other"}):
            wallet = get_solana_wallet()
            assert wallet.domain is None


class TestGetEthereumWallet:
    """get_ethereum_wallet() returns correct defaults and env overrides."""

    def test_default_address(self):
        wallet = get_ethereum_wallet()
        assert wallet.address == "0xe46D8557C3d93632e2D519Ebe9e42daff869217a"
        assert wallet.domain == "kiln3d.eth"
        assert wallet.chain == "ethereum"

    def test_env_override(self):
        with patch.dict(os.environ, {"KILN_WALLET_ETHEREUM": "0xCustom"}):
            wallet = get_ethereum_wallet()
            assert wallet.address == "0xCustom"
            assert wallet.domain is None

    def test_env_override_clears_domain(self):
        with patch.dict(os.environ, {"KILN_WALLET_ETHEREUM": "0xOther"}):
            wallet = get_ethereum_wallet()
            assert wallet.domain is None


class TestGetDonationInfo:
    """get_donation_info() returns structured donation data."""

    def test_has_required_keys(self):
        info = get_donation_info()
        assert "message" in info
        assert "wallets" in info
        assert "note" in info

    def test_wallets_structure(self):
        info = get_donation_info()
        assert "solana" in info["wallets"]
        assert "ethereum" in info["wallets"]

        sol = info["wallets"]["solana"]
        assert sol["address"] == "2jJUNvsDWGUrFqSVXokjS6253MPcMXGAhcvZsM5G55TS"
        assert sol["domain"] == "kiln3d.sol"
        assert "SOL" in sol["accepts"]
        assert "USDC" in sol["accepts"]

        eth = info["wallets"]["ethereum"]
        assert eth["address"] == "0xe46D8557C3d93632e2D519Ebe9e42daff869217a"
        assert eth["domain"] == "kiln3d.eth"
        assert "ETH" in eth["accepts"]
        assert "USDC" in eth["accepts"]

    def test_env_override_reflected(self):
        with patch.dict(os.environ, {"KILN_WALLET_SOLANA": "CustomAddr"}):
            info = get_donation_info()
            assert info["wallets"]["solana"]["address"] == "CustomAddr"
            assert info["wallets"]["solana"]["domain"] is None
