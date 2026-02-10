# octoprint-cli

Agent-friendly command-line interface for [OctoPrint](https://octoprint.org/) 3D printer management.

Designed for autonomous AI agent interaction with structured JSON outputs, clear exit codes, and safety guards for unattended operation.

## Installation

```bash
pip install .
```

Or in development mode:

```bash
pip install -e ".[dev]"
```

## Quick Start

### 1. Initialize configuration

```bash
octoprint-cli init
# Prompts for host URL and API key, saves to ~/.octoprint-cli/config.yaml
```

### 2. Or use environment variables

```bash
export OCTOPRINT_HOST="http://octopi.local"
export OCTOPRINT_API_KEY="your_api_key_here"
```

### 3. Check printer status

```bash
octoprint-cli status --json
```

## Commands

| Command | Description |
|---------|-------------|
| `status` | Get printer state, temperatures, and job progress |
| `files` | List available G-code files on the printer |
| `upload <file>` | Upload a G-code file |
| `print <file>` | Upload and start printing (requires `--confirm`) |
| `cancel` | Cancel current print (requires `--confirm`) |
| `pause` | Pause current print |
| `resume` | Resume paused print |
| `preflight [file]` | Run pre-flight safety checks |
| `temp` | Get or set temperatures |
| `gcode <cmds>` | Send raw G-code commands |
| `connect` | Connect printer to OctoPrint |
| `disconnect` | Disconnect printer from OctoPrint |
| `init` | Create configuration file |

## Agent-Friendly Design

### Structured JSON Output

Every command supports `--json` for machine-parseable output:

```bash
octoprint-cli status --json
```

```json
{
  "status": "success",
  "data": {
    "state": "Operational",
    "temperature": {
      "tool0": {"actual": 22.5, "target": 0.0},
      "bed": {"actual": 21.8, "target": 0.0}
    },
    "job": {
      "file": null,
      "completion": null,
      "print_time_left": null
    }
  }
}
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Printer offline / unreachable |
| `2` | File error (not found, invalid format) |
| `3` | Printer busy (already printing) |
| `4` | Other error (auth, server, validation) |

### Safety Flags

Destructive operations require explicit confirmation:

```bash
# Will fail without --confirm
octoprint-cli print model.gcode --confirm --json

# Cancel with confirmation
octoprint-cli cancel --confirm --json
```

### Idempotent Operations

```bash
# Exits 0 if already printing instead of erroring
octoprint-cli print model.gcode --confirm --skip-if-printing --json
```

## Configuration

Configuration is resolved with this precedence (highest first):

1. CLI flags (`--host`, `--api-key`)
2. Environment variables (`OCTOPRINT_HOST`, `OCTOPRINT_API_KEY`)
3. Config file (`~/.octoprint-cli/config.yaml`)

### Config File Format

```yaml
host: "http://octopi.local"
api_key: "YOUR_API_KEY_HERE"
timeout: 30
retries: 3
```

## Agent Workflow Example

A typical autonomous print workflow:

```bash
# Step 1: Verify printer is ready
octoprint-cli preflight ./model.gcode --json
# Parse JSON, check "ready": true

# Step 2: Upload and print
octoprint-cli print ./model.gcode --confirm --json
# Parse JSON, check "status": "success"

# Step 3: Monitor progress (poll periodically)
octoprint-cli status --json
# Parse JSON, read data.job.completion and data.job.print_time_left

# Step 4: Handle completion or errors based on exit codes
# Exit 0 = success, 1 = offline, 2 = file error, 3 = busy, 4 = other
```

### Error Handling Example

```bash
octoprint-cli status --json
echo "Exit code: $?"
```

```json
{
  "status": "error",
  "data": null,
  "error": {
    "code": "CONNECTION_ERROR",
    "message": "Could not connect to OctoPrint at http://octopi.local"
  }
}
```

Exit code: `1` (printer offline)

## Pre-flight Checks

The `preflight` command (and automatic checks before `print`) validates:

- Printer is connected and operational
- Printer is not already printing
- No printer errors detected
- Temperatures are within safe limits
- File exists, has valid extension, reasonable size

```bash
octoprint-cli preflight ./model.gcode --json
```

## Temperature Control

```bash
# View current temperatures
octoprint-cli temp --json

# Set hotend to 200C and bed to 60C
octoprint-cli temp --tool 200 --bed 60 --json

# Turn off all heaters
octoprint-cli temp --off --json
```

## Raw G-code

```bash
# Home all axes
octoprint-cli gcode G28 --json

# Multiple commands
octoprint-cli gcode G28 "M104 S200" "M140 S60" --json
```

## Requirements

- Python 3.8+
- OctoPrint server with API access enabled
- Dependencies: click, requests, pyyaml, rich

## License

MIT
