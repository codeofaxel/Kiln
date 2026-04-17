---
title: "Three artifacts, one version-control substrate"
slug: "three-artifacts-one-substrate"
description: "Why designs, decorations, and features share one git-style backbone — and why that matters for iteration."
tags: ["kiln", "version-control", "architecture", "fdm"]
date: "TODO-launch-day-+8"
status: draft
---

# Three artifacts, one version-control substrate

Most 3D-printing software treats a design as one monolithic thing — an STL, a 3MF, a printer job. Kiln splits the artifact surface into three:

- **Designs** — your mesh. The cup, the bracket, the coaster.
- **Decorations** — surface treatments. Logos, textures, photo-emboss, brand assets.
- **Features** — mechanical geometry. Chamfers, fillets, pockets, holes, bosses, ribs, screw-hole patterns, custom OpenSCAD.

All three are **first-class versioned objects**. All three get the same operations: branch, three-way merge, Ed25519-signed releases, cherry-pick a single change, cross-branch outcome A/B.

## Why split them

Because a real workflow rarely touches all three at the same pace.

You iterate on a *coaster design* once. You then iterate on the *coffee shop's logo depth* six times to find what scans cleanly in PLA vs PETG. You later iterate on the *screw-hole pattern* of the mount for that same coaster's base — different cadence, different review audience, different success metrics.

If those three things are a single monolith, iterating on any one means touching all of them. History gets tangled. "Who changed what" becomes unanswerable. A release that bundles `design + decoration + feature` into one version is a release you can't selectively improve.

Splitting lets each artifact evolve independently. Your logo is on v7 while the coaster design is still v3 and the mount screw-hole pattern is on v2. You can cherry-pick the v7 logo onto a v4 coaster variant without pulling in whatever else happened on the decoration branch.

## Why share the substrate

Because the operations are generic.

"Three-way diff against common ancestor → classify differences → resolve by policy → compose the merge" is the same machinery whether you're merging a logo depth or a fillet radius. The `VersionedArtifact` concept underneath means the branch/merge/release/cherry-pick/A-B code lives once.

This shows up in three places:

1. **Shared primitives.** `_artifact_primitives.py` holds the generic helpers — canonical JSON for fingerprinting, local-first authorship fallback, field-level divergence detection. Every artifact family specializes it.
2. **Shared signing.** Design releases, decoration preset releases, and feature releases all sign with the SAME Ed25519 keypair at `~/.kiln/release_keys/`. One operator identity, three release types. Auditors see one public key fingerprint to trust.
3. **Shared outcome feed.** When a print completes, its outcome attributes to the exact version of all three artifacts that were attached at print time. Your decoration branch's A/B stats and your feature branch's A/B stats both improve from the same print data.

## The taxonomy decision that matters

We considered making features a subtype of decorations. Or decorations a subtype of features. Both were wrong.

A logo is a *decoration* — the user's mental verb is "decorate." A fillet is a *feature* — the user's mental verb is "add a feature." Users shouldn't have to parse a taxonomy to figure out which menu to open.

Textures stay as a decoration *subtype* (via `pattern_family="procedural_texture"`) because that mental model *does* hold — a texture is a kind of decoration. But features are a *sibling* of decorations, not a child. They share machinery, not taxonomy.

## What you can do today

Every artifact family in Kiln v1.0 supports:

- `branch create` / `list` / `annotate` / `retire`
- `merge detect-conflicts` / `merge branches`
- `release sign` / `release verify` / `release retire`
- `cherrypick` a single field modification
- `ab` cross-branch outcome correlation
- `import-external` (bring your own .scad / .stl / .json / .png / .svg)

Designs. Decorations. Features. Same verbs, three artifacts, one system.

## What's coming

The `VersionedArtifact` abstract substrate is shipped as working code but not yet unified into a single ABC — the three modules mirror each other's shape with minimal duplication, and a future refactor will collapse them without changing the public surface.

For v1.0, ship three parallel surfaces that each work end-to-end. For v1.1, the ABC refactor. For v1.2, a fourth artifact type when user signal tells us what.

Git for 3D printing isn't about mapping git commands onto 3D files. It's about treating the *intents* that go into a print — the shape, the surface, the mechanical function — as first-class versioned objects with real operations. Kiln v1.0 is what happens when you take that idea seriously.
