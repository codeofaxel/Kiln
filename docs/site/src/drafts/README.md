# Blog post drafts — DO NOT PUBLISH without sign-off

Files in this directory are **drafts**, not live pages.  They sit
outside `src/pages/` so Astro's file-based router does NOT build them
and they do NOT appear on the live site.

Queue for the v1.0 marketing blitz:

1. `01_introducing_git_for_3d_printing.md` — flagship launch post (full
   draft, ready to review)
2. `02_semantic_mesh_merge.md` — technical deep-dive (outline)
3. `03_sketch_to_signed_release.md` — use-case walkthrough (outline)
4. `04_three_artifacts_one_substrate.md` — conceptual framing (outline)

**To publish:**
1. Review + edit the draft.
2. Move to `src/pages/blog/<slug>.astro` and convert the frontmatter to
   the live blog layout used by the rest of `pages/blog/`.
3. Add a card entry to `pages/blog.astro`.
4. **Don't edit any EXISTING blog post** — historical counts stay
   frozen at the value correct on that post's publication date.
