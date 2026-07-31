"""``kiln.mesh_payload`` on an install that has no trimesh.

trimesh ships in the ``mesh-diagnostics`` extra, not the base install, so
most people who ``pip install kiln3d`` do not have it.  Every module in
public Kiln that touches trimesh therefore imports it lazily, inside the
function that needs it — the package has to import, and the test suite has
to COLLECT, on a bare install.

``test_mesh_payload`` cannot pin that: it needs trimesh to build its fixture
meshes, so it skips on exactly the install this contract is about.  This
file is the other half, and it fakes trimesh's absence rather than waiting
to be run somewhere trimesh is missing — a guard that only runs where the
bug cannot be observed proves nothing.

Why it is worth a file: on 2026-07-30 ``test_mesh_payload`` imported trimesh
at module scope.  pytest treats a collection ImportError as fatal, so one
file that needed an optional dependency took all ~11.6k public tests down
with it, on every Python version, for a day.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager

import pytest

_BLOCKED = ("trimesh",)


class _Blocker:
    """A meta-path finder that makes ``import trimesh`` raise."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "trimesh" or fullname.startswith("trimesh."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def _is_blocked(name: str) -> bool:
    return name in _BLOCKED or name.startswith(tuple(f"{b}." for b in _BLOCKED))


@contextmanager
def trimesh_absent():
    """Run the block as if trimesh were not installed.

    Evicts any already-imported trimesh so a lazy ``import trimesh`` inside a
    function actually goes back through the finders, and puts the blocker
    first so it wins.

    ``sys.modules`` is restored wholesale on the way out — both the trimesh
    entries this removed and any ``kiln.*`` module a test re-imported under
    the block.  Restoring only the ones a test was known to touch is how this
    rots: a module object built while trimesh was unreachable, left in
    ``sys.modules``, would quietly become some later test's idea of
    ``kiln.local_stage``.
    """
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if _is_blocked(name) or name.startswith("kiln.")
    }
    blocker = _Blocker()
    for name in list(sys.modules):
        if _is_blocked(name):
            del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        # Drop anything imported under the block, then put back exactly what
        # was there before it.
        for name in list(sys.modules):
            if (_is_blocked(name) or name.startswith("kiln.")) and name not in saved:
                del sys.modules[name]
        sys.modules.update(saved)


def test_the_blocker_actually_blocks():
    """Guard the guard: if this stopped working, every test below would pass
    for the wrong reason — against a trimesh that was there all along."""
    with trimesh_absent(), pytest.raises(ModuleNotFoundError):
        importlib.import_module("trimesh")


def test_module_imports_with_no_trimesh_installed():
    """The import that has to survive a bare install.

    This is the exact shape that broke CI, one layer down: if
    ``kiln.mesh_payload`` imported trimesh at module scope, importing the
    module — which ``kiln.local_stage`` does at ITS module scope — would
    raise here.
    """
    with trimesh_absent():
        # pop, not del: on a bare install ``test_mesh_payload`` skips without
        # importing it, so there may be nothing cached to evict.
        sys.modules.pop("kiln.mesh_payload", None)
        module = importlib.import_module("kiln.mesh_payload")
        assert module.VIEWER_PAYLOAD_KIND == "kiln.mesh.v1"


def test_the_stage_module_imports_too():
    """``kiln.local_stage`` imports the encoder at module scope, so a hard
    trimesh requirement anywhere under it would break the stage's import,
    not just its rendering."""
    with trimesh_absent():
        for name in ("kiln.local_stage", "kiln.mesh_payload"):
            sys.modules.pop(name, None)
        module = importlib.import_module("kiln.local_stage")
        assert module.mesh_to_viewer_payload is not None


def test_size_arithmetic_needs_no_mesh_library():
    """The cap arithmetic decides whether a mesh is even worth loading, so it
    must not be gated behind the loader.

    The module is imported FRESH inside the block — reusing the one the outer
    session already imported would exercise a module built while trimesh was
    still reachable, and prove nothing about a bare install.
    """
    with trimesh_absent():
        sys.modules.pop("kiln.mesh_payload", None)
        mesh_payload = importlib.import_module("kiln.mesh_payload")

        estimate = mesh_payload._estimate_payload_bytes(
            5_000, 10_000, with_normals=False, with_colors=False
        )
        assert estimate > 0
        assert mesh_payload._bbox_dict([[0.0, 0.0, 0.0], [40.0, 30.0, 12.0]])["size"] == [
            40.0,
            30.0,
            12.0,
        ]


def test_conversion_raises_importerror_the_callers_already_catch(tmp_path):
    """Without trimesh the encoder cannot run, and says so as an ImportError.

    Both call sites in ``kiln.local_stage`` wrap this in ``except
    Exception``, so the stage degrades to its still-image fallback instead of
    breaking the tool call.  Pinned as ``ImportError`` rather than "raises
    something" because that is the shape a caller can tell apart from a bad
    mesh.
    """
    from kiln.mesh_payload import mesh_to_viewer_payload

    part = tmp_path / "part.stl"
    part.write_bytes(b"solid s\nendsolid s\n")

    with trimesh_absent(), pytest.raises(ImportError):
        mesh_to_viewer_payload(part)


def test_the_stage_degrades_instead_of_raising(tmp_path):
    """The door a user actually reaches.

    ``_inline_payload`` returns ``None`` rather than propagating, which is
    what keeps a missing optional dependency from turning a working tool call
    into a failed one.
    """
    from kiln import local_stage

    part = tmp_path / "part.stl"
    part.write_bytes(b"solid s\nendsolid s\n")
    token = local_stage._mint(str(part))

    with trimesh_absent():
        assert local_stage._inline_payload(token) is None


def test_no_test_module_imports_trimesh_at_module_scope():
    """The regression itself, kept from coming back by a different route.

    A lazy import inside a test function is fine; a module-scope ``import
    trimesh`` is not, because pytest resolves it during collection, where an
    ImportError is fatal for the WHOLE run rather than for one file.
    ``pytest.importorskip`` is the supported way to say the same thing.
    """
    import ast
    from pathlib import Path

    offenders = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only — nested imports are lazy
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "trimesh" or n.startswith("trimesh.") for n in names):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "module-scope `import trimesh` breaks collection on a base install "
        "(trimesh is in the mesh-diagnostics extra); use "
        '`pytest.importorskip("trimesh")` instead: ' + ", ".join(offenders)
    )


def test_the_block_leaves_nothing_behind():
    """The fixture restores what it moved.

    A leaked blocker would make every later test in the session look like a
    bare install — a far more confusing failure than the one this file
    exists to prevent.  Asserted as "importable before == importable after"
    so it means something on an install that HAS trimesh and on one that
    does not.
    """

    def importable() -> bool:
        try:
            importlib.import_module("trimesh")
        except ImportError:
            return False
        return True

    before = importable()
    payload_before = sys.modules.get("kiln.mesh_payload")

    with trimesh_absent():
        sys.modules.pop("kiln.mesh_payload", None)
        importlib.import_module("kiln.mesh_payload")

    assert not any(isinstance(f, _Blocker) for f in sys.meta_path)
    assert importable() is before
    # The module built under the block must not outlive it.
    assert sys.modules.get("kiln.mesh_payload") is payload_before
