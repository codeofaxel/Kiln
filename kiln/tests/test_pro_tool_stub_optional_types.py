"""A pro-tool stub's optional parameters keep their real type.

``_register_pro_tool_stubs`` rebuilds each pro tool's signature from
``pro_tool_manifest.json`` so FastMCP can publish a schema.  An optional
parameter (``float | None``, ``list[str] | None``) serialises with no
top-level ``"type"`` and an ``anyOf`` of the concrete type and ``null``.
The resolver used to default the missing ``"type"`` to ``"string"`` and only
consult ``anyOf`` when that default was NOT a known type -- and ``"string"``
always is, so the ``anyOf`` branch could never run and every optional
non-string parameter was published as ``str``.  (Replayed 2026-09-09 over
the committed manifest: ``record_nozzle_replacement`` came out with
``new_diameter_mm: str = None``.)
"""

from __future__ import annotations

import json

from kiln import server


def _stub_signature(tmp_path, monkeypatch, properties, required=()):
    manifest = {
        "tools": [
            {
                "name": "typed_tool",
                "description": "d",
                "tier": "pro",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                },
            }
        ]
    }
    (tmp_path / "pro_tool_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(server, "Path", lambda _p: tmp_path / "kiln")
    monkeypatch.setattr(server, "_PRO_TOOL_NUDGES", {})
    monkeypatch.setattr(server, "_PRO_TOOL_TIERS", {})
    monkeypatch.setattr(server, "_PRO_TOOL_QUOTA", {})
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    server._register_pro_tool_stubs(_FakeMCP())
    return captured["typed_tool"].__signature__.parameters


def test_an_optional_number_is_published_as_float(tmp_path, monkeypatch):
    params = _stub_signature(tmp_path, monkeypatch, {
        "new_diameter_mm": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None},
    })
    assert params["new_diameter_mm"].annotation is float
    assert params["new_diameter_mm"].default is None


def test_an_optional_list_is_published_as_list(tmp_path, monkeypatch):
    params = _stub_signature(tmp_path, monkeypatch, {
        "printer_names": {
            "anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}],
            "default": None,
        },
    })
    assert params["printer_names"].annotation is list


def test_first_concrete_non_null_type_wins_and_string_stays_the_fallback(tmp_path, monkeypatch):
    params = _stub_signature(tmp_path, monkeypatch, {
        "plain": {"type": "string"},
        "count": {"anyOf": [{"type": "null"}, {"type": "integer"}], "default": None},
        "flag": {"type": "boolean", "default": False},
        "mystery": {"anyOf": [{"type": "null"}], "default": None},
    }, required=("plain",))
    assert params["count"].annotation is int
    assert params["flag"].annotation is bool and params["flag"].default is False
    assert params["mystery"].annotation is str
    assert params["plain"].annotation is str and params["plain"].default is params["plain"].empty
