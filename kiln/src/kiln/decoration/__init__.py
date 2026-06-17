"""Decoration application — the generic, free "press" step.

A decoration *preset* (versioned, branchable, signable) is a kiln-pro
feature.  This package holds the public, mechanical last step that turns a
resolved preset fingerprint into actual carved geometry on a host mesh.
"""
from __future__ import annotations

from kiln.decoration.apply import apply_decoration_spec

__all__ = ["apply_decoration_spec"]
