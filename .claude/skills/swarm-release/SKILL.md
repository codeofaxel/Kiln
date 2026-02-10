# /swarm-release — Release Pipeline

A team-based skill for coordinating a release. Parallel teammates handle version bumping, content updates, and validation, with task dependencies ensuring the right ordering.

## When to Use
- Preparing a PyPI release or tagged version
- Need to bump version, update content, and validate in parallel
- Want thorough pre-release validation without doing it sequentially

## Phase 0: Release Context (Lead does this)
1. Run `git branch --show-current` — confirm branch
2. Run `git log --oneline -10` — understand what's being released
3. Determine the new version numbers (kiln and/or octoprint-cli)
4. Identify what content needs updating (changelog, README, etc.)
5. Create the task list with dependencies:
   - "validate" can start immediately (parallel with others)
   - "version" can start immediately
   - "content" can start immediately
   - "tag" depends on "validate" passing (blocked until validation completes)

## Phase 1: Spawn Teammates

### Teammate: "version"
**Prompt**: You are handling version updates for release [VERSION].

Tasks:
1. Update `version` in `kiln/pyproject.toml` to [VERSION]
2. Update `version` in `octoprint-cli/pyproject.toml` if applicable
3. Update `__version__` in any `__init__.py` files if present
4. Verify version strings are consistent across all locations

Your file ownership: WRITE to `kiln/pyproject.toml`, `octoprint-cli/pyproject.toml`, `*/__init__.py`

Report when done: old version -> new version, files changed.

### Teammate: "content"
**Prompt**: You are updating release content for version [VERSION].

Tasks:
1. Update or create changelog / release notes content
2. APPEND to changelog — do NOT replace existing entries
3. Update README.md if version references or feature lists need updating
4. Review docs/ for accuracy against current implementation

Your file ownership: WRITE to README.md, docs/, changelog files.
FORBIDDEN: Source code files, pyproject.toml (version teammate owns that).

Report when done: what content was updated.

### Teammate: "validation"
**Prompt**: You are running pre-release validation for version [VERSION].

Tasks:
1. Verify `python -m py_compile` passes on ALL Python source files in both packages
2. Run `pytest` in both `kiln/` and `octoprint-cli/` — report any failures
3. Verify all imports resolve: `python -c "import kiln"` and `python -c "import octoprint_cli"`
4. Scan for TODO stubs in critical paths: `grep -r '# TODO' kiln/src/kiln/server.py kiln/src/kiln/printers/`
5. Check that all PrinterAdapter abstract methods are implemented in every adapter
6. Verify pyproject.toml dependencies are consistent with actual imports

Report: validation status, test results, any issues found with severity.

## Phase 2: Gate Check (Lead does this)
- Wait for "validation" teammate to complete
- If validation FAILS: fix the issues (or spawn a bugfix teammate), then re-validate
- If validation PASSES: unblock the tagging step

## Phase 3: Tag & Finalize (Lead does this after validation passes)
1. Stage all changes from teammates
2. Create a commit with the version bump + content changes
3. Tag the commit: `v[VERSION]`
4. Report the release summary:

```
Release: v[VERSION]
Branch: [branch-name]
Validation: PASS
Content Updated: [list]
Tag: v[VERSION]
Ready for: [PyPI / GitHub Release]
```

Do NOT push unless the user explicitly asks.

## Rules
- Validation teammate MUST pass before tagging. This is a hard dependency.
- Content teammate APPENDS to changelogs, never replaces.
- Version teammate checks LESSONS_LEARNED.md for known pitfalls.
- Lead does not push to remote without explicit user approval.
