"""``from kiln import x`` waits for another thread that is still importing x.

``kiln.__getattr__`` resolves kiln-pro's shimmed submodules from
``sys.modules``, and a submodule another thread is importing sits there half
built.  It used to be handed out as it was: on a cold start, slices run in
parallel read the melt-rate bridge before its functions existed, and each of
them silently lost its material settings (41 of 48 in the slicer-pace run on
a fresh export).  An import statement waits for the other thread; so must
the hook.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import threading

import kiln

_LEAF = "_mid_import_probe"
_NAME = f"kiln.{_LEAF}"


class _HeldOpen(importlib.abc.Loader):
    """A module whose import stops halfway until *release* is set."""

    def __init__(self) -> None:
        self.halfway = threading.Event()
        self.release = threading.Event()

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        self.halfway.set()
        self.release.wait(10)
        module.answer = 42


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, loader: _HeldOpen) -> None:
        self.loader = loader

    def find_spec(self, fullname, path=None, target=None):
        return importlib.util.spec_from_loader(fullname, self.loader) if fullname == _NAME else None


def test_a_module_another_thread_is_importing_is_waited_for() -> None:
    loader = _HeldOpen()
    finder = _Finder(loader)
    sys.meta_path.insert(0, finder)
    seen: dict[str, object] = {}

    def read() -> None:
        try:
            from kiln import _mid_import_probe as probe

            seen["answer"] = probe.answer
        except Exception as exc:  # noqa: BLE001 -- what the reader got is the finding
            seen["error"] = repr(exc)

    try:
        importer = threading.Thread(target=importlib.import_module, args=(_NAME,), daemon=True)
        importer.start()
        assert loader.halfway.wait(5)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        # A reader that does not wait has its answer long before this.
        reader.join(0.5)
        loader.release.set()
        importer.join(5)
        reader.join(5)
        assert seen == {"answer": 42}
    finally:
        loader.release.set()
        sys.meta_path.remove(finder)
        sys.modules.pop(_NAME, None)
        kiln.__dict__.pop(_LEAF, None)


def test_a_module_already_imported_is_still_found() -> None:
    """The hook's own job is unchanged: a submodule in ``sys.modules`` that is
    not an attribute of the package yet resolves as one."""
    import types

    shim = types.ModuleType(_NAME)
    shim.answer = 7
    sys.modules[_NAME] = shim
    try:
        from kiln import _mid_import_probe as probe

        assert probe is shim
    finally:
        sys.modules.pop(_NAME, None)
        kiln.__dict__.pop(_LEAF, None)
