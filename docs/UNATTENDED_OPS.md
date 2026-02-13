# Unattended Operations Runbook

Practical guide for running a 3D print farm overnight or while away. Not aspirational — this is what you actually need to do.

---

## Pre-Flight Checklist

Run through this before leaving printers unattended:

| # | Check | Why |
|---|-------|-----|
| 1 | Filament level sufficient for all queued jobs | Runout detection pauses the print but wastes hours of idle time |
| 2 | Bed adhesion verified (clean bed, correct Z offset) | Detached prints are the #1 cause of overnight failures |
| 3 | Enclosure closed (if applicable) | Thermal stability, fire containment, reduces warping |
| 4 | Smoke detector within range | Non-negotiable. Thermal runaway is rare but catastrophic |
| 5 | Camera feed accessible remotely | You need eyes on the farm. Verify stream is live before leaving |
| 6 | Webhook alerts configured and tested | Send a test alert: `kiln webhook test --url <your-url>` |
| 7 | Print queue reviewed | Confirm job order, priorities, and printer assignments |
| 8 | Ambient temperature stable | HVAC off-cycles can cause warping on long prints |
| 9 | Firmware power-loss recovery enabled | M413 S1 on Marlin-based printers; built-in on Bambu |
| 10 | `kiln audit-log tail` shows no recent errors | Don't leave with unresolved warnings in the log |

---

## Automated Recovery Matrix

What Kiln does when things go wrong — no human needed.

| Failure | Detection | Automated Response | Human Action Needed? |
|---------|-----------|-------------------|---------------------|
| **Filament runout** | Printer firmware sensor | Pause print, hold queue, fire webhook alert | Reload filament and resume |
| **Network disconnect** | Heartbeat timeout (30s) | Serial adapter auto-reconnect (3 retries, exponential backoff). Queue holds new jobs until connection restored | Only if reconnect fails after all retries |
| **Print detachment** | Temperature anomaly (bed temp drop without target change) | Pause print, fire alert with thermal data snapshot | Inspect and decide: resume or cancel |
| **Power loss** | Printer firmware (M413) | Firmware saves position to EEPROM. On power restore, Kiln detects resumed state and re-syncs job tracking | Confirm print quality at resume point |
| **Thermal runaway** | Printer firmware (hardware-level) | Firmware kills heaters immediately. Kiln logs the event, marks job as failed, fires critical alert | Inspect printer hardware before restarting |
| **Printer goes offline** | Adapter health check fails | Job rerouted to next available printer with matching capabilities (`failure_rerouter.py`). Original printer marked degraded | Investigate offline printer when convenient |
| **Slicer temp exceeds safety limit** | Pre-flight G-code validation | Job rejected before submission. Alert with explanation | Fix slicer profile |
| **Queue stall (no healthy printers)** | Scheduler detects zero available printers | Queue pauses globally, fires alert | Bring at least one printer online |

---

## Alert Configuration

Kiln delivers alerts via webhooks. Wire them to your preferred notification channel.

### Setup

```bash
# Add a webhook endpoint
kiln webhook add --url https://hooks.slack.com/services/T.../B.../xxx --events print.failed,printer.offline,alert.critical

# Add SMS via Twilio (or any HTTP-to-SMS bridge)
kiln webhook add --url https://your-twilio-bridge.com/sms --events alert.critical

# Test delivery
kiln webhook test --url https://hooks.slack.com/services/T.../B.../xxx
```

### Event Types

| Event | Severity | When |
|-------|----------|------|
| `print.completed` | Info | Job finished successfully |
| `print.failed` | Warning | Job failed (detachment, runout, error) |
| `print.paused` | Warning | Job paused (manual or automated) |
| `printer.offline` | Warning | Printer unreachable after retries |
| `printer.error` | Critical | Printer in error state (thermal, mechanical) |
| `alert.critical` | Critical | Thermal runaway, queue stall, repeated failures |
| `queue.stalled` | Warning | No healthy printers available for pending jobs |

All webhooks include HMAC signatures for verification. See `kiln webhook --help` for full options.

---

## Morning-After Checklist

When you return to the farm:

1. **Review the audit log**: `kiln audit-log tail --since 12h` — scan for warnings or errors
2. **Check print results**: Visually inspect completed prints for quality issues (layer shifts, stringing, adhesion problems)
3. **Verify filament levels**: Enough remaining for the next batch of jobs?
4. **Review rerouted jobs**: If any jobs were rerouted, check the original printer's status
5. **Clear completed jobs**: `kiln queue clear --status completed` to clean the queue
6. **Check camera footage**: If any prints failed, review timelapse for root cause
7. **Update safety profiles**: If a printer showed new failure modes, update its safety profile

---

## Escalation Paths

When automated recovery is not enough:

### Tier 1: Automated (no human)
- Filament runout pause
- Network reconnect
- Job reroute to healthy printer

### Tier 2: Remote intervention (phone/laptop)
- Resume paused print after filament reload (if someone is on-site)
- Cancel and re-queue a failed job
- Manually mark a printer as offline: `kiln printer disable <id>`

### Tier 3: On-site required
- Thermal runaway investigation (do NOT restart without physical inspection)
- Print detachment cleanup (remove failed print, clean bed)
- Mechanical failure (stepper skip, belt slip, nozzle clog)
- Power supply issues

### Decision Tree

```
Failure detected
  |
  +-- Automated recovery succeeded? --> Done (logged)
  |
  +-- No --> Alert fired
        |
        +-- Can resolve remotely? --> Tier 2 (cancel/resume/disable)
        |
        +-- No --> Tier 3 (on-site visit required)
              |
              +-- Is it thermal/electrical? --> Power off remotely first,
                                                then inspect on-site
```

### Key Rule

**When in doubt, pause everything.** A paused print farm wastes time. An unmonitored failing printer wastes filament, damages hardware, or starts fires. Time is cheap; hardware is not.
