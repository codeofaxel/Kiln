"""A mesh file names its own companion files, and a stranger wrote it.

An OBJ says which MTL to read (``mtllib``), and an MTL says which picture to
read (``map_Kd``).  Both are names written INSIDE the file, by whoever made
it, and ``trimesh`` opens them while it reads the mesh.  On these servers
the file came up an upload door from someone we know nothing about, so the
names in it point wherever they like: an absolute path, a walk up out of
the folder, a device that never ends.  Kiln reads a mesh through one door
(``kiln.mesh_frame.load_mesh``), so the answer belongs at that door: a
companion file is read from the mesh's OWN folder and nowhere else, and
only up to a size a real companion file has.

A refused name is NOT an error for the person: their mesh still loads, with
its triangles and its shape, and only the decoration that pointed outside
is missing.  Every case below asserts both halves -- the read refused, and
the mesh still read.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiln import mesh_frame

_OBJ_BODY = (
    "v 0 0 0\nv 10 0 0\nv 0 10 0\n"
    "vt 0 0\nvt 1 0\nvt 0 1\n"
    "usemtl painted\nf 1/1 2/2 3/3\n"
)

#: A material a test can recognise again: this exact diffuse colour, as the
#: 0-255 channels a loaded material carries.
_WITNESS_MTL = "newmtl painted\nKd 0.5 0.25 0.125\n"
_WITNESS_KD = (128, 64, 32)


def _write_obj(folder: Path, mtllib: str, name: str = "part.obj") -> str:
    path = folder / name
    path.write_text(f"mtllib {mtllib}\n{_OBJ_BODY}")
    return str(path)


def _diffuse(mesh) -> tuple[int, int, int] | None:
    """The mesh's diffuse colour, or ``None`` when it has no material."""
    material = getattr(getattr(mesh, "visual", None), "material", None)
    diffuse = getattr(material, "diffuse", None)
    if diffuse is None:
        return None
    return tuple(int(c) for c in diffuse[:3])


def _read_a_mesh(path: str):
    mesh = mesh_frame.load_mesh(path, force="mesh")
    assert len(mesh.faces) == 1, "the mesh itself must still read"
    return mesh


# ---------------------------------------------------------------------------
# The door: a companion file outside the mesh's folder is not read
# ---------------------------------------------------------------------------


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A readable MTL somewhere else on this disk, with a colour a test can
    recognise if it ever arrives."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = elsewhere / "secret.mtl"
    path.write_text(_WITNESS_MTL)
    return path


def test_an_absolute_path_out_of_the_folder_is_not_read(tmp_path, outside):
    """The shape measured 2026-10-18: ``mtllib /somewhere/else.mtl`` in an
    uploaded OBJ had its target opened and parsed."""
    folder = tmp_path / "upload"
    folder.mkdir()
    mesh = _read_a_mesh(_write_obj(folder, str(outside)))
    assert _diffuse(mesh) != _WITNESS_KD


def test_a_walk_up_out_of_the_folder_is_not_read(tmp_path, outside):
    folder = tmp_path / "upload"
    folder.mkdir()
    walk = os.path.join("..", "elsewhere", "secret.mtl")
    mesh = _read_a_mesh(_write_obj(folder, walk))
    assert _diffuse(mesh) != _WITNESS_KD


def test_a_link_inside_the_folder_pointing_out_is_not_read(tmp_path, outside):
    """A name that stays inside the folder but LANDS outside it."""
    folder = tmp_path / "upload"
    folder.mkdir()
    try:
        os.symlink(outside, folder / "linked.mtl")
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("this platform does not make symlinks")
    mesh = _read_a_mesh(_write_obj(folder, "linked.mtl"))
    assert _diffuse(mesh) != _WITNESS_KD


def test_a_companion_file_that_never_ends_is_not_read(tmp_path):
    """``map_Kd /dev/zero``: a read with no end, inside the request."""
    folder = tmp_path / "upload"
    folder.mkdir()
    if not os.path.exists("/dev/zero"):  # pragma: no cover - platform
        pytest.skip("no /dev/zero on this platform")
    (folder / "part.mtl").write_text("newmtl painted\nmap_Kd /dev/zero\n")
    mesh = _read_a_mesh(_write_obj(folder, "part.mtl"))
    image = getattr(getattr(mesh.visual, "material", None), "image", None)
    assert image is None


def test_a_companion_file_past_the_cap_is_not_read(tmp_path):
    """A real MTL is kilobytes and a real texture a few megabytes; the door
    caps a companion file well above both, and refuses past it."""
    folder = tmp_path / "upload"
    folder.mkdir()
    big = folder / "big.mtl"
    big.write_bytes(_WITNESS_MTL.encode() + b"# " + b"x" * mesh_frame.MAX_ASSET_BYTES)
    mesh = _read_a_mesh(_write_obj(folder, "big.mtl"))
    assert _diffuse(mesh) != _WITNESS_KD


# ---------------------------------------------------------------------------
# A real customer's file still reads
# ---------------------------------------------------------------------------


def test_the_mtl_beside_the_obj_is_read(tmp_path):
    """The ordinary case, and the proof the cases above measure confinement
    rather than a door that stopped reading companion files at all."""
    folder = tmp_path / "upload"
    folder.mkdir()
    (folder / "part.mtl").write_text(_WITNESS_MTL)
    assert _diffuse(_read_a_mesh(_write_obj(folder, "part.mtl"))) == _WITNESS_KD


def test_a_windows_exporter_absolute_path_still_finds_its_mtl(tmp_path):
    """A real exporter writes the path the file had on ITS machine
    (``mtllib C:\\Users\\sam\\part.mtl``).  The companion file arrives in the
    same folder, so the name's last part finds it -- the behaviour trimesh
    already had, kept, because refusing it would refuse real customer files.
    """
    folder = tmp_path / "upload"
    folder.mkdir()
    (folder / "part.mtl").write_text(_WITNESS_MTL)
    obj = _write_obj(folder, "C:\\Users\\sam\\Desktop\\part.mtl")
    assert _diffuse(_read_a_mesh(obj)) == _WITNESS_KD


def test_a_companion_file_in_a_subfolder_is_read(tmp_path):
    """Inside the mesh's own folder, a subfolder is still the mesh's own."""
    folder = tmp_path / "upload"
    (folder / "materials").mkdir(parents=True)
    (folder / "materials" / "part.mtl").write_text(_WITNESS_MTL)
    obj = _write_obj(folder, os.path.join("materials", "part.mtl"))
    assert _diffuse(_read_a_mesh(obj)) == _WITNESS_KD


def test_a_texture_beside_the_obj_is_read(tmp_path):
    """The picture an MTL names, which is the larger of the two reads."""
    pytest.importorskip("PIL")
    from PIL import Image

    folder = tmp_path / "upload"
    folder.mkdir()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(folder / "skin.png")
    (folder / "part.mtl").write_text("newmtl painted\nmap_Kd skin.png\n")
    mesh = _read_a_mesh(_write_obj(folder, "part.mtl"))
    image = getattr(getattr(mesh.visual, "material", None), "image", None)
    assert image is not None and image.size == (8, 8)


# ---------------------------------------------------------------------------
# The resolver itself
# ---------------------------------------------------------------------------


def test_the_resolver_refuses_with_oserror(tmp_path, outside):
    """Refused by raising ``OSError``, which is what every reader above
    already treats as "that companion file is not there" -- so no reader has
    to learn a new exception for a mesh to keep loading."""
    folder = tmp_path / "upload"
    folder.mkdir()
    (folder / "part.obj").write_text(_OBJ_BODY)
    resolver = mesh_frame.BesideTheFileResolver(str(folder / "part.obj"))
    with pytest.raises(OSError):
        resolver.get(str(outside))
    with pytest.raises(OSError):
        resolver.get(os.path.join("..", "elsewhere", "secret.mtl"))


def test_the_resolver_keeps_reading_inside_its_folder(tmp_path):
    folder = tmp_path / "upload"
    folder.mkdir()
    (folder / "part.obj").write_text(_OBJ_BODY)
    (folder / "part.mtl").write_text(_WITNESS_MTL)
    resolver = mesh_frame.BesideTheFileResolver(str(folder / "part.obj"))
    assert resolver.get("part.mtl") == _WITNESS_MTL.encode()


def test_a_subfolder_resolver_stays_confined(tmp_path, outside):
    """``namespaced`` is how a reader descends into a subfolder; the
    resolver it returns must be confined too, or the guard ends one level
    down."""
    folder = tmp_path / "upload"
    (folder / "materials").mkdir(parents=True)
    (folder / "part.obj").write_text(_OBJ_BODY)
    inner = mesh_frame.BesideTheFileResolver(
        str(folder / "part.obj")
    ).namespaced("materials")
    assert isinstance(inner, mesh_frame.BesideTheFileResolver)
    with pytest.raises(OSError):
        inner.get(str(outside))
