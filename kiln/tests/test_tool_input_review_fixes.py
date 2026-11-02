"""Behaviour pinned by the tool-input review (public half).

Each test here was shown failing on the tree before its fix landed and
passing after; the fixes are behavioural, so every assertion goes through
the tool door (the plugin registered against a fake MCP, or the server
tool function) and reads what a caller would get back.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest


def _tools_of(plugin) -> dict:
    captured: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    plugin.register(_MCP())
    return captured


# ---------------------------------------------------------------------------
# get_fulfillment_quote_cached reads the quote the quote tool cached
# ---------------------------------------------------------------------------


def test_cached_quote_tool_finds_the_quote_cached_for_that_file(monkeypatch, tmp_path):
    import kiln.quote_cache as qc
    from kiln import server

    monkeypatch.setattr(qc, "_quote_cache", None)
    model = str(tmp_path / "part.stl")
    qc.cache_quote(
        "craftcloud", "PLA White", "pla_white", 2, 12.5, "USD", 4,
        quote_id="q-1", metadata={"file_path": model, "shipping_country": "US"},
    )

    hit = server.get_fulfillment_quote_cached(model)
    assert hit["success"] is True, hit
    assert hit["quote"] is not None, hit
    assert hit["quote"]["quote_id"] == "q-1"

    narrowed = server.get_fulfillment_quote_cached(model, provider="craftcloud", material="pla_white")
    assert narrowed["quote"]["quote_id"] == "q-1"

    other_material = server.get_fulfillment_quote_cached(model, material="petg_black")
    assert other_material["success"] is True
    assert other_material["quote"] is None
    assert other_material["message"] == "No cached quote available"

    miss = server.get_fulfillment_quote_cached(str(tmp_path / "other.stl"))
    assert miss["quote"] is None


# ---------------------------------------------------------------------------
# The firmware tools take a component, never a version
# ---------------------------------------------------------------------------


def test_firmware_tools_name_a_component_and_promise_no_version_choice():
    from kiln.plugins.firmware_tools import plugin

    tools = _tools_of(plugin)
    for name in ("update_machine_firmware", "roll_back_machine_firmware"):
        tool = tools[name]
        params = inspect.signature(tool).parameters
        assert "component" in params, name
        assert "target_version" not in params and "version" not in params, name
        doc = (tool.__doc__ or "").lower()
        assert "pinning" not in doc and "specific version" not in doc, name
        assert "target version" not in doc, name


# ---------------------------------------------------------------------------
# print_service_quote "recommended" confirms what the quote recommended
# ---------------------------------------------------------------------------


def test_recommended_option_confirms_the_quotes_own_recommendation(tmp_path):
    from kiln.persistence import KilnDB
    from kiln.print_service import (
        PrintServiceRequest,
        confirm_print_order,
        create_print_order,
    )

    model = tmp_path / "part.stl"
    model.write_bytes(b"solid t\nendsolid t\n")
    db = KilnDB(db_path=str(tmp_path / "kiln.db"))
    try:
        with patch("kiln.persistence.get_db", return_value=db):
            quote = create_print_order(
                PrintServiceRequest(model_path=str(model), material="pla", prefer_local=False)
            )
            assert quote.recommended == "fulfillment"
            order = confirm_print_order(quote.order_id, option="recommended")
            assert order.provider == "fulfillment"

            # A budget that flips the recommendation flips the confirmation too.
            quote2 = create_print_order(
                PrintServiceRequest(
                    model_path=str(model), material="pla",
                    prefer_local=False, max_budget_usd=2.0,
                )
            )
            assert quote2.recommended == "local"
            order2 = confirm_print_order(quote2.order_id, option="recommended")
            assert order2.provider == "local"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# save_decoration: an explicit value beats the recipe sidecar
# ---------------------------------------------------------------------------


def test_explicit_material_mode_and_style_beat_the_recipe_sidecar(tmp_path):
    from kiln.plugins.decoration_library_tools import plugin

    model = tmp_path / "part.stl"
    model.write_bytes(b"solid t\nendsolid t\n")
    (tmp_path / ".kiln_recipe.json").write_text(json.dumps({
        "material": "PETG", "mode": "deboss", "image_style": "portrait",
    }))
    tool = _tools_of(plugin)["save_decoration"]
    seen: list[dict] = []

    def fake_save(name, **kwargs):
        seen.append(kwargs)
        return MagicMock()

    with patch("kiln.server._check_auth", return_value=None), \
         patch("kiln.decoration_library.save_decoration", fake_save):
        tool("Mark", str(model), content_type="text", content_data="hi",
             material="PLA", mode="emboss", image_style="auto")
        tool("Mark", str(model), content_type="text", content_data="hi")

    explicit, omitted = seen
    assert explicit["material"] == "PLA"
    assert explicit["mode"] == "emboss"
    assert explicit["image_style"] == "auto"
    assert omitted["material"] == "PETG"
    assert omitted["mode"] == "deboss"
    assert omitted["image_style"] == "portrait"


# ---------------------------------------------------------------------------
# bed_type: a plate name outside Kiln's vocabulary is refused at both doors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("door", ["start_print", "print_plate_object"])
def test_unknown_plate_name_is_refused_by_name(door):
    from kiln import server
    from kiln.printers.bambu_3mf import BED_TYPES

    with patch("kiln.server._check_auth", return_value=None), \
         patch("kiln.server._preview_gate_error", return_value=None):
        if door == "start_print":
            out = server.start_print("part.gcode", bed_type="pei_plate")
        else:
            out = server.print_plate_object("plate.3mf", 1, bed_type="pei_plate")
    assert out.get("success") is False, out
    assert out["error"]["code"] == "UNKNOWN_BED_TYPE", out
    message = out["error"]["message"]
    assert "pei_plate" in message
    for name in BED_TYPES:
        assert name in message
    assert "auto" in message


def test_a_known_plate_name_is_not_refused_as_unknown():
    from kiln import server

    with patch("kiln.server._check_auth", return_value=None):
        out = server.start_print("part.gcode", bed_type="textured_plate")
    assert (out.get("error") or {}).get("code") != "UNKNOWN_BED_TYPE"


# ---------------------------------------------------------------------------
# repair_mesh prose says what the weld does, not its threshold
# ---------------------------------------------------------------------------


def test_repair_mesh_docstring_carries_no_weld_threshold():
    from kiln.plugins.mesh_tools import plugin

    doc = _tools_of(plugin)["repair_mesh"].__doc__ or ""
    assert "µm" not in doc and "um)" not in doc
    assert "0.1" not in doc


# ---------------------------------------------------------------------------
# bed_type: one check at every door, read the way the plate name is read
# ---------------------------------------------------------------------------


def test_plate_name_is_read_without_regard_to_case():
    from kiln import server

    assert server._resolve_bed_type("Textured_Plate") == ("textured_plate", None)
    assert server._resolve_bed_type(" AUTO ") == ("auto", None)
    with patch("kiln.server._check_auth", return_value=None):
        out = server.start_print("part.gcode", bed_type="Textured_Plate")
    assert (out.get("error") or {}).get("code") != "UNKNOWN_BED_TYPE", out


def test_wrap_gcode_as_3mf_refuses_an_unknown_plate_by_name():
    from kiln import server
    from kiln.printers.bambu_3mf import BED_TYPES

    adapter = MagicMock()
    with patch("kiln.server._check_auth", return_value=None), \
         patch("kiln.server._get_adapter", return_value=adapter):
        out = server.wrap_gcode_as_3mf("part.gcode", bed_type="pei_plate")
    assert out.get("success") is False, out
    assert out["error"]["code"] == "UNKNOWN_BED_TYPE", out
    for name in BED_TYPES:
        assert name in out["error"]["message"]
    adapter.wrap_gcode_as_3mf.assert_not_called()


# ---------------------------------------------------------------------------
# a quote is remembered under the file's absolute path
# ---------------------------------------------------------------------------


def test_quote_writers_record_the_absolute_path(monkeypatch, tmp_path):
    import os

    import kiln.quote_cache as qc
    from kiln import _pro_order_bridge as bridge

    monkeypatch.setattr(qc, "_quote_cache", None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "part.stl").write_bytes(b"solid t\nendsolid t\n")

    bridge.remember_quote(
        {"quote": {"quote_id": "q-rel", "provider": "craftcloud", "material": "pla",
                   "quantity": 1, "total_price": 3.0, "currency": "USD"},
         "quote_token": "tok"},
        file_path="part.stl", material_id="pla",
    )
    recorded = qc.get_quote_cache().get_for_file(str(tmp_path / "part.stl"))
    assert recorded is not None and recorded.quote_id == "q-rel"
    assert os.path.isabs(recorded.metadata["file_path"])

    # Read back from another working directory, as the order step is.
    monkeypatch.chdir(tmp_path.parent)
    assert qc.get_quote_cache().get_for_file(str(tmp_path / "part.stl")).quote_id == "q-rel"


def test_fulfillment_quote_tool_records_the_absolute_path(monkeypatch, tmp_path):
    import os
    from types import SimpleNamespace

    from kiln import server
    from kiln.plugins import fulfillment_tools

    seen: dict = {}

    def fake_cache_quote(*_a, **kw):
        seen.update(kw.get("metadata") or {})

    quote = SimpleNamespace(
        quote_id="q-tool", material="PLA", total_price=4.0, currency="USD",
        lead_time_days=3, shipping_options=[], to_dict=lambda: {"quote_id": "q-tool"},
    )
    provider = SimpleNamespace(name="craftcloud", get_quote=lambda req: quote)
    service = SimpleNamespace(
        FulfillmentError=type("FulfillmentError", (Exception,), {}),
        QuoteRequest=lambda **kw: kw, price_sheet=None, quote_message=None,
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "part.stl").write_bytes(b"solid t\nendsolid t\n")
    tool = _tools_of(fulfillment_tools.plugin)["fulfillment_quote"]
    with patch.object(fulfillment_tools, "order_service", return_value=service), \
         patch.object(server, "_get_fulfillment", return_value=provider), \
         patch.object(server, "_get_billing", return_value=None), \
         patch.object(server, "_provider_routing_metadata", return_value={}), \
         patch("kiln.quote_cache.cache_quote", fake_cache_quote):
        out = tool("part.stl", "pla")
    assert out.get("success", True) is not False, out
    assert seen.get("file_path") == str(tmp_path / "part.stl"), seen
    assert os.path.isabs(seen["file_path"])


# ---------------------------------------------------------------------------
# photo mask: a rectangle is a rectangle all the way to the heightmap
# ---------------------------------------------------------------------------


def _photo_like_png(path, side: int = 96) -> None:
    """A gradient with grain: content everywhere, so the photo path runs."""
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:side, 0:side]
    img = (xx * (255.0 / side) + rng.normal(0, 20, (side, side))).clip(0, 255)
    Image.fromarray(img.astype(np.uint8)).save(path)


def test_rectangle_mask_keeps_its_corners_in_the_heightmap(tmp_path):
    """Measured at the DAT the converter writes, never at the emboss call.

    Before the fix a photo asked for as a rectangle was written as a disc
    (the coin style's own mask), byte-identical to the circle, and a
    rounded rectangle and a rectangle were one branch.
    """
    import numpy as np

    from kiln.image_to_surface import prepare_image_for_emboss

    photo = tmp_path / "photo.png"
    _photo_like_png(photo)
    rows = {}
    for mask in ("circle", "rectangle", "rounded_rectangle"):
        out = prepare_image_for_emboss(
            str(photo), str(tmp_path / mask), style="coin", mask=mask,
            flip_rows=True, max_resolution=96,
        )
        rows[mask] = np.loadtxt(out["dat_path"], ndmin=2)
    assert not np.array_equal(rows["rectangle"], rows["circle"])
    assert not np.array_equal(rows["rectangle"], rows["rounded_rectangle"])
    # The corners are kept: more of the grid carves than under softened corners.
    assert rows["rectangle"].shape == rows["rounded_rectangle"].shape
    assert (rows["rectangle"] != 0).sum() > (rows["rounded_rectangle"] != 0).sum()


def test_unknown_mask_is_refused_by_name_by_the_converter(tmp_path):
    from kiln.image_to_surface import PHOTO_MASKS, prepare_image_for_emboss, unknown_photo_mask

    photo = tmp_path / "photo.png"
    _photo_like_png(photo)
    with pytest.raises(ValueError) as caught:
        prepare_image_for_emboss(str(photo), str(tmp_path / "out"), style="coin", mask="hexagon")
    message = str(caught.value)
    assert "hexagon" in message
    for name in PHOTO_MASKS:
        assert f'"{name}"' in message
    assert unknown_photo_mask("auto") is None and unknown_photo_mask("") is None


# ---------------------------------------------------------------------------
# wrap_gcode_as_3mf: an empty bed_type is an omitted one
# ---------------------------------------------------------------------------


def test_wrap_gcode_as_3mf_treats_an_empty_bed_type_as_omitted():
    from kiln import server

    adapter = MagicMock()
    adapter.wrap_gcode_as_3mf.return_value = {"success": True, "output_path": "x.3mf"}
    with patch("kiln.server._check_auth", return_value=None), \
         patch("kiln.server._check_rate_limit", return_value=None), \
         patch("kiln.server._get_adapter", return_value=adapter):
        out = server.wrap_gcode_as_3mf("part.gcode", bed_type="")
    assert (out.get("error") or {}).get("code") != "UNKNOWN_BED_TYPE", out
    assert adapter.wrap_gcode_as_3mf.called
    assert not adapter.wrap_gcode_as_3mf.call_args.kwargs.get("bed_type")


# ---------------------------------------------------------------------------
# served text: no brand shorthand, no plan pointer, no server address
# ---------------------------------------------------------------------------


def test_upgrade_kiln_names_no_third_party_brand():
    from kiln.plugins.utility_tools import plugin
    from kiln import self_update

    assert "Apple" not in (_tools_of(plugin)["upgrade_kiln"].__doc__ or "")
    assert "Apple" not in (self_update.__doc__ or "")


def test_listings_carry_no_plan_pointer_or_server_address():
    from kiln import server
    from kiln.plugins import estimate_tools, tier_diagnostic_tools

    no_site_link = [
        server.generate_from_template, server.get_material_properties,
        server.compare_material_properties, server.compare_print_options,
        _tools_of(estimate_tools.plugin)["estimate_cost"],
    ]
    for door in no_site_link:
        assert "https://kiln3d.com" not in (door.__doc__ or ""), door.__name__
    for door in (server.monitor_print, _tools_of(tier_diagnostic_tools.plugin)["check_my_tier"]):
        assert "api.kiln3d.com" not in (door.__doc__ or ""), door.__name__


def test_load_filament_slot_text_names_the_whole_ht_range():
    from kiln.plugins.filament_handling_tools import plugin

    doc = _tools_of(plugin)["load_filament"].__doc__ or ""
    assert "128–135" in doc and "HT-A is 128" in doc
