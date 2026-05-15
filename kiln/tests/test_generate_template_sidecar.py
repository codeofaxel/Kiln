"""Tests for the optional kiln-pro intent-sidecar hook on ``generate_from_template``.

When the kiln-pro package is installed, the public
``generate_from_template`` MCP tool derives a ``DeclaredIntent`` from
the template parameters and writes a ``<mesh>.intent.json`` sidecar
next to the produced STL.  Free / public installs see no sidecar and
no ``intent`` key on the result.

These tests exercise the bridge hook without requiring kiln-pro to
actually be installed — a fake ``kiln_pro.bridge`` module is injected
via ``sys.modules``.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import types
from pathlib import Path

import pytest

from kiln.generation.base import (
    GenerationJob,
    GenerationResult,
    GenerationStatus,
)


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    header = b"\x00" * 80
    count = struct.pack("<I", len(triangles))
    body = b""
    for v1, v2, v3 in triangles:
        normal = struct.pack("<3f", 0.0, 0.0, 0.0)
        verts = struct.pack("<9f", *v1, *v2, *v3)
        body += normal + verts + struct.pack("<H", 0)
    return header + count + body


def _cube_triangles(size: float = 10.0) -> list[tuple]:
    s = size
    verts = [
        (0, 0, 0),
        (s, 0, 0),
        (s, s, 0),
        (0, s, 0),
        (0, 0, s),
        (s, 0, s),
        (s, s, s),
        (0, s, s),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 4, 5),
        (0, 5, 1),
        (2, 6, 7),
        (2, 7, 3),
        (0, 3, 7),
        (0, 7, 4),
        (1, 5, 6),
        (1, 6, 2),
    ]
    return [(verts[a], verts[b], verts[c]) for a, b, c in faces]


class _StubOpenSCADProvider:
    """Minimal provider that pretends to render a fixed STL.

    Skips the real OpenSCAD subprocess so the test runs anywhere.
    """

    name = "openscad"

    def __init__(self, stl_path: str) -> None:
        self._stl_path = stl_path

    def generate(self, prompt: str, *, format: str = "stl", **kwargs) -> GenerationJob:
        return GenerationJob(
            id="job-1",
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.SUCCEEDED,
            progress=100,
            created_at=0.0,
            format=format,
        )

    def get_job_status(self, job_id: str) -> GenerationJob:  # pragma: no cover
        raise AssertionError("Synchronous stub should not be polled.")

    def download_result(self, job_id: str, output_dir: str | None = None) -> GenerationResult:
        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=self._stl_path,
            format="stl",
            file_size_bytes=os.path.getsize(self._stl_path),
            prompt="",
        )


def _patch_openscad_provider(monkeypatch, stl_path: str) -> None:
    """Replace the cached OpenSCAD provider with a stub.

    The public ``generate_from_template`` hard-codes ``"openscad"`` as
    the provider, so we stuff our stub into ``_generation_providers``
    before the call.  monkeypatch.setattr on the dict guarantees
    cleanup after the test exits.
    """
    from kiln import server as _server

    stub = _StubOpenSCADProvider(stl_path)
    # Swap the provider cache wholesale so test pollution is impossible:
    # the assignment is tracked by monkeypatch and reverted at teardown.
    monkeypatch.setattr(_server, "_generation_providers", {"openscad": stub})


class _FakeDeclaredIntent:
    """Stand-in for ``DeclaredIntent`` with the methods the hook calls."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return dict(self._payload)


def _install_fake_kiln_pro(
    monkeypatch,
    *,
    intent_payload: dict,
    sidecar_paths: list[Path],
) -> None:
    """Inject a fake ``kiln_pro.bridge`` exposing ``intent_verification``."""
    iv_module = types.ModuleType("kiln_pro.intent_verification")

    def _derive_intent_from_template(template_id, parameters, dimensions_mm=None):
        return _FakeDeclaredIntent(
            {
                "generator": f"generate_from_template:{template_id}",
                "parameters": dict(parameters or {}),
                "dimensions_mm": dimensions_mm,
                **intent_payload,
            }
        )

    def _write_intent_sidecar(intent, mesh_path):
        p = Path(mesh_path)
        sidecar = (
            p.with_suffix(p.suffix + ".intent.json")
            if p.suffix
            else p.with_suffix(".intent.json")
        )
        sidecar.write_text(
            '{"intent": "fake", "payload": ' + repr(intent.to_dict()) + "}",
            encoding="utf-8",
        )
        sidecar_paths.append(sidecar)
        return sidecar

    iv_module.derive_intent_from_template = _derive_intent_from_template
    iv_module.write_intent_sidecar = _write_intent_sidecar

    class _FakeProFeatures:
        intent_verification = iv_module

        def is_available(self, feature: str) -> bool:
            return feature == "intent_verification"

    bridge_module = types.ModuleType("kiln_pro.bridge")
    bridge_module.pro_features = _FakeProFeatures()

    pkg_module = types.ModuleType("kiln_pro")
    pkg_module.bridge = bridge_module

    monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
    monkeypatch.setitem(sys.modules, "kiln_pro.intent_verification", iv_module)


class TestGenerateFromTemplateIntentSidecar:
    """The optional kiln-pro intent-sidecar bridge call."""

    def test_no_kiln_pro_installed_emits_no_sidecar(self, monkeypatch):
        # Force the bridge import to fail.
        import builtins as _builtins
        real_import = _builtins.__import__

        def _no_kiln_pro(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("kiln_pro"):
                raise ImportError("simulated: kiln-pro not installed")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(_builtins, "__import__", _no_kiln_pro)

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = os.path.join(tmpdir, "rendered.stl")
            with open(stl_path, "wb") as fh:
                fh.write(_make_binary_stl(_cube_triangles(10.0)))

            _patch_openscad_provider(monkeypatch, stl_path)

            from kiln.server import generate_from_template

            result = generate_from_template(
                "phone_stand",
                {"phone_width": 80.0, "base_depth": 60.0, "thickness": 5.0},
            )

            assert result.get("success") is True
            # No intent block on the result.
            assert "intent" not in result
            # No sidecar written.
            sidecar = Path(stl_path).with_suffix(".stl.intent.json")
            assert not sidecar.is_file()

    def test_kiln_pro_installed_writes_sidecar_and_intent_block(self, monkeypatch):
        sidecar_paths: list[Path] = []
        intent_payload = {"intent_id": "abc123", "canonical_hash": "h"}

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = os.path.join(tmpdir, "rendered.stl")
            with open(stl_path, "wb") as fh:
                fh.write(_make_binary_stl(_cube_triangles(10.0)))

            _install_fake_kiln_pro(
                monkeypatch,
                intent_payload=intent_payload,
                sidecar_paths=sidecar_paths,
            )
            _patch_openscad_provider(monkeypatch, stl_path)

            from kiln.server import generate_from_template

            result = generate_from_template(
                "phone_stand",
                {"phone_width": 80.0, "base_depth": 60.0, "thickness": 5.0},
            )

            assert result.get("success") is True

            # The result carries the serialized intent block.
            assert "intent" in result
            assert result["intent"].get("intent_id") == "abc123"
            assert result["intent"].get("generator") == "generate_from_template:phone_stand"

            # The sidecar file was written next to the STL.
            expected_sidecar = Path(stl_path).with_suffix(".stl.intent.json")
            assert expected_sidecar.is_file()
            assert sidecar_paths == [expected_sidecar]

    def test_intent_emission_failure_does_not_break_generator(self, monkeypatch):
        # Inject a fake intent_verification whose derive call raises.
        iv_module = types.ModuleType("kiln_pro.intent_verification")

        def _broken(*_args, **_kwargs):
            raise RuntimeError("simulated derive failure")

        iv_module.derive_intent_from_template = _broken
        iv_module.write_intent_sidecar = lambda *a, **k: None

        class _FakeProFeatures:
            intent_verification = iv_module

            def is_available(self, feature: str) -> bool:
                return feature == "intent_verification"

        bridge_module = types.ModuleType("kiln_pro.bridge")
        bridge_module.pro_features = _FakeProFeatures()
        pkg_module = types.ModuleType("kiln_pro")
        pkg_module.bridge = bridge_module

        monkeypatch.setitem(sys.modules, "kiln_pro", pkg_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge_module)
        monkeypatch.setitem(sys.modules, "kiln_pro.intent_verification", iv_module)

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = os.path.join(tmpdir, "rendered.stl")
            with open(stl_path, "wb") as fh:
                fh.write(_make_binary_stl(_cube_triangles(10.0)))

            _patch_openscad_provider(monkeypatch, stl_path)

            from kiln.server import generate_from_template

            result = generate_from_template(
                "phone_stand",
                {"phone_width": 80.0, "base_depth": 60.0, "thickness": 5.0},
            )

            # Generator path still returns success; intent block absent.
            assert result.get("success") is True
            assert "intent" not in result
