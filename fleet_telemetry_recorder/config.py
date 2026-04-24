"""Default paths and tunables. Override via environment variables."""

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


# Klippy webhook unix socket. Default matches Kiauh/moonraker-deploy layouts.
KLIPPY_SOCKET = _env(
    "FTR_KLIPPY_SOCKET",
    str(Path.home() / "printer_data" / "comms" / "klippy.sock"),
)

# Where per-print telemetry files land. Must be under ~/printer_data/logs
# so Moonraker serves them via /server/files/logs/telemetry/... without any
# Moonraker config changes.
LOG_DIR = Path(_env(
    "FTR_LOG_DIR",
    str(Path.home() / "printer_data" / "logs" / "telemetry"),
))

# Local Moonraker HTTP endpoint. Used to resolve moonraker_job_id after a
# print ends (via /server/history/list).
MOONRAKER_URL = _env("FTR_MOONRAKER_URL", "http://127.0.0.1:7125")

# How long to keep local telemetry files before pruning. fleet_daemon pulls
# them within minutes normally; this is the runway for daemon outages.
PRUNE_DAYS = int(_env("FTR_PRUNE_DAYS", "60"))

# Whether to subscribe to motion_report/dump_trapq (per-segment motion log).
# Set FTR_INCLUDE_TRAPQ=0 to record only the baseline object-subscribe stream.
INCLUDE_TRAPQ = _env("FTR_INCLUDE_TRAPQ", "1") not in ("0", "false", "False", "no")

# Baseline object subscription. Each key is a Klippy object name (or prefix
# with "*" for wildcards), value None means "all fields".
BASELINE_OBJECT_PREFIXES = [
    "toolhead",
    "motion_report",
    "print_stats",
    "virtual_sdcard",
    "idle_timeout",
    "webhooks",
    "extruder",
    "extruder1",
    "heater_bed",
    "fan",
    # Wildcard families — expanded from objects/list at runtime
    "temperature_sensor ",
    "temperature_fan ",
    "heater_fan ",
    "controller_fan ",
]

# How often to prune old files (seconds).
PRUNE_INTERVAL_SECS = 24 * 3600

# Small cleanup-HTTP server: fleet_daemon POSTs DELETE here after a file is
# safely on NAS, so local copies are purged promptly (instead of waiting for
# the 60-day prune). Set FTR_CLEANUP_PORT=0 to disable.
CLEANUP_HOST = _env("FTR_CLEANUP_HOST", "0.0.0.0")
CLEANUP_PORT = int(_env("FTR_CLEANUP_PORT", "7130"))
