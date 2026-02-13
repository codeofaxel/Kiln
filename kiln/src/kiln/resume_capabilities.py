"""Resume capability tracking for each printer adapter type.

Provides structured data about what recovery and resume operations each
printer backend supports, enabling the scheduler and failure-rerouter to
make informed decisions about unattended operation viability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResumeCapability:
    """Describes what resume/recovery operations an adapter supports.

    :param adapter_type: Adapter identifier (e.g. ``"octoprint"``, ``"bambu"``).
    :param supports_pause_resume: Can pause and resume a running print.
    :param supports_firmware_recovery: Supports M413 power-loss recovery.
    :param supports_z_offset_resume: Can resume from a specific Z height.
    :param supports_layer_resume: Can resume from a specific layer number.
    :param supports_filament_change: Supports M600 mid-print filament change.
    :param recovery_methods: Ordered list of recovery methods to attempt.
    :param limitations: Known limitations or caveats for this adapter.
    """

    adapter_type: str
    supports_pause_resume: bool
    supports_firmware_recovery: bool
    supports_z_offset_resume: bool
    supports_layer_resume: bool
    supports_filament_change: bool
    recovery_methods: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "adapter_type": self.adapter_type,
            "supports_pause_resume": self.supports_pause_resume,
            "supports_firmware_recovery": self.supports_firmware_recovery,
            "supports_z_offset_resume": self.supports_z_offset_resume,
            "supports_layer_resume": self.supports_layer_resume,
            "supports_filament_change": self.supports_filament_change,
            "recovery_methods": list(self.recovery_methods),
            "limitations": list(self.limitations),
        }


# ---------------------------------------------------------------------------
# Recovery plan templates keyed by failure type
# ---------------------------------------------------------------------------

_RECOVERY_PLANS: Dict[str, Dict[str, List[str]]] = {
    "filament_runout": {
        "octoprint": [
            "Pause print via OctoPrint API",
            "Fire webhook alert with job context",
            "Wait for filament reload",
            "Send M600 filament change if supported",
            "Resume print",
        ],
        "moonraker": [
            "Pause print via Moonraker API",
            "Fire webhook alert with job context",
            "Wait for filament reload",
            "Send M600 filament change",
            "Resume print",
        ],
        "bambu": [
            "AMS detects runout automatically",
            "AMS switches to next spool if available",
            "Fire webhook alert if no backup spool",
            "Pause print and wait for reload",
            "Resume print",
        ],
        "prusa_connect": [
            "MMU detects runout",
            "Pause print via Prusa Connect API",
            "Fire webhook alert with job context",
            "Wait for filament reload via MMU",
            "Resume print",
        ],
        "serial": [
            "Detect M600 filament change request from firmware",
            "Pause G-code stream",
            "Fire webhook alert with job context",
            "Wait for filament reload confirmation",
            "Resume G-code stream",
        ],
    },
    "power_loss": {
        "octoprint": [
            "Firmware saves position via M413 to EEPROM",
            "Wait for power restore",
            "OctoPrint reconnects to printer",
            "Firmware prompts for power-loss resume",
            "Kiln re-syncs job tracking state",
        ],
        "moonraker": [
            "Firmware saves position if M413 enabled",
            "Wait for power restore",
            "Moonraker reconnects to printer",
            "Check firmware for power-loss resume prompt",
            "Re-sync job state (may require manual confirmation)",
        ],
        "bambu": [
            "Built-in power-loss recovery saves state to internal storage",
            "Wait for power restore",
            "Printer auto-resumes from saved state",
            "Kiln re-syncs job tracking on reconnect",
        ],
        "prusa_connect": [
            "Prusa firmware saves position via M413 to EEPROM",
            "Wait for power restore",
            "Prusa Connect reconnects",
            "Firmware resumes from saved position",
            "Kiln re-syncs job tracking state",
        ],
        "serial": [
            "Firmware saves position via M413 to EEPROM",
            "Wait for power restore",
            "Serial adapter reconnects",
            "Send M413 resume command",
            "Re-sync G-code stream position",
        ],
    },
    "network_disconnect": {
        "octoprint": [
            "Detect heartbeat timeout",
            "Attempt reconnect with exponential backoff (3 retries)",
            "Hold queue — do not assign new jobs",
            "Fire webhook alert if reconnect fails",
            "Mark printer as degraded after max retries",
        ],
        "moonraker": [
            "Detect WebSocket disconnect",
            "Attempt reconnect with exponential backoff (3 retries)",
            "Hold queue — do not assign new jobs",
            "Fire webhook alert if reconnect fails",
            "Mark printer as degraded after max retries",
        ],
        "bambu": [
            "Detect MQTT disconnect",
            "Attempt MQTT reconnect with exponential backoff",
            "Print continues on printer (autonomous operation)",
            "Re-sync state on reconnect",
            "Fire webhook alert if reconnect fails",
        ],
        "prusa_connect": [
            "Detect API heartbeat timeout",
            "Attempt reconnect with exponential backoff (3 retries)",
            "Print continues on printer (autonomous operation)",
            "Re-sync state on reconnect",
            "Fire webhook alert if reconnect fails",
        ],
        "serial": [
            "Detect serial port disconnect",
            "Attempt serial reconnect (port re-enumeration)",
            "Print is lost — serial has no autonomous operation",
            "Fire webhook alert immediately",
            "Mark job as failed, re-queue if possible",
        ],
    },
    "thermal_runaway": {
        "octoprint": [
            "Firmware kills heaters immediately (hardware-level)",
            "OctoPrint reports error state",
            "Kiln logs critical event",
            "Fire critical webhook alert",
            "Mark printer as error — requires physical inspection",
        ],
        "moonraker": [
            "Firmware kills heaters immediately (hardware-level)",
            "Moonraker reports error state",
            "Kiln logs critical event",
            "Fire critical webhook alert",
            "Mark printer as error — requires physical inspection",
        ],
        "bambu": [
            "Built-in thermal protection kills heaters",
            "Printer reports error via MQTT",
            "Kiln logs critical event",
            "Fire critical webhook alert",
            "Mark printer as error — requires physical inspection",
        ],
        "prusa_connect": [
            "Prusa firmware thermal protection kills heaters",
            "Prusa Connect reports error state",
            "Kiln logs critical event",
            "Fire critical webhook alert",
            "Mark printer as error — requires physical inspection",
        ],
        "serial": [
            "Firmware kills heaters immediately (hardware-level)",
            "Serial adapter detects error response",
            "Kiln logs critical event",
            "Fire critical webhook alert",
            "Mark printer as error — requires physical inspection",
        ],
    },
    "print_detachment": {
        "octoprint": [
            "Detect temperature anomaly (bed temp drop without target change)",
            "Pause print via OctoPrint API",
            "Fire webhook alert with thermal data snapshot",
            "Wait for human inspection decision",
        ],
        "moonraker": [
            "Detect temperature anomaly (bed temp drop without target change)",
            "Pause print via Moonraker API",
            "Fire webhook alert with thermal data snapshot",
            "Wait for human inspection decision",
        ],
        "bambu": [
            "Bambu lidar/camera detection (if equipped)",
            "Pause print",
            "Fire webhook alert",
            "Wait for human inspection decision",
        ],
        "prusa_connect": [
            "Detect temperature anomaly (bed temp drop without target change)",
            "Pause print via Prusa Connect API",
            "Fire webhook alert with thermal data snapshot",
            "Wait for human inspection decision",
        ],
        "serial": [
            "Detect temperature anomaly (bed temp drop without target change)",
            "Pause G-code stream",
            "Fire webhook alert with thermal data snapshot",
            "Wait for human inspection decision",
        ],
    },
}

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_KNOWN_ADAPTERS = ("octoprint", "moonraker", "bambu", "prusa_connect", "serial")


class ResumeCapabilityRegistry:
    """Pre-populated registry of resume/recovery capabilities per adapter."""

    def __init__(self) -> None:
        self._capabilities: Dict[str, ResumeCapability] = {
            "octoprint": ResumeCapability(
                adapter_type="octoprint",
                supports_pause_resume=True,
                supports_firmware_recovery=True,
                supports_z_offset_resume=True,
                supports_layer_resume=False,
                supports_filament_change=True,
                recovery_methods=[
                    "pause_resume",
                    "m413_power_loss",
                    "z_offset_resume",
                    "m600_filament_change",
                    "network_reconnect",
                ],
                limitations=[
                    "Layer resume requires re-slicing from the target layer",
                    "Power-loss recovery depends on M413 support in firmware",
                    "Network disconnect does not halt an in-progress print on the printer",
                ],
            ),
            "moonraker": ResumeCapability(
                adapter_type="moonraker",
                supports_pause_resume=True,
                supports_firmware_recovery=False,
                supports_z_offset_resume=True,
                supports_layer_resume=False,
                supports_filament_change=True,
                recovery_methods=[
                    "pause_resume",
                    "z_offset_resume",
                    "m600_filament_change",
                    "network_reconnect",
                ],
                limitations=[
                    "Firmware recovery depends on underlying firmware (Marlin vs Klipper)",
                    "Klipper does not natively support M413 power-loss recovery",
                    "Layer resume requires re-slicing from the target layer",
                ],
            ),
            "bambu": ResumeCapability(
                adapter_type="bambu",
                supports_pause_resume=True,
                supports_firmware_recovery=True,
                supports_z_offset_resume=False,
                supports_layer_resume=True,
                supports_filament_change=True,
                recovery_methods=[
                    "pause_resume",
                    "built_in_power_loss",
                    "layer_resume",
                    "ams_filament_change",
                    "mqtt_reconnect",
                ],
                limitations=[
                    "Z offset resume not available (proprietary firmware)",
                    "Recovery features locked to Bambu Lab ecosystem",
                    "MQTT protocol required — no REST fallback",
                    "Print continues autonomously during network outage",
                ],
            ),
            "prusa_connect": ResumeCapability(
                adapter_type="prusa_connect",
                supports_pause_resume=True,
                supports_firmware_recovery=True,
                supports_z_offset_resume=True,
                supports_layer_resume=False,
                supports_filament_change=True,
                recovery_methods=[
                    "pause_resume",
                    "m413_power_loss",
                    "z_offset_resume",
                    "mmu_filament_change",
                    "network_reconnect",
                ],
                limitations=[
                    "Layer resume requires re-slicing from the target layer",
                    "MMU filament change requires MMU hardware",
                    "Print continues autonomously during network outage",
                ],
            ),
            "serial": ResumeCapability(
                adapter_type="serial",
                supports_pause_resume=True,
                supports_firmware_recovery=True,
                supports_z_offset_resume=True,
                supports_layer_resume=False,
                supports_filament_change=True,
                recovery_methods=[
                    "pause_resume",
                    "m413_power_loss",
                    "z_offset_resume",
                    "m600_filament_change",
                ],
                limitations=[
                    "No autonomous operation — serial disconnect kills the print",
                    "Layer resume requires re-slicing from the target layer",
                    "Recovery limited by host-side G-code streaming model",
                    "No network reconnect — serial is a direct connection",
                ],
            ),
        }

    def get_capabilities(self, adapter_type: str) -> Optional[ResumeCapability]:
        """Return capabilities for an adapter, or ``None`` if unknown.

        :param adapter_type: Adapter identifier (e.g. ``"octoprint"``).
        """
        return self._capabilities.get(adapter_type)

    def get_recovery_plan(
        self, adapter_type: str, *, failure_type: str
    ) -> List[str]:
        """Return ordered recovery steps for a failure on a given adapter.

        :param adapter_type: Adapter identifier.
        :param failure_type: One of ``"filament_runout"``, ``"power_loss"``,
            ``"network_disconnect"``, ``"thermal_runaway"``, ``"print_detachment"``.
        :returns: Ordered list of recovery step descriptions. Empty list if
            the adapter or failure type is unknown.
        """
        failure_plans = _RECOVERY_PLANS.get(failure_type, {})
        return list(failure_plans.get(adapter_type, []))

    def supports_unattended(self, adapter_type: str) -> bool:
        """Check if an adapter supports enough recovery for unattended operation.

        An adapter qualifies for unattended operation if it supports:
        - pause/resume (to halt on detected failures)
        - firmware recovery (to survive power loss)
        - filament change (to handle runout)

        :param adapter_type: Adapter identifier.
        :returns: ``True`` if the adapter meets the minimum bar, ``False``
            otherwise (including for unknown adapters).
        """
        cap = self._capabilities.get(adapter_type)
        if cap is None:
            return False
        return (
            cap.supports_pause_resume
            and cap.supports_firmware_recovery
            and cap.supports_filament_change
        )
