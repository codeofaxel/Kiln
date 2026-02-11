# Kiln — Open Tasks

Prioritized backlog of features and improvements.

## High Priority

_(No high-priority tasks remaining.)_

## Medium Priority

- **Bundled slicer profiles per printer** — Ship curated PrusaSlicer/OrcaSlicer profiles (`.ini` files) inside the package, keyed by printer model. Agents auto-select the right profile when slicing, eliminating manual setup. Extend `slicer.py` to accept a `printer_id` and resolve the bundled profile path.

- **Printer profile intelligence (firmware quirks DB)** — Expand the safety profiles JSON into a full printer knowledge base: firmware quirks, known failure modes, optimal retraction settings, calibration sequences. Agents query this to make smarter decisions without trial-and-error.

- **Pre-validated print pipelines** — Build named command sequences (`quick-print`, `calibrate`, `benchmark`) that chain multiple MCP tools into reliable one-shot operations. E.g. `quick-print` = slice → preflight → upload → print with progress polling. Reduces agent prompt complexity.

- **Opt-in anonymous telemetry** — Collect anonymized print success/failure rates, common error patterns, and printer model distribution. Aggregate data feeds back into improving safety profiles and slicer defaults. Must be strictly opt-in with clear data policy.

## Low Priority / Future Ideas

- **Claim `kiln-print` and `kiln-mcp` on PyPI** — Register as pending publishers or publish placeholder packages to reserve the names.
- **Register `kiln3d-octoprint` on PyPI** — Add as pending publisher, then uncomment the publish job in `.github/workflows/publish.yml`.
- **Local model cache/library** — Agents save generated or downloaded models locally with tagged metadata (source, prompt, dimensions, print history) so they can reuse them across jobs without re-downloading or re-generating.
