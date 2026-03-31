"""MakerWorld marketplace adapter (metadata + URL construction).

Provides model discovery for Bambu Lab's MakerWorld platform.  Since
MakerWorld does not offer a public API and uses Cloudflare protection,
this adapter works by:

1. **Embedded metadata** — Extracting MakerWorld model identifiers from
   ``.gcode.3mf`` files that were downloaded from MakerWorld.  The 3MF
   metadata contains designer info, model IDs, and profile IDs.

2. **URL construction** — Building browseable MakerWorld URLs from known
   ID patterns so agents can direct users to the correct model page.

3. **Search** — Constructing MakerWorld search URLs (the actual search
   must be performed by the user in a browser due to Cloudflare).

This is a **metadata-only** adapter: it does NOT support direct file
downloads.  Users must download files through their browser or Bambu
Studio.

Environment variables
---------------------
(None required — this adapter works without authentication.)
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Any

from kiln.marketplaces.base import (
    MarketplaceAdapter,
    ModelDetail,
    ModelFile,
    ModelSummary,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://makerworld.com/en"
_SEARCH_URL = f"{_BASE_URL}/search/models"


# ---------------------------------------------------------------------------
# 3MF metadata extraction helpers
# ---------------------------------------------------------------------------


def resolve_makerworld_source(file_path: str) -> dict[str, Any] | None:
    """Extract MakerWorld model metadata from a .gcode.3mf file.

    Bambu Studio and OrcaSlicer embed MakerWorld metadata in .gcode.3mf
    files when models are downloaded from MakerWorld.  This function
    reads that metadata to identify the original model, designer, and
    construct a URL back to the model page.

    Extracted fields (when available):

    * **title** — model title
    * **designer** — creator username
    * **design_model_id** — MakerWorld internal model identifier
    * **design_profile_id** — print profile identifier
    * **license** — content license
    * **model_url** — constructed URL to the MakerWorld model page
    * **profile_url** — constructed URL including the print profile
    * **description** — model description (HTML-encoded)
    * **profile_description** — print profile notes

    Args:
        file_path: Path to a ``.3mf`` or ``.gcode.3mf`` file.

    Returns:
        Dict with extracted metadata and constructed URLs, or ``None``
        if the file does not contain MakerWorld metadata.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not a valid ZIP/3MF.
    """
    import html as _html
    import json as _json
    import xml.etree.ElementTree as ET

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not zipfile.is_zipfile(file_path):
        raise ValueError(f"Not a valid ZIP/3MF file: {file_path}")

    # Read model XML and plate metadata in a single open
    model_xml: str | None = None
    plate_json_raw: bytes | None = None

    with zipfile.ZipFile(file_path, "r") as zf:
        # Find the model XML
        for candidate in ["3D/3dmodel.model", "3d/3dmodel.model"]:
            if candidate in zf.namelist():
                model_xml = zf.read(candidate).decode("utf-8")
                break
        if model_xml is None:
            for name in zf.namelist():
                if name.lower().endswith(".model"):
                    model_xml = zf.read(name).decode("utf-8")
                    break

        # Read plate metadata while the archive is still open
        for candidate in ["Metadata/plate_1.json", "metadata/plate_1.json"]:
            if candidate in zf.namelist():
                plate_json_raw = zf.read(candidate)
                break

    if model_xml is None:
        raise ValueError(f"No 3D model metadata found in {file_path}")

    root = ET.fromstring(model_xml)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    # Extract all metadata fields
    meta: dict[str, str] = {}
    for el in root.findall(f"{ns}metadata"):
        name = el.get("name", "")
        text = el.text or ""
        if name and text:
            meta[name] = text

    # Check if this is from MakerWorld (look for telltale fields)
    design_model_id = meta.get("DesignModelId", "")
    design_profile_id = meta.get("DesignProfileId", "")
    designer = meta.get("Designer", "")
    title = meta.get("Title", "")

    if not design_model_id and not design_profile_id and not designer:
        return None

    # Construct URLs.
    # Empirically, the DesignProfileId maps to MakerWorld's model page
    # URL (e.g. makerworld.com/en/models/187258518).  The DesignModelId
    # is an opaque internal identifier (e.g. "US50765bb2a5bc94") and is
    # used as a fallback when the profile ID is absent.
    model_url = ""
    profile_url = ""
    if design_profile_id:
        model_url = f"{_BASE_URL}/models/{design_profile_id}"
        profile_url = model_url
    if design_model_id and not model_url:
        model_url = f"{_BASE_URL}/models/{design_model_id}"

    # Decode HTML entities in text fields (Bambu Studio double-encodes)
    def _decode(s: str) -> str:
        return _html.unescape(_html.unescape(s)) if s else ""

    result: dict[str, Any] = {
        "source": "makerworld",
        "title": _decode(title),
        "designer": designer,
        "designer_id": meta.get("DesignerUserId", ""),
        "design_model_id": design_model_id,
        "design_profile_id": design_profile_id,
        "license": meta.get("License", ""),
        "model_url": model_url,
        "profile_url": profile_url,
        "description": _decode(meta.get("Description", "")),
        "profile_title": _decode(meta.get("ProfileTitle", "")),
        "profile_description": _decode(meta.get("ProfileDescription", "")),
        "creation_date": meta.get("CreationDate", ""),
        "modification_date": meta.get("ModificationDate", ""),
        "origin": meta.get("Origin", ""),
        "application": meta.get("Application", ""),
    }

    # Include plate object names if available
    if plate_json_raw is not None:
        plate_data = _json.loads(plate_json_raw.decode("utf-8"))
        objects = plate_data.get("bbox_objects", [])
        result["plate_objects"] = [
            obj.get("name", f"object_{i}")
            for i, obj in enumerate(objects)
        ]

    return result


# ---------------------------------------------------------------------------
# MakerWorld marketplace adapter
# ---------------------------------------------------------------------------


class MakerWorldAdapter(MarketplaceAdapter):
    """Marketplace adapter for Bambu Lab's MakerWorld platform.

    This is a **metadata-only** adapter.  MakerWorld does not provide a
    public API and uses Cloudflare protection, so direct search and
    download are not possible from automated tools.

    The adapter provides:

    * **URL construction** — Builds MakerWorld model page URLs from
      identifiers embedded in ``.gcode.3mf`` files.
    * **Metadata extraction** — Reads designer, title, license, and
      print profile info from ``.gcode.3mf`` metadata.
    * **Search URL** — Constructs a MakerWorld search URL that the user
      can open in their browser.

    For file downloads, users should:

    1. Open the model URL in their browser, or
    2. Use Bambu Studio's built-in MakerWorld browser to download and
       open models directly.
    """

    @property
    def name(self) -> str:
        return "makerworld"

    @property
    def display_name(self) -> str:
        return "MakerWorld"

    @property
    def supports_download(self) -> bool:
        return False

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevant",
    ) -> list[ModelSummary]:
        """Construct a MakerWorld search URL.

        Since MakerWorld uses Cloudflare protection, automated search
        is not possible.  Returns a single ``ModelSummary`` entry
        containing the search URL that the user can open in their
        browser.
        """
        import urllib.parse

        search_url = f"{_SEARCH_URL}?keyword={urllib.parse.quote_plus(query)}"

        return [
            ModelSummary(
                id="search",
                name=f'MakerWorld search: "{query}"',
                url=search_url,
                creator="MakerWorld",
                source="makerworld",
                thumbnail=None,
                can_download=False,
                has_sliceable_files=False,
                is_free=True,
            ),
        ]

    def get_details(self, model_id: str) -> ModelDetail:
        """Construct model details from a MakerWorld model ID.

        Since we can't query the API directly, this constructs a URL
        and returns minimal metadata.
        """
        model_url = f"{_BASE_URL}/models/{model_id}"

        return ModelDetail(
            id=model_id,
            name=f"MakerWorld model {model_id}",
            url=model_url,
            creator="",
            source="makerworld",
            description=(
                f"View this model on MakerWorld: {model_url}\n\n"
                "To download: open the link in your browser or use "
                "Bambu Studio's built-in MakerWorld browser."
            ),
            can_download=False,
        )

    def get_files(self, model_id: str) -> list[ModelFile]:
        """List files for a MakerWorld model.

        Since we can't query the API directly, this returns an empty
        list with guidance on how to download.
        """
        logger.info(
            "MakerWorld file listing is not available via API. "
            "Use browser or Bambu Studio to download from: %s/models/%s",
            _BASE_URL,
            model_id,
        )
        return []
