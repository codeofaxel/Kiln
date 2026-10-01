# ClawHub skill source

ClawHub is published by CI when the GitHub release is created (and on
demand via `.github/workflows/publish-registries.yml`), from a minimal
folder assembled at publish time: `SKILL.md`, `server.json`, `README.md`,
and `LICENSE`. Never run `clawhub publish` by hand.

CI publishes the top-level `SKILL.md`. This folder holds no copy of it: one
file, so the published skill and the repository cannot disagree.
