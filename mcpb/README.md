# Kiln MCPB bundle

Packages Kiln as an [MCPB](https://github.com/modelcontextprotocol/mcpb)
extension — a one-click local install for MCP clients that support the
format (Claude Desktop, Smithery's local-bundle path, etc.).

## Why `uv`, not a vendored bundle

Kiln has three compiled dependencies (`pydantic` via the MCP SDK, `numpy`,
`cryptography`) that the MCPB spec's own docs say can't portably vendor
across platforms. Instead this bundle ships a 3-line wrapper
(`src/server.py`) plus a `pyproject.toml` declaring `kiln3d` as its only
dependency. The `uv` runtime resolves and installs it fresh on first run,
fetching platform-native wheels for whatever OS the user is actually on —
the same thing `pip install kiln3d` does today, just automated.

`pyproject.toml` declares `kiln3d` unpinned, but `mcpb pack` generates a
`uv.lock` that **does** pin the resolved version at pack time — verified:
packing today locked `kiln3d==1.1.9`. That means **this bundle must be
repacked at every Kiln release**, or users downloading it will keep
getting whatever version was current when it was last packed. This is not
optional hygiene; it is the same class of drift `server.json` had (version
1.1.8 sitting stale after a 1.1.9 release) — `tests/test_mcpb_manifest.py`
catches it by asserting `manifest.json`'s `version` and the lockfile's
pinned `kiln3d` version both match the live `kiln3d` package version.

## Rebuilding the `.mcpb` file (do this every release)

```bash
npm install -g @anthropic-ai/mcpb   # one-time
cd mcpb
rm -f uv.lock                       # force a fresh resolve, not the stale pin
mcpb pack
```

Produces `kiln-<version>.mcpb` in this directory — the file to upload to a
client's local-bundle install flow. Bump `manifest.json`'s `version` to
match `kiln3d`'s `pyproject.toml` version in the same commit.

## Manual smoke test

```bash
cd mcpb
uv run --directory . src/server.py
```

Should install `kiln3d` into an isolated `uv`-managed environment and then
block, waiting for MCP stdio input — the same behavior as `kiln serve`.
Ctrl-C to stop.
