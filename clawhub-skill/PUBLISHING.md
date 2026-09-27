# ClawHub skill source

ClawHub is published by CI when the GitHub release is created (and on
demand via `.github/workflows/publish-registries.yml`), from a minimal
folder assembled at publish time: `SKILL.md`, `server.json`, `README.md`,
and `LICENSE`. Never run `clawhub publish` by hand.

If the top-level `SKILL.md` changes, keep this folder's copy in sync.
