---
title: "Semantic mesh merge — why three-way diff on 3D files actually works"
slug: "semantic-mesh-merge"
description: "Ordinary diff treats STL bytes as opaque. Kiln's merge operates on a manufacturing-domain fingerprint. Here's how."
tags: ["kiln", "version-control", "semantic-merge", "technical-deep-dive"]
date: "TODO-launch-day-+3"
status: draft
---

# Semantic mesh merge — why three-way diff on 3D files actually works

## The problem

Two designers edit the same coaster in parallel. One thickens the base from 2mm to 3mm. The other adds a pocket for a cork inlay. In text, merging two such edits is trivial — `git merge` resolves it in microseconds. On a mesh file it's a binary wall. Every edit re-triangulates half the model. Running `diff` on two STLs tells you nothing useful.

That's the problem Kiln's semantic merge solves.

## What "semantic" means for geometry

Kiln computes a **manufacturing-domain fingerprint** from every mesh:

- **Z-level contour sets** at ±0.85 normal thresholds (sampled every 0.05mm)
- **Pocket detection** at 0.2mm minimum depth
- **Bounding-box dimensions** quantized to 0.01mm

Two meshes with different triangulations but the same geometry hash IDENTICAL. Two meshes with an added pocket hash DIFFERENT. That's the collapse that makes merge tractable.

## The six conflict types

When two branches diverge from a common ancestor, Kiln classifies every difference:

1. `Z_LEVEL_ADD_ADD` — both added a Z-level feature at the same coordinate
2. `Z_LEVEL_REMOVE_REMOVE` — both removed the same feature
3. `POCKET_ADD_ADD` — both added a pocket at the same (x, y, z)
4. `POCKET_REMOVE_REMOVE` — both removed a pocket at the same position
5. `BBOX_DIVERGENCE` — both changed the bounding box along the same axis
6. `SOURCE_REGION_OVERLAP` — both edited the same SCAD source region (when source is available)

Non-conflicting changes compose automatically. Conflicts surface in a structured report with a stable `feature_id` key so the user's resolution choices are durable across re-runs.

## Five resolution strategies (claim-mandated)

Every conflict advertises one or more of these:

- `prefer_branch_a`
- `prefer_branch_b`
- `apply_both_if_non_intersecting` (only when the field sets are disjoint)
- `apply_neither`
- `user_curated_mesh_input`

The user picks one per conflict. Kiln composes the merged fingerprint predictively so the reviewer sees exactly what the merge will produce before committing.

## Pocket-identity match (the FDA / AS9100 / ISO 13485 guarantee)

A naive implementation would match pockets by COUNT. Two meshes with 3 pockets each would hash the same. That breaks tamper-evidence: a malicious edit could *move* a pocket while preserving the count, and the fingerprint would still match.

Kiln uses **identity-based greedy bipartite matching** on (center_x, center_y, floor_z) within a 0.05mm spatial tolerance. Two pockets at the same (x, y, z) match. Two pockets with identical count but different positions fail verification. That's what makes release verification a real tamper-evidence guarantee, not marketing theater.

## Why this matters

For a regulated supply chain — medical devices, aerospace, defense — "version 1.2.4 of this bracket shipped on date X" has to mean something cryptographically. Kiln's signed releases carry the semantic fingerprint; verifying a release recomputes the fingerprint from the STL on disk and compares. If the mesh was tampered with post-signing, `kiln release verify` returns `valid: false` with a clear reason.

For a hobbyist, the same primitive gives you "my shorter branch + thinner wall + textured rim merged cleanly into main, and the print outcome data says the combination beats the old version 87% vs 73%." Version control, with real feedback.

## What's next

Semantic merge is the crown jewel of Kiln's Priority 8 patent claim. It extends to decoration presets (field-level diff on pattern / depth / surface) and features (field-level diff on dimensions / target / OpenSCAD module). One substrate, three artifact types.

Next post in this series: how the same machinery handles mechanical features.
