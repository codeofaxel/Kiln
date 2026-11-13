"""The fonts a word can be carved in, and the doors that take one."""

from __future__ import annotations

import shutil

import pytest

from kiln.text_fonts import (
    DEFAULT_TEXT_FONT,
    TEXT_FONTS,
    UnknownTextFontError,
    list_text_fonts,
    resolve_text_font,
)

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None, reason="drawing text needs OpenSCAD"
)


class TestTheList:
    def test_no_font_named_is_the_default(self):
        assert resolve_text_font("").id == DEFAULT_TEXT_FONT
        assert resolve_text_font(None).id == DEFAULT_TEXT_FONT

    def test_an_id_is_read_whatever_its_case(self):
        assert resolve_text_font(" Mono ").id == "mono"

    def test_an_unknown_font_names_the_choices(self):
        with pytest.raises(UnknownTextFontError) as refused:
            resolve_text_font("comic")
        assert refused.value.choices == [font.id for font in TEXT_FONTS]
        assert "sans" in str(refused.value)

    def test_the_list_a_caller_sees_leads_with_the_default(self):
        listed = list_text_fonts()
        assert listed[0]["id"] == DEFAULT_TEXT_FONT
        assert all(set(row) == {"id", "label", "note"} for row in listed)

    def test_ids_are_unique(self):
        assert len({font.id for font in TEXT_FONTS}) == len(TEXT_FONTS)


class TestEveryShippedFontHasItsLicense:
    def test_each_font_file_sits_beside_a_license(self):
        from pathlib import Path

        from kiln.openscad_runner import bundled_font_path

        folder = Path(bundled_font_path())
        fonts = sorted(folder.glob("*.ttf"))
        assert len(fonts) == 4, fonts
        licenses = [p.read_text() for p in folder.glob("*-OFL.txt")]
        assert len(licenses) == len(fonts)
        assert all("SIL Open Font License, Version 1.1" in text for text in licenses)

    def test_the_wheel_carries_fonts_and_licenses(self):
        from pathlib import Path

        packaged = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert '"data/fonts/*.ttf"' in packaged and '"data/fonts/*.txt"' in packaged


@needs_openscad
class TestEveryFontIsReallyDrawn:
    """A font the engine cannot find is drawn in the default face with no
    error, so each one is measured against what it is recorded to draw."""

    def test_every_font_draws_as_recorded(self):
        from kiln.text_fonts import fonts_not_drawn

        assert fonts_not_drawn() == []

    def test_no_two_fonts_could_pass_for_each_other(self):
        from kiln.text_fonts import _INK_TOLERANCE

        for a in TEXT_FONTS:
            for b in TEXT_FONTS:
                if a.id == b.id:
                    continue
                ink_apart = abs(a.ink_mm2 - b.ink_mm2) > _INK_TOLERANCE * a.ink_mm2
                width_apart = abs(a.ink_width_mm - b.ink_width_mm) > _INK_TOLERANCE * a.ink_width_mm
                assert ink_apart or width_apart, (a.id, b.id)

    def test_a_font_that_is_not_there_is_named(self, monkeypatch, tmp_path):
        import kiln.openscad_runner as runner
        from kiln.text_fonts import fonts_not_drawn

        monkeypatch.setattr(runner, "bundled_font_path", lambda: str(tmp_path))
        assert fonts_not_drawn() == ["fredoka", "zilla-slab", "oswald", "saira-stencil"]


@pytest.fixture
def plate(tmp_path):
    import trimesh

    # A name of its own: the carve writes beside a shared folder by the
    # model's name, and parallel workers carving "plate.stl" collide.
    path = tmp_path / f"plate_{tmp_path.name}.stl"
    trimesh.creation.box(extents=(80.0, 40.0, 6.0)).export(str(path))
    return str(path)


@needs_openscad
class TestCarvingAWord:
    def test_the_reply_names_the_font_and_the_carve_differs_by_font(self, plate):
        import trimesh

        from kiln.decoration.apply import apply_decoration_spec

        volume = {}
        for font in ("", "mono"):
            carved = apply_decoration_spec(
                host_mesh_path=plate, pattern_family="text_deboss",
                content="text:Kiln 12", font=font, face="top",
            )
            assert carved.get("success") is True, carved
            assert carved["font"] == (font or DEFAULT_TEXT_FONT)
            volume[font] = trimesh.load(carved["decorated_model_path"]).volume
        assert abs(volume[""] - volume["mono"]) > 1.0, volume

    def test_a_raised_word_adds_material(self, plate):
        import trimesh

        from kiln.decoration.apply import apply_decoration_spec

        raised = apply_decoration_spec(
            host_mesh_path=plate, pattern_family="text_emboss",
            content="text:Kiln", font="sans", face="top",
        )
        assert raised.get("success") is True, raised
        assert trimesh.load(raised["decorated_model_path"]).volume > trimesh.load(plate).volume

    def test_an_unknown_font_is_refused_and_nothing_is_carved(self, plate):
        from kiln.decoration.apply import apply_decoration_spec

        refused = apply_decoration_spec(
            host_mesh_path=plate, pattern_family="text_deboss",
            content="text:Kiln", font="comic", face="top",
        )
        assert refused.get("success") is False
        assert refused["error"]["code"] == "UNKNOWN_FONT"
        assert refused["fonts"] == list_text_fonts()

    def test_a_text_family_with_no_words_is_refused(self, plate):
        from kiln.decoration.apply import apply_decoration_spec

        refused = apply_decoration_spec(
            host_mesh_path=plate, pattern_family="text_deboss", content="text:  ",
        )
        assert refused == {
            "success": False,
            "error": "text pattern 'text_deboss' requires content='text:YOUR TEXT'",
            "code": "INVALID_CONTENT",
        }
