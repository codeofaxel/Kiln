# Patent-Bundle Smoke-Print Runbook

**Status:** Manual runbook.  NOT a CI test — needs a live FDM printer.

**Goal:** Validate the patent-coverage bundle (KILN-003 + KILN-010
wiring) on real hardware before quoting it as production-ready.  All
the unit tests prove the code paths fire; this runbook proves they
fire on the right signals at the right times.

**Time:** ~30 minutes including a small Benchy print + cancel + recovery.

**Prereq printer:** any registered Kiln printer.  The default
playbook assumes the "default" Bambu A1 — adapt as needed.  Bambu A1
needs Dev Mode ON (per `MEMORY.md`).

**Prereq software:** kiln-pro installed (otherwise the predictive +
rerouter + outcome paths silently no-op and there's nothing to
validate).

---

## Pre-flight

```bash
# Verify the new tools are reachable.  All three must succeed.
kiln tool predict_print_failure_risk --help
kiln tool evaluate_fleet_reroute --help
kiln tool complete_print_recovery --help

# Verify manifest is current — should report 281 pro tools.
python3 -m kiln_pro.generate_manifest 2>&1 | grep "Generated"
# Expect: "Generated 281 pro tool entries"
```

---

## 1. Predictive amber/red on real telemetry (~5 min)

Goal: verify `predict_risk` doesn't false-positive on a healthy print.

```bash
# Start a small Benchy.  Don't watch — just kick it off.
kiln print start --file benchy.stl --material pla

# Wait ~3 minutes for the print to lay down 6+ telemetry samples.
# (predict_risk needs 6 to compute trends; below that it returns
# severity=clear with the "insufficient_history" signal.)
sleep 180

# Score risk against the live telemetry stream.
kiln tool predict_print_failure_risk \
  --telemetry "$(kiln tool printer_status --json)" \
  --telemetry-history "$(kiln tool monitor_print --history --json)"
```

**Expected:**
- `severity == "clear"` on a healthy print
- `signals == []` OR a single `kind=insufficient_history` signal early on

**Red flag:**
- Any `severity == "amber"` or `severity == "red"` on a known-healthy
  print = false positive in the heuristics. Capture the telemetry +
  signal kind and tune the threshold.

---

## 2. Real failure detection + recovery walk-through (~15 min)

Goal: verify the full pipeline (detect → plan → start → monitor → complete)
runs end-to-end on real signals AND records the outcome.

```bash
# While the Benchy is mid-print:
# (a) Snapshot the current telemetry to a known-good state.
kiln tool printer_status --json > /tmp/pre_cancel_state.json

# (b) Cancel the print to simulate a controlled failure.
kiln tool cancel_print

# (c) Manually feed a "thermal runaway" telemetry to detect_failure.
#     This is a CONTROLLED test — we're NOT actually overheating the
#     printer; we're feeding the engine a synthetic snapshot to walk
#     the pipeline without putting hardware at risk.
FAILURE_ID=$(kiln tool detect_print_failure \
  --printer-name default \
  --telemetry '{"hotend_temp":320,"hotend_target":220,"bed_temp":60,"bed_target":60,"connected":true}' \
  --json | jq -r '.failure.failure_id')

echo "Detected failure_id=$FAILURE_ID"

# (d) Plan recovery.
PLAN_ID=$(kiln tool plan_failure_recovery \
  --failure-id "$FAILURE_ID" \
  --json | jq -r '.plan.plan_id')

# (e) Start session.
SESSION_ID=$(kiln tool start_print_recovery \
  --plan-id "$PLAN_ID" --failure-id "$FAILURE_ID" \
  --json | jq -r '.session.session_id')

# (f) Try to complete EARLY (no checks recorded).  Should fail with
#     MONITORING_THRESHOLD_NOT_MET (claim 79).
kiln tool complete_print_recovery \
  --session-id "$SESSION_ID" --success true 2>&1 | tee /tmp/early_complete.json
# Expect: error.code == "MONITORING_THRESHOLD_NOT_MET"
#         error.deficit == 5 (critical severity)

# (g) Record 5 passing monitoring checks.
for i in 1 2 3 4 5; do
  kiln tool record_recovery_check --session-id "$SESSION_ID" --passed true
done

# (h) Complete WITH the new fleet-recommendation arg.
kiln tool complete_print_recovery \
  --session-id "$SESSION_ID" \
  --success true \
  --notes "smoke print walkthrough" \
  --json | tee /tmp/complete_result.json

# Verify expected fields landed in the response:
jq '.pro_outcome_recorded' /tmp/complete_result.json   # expect: true
jq '.session.status' /tmp/complete_result.json         # expect: "completed"
```

**Red flag:**
- `pro_outcome_recorded` is `false` or absent → the kiln-pro outcome
  side path is broken again.  Re-check the `session.failure` fix at
  `recovery_tools.py:920`.
- The early-complete call returns `success: true` instead of the
  threshold error → claim 79 wiring is broken.

---

## 3. Reroute recommendation on failed recovery (~5 min)

Goal: verify the rerouter actually returns a useful recommendation
when fed a real failure on a fake fleet.

```bash
# Use the failure_id from step 2.  Call evaluate_fleet_reroute
# directly with two fake alternatives.
kiln tool evaluate_fleet_reroute \
  --failure-id "$FAILURE_ID" \
  --completion-pct 0.45 \
  --material pla \
  --alternative-printers '[
    {"printer_id":"voron-2","is_idle":true,"supported_materials":["pla","petg"],"success_rate":0.92},
    {"printer_id":"prusa-mk4","is_idle":true,"supported_materials":["pla"],"success_rate":0.85}
  ]' \
  --json
```

**Expected** for a `thermal_runaway` failure:
- `decision.should_reroute == false`
- `decision.blocked_by_rule == "safety_critical"`
- `decision.reason` mentions physical inspection

(Patent claim 23: thermal_runaway must NEVER auto-reroute.)

Repeat with a non-safety-critical failure_id (you'll need to detect a
new failure first using e.g. layer-shift telemetry):

```bash
LAYER_SHIFT_ID=$(kiln tool detect_print_failure \
  --printer-name default \
  --telemetry '{"hotend_temp":220,"hotend_target":220,"x_position":100,"x_expected":100.5,"y_position":80,"y_expected":82,"connected":true}' \
  --json | jq -r '.failure.failure_id')

kiln tool evaluate_fleet_reroute \
  --failure-id "$LAYER_SHIFT_ID" \
  --completion-pct 0.45 \
  --material pla \
  --alternative-printers '[
    {"printer_id":"voron-2","is_idle":true,"supported_materials":["pla"],"success_rate":0.92}
  ]' \
  --json
```

**Expected**:
- `decision.should_reroute == true`
- `decision.target_printer_id == "voron-2"`
- `decision.blocked_by_rule == null`

---

## 4. Sanity gate refusal on real prompt (~5 min)

Goal: verify the gate refuses contradictory prompts and lets healthy
ones through.

```bash
# (a) Healthy prompt — should pass through with a well-formed
#     improved prompt and sanity.passed == true.
kiln tool improve_generation_prompt \
  --original-prompt "phone stand for iphone 16 pro max" \
  --failure-mode adhesion \
  --json | jq '.improved_prompt.sanity.passed'
# Expect: true

# (b) Contradictory prompt — should refuse with SANITY_GATE_FAILED.
#     We synthesize a contradictory feedback by passing both an
#     adhesion failure_mode AND a stringing failure_mode in
#     sequence with the analyze_generation_feedback tool.
#     (This is the simplest way to trigger contradicting constraints
#     without building a malformed prompt manually.)
kiln tool improve_generation_prompt \
  --original-prompt "rigid load-bearing flexible TPU phone stand" \
  --failure-mode adhesion \
  --json | jq '.error.code, .sanity.passed'
# Expect:
#   "SANITY_GATE_FAILED"
#   false

# (c) Same input with enforce_sanity=false — should return the prompt
#     anyway with the failure list attached.
kiln tool improve_generation_prompt \
  --original-prompt "rigid load-bearing flexible TPU phone stand" \
  --failure-mode adhesion \
  --enforce-sanity false \
  --json | jq '.success, .improved_prompt.sanity.passed'
# Expect:
#   true
#   false
```

**Red flag:**
- (b) returns `success: true` with the contradictory prompt → gate
  isn't gating.  Re-check `enforce_sanity=True` default at
  `recovery_tools.py:improve_generation_prompt`.
- (a) returns `SANITY_GATE_FAILED` on a clean prompt → false positive
  in the gate; tune `_MIN_TOKEN_OVERLAP_PCT` or the contradiction
  detectors.

---

## Sign-off checklist

Tick each before marking the bundle production-ready:

- [ ] Predictive returns `clear` on healthy 5-min print (no false positives)
- [ ] Predictive returns `red` on real thermal anomaly (manually
      heated nozzle 30°C above target — controlled test)
- [ ] Full pipeline runs to `status=completed` for a critical failure
      after 5 monitoring checks
- [ ] Early `complete_print_recovery` returns `MONITORING_THRESHOLD_NOT_MET`
      with the correct deficit
- [ ] `pro_outcome_recorded: true` in the completion response
- [ ] `~/.kiln/recovery_outcomes.json` actually contains the new entry
- [ ] Rerouter blocks safety-critical failure types
- [ ] Rerouter approves layer_shift / blob to a healthy alternative
- [ ] Sanity gate refuses contradictory prompts by default
- [ ] Sanity gate passes through clean prompts
- [ ] `enforce_sanity=false` returns the contradictory prompt for repair

If any line fails: capture the response payload, the printer model,
the kiln-pro version, and the kiln3d version into a comment on this
runbook.  Re-run the corresponding unit test to confirm the regression
is reproducible in CI before patching live.

---

## 5. Auto-pause smoke test

Validates that `report_issue` actually calls `pause_print()` when
auto_pause_triggered fires, and that the
`KILN_MONITOR_PAUSE_DISABLED` kill-switch behaves as a true
operator override.  Use the live Bambu A1 here — kill-switch
behavior is too easy to fake in unit tests.

### 5a. Auto-pause fires on a real thermal spike

```bash
# Set up a small print (a coaster works well — short and cheap to
# abandon) and start the health monitor.
kiln tool start_monitored_print --model-path /tmp/coaster.3mf
SESSION_ID=$(kiln tool start_printer_health_monitoring --json | jq -r '.session_id')

# Wait until the print is mid-infill (so a pause is meaningful),
# then artificially heat the nozzle 30°C above its current target
# to trip the temp_drift CRITICAL threshold.
kiln tool printer_status --json | jq '.tool_temp_actual, .tool_temp_target'
kiln tool send_gcode --command "M104 S270"

# Within ~2 monitoring intervals the monitor should:
#   1. log "Auto-pause triggered for session ..."
#   2. log "Auto-paused printer ... for issue health_critical"
#   3. show pause state on the printer
kiln tool printer_status --json | jq '.is_paused'
# Expect: true

# Pull the issue list to confirm auto_pause_triggered=True with no
# auto_pause_skipped / auto_pause_error fields.
kiln tool get_session_log --session-id "$SESSION_ID" --json \
  | jq '.issues[] | select(.issue_type == "health_critical")'
# Expect: auto_pause_triggered: true, no auto_pause_skipped, no auto_pause_error.
```

### 5b. Resume after restoring the temperature

```bash
# Restore the original target (replace 220 with whatever your slicer set).
kiln tool send_gcode --command "M104 S220"

# Wait for the hotend to settle back to target, then resume.
kiln tool resume_print
kiln tool printer_status --json | jq '.is_paused, .is_printing'
# Expect: false, true
```

**Red flag:**
- `pause_print()` was logged but `is_paused` is still `false` →
  adapter pause_print is no-op'ing.  Inspect the Bambu MQTT
  publish path (Dev Mode + access code).
- `auto_pause_triggered: true` with `auto_pause_error` set → the
  adapter rejected the pause; capture the error string into the
  comment on this runbook.

### 5c. Kill-switch disables auto-pause

```bash
# Restart the monitor with the kill-switch on.
kiln tool stop_printer_health_monitoring
KILN_MONITOR_PAUSE_DISABLED=true \
  kiln tool start_printer_health_monitoring --json

# Repeat the heat-spike from 5a.
kiln tool send_gcode --command "M104 S270"

# Within ~2 intervals the issue should be recorded but NO pause.
kiln tool printer_status --json | jq '.is_paused'
# Expect: false (printer keeps printing despite the spike)

kiln tool get_session_log --session-id "$SESSION_ID" --json \
  | jq '.issues[] | select(.issue_type == "health_critical") | .auto_pause_skipped'
# Expect: "kill_switch"

# Restore target before the print actually fails.
kiln tool send_gcode --command "M104 S220"
```

**Red flag:**
- `is_paused: true` while `KILN_MONITOR_PAUSE_DISABLED=true` →
  the kill-switch isn't being honored.  Inspect
  `_pause_kill_switch_enabled` in `print_health_monitor.py`.
- `auto_pause_skipped` is null/missing → the issue dict isn't
  being mutated; check the `_honor_auto_pause` mutation path.

### Sign-off additions

- [ ] Auto-pause: heat-spike trips `health_critical` and the printer
      actually pauses (5a)
- [ ] Auto-pause: `resume_print` returns to `is_printing: true`
      after restoring the target (5b)
- [ ] Kill-switch: `KILN_MONITOR_PAUSE_DISABLED=true` records the
      issue with `auto_pause_skipped: "kill_switch"` and does NOT
      pause the printer (5c)
