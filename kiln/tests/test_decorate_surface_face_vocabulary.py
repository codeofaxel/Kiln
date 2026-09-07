"""decorate_surface names faces by the direction they point — say so.

The product face registry and the template decoration profiles (kiln-pro)
name faces by what they are FOR: ``interior_bed``, ``exterior_bottom``,
``exterior``, a flat keychain's ``front``.  ``decorate_surface`` names them
by where they point: top / bottom / front / back / left / right, plus
``wall`` for a round upright wall.  An agent that reads a face name off
one tool and hands it to the other used to get ``Invalid face name`` with
no way to learn the translation — the listing tool said the name could be
passed here, this tool said it could not, and neither named the bridge.

These pin the public half of the fix: the accepted set is one constant
every door reads, and a name outside it comes back with the bridge in the
error, quota refunded.
"""

from __future__ import annotations

import struct

import pytest

from kiln import decoration_quota
from kiln.server import decorate_surface
from kiln.surface_intelligence import _FACE_NAMES, find_named_face

_decorate = getattr(decorate_surface, "fn", decorate_surface)

# Derived from the resolver's own direction table, never retyped here.
_CARDINALS = sorted(name for name, _ in _FACE_NAMES)


def _cube_stl_bytes(size: float = 10.0) -> bytes:
    s = size
    faces = [
        ((0, 0, -1), [(0, 0, 0), (s, 0, 0), (s, s, 0)]),
        ((0, 0, -1), [(0, 0, 0), (s, s, 0), (0, s, 0)]),
        ((0, 0, 1), [(0, 0, s), (s, s, s), (s, 0, s)]),
        ((0, 0, 1), [(0, 0, s), (0, s, s), (s, s, s)]),
        ((0, -1, 0), [(0, 0, 0), (s, 0, s), (s, 0, 0)]),
        ((0, -1, 0), [(0, 0, 0), (0, 0, s), (s, 0, s)]),
        ((0, 1, 0), [(0, s, 0), (s, s, 0), (s, s, s)]),
        ((0, 1, 0), [(0, s, 0), (s, s, s), (0, s, s)]),
        ((-1, 0, 0), [(0, 0, 0), (0, s, 0), (0, s, s)]),
        ((-1, 0, 0), [(0, 0, 0), (0, s, s), (0, 0, s)]),
        ((1, 0, 0), [(s, 0, 0), (s, 0, s), (s, s, s)]),
        ((1, 0, 0), [(s, 0, 0), (s, s, s), (s, s, 0)]),
    ]
    out = b"\x00" * 80 + struct.pack("<I", len(faces))
    for normal, verts in faces:
        out += struct.pack("<12fH", *normal, *verts[0], *verts[1], *verts[2], 0)
    return out


@pytest.fixture()
def cube_stl(tmp_path):
    p = tmp_path / "cube.stl"
    p.write_bytes(_cube_stl_bytes())
    return str(p)


class _SpyQuota:
    def __init__(self) -> None:
        self.refunds = 0

    def refund(self) -> None:
        self.refunds += 1


@pytest.fixture()
def spy_quota(monkeypatch):
    q = _SpyQuota()
    monkeypatch.setattr(decoration_quota, "_quota", q)
    monkeypatch.setattr(decoration_quota, "check_decoration_quota", lambda: (True, None))
    return q


class TestCardinalFaceNamesConstant:
    def test_constant_is_the_resolver_direction_table(self, cube_stl):
        from kiln.surface_intelligence import CARDINAL_FACE_NAMES

        assert sorted(CARDINAL_FACE_NAMES) == _CARDINALS
        for name in CARDINAL_FACE_NAMES:
            assert find_named_face(cube_stl, name)["face_name"] == name

    def test_a_name_outside_the_constant_is_rejected(self, cube_stl):
        with pytest.raises(ValueError, match="Invalid face name"):
            find_named_face(cube_stl, "interior_bed")


class TestSemanticFaceNamesGetTheBridge:
    @pytest.mark.parametrize("name", ["interior_bed", "exterior_bottom", "exterior"])
    def test_registry_name_is_refused_with_the_translation(self, cube_stl, spy_quota, name):
        result = _decorate(model_path=cube_stl, content="text:KILN", face=name)
        assert result.get("success") is False, result
        err = result["error"]
        assert err["code"] == "VALIDATION_ERROR"
        msg = err["message"]
        # Names the vocabulary this tool speaks ...
        for accepted in [*_CARDINALS, "wall"]:
            assert accepted in msg
        # ... and the fields on the OTHER tools that carry the translation.
        assert "decorate_surface_face" in msg
        assert "decorate_next" in msg

    def test_refused_name_hands_the_quota_slot_back(self, cube_stl, spy_quota):
        _decorate(model_path=cube_stl, content="text:KILN", face="interior_bed")
        assert spy_quota.refunds == 1

    def test_cardinal_name_never_trips_the_vocabulary_gate(self, cube_stl, spy_quota):
        # Whatever happens downstream (OpenSCAD may be absent), a real name
        # is never answered with the vocabulary error.
        result = _decorate(model_path=cube_stl, content="text:KILN", face="top")
        if result.get("success") is False:
            assert "decorate_surface_face" not in result["error"]["message"]
