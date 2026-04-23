# fleet-telemetry-recorder

Per-print motion + temperature telemetry logger for Klipper-based printers.
Runs on each printer's Pi as a systemd service, writes one gzipped JSONL file
per print, and leaves it at `~/printer_data/logs/telemetry/` so the central
`fleet_daemon` can pull it via Moonraker's default `/server/files/logs/...`
endpoint.

This project is an independent sibling of `fleet_daemon/` and `Fleet_Mainsail/`.
It does **not** modify Klipper or Moonraker — it only connects to Klipper's
existing webhook unix socket (read-only subscriptions) and writes files into
Moonraker's default `logs` file root.

## What it records

- **Baseline** (from `objects/subscribe` on the webhook socket):
  toolhead position, motion_report live position/velocity, extruder + bed
  temperature/target/power, all `temperature_sensor` / `heater_fan` /
  `temperature_fan` / `controller_fan`, fan speed, `print_stats`
  (filename, state, print_duration, filament_used, current_layer).

- **Motion segments** (optional, on by default): per-trapq
  `motion_report/dump_trapq` — the trapezoid move planner output.
  Disable with `FTR_INCLUDE_TRAPQ=0` if Pi CPU is constrained.

## File lifecycle

1. On `print_stats.state` → `printing`: open
   `~/printer_data/logs/telemetry/.inprogress__{timestamp}__{stub}.jsonl.gz`
   and start appending events.
2. On `print_stats.state` → `complete|cancelled|error|standby`:
   close the file, query the local Moonraker for the resulting
   `moonraker_job_id`, rename to `{moonraker_job_id}__{stub}.jsonl.gz`.
3. Files older than `FTR_PRUNE_DAYS` (default 60) are deleted locally;
   fleet_daemon has already pulled them to NAS by then.

If the recorder crashes mid-print, on next start the leftover
`.inprogress__*` file is renamed to `crash__*.jsonl.gz` (it won't match any
job_id, so fleet_daemon eventually flags the corresponding row as
`unavailable`).

## Install

On each printer's Pi (tested with Klipper + Moonraker on Raspberry Pi OS):

```bash
git clone <this_repo>             # or rsync the fleet_telemetry_recorder/ dir
cd fleet_telemetry_recorder
./install.sh
```

The installer creates a venv at `~/fleet-telemetry-recorder-venv`, installs
the package, drops a systemd unit at
`/etc/systemd/system/fleet-telemetry-recorder.service`, and enables it.

Tail the log with:
```bash
journalctl -u fleet-telemetry-recorder.service -f
```

## Configuration

All settings are environment variables, set in the systemd unit or
`~/.config/environment.d/` (any standard method):

| Variable | Default | Purpose |
|---|---|---|
| `FTR_KLIPPY_SOCKET` | `~/printer_data/comms/klippy.sock` | Klippy webhook unix socket |
| `FTR_LOG_DIR` | `~/printer_data/logs/telemetry` | Where files land |
| `FTR_MOONRAKER_URL` | `http://127.0.0.1:7125` | For job_id resolution |
| `FTR_PRUNE_DAYS` | `60` | Local file retention |
| `FTR_INCLUDE_TRAPQ` | `1` | Record dump_trapq (0 disables) |

## Expected load

On a Raspberry Pi 4 during a typical print: ~1–3 % CPU, ~15 MB RSS,
5–150 KB/s disk write rate, ~5–30 MB gzipped per print.
Klipper's real-time paths are never touched; the service runs at `nice=10`.

## Upgrade

Re-run `./install.sh` after pulling new source. The service is restarted
automatically.

## Uninstall

```bash
sudo systemctl disable --now fleet-telemetry-recorder.service
sudo rm /etc/systemd/system/fleet-telemetry-recorder.service
sudo systemctl daemon-reload
rm -rf ~/fleet-telemetry-recorder-venv
```

Leftover files under `~/printer_data/logs/telemetry/` can be kept or deleted —
they don't affect Klipper or Moonraker.
