# Fleet Telemetry Viewer

Native desktop app (Python + PySide6 + pyqtgraph) for inspecting `.jsonl.gz`
telemetry files produced by `fleet_telemetry_recorder`. Uses your machine's
RAM and CPU directly — handles multi-hundred-megabyte files without the
browser-tab overhead the web version would have.

Cross-platform: Windows, macOS, Linux.

## What it shows

Three time-linked live-status charts stacked vertically (pan or zoom any
chart and the others follow):

1. **Temperatures** — extruder T/setpoint, bed T/setpoint, every
   `temperature_sensor` / `temperature_fan` / `heater_fan` / `controller_fan`
   that appeared in the print.
2. **Heater power + fan** — extruder power, bed power, part-cooling fan,
   all on a shared 0–1 axis.
3. **Motion** — toolhead live velocity + extruder live velocity.

Plus a **Motion Analysis** panel at the bottom running Klipper's `motan`
analyzer pipeline directly on the trapq move-planner stream (see below).

Plus:

- A **summary card** at the top (printer, Klippy version, duration,
  max temps, event counts).
- **State transitions** drawn as dashed vertical lines on every chart,
  with labels, plus translucent background bands colored by state
  (blue = printing, amber = paused, red = error/cancelled, green = complete).
- `print_stats` deltas are **forward-filled** — Klippy only sends changed
  fields per status push, so a naive plot would look like sparse dots. The
  viewer carries the last known value forward so every line is continuous.

## Install

Python 3.9+. On a fresh machine:

```bash
cd fleet_telemetry_recorder/telemetry_viewer
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Dependencies:

- `PySide6` — cross-platform Qt bindings (the GUI)
- `pyqtgraph` — fast charting with built-in peak-based downsampling
- `numpy` — backing arrays (pyqtgraph dep anyway)

Note: `gzip`, `json`, and streaming decompression are Python stdlib — no
separate gzip library needed.

## Run

```bash
python telemetry_viewer.py                       # opens empty, drop a file on the window
python telemetry_viewer.py path/to/file.jsonl.gz # open a specific file
```

Drag any `.jsonl.gz` onto the window to load it. **File → Open** (or
`Ctrl+O` / `⌘O`) also works.

## Controls

| Action | How |
|---|---|
| Pan | Click + drag inside any chart |
| Zoom X axis | Mouse wheel over any chart |
| Box zoom | Right-click + drag |
| Reset zoom | "Reset zoom" button in header, or right-click → "View All" |
| Toggle a series | Click its legend entry |
| Exact cursor value | Hover — the crosshair follows your cursor on each chart |

Because all the live-status charts share their X axis, any pan or zoom on
one immediately moves all of them. Scrub to a suspicious moment and see
temps, power, and motion together.

## Motion Analysis panel

After the main charts load, the viewer automatically runs Klipper's own
`motan` analyzer pipeline against the recorded trapezoidal motion queue
(`motion_report/dump_trapq`), producing a second stack of matplotlib charts:

- **Extruder velocity** (commanded mm/s from the planner)
- **Extruder acceleration**
- **Toolhead velocity**
- **Toolhead acceleration**
- **Toolhead X / Y / Z velocity components**

This is a strictly deeper view than chart #3 — chart #3 shows live reported
values at ~4 Hz; motion analysis reconstructs the planner's *intent* at
1 ms resolution. It's what you want when hunting motion artifacts, layer
shifts, or anomalous accel patterns.

Controls at the top of the panel:

- **Skip (s)** — seconds to skip past the start before analyzing
- **Duration (s)** — length of the analysis window (default 60s)
- **Re-analyze** — re-run with new parameters

First run defaults to the first 60 s of the print. For a 97 MB / 5-hour
file the default run completes in ~3 s on a modern laptop.

The motion charts are matplotlib figures with the full pan/zoom/save
toolbar above them (standard matplotlib Qt controls). You can save the
rendered figure to PNG/SVG/PDF from there.

### What's under the hood

`motion_analysis.py` + `motan_adapter.py` wrap Klipper's motan scripts
(vendored at `./motan/`). On load:

1. The `.jsonl.gz` is streamed and trapq events are re-emitted in motan's
   native two-file format (`.json.gz` + `.index.gz`) to a tempdir. String
   slicing avoids a JSON round-trip — conversion of a 97 MB file takes ~3 s.
2. Motan's `LogManager` reads that tempdir; `AnalyzerManager` generates
   velocity/acceleration datasets at 1 ms segment time (10× coarser than
   motan's CLI default but plenty for visual analysis — drop in
   `motion_analysis.py` if you need finer).
3. A matplotlib `Figure` is built directly (no `pyplot`, so it's
   thread-safe) and then embedded on the main thread via
   `FigureCanvasQTAgg`.

Motan's other analyzers (stepper deviation, accelerometer traces,
stallguard, etc.) would need the recorder to capture those streams too —
currently out of scope. If you need them, motan's own `motan_graph.py`
can still be run manually on the raw `.jsonl.gz` after re-running the
in-memory conversion.

## Performance

- Parsing is offloaded to a `QThread` so the UI never blocks on load.
- `gzip.GzipFile` streams bytes off disk — we never hold the full
  decompressed file in memory; only the parsed numpy arrays do.
- pyqtgraph's `setDownsampling(auto=True, mode="peak")` keeps
  multi-million-point series smooth; zoom in for true sample density.
- On a 97 MB gzipped / ~500 MB decompressed file (5h print with trapq),
  load time on a modern laptop is ~15–30 s; pan/zoom is real-time.

## Build a standalone binary (optional)

If you want a single double-clickable executable so teammates don't need to
install Python:

```bash
pip install pyinstaller
pyinstaller --name FleetTelemetryViewer --windowed --onefile telemetry_viewer.py
# Output: dist/FleetTelemetryViewer.exe   (Windows)
#         dist/FleetTelemetryViewer.app   (macOS)
```

The resulting binary is ~80–120 MB (ships a Python runtime + Qt). Build on
each target OS — pyinstaller does not cross-compile.

## Limitations (v1)

- `trapq` move-segment events are counted in the summary but not charted.
  For deep motion / acceleration analysis, use Klipper's `motan_graph.py`
  against the raw `.jsonl.gz`, or extend `worker`-equivalent logic in
  `telemetry_viewer.py` (search for the `kind == "trapq"` branch).
- Single file at a time — no cross-print overlay yet.
- Backfill for the `extras` sensors starts at the sample the sensor first
  appeared. Sensors that only broadcast temperatures sporadically may look
  stepped; this is fidelity to the source data, not a render bug.
