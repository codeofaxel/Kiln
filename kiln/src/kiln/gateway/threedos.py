"""3DOS distributed manufacturing gateway client.

3DOS connects designers with a global network of 3D printers.  This module
provides the client interface for Kiln to act as both a **provider** (offering
local printers to the network) and a **consumer** (routing jobs to remote
printers when local capacity is full).

The 3DOS integration is a key monetisation path for Kiln — every job
routed through the network generates referral revenue.

Environment variables
---------------------
``KILN_3DOS_API_KEY``
    API key for authenticating with the 3DOS platform.
``KILN_3DOS_BASE_URL``
    Base URL of the 3DOS API (defaults to production).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.3dos.io/v1"


@dataclass
class PrinterListing:
    """A printer registered on the 3DOS network."""

    id: str
    name: str
    location: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    available: bool = True
    price_per_gram: float | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetworkJob:
    """A print job on the 3DOS network."""

    id: str
    file_url: str
    material: str
    status: str
    printer_id: str | None = None
    estimated_cost: float | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ThreeDOSError(Exception):
    """Error communicating with the 3DOS platform."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ThreeDOSClient:
    """Client for the 3DOS distributed manufacturing API.

    Args:
        api_key: 3DOS API key.  If not provided, reads from
            ``KILN_3DOS_API_KEY`` environment variable.
        base_url: Base URL of the 3DOS API.

    Raises:
        ValueError: If no API key is available.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_3DOS_API_KEY", "")
        self._base_url = (base_url or os.environ.get("KILN_3DOS_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")

        if not self._api_key:
            raise ValueError("3DOS API key required. Set KILN_3DOS_API_KEY or pass api_key.")

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make an authenticated request to the 3DOS API."""
        url = f"{self._base_url}{path}"
        try:
            response = self._session.request(method, url, timeout=30, **kwargs)
            if not response.ok:
                raise ThreeDOSError(
                    f"3DOS API returned {response.status_code}: {response.text[:200]}",
                    status_code=response.status_code,
                )
            return response.json()
        except RequestException as exc:
            raise ThreeDOSError(f"Failed to reach 3DOS API: {exc}") from exc

    # ------------------------------------------------------------------
    # Provider API (offer local printers to the network)
    # ------------------------------------------------------------------

    def register_printer(
        self,
        name: str,
        location: str,
        capabilities: dict[str, Any] | None = None,
        price_per_gram: float | None = None,
    ) -> PrinterListing:
        """Register a local printer on the 3DOS network.

        Makes this printer available for remote jobs from the network.
        """
        payload = {
            "name": name,
            "location": location,
            "capabilities": capabilities or {},
            "price_per_gram": price_per_gram,
        }
        data = self._request("POST", "/printers", json=payload)
        return PrinterListing(
            id=data.get("id", ""),
            name=data.get("name", name),
            location=data.get("location", location),
            capabilities=data.get("capabilities", {}),
            available=data.get("available", True),
            price_per_gram=data.get("price_per_gram"),
        )

    def update_printer_status(self, printer_id: str, available: bool) -> None:
        """Update a registered printer's availability on the network."""
        self._request("PATCH", f"/printers/{printer_id}", json={"available": available})

    def list_my_printers(self) -> list[PrinterListing]:
        """List all printers registered by this account."""
        data = self._request("GET", "/printers/mine")
        return [
            PrinterListing(
                id=p.get("id", ""),
                name=p.get("name", ""),
                location=p.get("location", ""),
                capabilities=p.get("capabilities", {}),
                available=p.get("available", True),
                price_per_gram=p.get("price_per_gram"),
            )
            for p in data.get("printers", [])
        ]

    # ------------------------------------------------------------------
    # Consumer API (route jobs to the network)
    # ------------------------------------------------------------------

    def find_printers(
        self,
        material: str,
        location: str | None = None,
    ) -> list[PrinterListing]:
        """Search for available printers on the 3DOS network."""
        params: dict[str, str] = {"material": material}
        if location:
            params["location"] = location
        data = self._request("GET", "/printers/search", params=params)
        return [
            PrinterListing(
                id=p.get("id", ""),
                name=p.get("name", ""),
                location=p.get("location", ""),
                capabilities=p.get("capabilities", {}),
                available=p.get("available", True),
                price_per_gram=p.get("price_per_gram"),
                currency=p.get("currency", "USD"),
            )
            for p in data.get("printers", [])
        ]

    def submit_network_job(
        self,
        file_url: str,
        material: str,
        printer_id: str | None = None,
    ) -> NetworkJob:
        """Submit a print job to the 3DOS network.

        If ``printer_id`` is given, the job targets that specific printer.
        Otherwise, 3DOS auto-assigns to the best available printer.
        """
        payload: dict[str, Any] = {
            "file_url": file_url,
            "material": material,
        }
        if printer_id:
            payload["printer_id"] = printer_id
        data = self._request("POST", "/jobs", json=payload)
        return NetworkJob(
            id=data.get("id", ""),
            file_url=data.get("file_url", file_url),
            material=data.get("material", material),
            status=data.get("status", "submitted"),
            printer_id=data.get("printer_id"),
            estimated_cost=data.get("estimated_cost"),
            currency=data.get("currency", "USD"),
        )

    def get_network_job(self, job_id: str) -> NetworkJob:
        """Check the status of a network job."""
        data = self._request("GET", f"/jobs/{job_id}")
        return NetworkJob(
            id=data.get("id", job_id),
            file_url=data.get("file_url", ""),
            material=data.get("material", ""),
            status=data.get("status", "unknown"),
            printer_id=data.get("printer_id"),
            estimated_cost=data.get("estimated_cost"),
            currency=data.get("currency", "USD"),
        )
