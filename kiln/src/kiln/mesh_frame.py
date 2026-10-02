"""Kiln's frame: Z is up, the way a part stands on the printer bed.

STL and OBJ carry no up axis of their own and 3MF names Z, which is already
Kiln's, so those are read as written.  glTF (``.glb`` / ``.gltf``), the
format AI model generators and most model sites hand back, is different:
the glTF 2.0 specification defines +Y as up and says the front of an asset
faces +Z.  Read as written, every such model lies on its side.  The letter
a prompt asked for on top lands on a wall, and the height Kiln reports is
really the depth.

:func:`load_mesh` is the door every mesh read in Kiln goes through.  For
glTF it places each part where the file's scene graph puts it, then stands
the whole model up with one rigid turn, (x, y, z) -> (x, -z, y): up becomes
+Z and the front faces -Y, the side Kiln's front view looks at.  Every
other format comes back exactly as ``trimesh.load`` returns it.
"""

from __future__ import annotations

import ast
import os
from typing import Any

import trimesh

#: File types whose own specification puts up on +Y.
Y_UP_TYPES = frozenset({"glb", "gltf"})

#: glTF -> Kiln: a +90 degree turn about X.  A rotation, never a mirror,
#: so a part's handedness and its threads survive the turn.
GLTF_TO_KILN = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, -1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def is_y_up(source: Any, file_type: str | None = None) -> bool:
    """Whether *source* is a format that puts up on +Y.

    *file_type* wins when given, as it does for ``trimesh.load``; otherwise
    the path's suffix decides.  A file object with no *file_type* is not one.
    """
    if file_type is None:
        try:
            file_type = os.path.splitext(os.fspath(source))[1]
        except TypeError:
            return False
    return file_type.lower().lstrip(".") in Y_UP_TYPES


def load_mesh(source: Any, **kwargs: Any) -> Any:
    """``trimesh.load`` in Kiln's frame: same arguments, same return types.

    A glTF file comes back with its parts where its scene graph places them,
    turned Z-up.  A glTF file whose scene places none of its meshes (some
    minimal writers leave the node list out) has them read where they are
    written, rather than coming back as an empty model.
    """
    if not is_y_up(source, kwargs.get("file_type")):
        return trimesh.load(source, **kwargs)
    force = kwargs.pop("force", None)
    scene = trimesh.load_scene(source, **kwargs)
    if scene.geometry and not scene.graph.nodes_geometry:
        for name in scene.geometry:
            scene.graph.update(frame_to=name, geometry=name)
    scene.apply_transform(GLTF_TO_KILN)
    return scene.to_mesh() if force == "mesh" else scene


#: trimesh's file readers.  Calling one directly skips the turn above.
_READERS = frozenset({"load", "load_mesh", "load_scene"})


def unrouted_loads(source: str) -> list[int]:
    """Lines in *source* that open a mesh with trimesh directly.

    Each one reads a glTF file lying on its side unless it goes through
    :func:`load_mesh`.  A line that must see the file exactly as written (a
    fingerprint of its bytes, say) says why with a ``raw frame:`` comment
    and is not listed.  Both repositories' tests run this over their trees.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    aliases = {"trimesh"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "trimesh" and alias.asname
    }
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            hit = node.module == "trimesh" and any(a.name in _READERS for a in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            root = node.func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            hit = node.func.attr in _READERS and isinstance(root, ast.Name) and root.id in aliases
        else:
            continue
        if hit and "raw frame:" not in lines[node.lineno - 1]:
            found.append(node.lineno)
    return sorted(found)
