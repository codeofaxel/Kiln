# Kiln MCPB bundle

Packages Kiln as an [MCPB](https://github.com/modelcontextprotocol/mcpb)
extension — a one-click local install for MCP clients that support the
format (Claude Desktop, Smithery's local-bundle path, etc.).

## Privacy Policy

Full policy: **https://www.kiln3d.com/privacy**

Kiln is local-first. This bundle runs on your own machine and talks
directly to printers on your network; printer hosts, API keys, and the
models you make stay on your device. Kiln contacts its own services
(operated by Hadron Labs Inc.) only for the optional account-backed
features you choose to use — signing in, cloud sync, licensed Pro
tools, and professional print fulfillment — and, when enabled, sends
only what those features need. The linked policy covers what is
collected, how it is used and stored, third-party subprocessors
(including the fulfillment and payment partners), retention periods,
and how to contact us. Questions: **adam@kiln3d.com**.

## Why `uv`, not a vendored bundle

Kiln has three compiled dependencies (`pydantic` via the MCP SDK, `numpy`,
`cryptography`) that the MCPB spec's own docs say can't portably vendor
across platforms. Instead this bundle ships a 3-line wrapper
(`src/server.py`) plus a `pyproject.toml` declaring `kiln3d` as its only
dependency. The `uv` runtime resolves and installs it fresh on first run,
fetching platform-native wheels for whatever OS the user is actually on —
the same thing `pip install kiln3d` does today, just automated.

`pyproject.toml` declares `kiln3d` unpinned. As of `mcpb` CLI 2.1.2,
`mcpb pack` does **not** bundle a lockfile at all — the archive ships no
pinned version, so every install resolves whatever's live on PyPI at
that moment. (An earlier version of this doc, and an earlier CLI
version, observed `pack` generating and including a pinned `uv.lock` —
that's no longer true; verified directly against 2.1.2. If you see a
`uv.lock` appear in a pack's file list again, something changed back and
the staleness risk is real again.) `manifest.json`'s own `version` field
still needs bumping each release for display consistency — cosmetic, not
a functional-staleness issue — caught by
`tests/test_mcpb_manifest_version_matches_package`.

**`server.type` is `"python"`, not `"uv"`, despite `mcp_config.command`
being `uv`.** Smithery's publish tool only recognizes `bun` (via command
basename), `python`, `node`, or `binary` as a `server.type` — `"uv"` is a
legitimate MCPB spec value but isn't in Smithery's classifier yet
(verified by reading `smithery`'s installed CLI source directly:
`dist/index.js`, function that throws `"Could not determine bundle
runtime from manifest"`). That check only reads the `type` label to
categorize the listing — it does not change what actually executes,
which still comes straight from `mcp_config.command`/`args` (`uv run
--directory ... src/server.py`), unchanged. So the label is a compat
shim for Smithery specifically; the real behavior stays exactly the
`uv`-resolved, no-vendoring approach described above. If Smithery adds
real `uv` recognition later, revert this to `"uv"`.

**`.mcpbignore` matters — `mcpb pack` does NOT read `.gitignore`.** A
stray local `.venv/` (e.g. left over from the manual smoke test below)
gets vendored wholesale into the archive otherwise — observed first-hand
as a 25.5 MB / 3,744-file bundle that blew past Smithery's 25 MB limit,
instead of the correct ~55 KB / 5 files. `.mcpbignore` in this directory
excludes `.venv/`, `*.mcpb`, `uv.lock`, `.gitignore` — keep it in sync if
new local build artifacts show up here.

## Rebuilding the `.mcpb` file (do this every release)

```bash
npm install -g @anthropic-ai/mcpb   # one-time
cd mcpb
rm -rf .venv uv.lock *.mcpb         # clear any local build/smoke-test artifacts
mcpb pack
```

Produces `kiln-<version>.mcpb` in this directory — the file to upload to a
client's local-bundle install flow. Bump `manifest.json`'s `version` to
match `kiln3d`'s `pyproject.toml` version in the same commit. Sanity-check
the pack output before publishing: package size should be ~50-60 KB and
"total files" should be 5 — anything dramatically larger means a build
artifact leaked in.

## Distribution (going live)

The manual pack above is for local testing. In production the bundle ships
two ways, and **neither goes live until a release is cut**:

1. **GitHub release asset (canonical download).** `.github/workflows/attach-mcpb.yml`
   fires on `release: published`, packs the bundle, and attaches it as a
   **stable-named** asset so the site can link one always-current URL:

   ```
   https://github.com/codeofaxel/Kiln/releases/latest/download/kiln.mcpb
   ```

   That workflow is isolated from `publish.yml` (packing is just a zip — no
   uv, no PyPI), so a pack failure can never block the package publish.

2. **Smithery (local-bundle listing).** Publish with Smithery's CLI from a
   packed bundle; the manifest's `server.type` is already the Smithery-
   compatible `"python"` label (see the note above). This is a listing on
   Smithery's registry, separate from Anthropic's Connectors Directory.

**Anthropic Connectors Directory** listing is a THIRD, later path with a
higher bar: the desktop-extension review requires a `title` +
`readOnly`/`destructive` annotation on **every** tool the bundle exposes —
the full local Kiln registry (hundreds of tools), not the curated remote
slice. Distribute via (1) and (2) first; take on the directory annotation
pass only when it's worth it.

**Go-live order:** cut the `kiln3d` release (asset auto-attaches) →
publish to Smithery → deploy the site section that links the download.

## Manual smoke test

```bash
cd mcpb
uv run --directory . src/server.py
```

Should install `kiln3d` into an isolated `uv`-managed environment and then
block, waiting for MCP stdio input — the same behavior as `kiln serve`.
Ctrl-C to stop.
