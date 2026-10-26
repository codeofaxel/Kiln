"""The photo styles: one vocabulary, the engine where it can be reached,
the floor here otherwise -- and the floor says so.

Pinned incidents (2026-10-25): seven unrelated per-style hacks with the
vocabulary copied five times; a photo's rounded rectangle carved through a
round coaster's rim; a coin that was a five-terrace contour map; a relief
nobody saw lit before printing.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

pytest.importorskip("PIL.Image")

from kiln import image_to_surface as its  # noqa: E402


@pytest.fixture()
def photo(tmp_path):
    from PIL import Image, ImageDraw

    rng = np.random.default_rng(3)
    img = Image.fromarray(rng.integers(80, 180, size=(300, 300), dtype=np.uint8)).convert("RGB")
    d = ImageDraw.Draw(img)
    d.ellipse([90, 40, 210, 180], fill=(210, 175, 150))
    d.rectangle([60, 190, 240, 300], fill=(30, 30, 50))
    p = tmp_path / "photo.jpg"
    img.save(p, quality=92)
    return str(p)


@pytest.fixture(autouse=True)
def _fresh_session(tmp_path):
    import kiln._pro_relief_bridge as bridge
    from kiln import signin_offer

    bridge._reset_for_tests()
    signin_offer.reset_for_tests(tmp_path / "offers.json")
    yield
    bridge._reset_for_tests()
    signin_offer.reset_for_tests()


@pytest.fixture()
def no_engine(monkeypatch):
    """A plain install, signed out: kiln-pro absent and the servers not asked."""
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    import kiln.server as srv

    monkeypatch.setattr(srv, "_served_bearer", lambda: (None, None))


class TestTheVocabulary:
    def test_one_list_with_the_old_names_as_aliases(self):
        assert its.resolve_style("photo") == "coin"
        assert its.resolve_style("composite") == "coin"
        assert its.resolve_style("portrait") == "sketch"
        assert its.resolve_style("logo") == "stencil"
        assert its.resolve_style("medallion") == "coin"
        assert its.resolve_style("auto") == "auto"
        assert its.resolve_style("Coin ") == "coin"
        assert set(its.PHOTO_STYLES) <= set(its.IMAGE_STYLES)
        for name in its.accepted_styles():
            its.resolve_style(name)

    def test_an_unknown_style_is_refused_naming_the_list(self):
        with pytest.raises(ValueError) as err:
            its.resolve_style("hologram")
        assert "coin" in str(err.value) and "sketch" in str(err.value)

    def test_lithophane_is_retired_with_its_reason(self):
        with pytest.raises(ValueError) as err:
            its.resolve_style("lithophane")
        assert "lithophane maker" in str(err.value)
        assert "lithophane" not in its.accepted_styles()

    def test_the_preset_bridge_reads_the_same_list(self):
        from kiln.decoration import apply

        assert "cameo" in apply._valid_image_styles()
        assert "portrait" in apply._valid_image_styles()


class TestTheFloor:
    def test_a_photo_style_without_the_engine_is_the_floor_and_says_so(self, photo, tmp_path, no_engine):
        out = its.prepare_image_for_emboss(photo, str(tmp_path / "w"), style="coin", invert=True, flip_rows=True, max_resolution=200)
        assert out["relief_source"] == "floor"
        assert out["levels"] == 8 and out["layer_mm"] == 0.2
        # Never signed in: the account offer, not a note, and only once.
        assert out["notes"] == []
        assert out["signin_offer"]["tool"] == "kiln_signin" and "free Kiln account" in out["signin_offer"]["message"]
        again = its.prepare_image_for_emboss(photo, str(tmp_path / "w2"), style="coin", invert=True, max_resolution=120)
        assert "signin_offer" not in again and again["notes"] == []
        rows = np.loadtxt(out["dat_path"], ndmin=2)
        assert rows.shape == (200, 200)
        assert out["preview_png"] and out["preview_png"].endswith(".png")
        # What it was made for, never how.
        assert not {"mask_source", "form_source", "px_mm", "diagnostics"} & set(out)

    def test_the_floor_spends_its_height_in_layers(self, photo, tmp_path, no_engine):
        out = its.prepare_image_for_emboss(photo, str(tmp_path / "w"), style="coin", depth_mm=1.2, layer_mm=0.2, max_resolution=160)
        rows = np.loadtxt(out["dat_path"], ndmin=2)
        assert out["levels"] == 6
        assert np.allclose(rows * 6, np.round(rows * 6), atol=1e-3)

    def test_a_round_pool_leaves_the_corners_alone(self, photo, tmp_path, no_engine):
        out = its.prepare_image_for_emboss(photo, str(tmp_path / "w"), style="coin", mask="circle", invert=True, max_resolution=120)
        rows = np.loadtxt(out["dat_path"], ndmin=2)
        # Written inverted for the deboss (the generator inverts once more),
        # so an untouched corner is 1.0 in the file and 0 at the carve.
        assert rows[0, 0] == 1.0 and rows[-1, -1] == 1.0

    def test_the_old_names_still_carve(self, photo, tmp_path, no_engine):
        out = its.prepare_image_for_emboss(photo, str(tmp_path / "w"), style="photo", max_resolution=100)
        assert out["style_applied"] == "coin"
        out = its.prepare_image_for_emboss(photo, str(tmp_path / "w2"), style="portrait", max_resolution=100)
        assert out["style_applied"] == "sketch"

    def test_a_mark_still_takes_the_mark_path(self, tmp_path, no_engine):
        from PIL import Image, ImageDraw

        img = Image.new("L", (200, 200), 255)
        ImageDraw.Draw(img).rectangle([60, 60, 140, 140], fill=0)
        p = tmp_path / "mark.png"
        img.save(p)
        out = its.prepare_image_for_emboss(str(p), str(tmp_path / "w"), style="coin")
        assert out["treatment"] == "mark"
        assert "levels" not in out


class TestTheEngineIsPreferred:
    def test_the_bridge_answer_is_used_as_is(self, photo, tmp_path, monkeypatch):
        import kiln._pro_relief_bridge as bridge

        def fake_sculpt(image_path, work_dir, **spec):
            import os

            os.makedirs(work_dir, exist_ok=True)
            rows = np.zeros((10, 10))
            rows[5, 5] = 1.0
            dat = os.path.join(work_dir, "engine.dat")
            np.savetxt(dat, rows, fmt="%.4f")
            assert spec["style"] == "etched" and spec["mode"] == "deboss"
            assert spec["canvas_mm"] == pytest.approx(42.0) and spec["depth_mm"] == pytest.approx(1.0)
            assert spec.get("max_px") is None, "the engine's own size limits are never restated here"
            return {
                "type": "heightmap", "dat_path": dat, "width_px": 10, "height_px": 10, "aspect_ratio": 1.0,
                "treatment": "relief", "read_as": None, "style_applied": "etched", "levels": 5, "layer_mm": 0.2,
                "depth_mm": 1.0, "base": "pool", "preview_png": None, "notes": [], "relief_source": "engine",
            }

        monkeypatch.setattr(bridge, "sculpt", fake_sculpt)
        out = its.prepare_image_for_emboss(
            photo, str(tmp_path / "w"), style="etched", invert=True, flip_rows=True, canvas_mm=42.0, depth_mm=1.0,
        )
        assert out["relief_source"] == "engine"
        rows = np.loadtxt(out["dat_path"], ndmin=2)
        # Inverted for the deboss and flipped for surface(): the one carved
        # pixel is now the one UNcarved value, in the mirrored row.
        assert rows[4, 5] == pytest.approx(0.0) and rows[5, 5] == pytest.approx(1.0)

    def test_a_served_miss_is_worded_once_in_the_answer(self, photo, tmp_path, monkeypatch):
        import kiln._pro_relief_bridge as bridge
        from kiln.served_answer import Miss

        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        import kiln.server as srv

        monkeypatch.setattr(srv, "_served_bearer", lambda: ("token", None))
        monkeypatch.setattr(srv, "_pro_api_call", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
        from kiln import served_makes

        monkeypatch.setattr(served_makes, "send_inputs", lambda tool, kwargs, inputs, trip=None: (dict(kwargs, image_token="t"), None))
        out = its.prepare_image_for_emboss(photo, str(tmp_path / "w"), style="coin", max_resolution=100)
        assert out["relief_source"] == "floor"
        assert out["why"] in {"offline", "unanswered"}
        assert "Kiln can't" in out["notes"][0] and "basic relief" in out["notes"][0]
        assert isinstance(bridge.last_miss(), dict)
        # The same cause is said once per session.
        again = its.prepare_image_for_emboss(photo, str(tmp_path / "w2"), style="coin", max_resolution=100)
        assert again["notes"] == [] and "why" not in again


class TestTheRoster:
    def test_the_bridge_is_a_served_door(self):
        from kiln import served_answer

        assert served_answer.HOSTED_DOORS["kiln._pro_relief_bridge"][0] == "served_answer"
