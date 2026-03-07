# Subject Matter Expertise: Original Printable Design

This note is for agents creating new FDM parts from an idea, not merely tweaking an existing mesh.

Success means four things at once:
- the part matches the functional brief
- the geometry fits the target printer
- the model is printable with predictable settings
- the printed part survives its intended use

## Core stance

- Treat original design as design-for-additive-manufacturing, not generic text-to-3D.
- Prefer constructive, parametric geometry for functional parts.
- Use mesh generators more cautiously for decorative or exploratory concepts.
- Do not confuse `openscad` with natural-language generation. In Kiln it is a deterministic compiler for explicit OpenSCAD code.

## Backend hierarchy for Kiln

1. `generate_original_design(provider="auto")`
   Use this first for original part creation. It builds a design-aware prompt, generates a candidate, audits it, and retries with feedback when needed.
2. `gemini`
   Best default for functional parts. Kiln's Gemini path reasons into OpenSCAD and compiles locally, which is much closer to "idea -> blueprint" than raw mesh synthesis.
3. `meshy`, `tripo3d`, `stability`
   Useful for ideation, rough concepts, and more organic forms. Assume harsher post-generation audit and more geometry drift.
4. `openscad`
   Use only when the agent already has explicit OpenSCAD code or a very clear parametric construction plan.

## Required design brief fields

Before generation, the agent should pin down as many of these as possible:

- function: what the part must do
- load: static load, impact, torque, vibration, or none
- environment: indoor, outdoor, hot car, wet, UV, chemicals
- fit: exact mating dimensions, hole spacing, keep-out zones, assembly clearances
- material goal: fixed material or performance target
- printer target: model, build volume, nozzle assumptions
- print orientation constraints: what must face down, stay flat, or avoid support scars
- surface priorities: cosmetic finish, grip texture, watertightness, tolerance-critical faces
- assembly strategy: one piece, glued assembly, screws, snap-fit, captured hardware

If any of these are unknown, the agent should state the assumption explicitly.

## Hard geometry rules for FDM

- Design for the nozzle. UltiMaker's FFF guide recommends minimum wall thickness at least as large as the nozzle diameter. For functional parts, default to thicker walls unless there is a reason not to.
- Design a generous first layer. Large bottom contact improves adhesion and reduces distortion risk. Bottom-edge chamfers are safer than sharp corners.
- Respect overhang limits. A common FFF rule is that overhangs more severe than about 45 degrees from vertical need support. Bridges should be kept short.
- Treat orientation as part of the design. UltiMaker and Prusa both emphasize that load direction and print orientation materially change strength because FDM parts are anisotropic.
- Use fillets or gussets in stress-prone regions. This reduces stress concentration and improves print reliability.
- Split parts when geometry fights the printer. Modular designs beat support-heavy hero parts when build volume, strength direction, or finish quality are in conflict.
- Validate tolerance with test coupons when fit matters. Plastic shrink, material choice, and slicer settings change final dimensions.

## Bambu Lab A1 quick constraints

Source-backed defaults for Kiln's A1-centered home workflow:

- build volume: `256 x 256 x 256 mm`
- included nozzle: `0.4 mm`
- optional nozzles: `0.2 mm`, `0.6 mm`, `0.8 mm`
- ideal materials: `PLA`, `PETG`, `TPU`, `PVA`
- not recommended on this profile: `ABS`, `ASA`, `PC`, `PA`, `PET`, carbon/glass-filled polymers`

Implications for agents:

- assume a `0.4 mm` nozzle unless the user says otherwise
- bias material recommendations toward `PLA`, `PETG`, or `TPU`
- do not recommend heat- or enclosure-demanding materials on the A1 without an explicit override
- keep "printer-aware" prompts and audits inside the `256 mm` cube

## Kiln workflow for original creation

1. Call `get_design_brief` when the task is ambiguous or functional.
2. Call `build_generation_prompt` to ground the idea in material, printer, and printability constraints.
3. Prefer `generate_original_design` for end-to-end creation.
4. If the result is not ready, inspect `feedback`, `next_actions`, and `next_prompt_suggestion`.
5. Re-run with the improved prompt or switch to a more suitable backend.
6. Use `audit_original_design`, `analyze_printability`, and `auto_orient_model` for manual review before slicing.

## Prompting rules that produce better parts

- State function before form.
- Use millimeters and concrete dimensions.
- Name the load path and the surfaces that matter.
- Explicitly request a flat bottom, single solid body, watertight geometry, and no floating islands for functional parts.
- Call out print-critical geometry: holes, slots, clips, mating faces, screw bosses, cable paths, lid interfaces.
- Ask for modular assemblies when one-piece geometry would force poor orientation or excessive support.
- Separate "must-haves" from "nice-to-haves" so the backend can sacrifice aesthetics before function.

## Backend-specific guidance

### Gemini / code-driven CAD

- Favor simple constructive geometry: primitives, booleans, extrusions, and reusable modules.
- Prefer explicit parameters and named dimensions over sculptural descriptions.
- Ask for clean origin placement and predictable part orientation.
- Great fit for brackets, fixtures, enclosures, adapters, mounts, organizers, and jigs.

### Mesh generators

- Be unusually explicit about printability because these systems will happily create thin fins, floating fragments, and decorative nonsense.
- Ask for a broad flat base, thick structural members, minimal unsupported spans, and a single connected body.
- Expect to reject more candidates.

## Red flags

- beautiful preview, terrible engineering
- thin walls on load-bearing parts
- low bed contact area
- large unsupported ceilings or bridges
- material recommendation that the chosen printer profile does not support
- orientation-sensitive parts designed without any orientation discussion
- natural-language prompts sent directly to `openscad`

## Output contract for agents

When an agent says a design is ready, it should be able to state:

- chosen material and why
- target printer and nozzle assumption
- overall dimensions
- preferred print orientation
- readiness score and blockers
- any assumptions still unresolved

## Sources

- OpenSCAD documentation: <https://openscad.org/documentation>
- Bambu Lab A1 technical specification: <https://store.bblcdn.com/8137fad1525a4454ac8a28502edbc919.pdf>
- Prusa knowledge base, "Modeling with 3D printing in mind": <https://help.prusa3d.com/article/modeling-with-3d-printing-in-mind_164135>
- UltiMaker, "How to design for FFF 3D printing": <https://ultimaker.com/wp-content/uploads/2024/06/How-to-design-for-FFF-1.pdf>
