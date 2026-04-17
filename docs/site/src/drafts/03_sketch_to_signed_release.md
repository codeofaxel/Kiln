---
title: "Sketch to signed release — designing a coaster in one afternoon"
slug: "sketch-to-signed-release"
description: "A walkthrough of the full git-for-3D flow — generate, branch, merge, sign, print."
tags: ["kiln", "version-control", "walkthrough", "design-to-print"]
date: "TODO-launch-day-+5"
status: draft
---

# Sketch to signed release — designing a coaster in one afternoon

Nothing in this post is hypothetical. Every command is real. Every output is structured. Every decision is backed by data.

*(Swap real screenshots / gifs in when you produce them on Tuesday's print session.)*

## The starting point

You want a custom coaster for a coffee shop: 100mm diameter, 4mm thick, their logo debossed on the top, a cork pocket on the underside. You also want three emboss-depth variants to see which prints cleanest on your Bambu A1.

## Step 1 — describe it

```bash
kiln agent "generate a 100mm round coaster, 4mm thick, with a 2mm cork pocket on the underside, PLA"
```

Kiln's design intelligence picks a parametric template, applies the material's min-wall constraints (PLA → 1.2mm), generates the OpenSCAD, validates printability (score: 94/100), and returns a design id + initial version id.

## Step 2 — branch three variants

```bash
kiln branch create coffeeshop-coaster emboss-1mm --base <v1>
kiln branch create coffeeshop-coaster emboss-2mm --base <v1>
kiln branch create coffeeshop-coaster emboss-3mm --base <v1>
```

On each branch, apply the logo at a different depth:

```bash
kiln decorate --branch emboss-1mm --logo coffeeshop.png --depth 1.0
kiln decorate --branch emboss-2mm --logo coffeeshop.png --depth 2.0
kiln decorate --branch emboss-3mm --logo coffeeshop.png --depth 3.0
```

## Step 3 — print all three, record outcomes

```bash
kiln print --branch emboss-1mm
kiln print --branch emboss-2mm
kiln print --branch emboss-3mm
```

After each finishes, record what happened:

```bash
kiln versions record-outcome <v-1mm> bambu-a1 PLA --success --warp 0 --notes "crisp, scannable"
kiln versions record-outcome <v-2mm> bambu-a1 PLA --success --warp 0.5 --notes "nice depth"
kiln versions record-outcome <v-3mm> bambu-a1 PLA --failed --warp 2 --adhesion-issues --notes "lifted at corners"
```

## Step 4 — ask Kiln which one to ship

```bash
kiln versions best <v-1mm> <v-2mm> <v-3mm> --material PLA --printer bambu-a1
```

Output:

> Best: `<v-2mm>` — 100% success across 1 print. Ranked above `<v-1mm>` (tied success but lower aesthetic score) and `<v-3mm>` (failed adhesion at 3mm depth).

A/B insight:

```bash
kiln ab coffeeshop-coaster
```

> Varying `decoration.depth_mm` distinguishes `emboss-2mm` from `emboss-3mm`: success rate +100% with medium confidence (N=1 vs 1). Shallower wins at this material + printer combo.

## Step 5 — merge the winner back to main

```bash
kiln merge detect-conflicts <v1> <v-2mm> <main-head>
# no conflicts — the two branches edited disjoint surfaces
kiln merge branches --source emboss-2mm --target main --merged <v-2mm>
```

## Step 6 — sign the release

```bash
kiln release sign coffeeshop-coaster <v-2mm> 1.0.0 --signed-by "Adam"
```

Output:

> Release `1.0.0` signed.
> Public key fingerprint: `a3f9b2c1...`
> Manifest includes: mesh fingerprint, ancestry chain (v1 → emboss-2mm → merged), outcomes, provenance, timestamp, branch_name.
> Signed with Ed25519 key at `~/.kiln/release_keys/release_signing_key.pem`.

## Step 7 — verify (customer or auditor side)

```bash
kiln release verify coffeeshop-coaster 1.0.0 --recompute-fingerprint
```

Output:

> Valid: true
> Signature: OK
> Fingerprint: OK
> Reason: signature valid and fingerprint matches stored source.

If someone ever edits the STL after signing, that `recompute-fingerprint` step catches it and verification fails. That's the tamper-evidence guarantee in one command.

## Step 8 — ship the print run

```bash
kiln print --branch main --copies 50 --use-ams --ams-mapping 2
```

Fifty coasters print overnight. Every one matches the signed `1.0.0` bytes. Your customer can verify the release from the public key fingerprint you share with the delivery.

## What just happened

In ~two hours, you:

- Generated a parametric design with material-aware constraints
- Branched three variants in parallel, preserving the original
- Printed all three, recorded outcomes, asked Kiln which won
- Merged the winner, signed a release, printed production
- Left behind a cryptographic audit trail from concept to customer

No lost versions. No "which file was the good one." No spreadsheet tracking print outcomes. One system, one genealogy, one signed receipt.

This is what version control looks like for physical objects.
