#!/usr/bin/env python3
"""
Fleet Telemetry Viewer — native desktop app.

Loads a .jsonl.gz telemetry file produced by fleet_telemetry_recorder,
forward-fills Klipper's sparse status deltas, and renders a stack of
time-linked pyqtgraph charts: temperatures, heater power + fan, motion,
layer progress, Z height. Drag-drop or File > Open.

Cross-platform (Windows / macOS / Linux). Deps in requirements.txt.

Usage:
    pip install -r requirements.txt
    python telemetry_viewer.py                 # opens empty, drop a file
    python telemetry_viewer.py path/to/file.jsonl.gz
"""

from __future__ import annotations

import gzip
import io
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Motion analysis is optional — if matplotlib / motan can't import we still
# want the main viewer to work.
try:
    from motion_analysis import run_motion_analysis, MotionAnalysisResult
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg,
        NavigationToolbar2QT,
    )
    MOTION_OK = True
    _MOTION_IMPORT_ERR = None
except Exception as _e:  # noqa: BLE001
    MOTION_OK = False
    _MOTION_IMPORT_ERR = f"{type(_e).__name__}: {_e}"

# antialias off: with million-point series it dominates paint time.
# Lines still look fine because every pixel of a dense curve is drawn anyway.
pg.setConfigOptions(antialias=False, background="#0e1116", foreground="#d5dae5")


# ----------------------------------------------------------------------------
# Background parser — streaming gzip + forward-fill, runs in a QThread so the
# UI stays responsive on multi-hundred-megabyte files.
# ----------------------------------------------------------------------------

class ParseWorker(QThread):
    progress = Signal(str, float)       # message, pct 0..1
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def run(self) -> None:
        try:
            self.done.emit(self._parse())
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")

    def _parse(self) -> dict:
        path = self.path
        total_bytes = Path(path).stat().st_size
        self.progress.emit(
            f"Opening {path} ({total_bytes / (1024*1024):.1f} MB gzipped)...", 0.0
        )

        # Inline forward-fill carry variables. Init all numeric ones to NaN so
        # we can dump lists straight into numpy arrays at the end without a
        # per-element None->NaN conversion pass.
        NAN = float("nan")
        c_ex_T = c_ex_set = c_ex_pwr = NAN
        c_bed_T = c_bed_set = c_bed_pwr = NAN
        c_mr_vel = c_mr_evel = NAN
        c_fan = NAN
        c_layer = c_pd = c_fil = NAN
        c_pos_x = c_pos_y = c_pos_z = NAN
        c_state: Optional[str] = None

        ts: list[float] = []
        ex_T_l:   list[float] = []
        ex_set_l: list[float] = []
        ex_pwr_l: list[float] = []
        bed_T_l:   list[float] = []
        bed_set_l: list[float] = []
        bed_pwr_l: list[float] = []
        mr_vel_l:  list[float] = []
        mr_evel_l: list[float] = []
        fan_l:     list[float] = []
        layer_l:   list[float] = []
        pd_l:      list[float] = []
        fil_l:     list[float] = []
        pos_x_l:   list[float] = []
        pos_y_l:   list[float] = []
        pos_z_l:   list[float] = []

        # Extras: store sparse (sample_index, temperature) points per sensor.
        # We'll forward-fill into dense arrays at end — O(N + total_points)
        # instead of the per-sample O(N * sensors) loop the previous version
        # did (that was the main reason parsing was slow).
        extras_points: dict[str, list[tuple[int, float]]] = {}

        state_events: list[tuple[float, str]] = []
        header: Optional[dict] = None
        status_count = 0
        trapq_count = 0
        line_count = 0

        # Bind hot names locally — saves a global lookup per-call.
        json_loads = json.loads

        with open(path, "rb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as gz:
                text = io.TextIOWrapper(gz, encoding="utf-8", errors="replace")
                for line in text:
                    line_count += 1

                    # Fast-path skip for trapq lines without running JSON parse.
                    # trapq is typically ~50% of all lines and we don't chart it.
                    if '"kind":"trapq"' in line:
                        trapq_count += 1
                        if line_count % 50000 == 0:
                            self._report_progress(raw, total_bytes, line_count)
                        continue

                    if not line.strip():
                        continue
                    try:
                        obj = json_loads(line)
                    except ValueError:
                        continue

                    kind = obj.get("kind")
                    if kind == "header":
                        header = obj
                        continue
                    if kind != "status":
                        continue

                    status_count += 1

                    # Forward-fill inline — dict.get() only, no helper calls.
                    g = obj.get("ex")
                    if g:
                        v = g.get("T");    c_ex_T   = v if v is not None else c_ex_T
                        v = g.get("set");  c_ex_set = v if v is not None else c_ex_set
                        v = g.get("pwr");  c_ex_pwr = v if v is not None else c_ex_pwr
                    g = obj.get("bed")
                    if g:
                        v = g.get("T");    c_bed_T   = v if v is not None else c_bed_T
                        v = g.get("set");  c_bed_set = v if v is not None else c_bed_set
                        v = g.get("pwr");  c_bed_pwr = v if v is not None else c_bed_pwr
                    g = obj.get("mr")
                    if g:
                        v = g.get("vel");   c_mr_vel  = v if v is not None else c_mr_vel
                        v = g.get("evel");  c_mr_evel = v if v is not None else c_mr_evel
                    g = obj.get("th")
                    if g:
                        p = g.get("pos")
                        if p and len(p) >= 3:
                            c_pos_x, c_pos_y, c_pos_z = p[0], p[1], p[2]
                    g = obj.get("ps")
                    if g:
                        v = g.get("layer");  c_layer = v if v is not None else c_layer
                        v = g.get("pd");     c_pd    = v if v is not None else c_pd
                        v = g.get("fil");    c_fil   = v if v is not None else c_fil
                        st = g.get("state")
                        if st is not None and st != c_state:
                            c_state = st
                            state_events.append((obj.get("t", 0.0), st))
                    v = obj.get("fan")
                    if v is not None:
                        c_fan = v

                    # Extras — only touch when the sample has them, keep sparse.
                    ex_dict = obj.get("extras")
                    if ex_dict:
                        idx = status_count - 1
                        for name, e in ex_dict.items():
                            if e:
                                T = e.get("T")
                                if T is not None:
                                    pts = extras_points.get(name)
                                    if pts is None:
                                        pts = []
                                        extras_points[name] = pts
                                    pts.append((idx, T))

                    ts.append(obj.get("t", 0.0))
                    ex_T_l.append(c_ex_T);     ex_set_l.append(c_ex_set);     ex_pwr_l.append(c_ex_pwr)
                    bed_T_l.append(c_bed_T);   bed_set_l.append(c_bed_set);   bed_pwr_l.append(c_bed_pwr)
                    mr_vel_l.append(c_mr_vel); mr_evel_l.append(c_mr_evel)
                    fan_l.append(c_fan)
                    layer_l.append(c_layer);   pd_l.append(c_pd);             fil_l.append(c_fil)
                    pos_x_l.append(c_pos_x);   pos_y_l.append(c_pos_y);       pos_z_l.append(c_pos_z)

                    if line_count % 50000 == 0:
                        self._report_progress(raw, total_bytes, line_count)

        self.progress.emit("Building arrays...", 0.97)

        n = len(ts)
        t_arr = np.asarray(ts, dtype=np.float64)
        # Fast list → numpy: lists are all floats (possibly NaN), no None.
        series = {
            "ex_T":    np.asarray(ex_T_l,   dtype=np.float32),
            "ex_set":  np.asarray(ex_set_l, dtype=np.float32),
            "ex_pwr":  np.asarray(ex_pwr_l, dtype=np.float32),
            "bed_T":   np.asarray(bed_T_l,   dtype=np.float32),
            "bed_set": np.asarray(bed_set_l, dtype=np.float32),
            "bed_pwr": np.asarray(bed_pwr_l, dtype=np.float32),
            "mr_vel":  np.asarray(mr_vel_l,  dtype=np.float32),
            "mr_evel": np.asarray(mr_evel_l, dtype=np.float32),
            "fan":     np.asarray(fan_l,     dtype=np.float32),
            "layer":   np.asarray(layer_l,   dtype=np.float32),
            "pd":      np.asarray(pd_l,      dtype=np.float32),
            "fil":     np.asarray(fil_l,     dtype=np.float32),
            "pos_x":   np.asarray(pos_x_l,   dtype=np.float32),
            "pos_y":   np.asarray(pos_y_l,   dtype=np.float32),
            "pos_z":   np.asarray(pos_z_l,   dtype=np.float32),
        }

        # Expand extras: fill a full-length NaN array from sparse (idx, T) pairs,
        # holding each value forward until the next update.
        extras: dict[str, np.ndarray] = {}
        for name, pts in extras_points.items():
            arr = np.full(n, np.nan, dtype=np.float32)
            for i, (idx, T) in enumerate(pts):
                next_idx = pts[i + 1][0] if i + 1 < len(pts) else n
                arr[idx:next_idx] = T
            extras[name] = arr

        return {
            "header": header,
            "status_count": status_count,
            "trapq_count": trapq_count,
            "line_count": line_count,
            "t": t_arr,
            "series": series,
            "extras": extras,
            "state_events": state_events,
            "source_path": path,
        }

    def _report_progress(self, raw, total_bytes: int, line_count: int) -> None:
        try:
            pct = raw.tell() / total_bytes if total_bytes else 0.0
        except Exception:
            pct = 0.0
        self.progress.emit(
            f"Parsing... {line_count/1000:.0f}k lines ({pct*100:.0f}%)", pct
        )


# ----------------------------------------------------------------------------
# Motion analysis worker — runs motan's analyzer pipeline. Kept separate
# from ParseWorker because the two run in sequence and the motion analysis
# can be re-run (different time window) without re-parsing the file.
# ----------------------------------------------------------------------------

class MotionAnalysisWorker(QThread):
    progress = Signal(str)
    done = Signal(object)   # MotionAnalysisResult
    failed = Signal(str)

    def __init__(self, path: str, skip: float, duration: float) -> None:
        super().__init__()
        self.path = path
        self.skip = skip
        self.duration = duration

    def run(self) -> None:
        try:
            result = run_motion_analysis(
                self.path,
                skip=self.skip,
                duration=self.duration,
                progress=lambda msg: self.progress.emit(msg),
            )
            self.done.emit(result)
        except Exception as e:   # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------

# A palette that reads well on the dark background.
PALETTE = [
    "#ef6d6d", "#f5b400", "#6cc24a", "#5aa9ff", "#c586ff",
    "#ff8ab5", "#4dd6c8", "#d8c468", "#8f9fb3", "#e68a44",
]

STATE_BAND_COLORS = {
    "printing":  (90, 169, 255, 28),
    "paused":    (245, 180, 0, 50),
    "error":     (239, 109, 109, 55),
    "cancelled": (239, 109, 109, 55),
    "complete":  (108, 194, 74, 40),
    "standby":   (139, 147, 167, 22),
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fleet Telemetry Viewer")
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        # Menu
        file_menu = self.menuBar().addMenu("&File")
        open_act = QAction("&Open...", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self.choose_file)
        file_menu.addAction(open_act)

        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Header bar
        header = QHBoxLayout()
        self.file_label = QLabel("No file loaded — drop a .jsonl.gz anywhere, or File → Open...")
        self.file_label.setStyleSheet("color: #8b93a7;")
        open_btn = QPushButton("Open file...")
        open_btn.clicked.connect(self.choose_file)
        self.reset_btn = QPushButton("Reset zoom")
        self.reset_btn.clicked.connect(self.reset_zoom)
        self.reset_btn.setEnabled(False)
        header.addWidget(self.file_label, 1)
        header.addWidget(open_btn)
        header.addWidget(self.reset_btn)
        root.addLayout(header)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(16)
        root.addWidget(self.progress)

        # Scroll area with chart stack
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.charts_host = QWidget()
        self.charts_layout = QVBoxLayout(self.charts_host)
        self.charts_layout.setContentsMargins(0, 0, 0, 0)
        self.charts_layout.setSpacing(8)
        self.scroll.setWidget(self.charts_host)
        root.addWidget(self.scroll, 1)

        self.plots: list[pg.PlotWidget] = []
        self.worker: Optional[ParseWorker] = None
        self.motion_worker: Optional["MotionAnalysisWorker"] = None
        self.motion_panel: Optional[QWidget] = None
        self.motion_canvas: Optional[object] = None  # FigureCanvasQTAgg
        self.motion_toolbar: Optional[object] = None
        self.motion_status_label: Optional[QLabel] = None
        self.motion_skip_spin: Optional[QDoubleSpinBox] = None
        self.motion_duration_spin: Optional[QDoubleSpinBox] = None
        self.motion_rerun_btn: Optional[QPushButton] = None
        self.motion_result: Optional[object] = None  # MotionAnalysisResult
        self._motion_fig_layout: Optional[QVBoxLayout] = None
        self.current_path: Optional[str] = None

        self.statusBar().showMessage("Ready")

    # ---- Drag-drop ---------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p:
                self.load_file(p)
                return

    # ---- File pick / load --------------------------------------------------

    def choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open telemetry file", "",
            "Telemetry (*.jsonl.gz *.gz);;All files (*.*)"
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> None:
        self.current_path = path
        self.file_label.setText(str(Path(path).name))
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.progress.setFormat("Loading...")
        self.reset_btn.setEnabled(False)

        # Cancel any prior work before starting over.
        for w in (self.worker, self.motion_worker):
            if w and w.isRunning():
                w.terminate()
                w.wait(1000)
        self.motion_worker = None

        self.worker = ParseWorker(path)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, msg: str, pct: float) -> None:
        self.progress.setFormat(msg)
        self.progress.setValue(int(pct * 100))

    def _on_done(self, data: dict) -> None:
        self.progress.setVisible(False)
        self.render_all(data)
        self.statusBar().showMessage(
            f"Loaded {data['status_count']} status + {data['trapq_count']} trapq events"
        )
        # Kick off the motion-analysis phase (if enabled and there's trapq data).
        if MOTION_OK and data.get("trapq_count", 0) > 0 and self.current_path:
            self._start_motion_analysis(skip=0.0, duration=60.0)
        elif not MOTION_OK and self.motion_status_label is None:
            # Surface a one-shot hint about why motion analysis is missing.
            self.statusBar().showMessage(
                f"Motion analysis unavailable — {_MOTION_IMPORT_ERR}", 10000
            )

    def _on_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        QMessageBox.critical(self, "Parse error", msg)

    def reset_zoom(self) -> None:
        for p in self.plots:
            p.enableAutoRange()

    # ---- Rendering ---------------------------------------------------------

    def render_all(self, data: dict) -> None:
        # Clear previous
        self._clear_motion_figure()
        self.motion_panel = None
        self.motion_skip_spin = None
        self.motion_duration_spin = None
        self.motion_rerun_btn = None
        self.motion_status_label = None
        self.motion_result = None
        while self.charts_layout.count():
            item = self.charts_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.plots = []

        # Summary
        self.charts_layout.addWidget(self._make_summary_widget(data))

        t = data["t"]
        S = data["series"]
        EX = data["extras"]
        events = data["state_events"]

        # Build every plot with auto-range DISABLED so each plot() call doesn't
        # trigger a full range recompute. We enable auto-range once at the end.
        self.setUpdatesEnabled(False)
        try:
            # Chart 1 — Temperatures
            p_temp = self._new_plot("Temperatures (°C)", y_label="°C")
            p_temp.disableAutoRange()
            self._plot_line(p_temp, t, S["ex_T"],   "Extruder T",    "#ef6d6d")
            self._plot_line(p_temp, t, S["ex_set"], "Extruder set",  "#ef6d6d", dashed=True)
            self._plot_line(p_temp, t, S["bed_T"],   "Bed T",         "#f5b400")
            self._plot_line(p_temp, t, S["bed_set"], "Bed set",       "#f5b400", dashed=True)
            for i, (name, arr) in enumerate(sorted(EX.items())):
                self._plot_line(p_temp, t, arr, name, PALETTE[(i + 3) % len(PALETTE)])
            t_max = float(t[-1]) if len(t) else None
            self._add_state_markers(p_temp, events, t_max=t_max)

            # Chart 2 — Heater power + fan
            p_pwr = self._new_plot("Heater Power + Fan (0–1)", y_label="0–1")
            p_pwr.disableAutoRange()
            self._plot_line(p_pwr, t, S["ex_pwr"],  "Extruder pwr", "#ef6d6d")
            self._plot_line(p_pwr, t, S["bed_pwr"], "Bed pwr",      "#f5b400")
            self._plot_line(p_pwr, t, S["fan"],     "Part fan",     "#5aa9ff", dashed=True)
            self._add_state_markers(p_pwr, events, t_max=t_max)

            # Chart 3 — Motion
            p_mot = self._new_plot("Motion — live velocity (mm/s)", y_label="mm/s")
            p_mot.disableAutoRange()
            self._plot_line(p_mot, t, S["mr_vel"],  "Toolhead vel", "#5aa9ff")
            self._plot_line(p_mot, t, S["mr_evel"], "Extruder vel", "#c586ff")
            self._add_state_markers(p_mot, events, t_max=t_max)

            # Link X axes — pan/zoom on any chart moves all of them.
            for p in self.plots[1:]:
                p.setXLink(self.plots[0])

            # One auto-range pass per plot now that all curves are in place.
            for p in self.plots:
                p.enableAutoRange()
        finally:
            self.setUpdatesEnabled(True)

        self.reset_btn.setEnabled(True)

    # ---- Motion analysis --------------------------------------------------

    def _start_motion_analysis(self, skip: float, duration: float) -> None:
        """Kick off motan's analyzer pipeline in a QThread."""
        if not MOTION_OK or not self.current_path:
            return
        # Build (or clear) the motion panel so the user sees "running..." feedback.
        self._ensure_motion_panel()
        if self.motion_status_label is not None:
            self.motion_status_label.setText("Starting motion analysis...")
        if self.motion_rerun_btn is not None:
            self.motion_rerun_btn.setEnabled(False)

        # Clean up any prior figure / canvas — keep this panel single-figure.
        self._clear_motion_figure()

        if self.motion_worker and self.motion_worker.isRunning():
            self.motion_worker.terminate()
            self.motion_worker.wait(1000)

        self.motion_worker = MotionAnalysisWorker(
            self.current_path, skip=skip, duration=duration,
        )
        self.motion_worker.progress.connect(self._on_motion_progress)
        self.motion_worker.done.connect(self._on_motion_done)
        self.motion_worker.failed.connect(self._on_motion_failed)
        self.motion_worker.start()

    def _ensure_motion_panel(self) -> None:
        """Create the motion-analysis card at the bottom of the chart stack
        if it doesn't exist yet. Re-created on each file load."""
        if self.motion_panel is not None:
            return

        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setStyleSheet(
            "QFrame { background:#171c26; border:1px solid #252b38; border-radius:6px; }"
            "QLabel { color:#bec4d1; }"
        )
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        title = QLabel("MOTION ANALYSIS")
        title.setStyleSheet(
            "color:#8b93a7; font-size:10px; letter-spacing:1px; font-weight:600;"
        )
        outer.addWidget(title)

        # Controls row
        controls = QHBoxLayout()
        controls.setSpacing(10)

        lbl_skip = QLabel("Skip (s):")
        skip_spin = QDoubleSpinBox()
        skip_spin.setRange(0.0, 99999.0)
        skip_spin.setSingleStep(5.0)
        skip_spin.setDecimals(1)
        skip_spin.setValue(0.0)
        skip_spin.setFixedWidth(100)

        lbl_dur = QLabel("Duration (s):")
        dur_spin = QDoubleSpinBox()
        dur_spin.setRange(1.0, 99999.0)
        dur_spin.setSingleStep(10.0)
        dur_spin.setDecimals(1)
        dur_spin.setValue(60.0)
        dur_spin.setFixedWidth(100)

        rerun = QPushButton("Re-analyze")
        rerun.clicked.connect(self._on_rerun_motion)

        status = QLabel("")
        status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        status.setStyleSheet("color:#8b93a7;")

        controls.addWidget(lbl_skip)
        controls.addWidget(skip_spin)
        controls.addSpacing(12)
        controls.addWidget(lbl_dur)
        controls.addWidget(dur_spin)
        controls.addSpacing(12)
        controls.addWidget(rerun)
        controls.addWidget(status, 1)

        outer.addLayout(controls)

        # Placeholder where the figure/canvas will be inserted on done.
        fig_container = QFrame()
        fig_container.setMinimumHeight(520)
        fig_container.setFrameShape(QFrame.Shape.NoFrame)
        fig_layout = QVBoxLayout(fig_container)
        fig_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(fig_container, 1)

        # Remember handles
        self.motion_panel = panel
        self.motion_skip_spin = skip_spin
        self.motion_duration_spin = dur_spin
        self.motion_rerun_btn = rerun
        self.motion_status_label = status
        self._motion_fig_layout = fig_layout

        self.charts_layout.addWidget(panel)

    def _clear_motion_figure(self) -> None:
        if self.motion_canvas is not None:
            self.motion_canvas.setParent(None)
            self.motion_canvas.deleteLater()
            self.motion_canvas = None
        if self.motion_toolbar is not None:
            self.motion_toolbar.setParent(None)
            self.motion_toolbar.deleteLater()
            self.motion_toolbar = None
        # Close any prior matplotlib figure to free memory.
        prev = getattr(self, "motion_result", None)
        if prev is not None and getattr(prev, "fig", None) is not None:
            try:
                import matplotlib.pyplot as plt
                plt.close(prev.fig)
            except Exception:
                pass

    def _on_motion_progress(self, msg: str) -> None:
        if self.motion_status_label is not None:
            self.motion_status_label.setText(msg)

    def _on_motion_done(self, result) -> None:
        self.motion_result = result
        if self.motion_status_label is not None:
            meta = result.meta
            self.motion_status_label.setText(
                f"{meta['trapq_event_count']} trapq events across "
                f"{', '.join(meta['trapq_names'])} — "
                f"analyzed {result.duration:.1f}s starting at +{result.skip:.1f}s"
            )
        if self.motion_rerun_btn is not None:
            self.motion_rerun_btn.setEnabled(True)

        # Embed the figure (must happen on the main thread since
        # FigureCanvasQTAgg is a QWidget).
        canvas = FigureCanvasQTAgg(result.fig)
        canvas.setMinimumHeight(520)
        toolbar = NavigationToolbar2QT(canvas, self.motion_panel)
        self._motion_fig_layout.addWidget(toolbar)
        self._motion_fig_layout.addWidget(canvas, 1)
        self.motion_canvas = canvas
        self.motion_toolbar = toolbar

    def _on_motion_failed(self, msg: str) -> None:
        if self.motion_status_label is not None:
            self.motion_status_label.setText(f"Motion analysis failed — {msg}")
        if self.motion_rerun_btn is not None:
            self.motion_rerun_btn.setEnabled(True)

    def _on_rerun_motion(self) -> None:
        skip = self.motion_skip_spin.value() if self.motion_skip_spin else 0.0
        dur = self.motion_duration_spin.value() if self.motion_duration_spin else 60.0
        self._start_motion_analysis(skip=skip, duration=dur)

    # ---- Summary ----------------------------------------------------------

    def _make_summary_widget(self, data: dict) -> QWidget:
        header = data.get("header") or {}
        t = data["t"]
        dur = float(t[-1] - t[0]) if len(t) else 0.0
        max_ex = _nanmax(data["series"].get("ex_T"))
        max_bed = _nanmax(data["series"].get("bed_T"))

        rows = [
            ("File",         Path(data["source_path"]).name),
            ("Printer",      header.get("printer", "—")),
            ("Klippy",       header.get("klippy_version", "—")),
            ("Started",      header.get("started_at", "—")),
            ("Source file",  header.get("filename", "—")),
            ("Duration",     _fmt_duration(dur)),
            ("Events",       f"{data['status_count']} status / {data['trapq_count']} trapq"),
            ("Max Ex T",     f"{max_ex:.1f} °C" if max_ex is not None else "—"),
            ("Max Bed T",    f"{max_bed:.1f} °C" if max_bed is not None else "—"),
            ("State changes", str(len(data['state_events']))),
        ]

        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(
            "QFrame { background:#171c26; border:1px solid #252b38; border-radius:6px; }"
            "QLabel { color:#e5e9f0; }"
            "QLabel.k { color:#8b93a7; font-size:10px; letter-spacing:1px; }"
        )
        grid = QHBoxLayout(box)
        grid.setContentsMargins(16, 10, 16, 10)
        grid.setSpacing(24)

        for k, v in rows:
            col = QVBoxLayout()
            col.setSpacing(2)
            kl = QLabel(k.upper())
            kl.setProperty("class", "k")
            kl.setStyleSheet("color:#8b93a7; font-size:10px; letter-spacing:1px;")
            vl = QLabel(str(v))
            vl.setStyleSheet("color:#e5e9f0; font-weight:500;")
            col.addWidget(kl)
            col.addWidget(vl)
            holder = QWidget()
            holder.setLayout(col)
            grid.addWidget(holder)
        grid.addStretch(1)

        return box

    def _new_plot(self, title: str, y_label: str = "") -> pg.PlotWidget:
        p = pg.PlotWidget()
        p.setMinimumHeight(260)
        p.setTitle(title, color="#bec4d1", size="10pt")
        p.showGrid(x=True, y=True, alpha=0.18)
        p.setLabel("left", y_label)
        p.setLabel("bottom", "t (s)")
        p.addLegend(offset=(10, 10), labelTextColor="#bec4d1")
        # Built-in downsampling keeps pan/zoom smooth on multi-million-point series.
        p.setDownsampling(auto=True, mode="peak")
        p.setClipToView(True)
        self.charts_layout.addWidget(p)
        self.plots.append(p)
        return p

    def _plot_line(
        self, plot: pg.PlotWidget, t: np.ndarray, y: Optional[np.ndarray],
        name: str, color: str, dashed: bool = False,
    ) -> None:
        if y is None or len(y) == 0:
            return
        # Skip all-NaN series so legend stays clean.
        if not np.any(np.isfinite(y)):
            return
        pen = pg.mkPen(
            QColor(color), width=1.4,
            style=Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine,
        )
        plot.plot(t, y, pen=pen, name=name, connect="finite")

    def _add_state_markers(
        self, plot: pg.PlotWidget, events: list[tuple[float, str]],
        t_max: Optional[float] = None,
    ) -> None:
        # Dashed vertical lines at each state transition + translucent bands
        # between adjacent transitions colored by the entered state.
        if not events:
            return
        if t_max is None:
            # Fall back to the largest event timestamp + 1s; better than
            # querying viewRange() which may be stale while auto-range is off.
            t_max = max(e[0] for e in events) + 1.0

        for i, (t_start, state) in enumerate(events):
            band_color = STATE_BAND_COLORS.get(state)
            if band_color is not None:
                t_end = events[i + 1][0] if i + 1 < len(events) else t_max
                if t_end <= t_start:
                    t_end = t_start + 0.5
                region = pg.LinearRegionItem(
                    values=[t_start, t_end],
                    brush=QColor(*band_color),
                    pen=pg.mkPen(None),
                    movable=False,
                )
                region.setZValue(-10)
                plot.addItem(region)

            vline = pg.InfiniteLine(
                pos=t_start, angle=90,
                pen=pg.mkPen(QColor(139, 147, 167), style=Qt.PenStyle.DashLine, width=1),
                label=state,
                labelOpts={
                    "color": "#e5e9f0", "position": 0.95, "movable": False,
                    "fill": QColor(23, 28, 38, 200),
                },
            )
            plot.addItem(vline)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _nanmax(a: Optional[np.ndarray]) -> Optional[float]:
    if a is None or len(a) == 0:
        return None
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return None
    return float(finite.max())


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Dark palette across the whole app (menu bar, dialogs) so it matches the
    # chart canvases.
    from PySide6.QtGui import QPalette
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,        QColor("#0e1116"))
    pal.setColor(QPalette.ColorRole.WindowText,    QColor("#e5e9f0"))
    pal.setColor(QPalette.ColorRole.Base,          QColor("#171c26"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#12161e"))
    pal.setColor(QPalette.ColorRole.Text,          QColor("#e5e9f0"))
    pal.setColor(QPalette.ColorRole.Button,        QColor("#202635"))
    pal.setColor(QPalette.ColorRole.ButtonText,    QColor("#e5e9f0"))
    pal.setColor(QPalette.ColorRole.Highlight,     QColor("#5aa9ff"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#0e1116"))
    app.setPalette(pal)

    win = MainWindow()
    win.show()

    # Optional: file path as argv[1]
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if Path(arg).exists():
            win.load_file(arg)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
