"""Tests for 3MF color parser."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from kiln.threemf_parser import (
    _PAINT_STATE_PALETTE,
    _decode_paint_states,
    _dominant_paint_state,
    _parse_hex_color,
    object_display_colors,
    parse_colored_3mf,
    unique_object_names,
)

# ---------------------------------------------------------------------------
# Helper: build minimal 3MF archives in memory
# ---------------------------------------------------------------------------


def _make_3mf(
    tmp_path: Path,
    model_xml: str,
    *,
    name: str = "test.3mf",
    settings_xml: str | None = None,
) -> str:
    """Create a 3MF ZIP with the given model XML and return its path.

    *settings_xml*, when given, lands at ``Metadata/model_settings.config`` —
    the slicer sidecar where the BambuStudio family (and Kiln's own composer)
    records per-object colors.
    """
    fpath = tmp_path / name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", model_xml)
        if settings_xml is not None:
            zf.writestr("Metadata/model_settings.config", settings_xml)
    fpath.write_bytes(buf.getvalue())
    return str(fpath)


_BASIC_VERTICES = """\
<vertices>
  <vertex x="0" y="0" z="0" />
  <vertex x="10" y="0" z="0" />
  <vertex x="0" y="10" z="0" />
  <vertex x="10" y="10" z="0" />
  <vertex x="5" y="5" z="10" />
</vertices>"""


# ---------------------------------------------------------------------------
# _parse_hex_color
# ---------------------------------------------------------------------------


class TestParseHexColor:
    """Hex color parsing — both 6-digit and 8-digit formats."""

    def test_six_digit(self) -> None:
        assert _parse_hex_color("#FF0000") == (255, 0, 0)

    def test_eight_digit_strips_alpha(self) -> None:
        assert _parse_hex_color("#00FF00FF") == (0, 255, 0)

    def test_lowercase(self) -> None:
        assert _parse_hex_color("#aabbcc") == (170, 187, 204)

    def test_none_returns_fallback(self) -> None:
        assert _parse_hex_color(None) == (170, 170, 170)

    def test_malformed_returns_fallback(self) -> None:
        assert _parse_hex_color("not-a-color") == (170, 170, 170)

    def test_custom_fallback(self) -> None:
        assert _parse_hex_color(None, fallback=(1, 2, 3)) == (1, 2, 3)

    def test_short_hex_rejected(self) -> None:
        assert _parse_hex_color("#FFF") == (170, 170, 170)


# ---------------------------------------------------------------------------
# parse_colored_3mf — basematerials (core spec)
# ---------------------------------------------------------------------------


class TestParseBasematerials:
    """3MF files using <basematerials> (core spec)."""

    def test_two_color_basematerials(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Blue" displaycolor="#0000FF" />
    </basematerials>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" pid="1" p1="1" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="2" />
  </build>
</model>"""
        path = _make_3mf(tmp_path, xml)
        mesh = parse_colored_3mf(path)

        assert len(mesh.triangles) == 2
        assert mesh.colors_found is True
        assert mesh.color_count == 2
        # First triangle inherits object pindex=0 → Red
        assert mesh.triangles[0].color == (255, 0, 0)
        # Second triangle overrides to p1=1 → Blue
        assert mesh.triangles[1].color == (0, 0, 255)

    def test_eight_digit_hex_from_bambu(self, tmp_path: Path) -> None:
        """Bambu Studio writes 8-digit hex (#RRGGBBAA)."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="White" displaycolor="#FFFFFFFF" />
    </basematerials>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert mesh.triangles[0].color == (255, 255, 255)


# ---------------------------------------------------------------------------
# parse_colored_3mf — colorgroup (materials extension)
# ---------------------------------------------------------------------------


class TestParseColorgroup:
    """3MF files using <m:colorgroup> (materials extension)."""

    def test_four_color_colorgroup(self, tmp_path: Path) -> None:
        """Multi-color (4 colors, like camo textures)."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">
  <resources>
    <m:colorgroup id="1">
      <m:color color="#3B5323" />
      <m:color color="#556B2F" />
      <m:color color="#8B7355" />
      <m:color color="#2F4F2F" />
    </m:colorgroup>
    <object id="2" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" pid="1" p1="1" />
          <triangle v1="0" v2="1" v3="4" pid="1" p1="2" />
          <triangle v1="2" v2="3" v3="4" pid="1" p1="3" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="2" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))

        assert len(mesh.triangles) == 4
        assert mesh.colors_found is True
        assert mesh.color_count == 4
        assert mesh.triangles[0].color == (59, 83, 35)
        assert mesh.triangles[1].color == (85, 107, 47)
        assert mesh.triangles[2].color == (139, 115, 85)
        assert mesh.triangles[3].color == (47, 79, 47)


# ---------------------------------------------------------------------------
# parse_colored_3mf — multi-object assemblies
# ---------------------------------------------------------------------------


class TestParseAssembly:
    """Multi-object 3MF assemblies using <components>."""

    def test_two_part_assembly(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Green" displaycolor="#00FF00" />
    </basematerials>
    <object id="10" type="model" pid="1" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
    <object id="20" type="model" pid="1" pindex="1">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
    <object id="30" type="model">
      <components>
        <component objectid="10" />
        <component objectid="20" />
      </components>
    </object>
  </resources>
  <build><item objectid="30" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))

        assert len(mesh.triangles) == 2
        assert mesh.colors_found is True
        assert mesh.triangles[0].color == (255, 0, 0)
        assert mesh.triangles[1].color == (0, 255, 0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_no_color_data_uses_default(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))

        assert len(mesh.triangles) == 1
        assert mesh.colors_found is False
        assert mesh.triangles[0].color == (170, 170, 170)

    def test_custom_default_color(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(
            _make_3mf(tmp_path, xml), default_color=(50, 50, 50),
        )
        assert mesh.triangles[0].color == (50, 50, 50)

    def test_missing_model_xml_raises(self, tmp_path: Path) -> None:
        fpath = tmp_path / "empty.3mf"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.txt", "not a model")
        fpath.write_bytes(buf.getvalue())

        with pytest.raises(ValueError, match="No 3D model XML found"):
            parse_colored_3mf(str(fpath))

    def test_corrupt_xml_raises(self, tmp_path: Path) -> None:
        fpath = tmp_path / "corrupt.3mf"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<not valid xml >>>")
        fpath.write_bytes(buf.getvalue())

        with pytest.raises(ValueError, match="Failed to parse 3MF model XML"):
            parse_colored_3mf(str(fpath))

    def test_file_not_found_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_colored_3mf("/nonexistent/path.3mf")

    def test_not_a_zip_raises(self, tmp_path: Path) -> None:
        fpath = tmp_path / "notazip.3mf"
        fpath.write_text("this is not a zip file")
        with pytest.raises(zipfile.BadZipFile):
            parse_colored_3mf(str(fpath))

    def test_empty_mesh_returns_empty(self, tmp_path: Path) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices></vertices>
        <triangles></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert len(mesh.triangles) == 0
        assert mesh.colors_found is False

    def test_to_dict(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        d = mesh.to_dict()
        assert d["triangle_count"] == 1
        assert isinstance(d["triangles"], list)
        assert d["colors_found"] is False

    def test_invalid_vertex_indices_skipped(self, tmp_path: Path) -> None:
        xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0" />
          <vertex x="1" y="0" z="0" />
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="99" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert len(mesh.triangles) == 0


# ---------------------------------------------------------------------------
# Slicer sidecar (Metadata/model_settings.config)
# ---------------------------------------------------------------------------


_SIDECAR_MODEL = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model" name="zone_0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""


class TestSlicerSidecarColors:
    """Colors recorded only in the slicer sidecar — the shape Kiln's own
    ``compose_multicolor_3mf`` writes, invisible to the core spec."""

    def test_sidecar_colors_an_object_the_core_spec_left_silent(
        self, tmp_path: Path
    ) -> None:
        settings = """\
<?xml version="1.0" encoding="utf-8"?>
<config>
  <object id="1">
    <metadata key="name"  value="zone_0"/>
    <metadata key="color" value="f72323"/>
  </object>
</config>"""
        mesh = parse_colored_3mf(
            _make_3mf(tmp_path, _SIDECAR_MODEL, settings_xml=settings)
        )
        assert mesh.colors_found is True
        assert mesh.triangles[0].color == (247, 35, 35)

    def test_core_spec_beats_the_sidecar(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="9">
      <base name="Red" displaycolor="#FF0000" />
    </basematerials>
    <object id="1" type="model" pid="9" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        settings = """\
<config>
  <object id="1"><metadata key="color" value="00FF00"/></object>
</config>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml, settings_xml=settings))
        assert mesh.triangles[0].color == (255, 0, 0)

    def test_a_hash_prefixed_sidecar_value_parses_too(self, tmp_path: Path) -> None:
        """BambuStudio writes ``#RRGGBB``; Kiln's composer writes it bare."""
        settings = """\
<config>
  <object id="1"><metadata key="color" value="#2366F7"/></object>
</config>"""
        mesh = parse_colored_3mf(
            _make_3mf(tmp_path, _SIDECAR_MODEL, settings_xml=settings)
        )
        assert mesh.triangles[0].color == (35, 102, 247)

    def test_a_malformed_sidecar_reads_as_no_color_information(
        self, tmp_path: Path
    ) -> None:
        mesh = parse_colored_3mf(
            _make_3mf(tmp_path, _SIDECAR_MODEL, settings_xml="<not-xml")
        )
        assert mesh.colors_found is False
        assert mesh.triangles[0].color == (170, 170, 170)


# ---------------------------------------------------------------------------
# object_display_colors — the color half of the trimesh Scene trip
# ---------------------------------------------------------------------------


class TestObjectDisplayColors:
    """Per-object uniform colors, keyed the way trimesh keys Scene geometry."""

    def test_keys_follow_trimesh_naming(self, tmp_path: Path) -> None:
        """A named object keys by name; a nameless one by its id string."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="9">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Blue" displaycolor="#0000FF" />
    </basematerials>
    <object id="1" type="model" name="zone_0" pid="9" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
    <object id="2" type="model" pid="9" pindex="1">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /><item objectid="2" /></build>
</model>"""
        colors = object_display_colors(_make_3mf(tmp_path, xml))
        assert colors == {"zone_0": (255, 0, 0), "2": (0, 0, 255)}

    def test_sidecar_fills_in_when_the_core_spec_is_silent(
        self, tmp_path: Path
    ) -> None:
        settings = """\
<config>
  <object id="1"><metadata key="color" value="f72323"/></object>
</config>"""
        colors = object_display_colors(
            _make_3mf(tmp_path, _SIDECAR_MODEL, settings_xml=settings)
        )
        assert colors == {"zone_0": (247, 35, 35)}

    def test_a_painted_object_is_omitted_not_flattened(self, tmp_path: Path) -> None:
        """Per-triangle overrides mean no single color tells the truth."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="9">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Blue" displaycolor="#0000FF" />
    </basematerials>
    <object id="1" type="model" name="painted" pid="9" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" pid="9" p1="1" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        assert object_display_colors(_make_3mf(tmp_path, xml)) == {}

    def test_duplicate_names_refuse_to_guess(self, tmp_path: Path) -> None:
        """trimesh renames duplicates with suffixes that can collide with
        real sibling names — a name-keyed map could color the wrong part."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="9">
      <base name="Red" displaycolor="#FF0000" />
    </basematerials>
    <object id="1" type="model" name="zone_0" pid="9" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
    <object id="2" type="model" name="zone_0" pid="9" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /><item objectid="2" /></build>
</model>"""
        assert object_display_colors(_make_3mf(tmp_path, xml)) == {}

    def test_a_file_kiln_wrote_never_hits_that_refusal(
        self, tmp_path: Path
    ) -> None:
        """The refusal above is for files from elsewhere: names that arrive
        duplicated go through unique_object_names on the way out, so both
        parts keep their own color."""
        names = unique_object_names(["zone_0", "zone_0"])
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="9">
      <base name="Red" displaycolor="#FF0000" />
      <base name="Blue" displaycolor="#0000FF" />
    </basematerials>
    <object id="1" type="model" name="{names[0]}" pid="9" pindex="0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
    <object id="2" type="model" name="{names[1]}" pid="9" pindex="1">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /><item objectid="2" /></build>
</model>"""
        assert object_display_colors(_make_3mf(tmp_path, xml)) == {
            "zone_0": (255, 0, 0),
            "zone_0 (2)": (0, 0, 255),
        }

    def test_an_uncolored_object_claims_nothing(self, tmp_path: Path) -> None:
        assert object_display_colors(_make_3mf(tmp_path, _SIDECAR_MODEL)) == {}

    def test_archive_trouble_reads_as_empty_never_raises(
        self, tmp_path: Path
    ) -> None:
        not_a_zip = tmp_path / "junk.3mf"
        not_a_zip.write_text("this is not a zip")
        assert object_display_colors(str(not_a_zip)) == {}
        assert object_display_colors(str(tmp_path / "missing.3mf")) == {}

    def test_uniform_per_triangle_references_are_one_color(
        self, tmp_path: Path
    ) -> None:
        """Kiln's own composer writes colorgroup + per-triangle references
        (the shape three.js bakes to vertex colors); a single effective
        color is a uniform part, not a painted one."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">
  <resources>
    <m:colorgroup id="9">
      <m:color color="#F72323" />
      <m:color color="#2366F7" />
    </m:colorgroup>
    <object id="1" type="model" name="zone_0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" pid="9" p1="1" />
          <triangle v1="1" v2="3" v3="2" pid="9" p1="1" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        colors = object_display_colors(_make_3mf(tmp_path, xml))
        assert colors == {"zone_0": (35, 102, 247)}


class TestUniqueObjectNames:
    """The write-side guarantee that keeps object_display_colors readable."""

    def test_unique_names_are_left_exactly_alone(self) -> None:
        assert unique_object_names(["lid", "base_plate"]) == ["lid", "base_plate"]

    def test_a_repeat_is_suffixed_and_the_first_use_keeps_the_name(self) -> None:
        """The ordinary CAD case: four identical bolts in one assembly."""
        assert unique_object_names(["M3x8"] * 4) == [
            "M3x8",
            "M3x8 (2)",
            "M3x8 (3)",
            "M3x8 (4)",
        ]

    def test_a_suffix_never_lands_on_a_name_claimed_elsewhere(self) -> None:
        """"A (2)" is already spoken for LATER in the list, so the repeat has
        to skip past it — otherwise deduplication would introduce the very
        collision it exists to remove."""
        assert unique_object_names(["A", "A (2)", "A"]) == ["A", "A (2)", "A (3)"]
        assert unique_object_names(["A", "A", "A (2)"]) == ["A", "A (3)", "A (2)"]

    def test_a_blank_name_becomes_its_position(self) -> None:
        """Blank is not a name: the reader would key such an object by its id,
        which a real sibling could be named after.  Whitespace-only counts as
        blank — it is truthy to the reader but invisible in a slicer's list."""
        assert unique_object_names(["", None, "   ", "lid"]) == [
            "part_1",
            "part_2",
            "part_3",
            "lid",
        ]

    def test_a_positional_fallback_that_collides_is_still_resolved(self) -> None:
        """A part literally named "part_2" next to a blank one at position 2."""
        assert unique_object_names(["part_2", ""]) == ["part_2", "part_2 (2)"]

    def test_names_are_preserved_verbatim_otherwise(self) -> None:
        """Only blanks and repeats are touched — surrounding whitespace and
        case belong to the user, and the reader compares exact strings."""
        assert unique_object_names([" lid ", "LID", "lid"]) == [
            " lid ",
            "LID",
            "lid",
        ]

    def test_applying_it_twice_changes_nothing(self) -> None:
        """Idempotent, so a composer may guarantee the invariant itself
        without caring whether its caller already did."""
        once = unique_object_names(["COMPOUND", "COMPOUND", "", "COMPOUND"])
        assert unique_object_names(once) == once

    def test_the_result_is_always_unique_and_never_blank(self) -> None:
        """The invariant itself, stated against the key the reader builds."""
        for case in (
            ["COMPOUND", "COMPOUND"],
            ["", "", ""],
            ["a", "a (2)", "a", "a (2)", "a"],
            ["part_1", "", "part_1", ""],
            ["x"] * 12,
        ):
            out = unique_object_names(case)
            assert len(out) == len(case)
            assert all(n.strip() for n in out), case
            assert len(set(out)) == len(out), case


# ---------------------------------------------------------------------------
# The slicer painting channel (paint_color / slic3rpe:mmu_segmentation)
# ---------------------------------------------------------------------------


class TestPaintedStateCanon:
    """Pins exact TriangleSelector state strings to their decoded states.

    The literals are derived from PrusaSlicer's
    ``TriangleSelector::serialize`` / ``FacetsAnnotation``
    string codec (see the ``_decode_paint_states`` docstring for the
    full citation).  The ENCODER in ``kiln.multicolor_3mf`` pins the
    same canon from the same source, deliberately without importing
    from this module — if the two ever diverge, one of these suites
    goes red at merge instead of both drifting together.
    """

    def test_whole_triangle_states(self) -> None:
        assert _decode_paint_states("4") == ({1: 1.0}, False)
        assert _decode_paint_states("8") == ({2: 1.0}, False)
        # State 3 is the first that needs the 0b11 indicator nibble;
        # the second nibble carries state − 3.
        assert _decode_paint_states("0C") == ({3: 1.0}, False)
        assert _decode_paint_states("1C") == ({4: 1.0}, False)
        assert _decode_paint_states("DC") == ({16: 1.0}, False)

    def test_explicit_state_zero_is_unpainted(self) -> None:
        assert _decode_paint_states("0") == ({0: 1.0}, False)

    def test_extended_states_use_two_more_nibbles(self) -> None:
        """States ≥ 17 (PrusaSlicer ≥ 2.7 only; OrcaSlicer stops at 16)."""
        assert _decode_paint_states("00EC") == ({17: 1.0}, False)
        assert _decode_paint_states("01EC") == ({18: 1.0}, False)

    def test_split_triangles_report_leaf_weights(self) -> None:
        # One split side → two children, serialized in reverse order:
        # stream [0x1, leaf-2, leaf-1] reads as the string "481".
        assert _decode_paint_states("481") == ({2: 0.5, 1: 0.5}, True)
        # Three split sides → four children, all state 1.
        assert _decode_paint_states("44443") == ({1: 1.0}, True)
        # Nested: root 1-split; one child a 3-split of four state-2
        # leaves, the other a state-1 leaf — half the area each.
        assert _decode_paint_states("4888831") == ({2: 0.5, 1: 0.5}, True)

    def test_dominant_state_is_area_weighted_with_deterministic_ties(
        self,
    ) -> None:
        # Three of four children state 2 → 2 dominates.
        weights, is_split = _decode_paint_states("48883")
        assert is_split is True
        assert weights == {2: 0.75, 1: 0.25}
        assert _dominant_paint_state(weights) == 2
        # An exact tie breaks toward the lower state.
        assert _dominant_paint_state({2: 0.5, 1: 0.5}) == 1

    def test_malformed_strings_read_as_no_paint(self) -> None:
        for bad in (
            "",       # empty — upstream spells "unpainted" this way
            "G4",     # not hex
            "C",      # truncated: the 0b11 indicator promises a nibble
            "44",     # trailing nibble a legitimate string never has
            "41",     # split promising two children, only one present
        ):
            assert _decode_paint_states(bad) is None, bad


class TestPaintedChannelParse:
    """Painted files end-to-end: the attribute every BambuStudio /
    OrcaSlicer / PrusaSlicer-painted model carries, with no colorgroup."""

    STATE_1 = _PAINT_STATE_PALETTE[0]
    STATE_2 = _PAINT_STATE_PALETTE[1]

    _BAMBU_PAINTED = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model" name="painted">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" paint_color="4" />
          <triangle v1="0" v2="1" v3="4" paint_color="8" />
          <triangle v1="2" v2="3" v3="4" paint_color="48883" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""

    def test_bambu_paint_color_states_reach_the_triangles(
        self, tmp_path: Path
    ) -> None:
        mesh = parse_colored_3mf(_make_3mf(tmp_path, self._BAMBU_PAINTED))
        assert mesh.colors_found is True
        assert mesh.color_count == 3
        # Unpainted triangle falls through the existing chain.
        assert mesh.triangles[0].color == (170, 170, 170)
        assert mesh.triangles[0].paint_state is None
        # Whole-triangle states map onto the deterministic palette.
        assert mesh.triangles[1].color == self.STATE_1
        assert mesh.triangles[1].paint_state == 1
        assert mesh.triangles[2].color == self.STATE_2
        assert mesh.triangles[2].paint_state == 2
        # The split triangle takes its dominant state — and is counted.
        assert mesh.triangles[3].color == self.STATE_2
        assert mesh.triangles[3].paint_state == 2
        assert mesh.split_faces == 1
        # Minority states of split triangles still register as present.
        assert mesh.states_present == [1, 2]

    def test_prusa_spelling_decodes_identically(self, tmp_path: Path) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" slic3rpe:mmu_segmentation="4" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert mesh.colors_found is True
        assert mesh.triangles[0].color == self.STATE_1
        assert mesh.triangles[0].paint_state == 1

    def test_core_spec_per_triangle_refs_beat_the_paint_attribute(
        self, tmp_path: Path
    ) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="9">
      <base name="Green" displaycolor="#00FF00" />
    </basematerials>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" pid="9" p1="0" paint_color="4" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert mesh.triangles[0].color == (0, 255, 0)
        assert mesh.triangles[0].paint_state is None

    def test_unpainted_triangles_keep_the_sidecar_fallback(
        self, tmp_path: Path
    ) -> None:
        """Painting and the object color chain coexist: painted faces get
        their filament's palette color, the rest keep the object's own."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" paint_color="4" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        settings = """\
<config>
  <object id="1"><metadata key="color" value="f72323"/></object>
</config>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml, settings_xml=settings))
        assert mesh.triangles[0].color == (247, 35, 35)
        assert mesh.triangles[1].color == self.STATE_1

    def test_a_malformed_paint_attribute_reads_as_unpainted(
        self, tmp_path: Path
    ) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="not-hex" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert mesh.colors_found is False
        assert mesh.triangles[0].color == (170, 170, 170)
        assert mesh.triangles[0].paint_state is None


class TestObjectSegments:
    """ColoredMesh.segments — per-object triangle runs, so a consumer
    holding per-object geometry from another reader can line the two up."""

    def test_segments_carry_object_boundaries_and_keys(
        self, tmp_path: Path
    ) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model" name="zone_a">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" />
        </triangles>
      </mesh>
    </object>
    <object id="2" type="model">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /><item objectid="2" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        assert len(mesh.triangles) == 3
        assert [
            (s.object_id, s.name, s.start, s.count) for s in mesh.segments
        ] == [(1, "zone_a", 0, 2), (2, None, 2, 1)]
        # Keys follow the trimesh Scene-geometry convention: name, else
        # the id as a string — the same one object_display_colors uses.
        assert [s.key for s in mesh.segments] == ["zone_a", "2"]

    def test_assembly_components_get_their_own_segments(
        self, tmp_path: Path
    ) -> None:
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="10" type="model" name="left">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
    <object id="20" type="model" name="right">
      <mesh>
        {_BASIC_VERTICES}
        <triangles><triangle v1="0" v2="1" v3="2" /></triangles>
      </mesh>
    </object>
    <object id="30" type="model">
      <components>
        <component objectid="10" />
        <component objectid="20" />
      </components>
    </object>
  </resources>
  <build><item objectid="30" /></build>
</model>"""
        mesh = parse_colored_3mf(_make_3mf(tmp_path, xml))
        # The components wrapper has no triangles of its own — only the
        # leaf objects appear, in collection order.
        assert [
            (s.object_id, s.name, s.start, s.count) for s in mesh.segments
        ] == [(10, "left", 0, 1), (20, "right", 1, 1)]

    def test_to_dict_carries_the_new_fields(self, tmp_path: Path) -> None:
        mesh = parse_colored_3mf(
            _make_3mf(tmp_path, TestPaintedChannelParse._BAMBU_PAINTED)
        )
        d = mesh.to_dict()
        assert d["states_present"] == [1, 2]
        assert d["split_faces"] == 1
        assert d["segments"] == [
            {"object_id": 1, "name": "painted", "start": 0, "count": 4,
             "key": "painted"},
        ]
        assert d["triangles"][1]["paint_state"] == 1
        assert d["triangles"][0]["paint_state"] is None


class TestObjectDisplayColorsPaintedChannel:
    """object_display_colors must SEE the painting channel: a painted
    object is refused, a single-filament paint is honestly uniform."""

    def test_a_paint_attribute_marks_the_object_painted(
        self, tmp_path: Path
    ) -> None:
        """A sidecar color plus one painted triangle is two effective
        colors — the sidecar alone may not stand for the object."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model" name="zone_0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" />
          <triangle v1="1" v2="3" v3="2" paint_color="4" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        settings = """\
<config>
  <object id="1"><metadata key="color" value="f72323"/></object>
</config>"""
        assert object_display_colors(
            _make_3mf(tmp_path, xml, settings_xml=settings)
        ) == {}

    def test_a_wholly_single_state_paint_is_uniform(
        self, tmp_path: Path
    ) -> None:
        """Every triangle painted the same filament IS one color — and
        this file's only color construct is the paint attribute, so it
        also proves the byte pre-scan lets such files through."""
        xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model" name="zone_0">
      <mesh>
        {_BASIC_VERTICES}
        <triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="4" />
          <triangle v1="1" v2="3" v3="2" paint_color="4" />
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1" /></build>
</model>"""
        assert object_display_colors(_make_3mf(tmp_path, xml)) == {
            "zone_0": _PAINT_STATE_PALETTE[0],
        }
