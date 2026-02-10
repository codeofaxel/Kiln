# /swarm-bugbash — Parallel Bug Fixing

A team-based skill for fixing multiple independent bugs in parallel. One teammate per bug, with file ownership ensuring no conflicts.

## When to Use
- 3+ independent bugs to fix
- Bugs affect different files (no overlapping edits needed)
- You want throughput over a batch of known issues

For a single bug, just fix it directly. For bugs that share the same files, assign them to one teammate sequentially.

## Phase 0: Bug Inventory (Lead does this)
1. Run `git branch --show-current` — confirm branch
2. Collect the bug list from the user, or scan for issues:
   - Import/syntax errors from `python -m py_compile`
   - Test failures from `pytest`
   - TODO stubs: `grep -r '# TODO' kiln/src/ octoprint-cli/src/`
   - Type safety issues: `grep -r 'type: ignore' kiln/src/ octoprint-cli/src/`
   - Known issues from conversation context
3. For each bug, identify the affected file(s)
4. Check for file overlaps — bugs sharing files go to the SAME teammate
5. Create the task list with one task per bug (or bug group)

## Phase 1: Spawn Teammates

One teammate per bug (or bug group if files overlap). Each gets:

**Prompt template**:
You are fixing [BUG DESCRIPTION].

Affected files: [FILE LIST]
Your file ownership: WRITE access to [FILE LIST] only.

Process:
1. Read the affected files and understand the current behavior
2. Identify the root cause
3. Implement the fix
4. Search for the same pattern elsewhere: `grep` for similar code across the codebase and flag if found
5. Verify your fix compiles (`python -m py_compile [files]`)
6. Report: what you found, what you changed, and any sibling occurrences

Constraints:
- Follow all Hard Laws in CLAUDE.md
- Do NOT edit files outside your ownership list
- If the fix requires changes in files you don't own, message the lead

## Phase 2: Monitor (Lead watches)
- Track teammate progress via task list
- If a teammate discovers the bug requires touching another teammate's files, reassign
- If a teammate finishes early, they can pick up the next unassigned bug

## Phase 3: Synthesize (Lead does this)
1. Collect all teammate reports
2. Run `python -m py_compile` on all edited files to verify together
3. Run tests if they exist (`pytest`)
4. Produce summary:

```
Branch: [branch-name]
Bugs Fixed: [count]

1. [Bug description] — Fixed in [file:line]. Root cause: [one sentence].
2. [Bug description] — Fixed in [file:line]. Root cause: [one sentence].
...

Sibling Patterns Found: [any similar bugs discovered elsewhere]
Verified: [compiles / tests pass / fails]
```

## Rules
- Maximum 5 teammates. Beyond that, coordination cost exceeds benefit.
- File ownership is enforced — no two teammates edit the same file.
- Each teammate does the full fix loop autonomously (no stopping to ask).
- If a bug turns out to be more complex than expected, the teammate messages the lead rather than going rogue across file boundaries.
