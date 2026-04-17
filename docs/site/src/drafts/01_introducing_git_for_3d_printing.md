---
title: "Introducing Git for 3D Printing — branch, merge, sign, release"
slug: "introducing-git-for-3d-printing"
description: "Kiln v1.0 ships git-style version control for your 3D designs. Branch, merge, sign, release — the workflow you know from code, now for physical objects."
tags: ["kiln", "version-control", "3d-printing", "fdm", "v1.0"]
date: "TODO-v1.0-launch-day"
status: draft
---

# Introducing Git for 3D Printing

You've iterated on a design twelve times. Version four printed perfectly at 210°C. Version seven warped. Version nine had the logo in the wrong spot but the base was finally right. Which one do you send to the printer for the real job?

Kiln v1.0 ships a full git-style version control system for 3D designs. Every edit is a commit. Every experiment is a branch. Every production handoff is a signed release. The workflow you know from code, now for physical objects.

## Branch your design the way you branch code

Fork a named branch from any version:

```bash
kiln branch create my-coaster experimental --base <version_id>
```

Iterate freely on `experimental`. Your `main` branch stays untouched. When you're ready, detect conflicts against `main`:

```bash
kiln merge detect-conflicts <ancestor_id> <source_id> <target_id>
```

## Three-way mesh merge that actually understands geometry

Ordinary diff tools see mesh files as a wall of binary bytes. Kiln diffs at the *semantic* level — Z-level set differences, pocket additions and removals, bounding-box changes — each quantized to manufacturing tolerances (0.01mm bbox, 0.2mm pocket depth). A conflict in Kiln means something real: "both branches added a pocket at the same coordinate." Resolve with one of five strategies: prefer A, prefer B, apply both if non-intersecting, apply neither, or supply a user-curated mesh.

## Sign a release, prove it shipped

```bash
kiln release sign my-coaster <version_id> 1.0.0
```

Kiln produces an Ed25519-signed manifest over the design's full mesh fingerprint, its ancestry chain, all recorded outcomes, and provenance metadata. The signing key lives at `~/.kiln/release_keys/` — yours alone. Any downstream party (auditor, supplier, customer) can verify the release with `kiln release verify`. The verification also optionally recomputes the fingerprint from the STL on disk and compares — if the mesh was tampered with after signing, verification fails. This is the FDA 21 CFR 820.70 / AS9100 clause 8.5.6 / ISO 13485 clause 7.5.6 guarantee in one command.

## Three artifact types, one system

Version control isn't just for the mesh itself. Kiln extends the same machinery to:

- **Decorations** — textures, logos, photo-emboss, brand assets. Branch a logo's depth, merge a new pattern, sign the preset as a production-ready brand asset.
- **Features** — mechanical geometry (chamfers, fillets, pockets, holes, bosses, ribs, screw-hole patterns, custom OpenSCAD). Version a "reinforced handle fillet" the same way you'd version a library function.

Every artifact type gets branching, three-way merge, signed releases, cherry-pick, and cross-branch A/B outcome correlation.

## Cherry-pick a single change

```bash
kiln cherrypick <ancestor_id> <source_id> <target_id> --field parameters.radius_mm
```

Isolate one change from an experimental branch and apply it to main — without pulling in the unrelated changes that branch also made.

## A/B on print outcomes

Record what actually happened on each branch:

```bash
kiln versions record-outcome <version_id> bambu-a1 PLA --success --warp 0.5
```

Kiln builds the correlation across all branches and tells you which design change is statistically driving outcome differences. *"Shorter branch has 33% higher success rate with high confidence (N=6)."* That's a design decision backed by data, not vibes.

## Runs offline by default

No account. No cloud. No login. Kiln's version control uses your OS username as the author (falls back to `"local"` in locked-down environments). Every operation — branch, merge, release, cherry-pick, A/B — runs on your laptop, against SQLite stores in `~/.kiln/`. Your designs never leave your machine unless you choose.

Optional: push branches and signed releases to your own Supabase project for multi-device access. Same operations, server-side RLS, Ed25519-signed Merkle push receipts for every upload. GitHub-equivalent security, operator-controlled infrastructure.

## Who benefits

- **Hobbyists** who want to stop losing track of which version printed right
- **Small makers and print-for-hire shops** who need traceability without enterprise PLM overhead
- **Regulated industries** (medical, aerospace, defense) who need FDA / AS9100 / ISO 13485 tamper-evidence on production release handoff
- **AI agents** — Kiln exposes the whole git surface as MCP tools, so Claude/GPT/custom agents can branch, merge, sign, and release on your behalf

## FDM today, the pattern is universal

Kiln v1.0 ships git-for-3D-printing scoped to FDM. The architecture is agnostic — the same semantic-merge primitives apply to any printable geometry — but we built and tested FDM first because that's what our users print. Broader support follows user signal.

## Try it

```bash
pipx install kiln3d
kiln verify
kiln branch create coaster experimental --base <version>
```

Full CLI reference, MCP tool catalog, and architecture whitepaper at [kiln3d.com](https://kiln3d.com). Source at [github.com/codeofaxel/Kiln](https://github.com/codeofaxel/Kiln), AGPL-3.0.
