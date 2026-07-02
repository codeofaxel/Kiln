# ClawHub skill source — publish from HERE only

This folder is the single source for the Kiln skill on ClawHub. By design it
contains **only** the skill file, so nothing extraneous can ever be bundled
into a publish. Anything added here shows up in `git status`, so the contents
stay auditable.

Publish a new version:

```sh
clawhub publish clawhub-skill/ \
  --slug kiln --name "Kiln" --version <version> \
  --tags "latest,3d-printing,mcp,ai-agent,bambu,octoprint,moonraker,prusa,elegoo,creality" \
  --changelog "<what changed>"
```

Rules:
- Publish **only** from this folder — never from a scratch or notes directory.
- Keep this folder limited to the skill file(s); if the top-level `SKILL.md`
  changes, copy it here so the two stay in sync.
