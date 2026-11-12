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


@needs_openscad
class TestEveryFontIsReallyDrawn:
    """A font the engine cannot find is drawn in the default face with no
    error, so each one is measured: a different font is a different width."""

    def test_each_font_measures_differently_from_the_default(self):
        from kiln.emboss_generator import measure_text_block_mm

        default = resolve_text_font("")
        width = {
            font.id: measure_text_block_mm("Kiln 12", font.spec, 10.0)[0]
            for font in TEXT_FONTS
        }
        for font in TEXT_FONTS:
            if font.id != default.id:
                assert abs(width[font.id] - width[default.id]) > 0.5, width


@pytest.fixture
def plate(tmp_path):
    import trimesh

    path = tmp_path / "plate.stl"
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
