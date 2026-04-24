"""
Run Klipper motan's analyzer pipeline against a fleet telemetry file and
return matplotlib Figures ready for embedding in the Qt viewer.

Makes motan scripts (`readlog.py`, `analyzers.py`, `motan_graph.py`)
importable by adding the `motan/` subdirectory to sys.path at module-load.
Matplotlib backend is forced to QtAgg so the resulting Figure can be
wrapped by `FigureCanvasQTAgg`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Ensure matplotlib's Qt bindings talk to PySide6 if/when the main thread
# attaches a canvas. We do NOT call matplotlib.use() here because this
# module is imported and used from a worker thread — touching the backend
# machinery from a non-main thread trips Qt warnings. We build the Figure
# directly via `matplotlib.figure.Figure(...)` (no pyplot), so no backend
# is ever selected inside the worker.
os.environ.setdefault("QT_API", "pyside6")
import matplotlib                              # noqa: E402
from matplotlib.figure import Figure           # noqa: E402
import matplotlib.font_manager as _fm          # noqa: E402

# Make motan scripts importable.
_MOTAN_DIR = Path(__file__).parent / "motan"
if str(_MOTAN_DIR) not in sys.path:
    sys.path.insert(0, str(_MOTAN_DIR))

import analyzers       # noqa: E402
import readlog         # noqa: E402

from motan_adapter import convert_to_motan


def default_graph_descs(trapq_names: list[str]) -> list[list[tuple[str, dict]]]:
    """Default analysis set — velocity + acceleration per trapq, plus the
    xyz velocity components for the toolhead if present."""
    descs: list[list[tuple[str, dict]]] = []
    for name in trapq_names:
        descs.append([(f"trapq({name},velocity)", {"color": "#5aa9ff"})])
        descs.append([(f"trapq({name},accel)",    {"color": "#ef6d6d"})])
    if "toolhead" in trapq_names:
        descs.append([
            (f"trapq(toolhead,x_velocity)", {"color": "#ef6d6d"}),
            (f"trapq(toolhead,y_velocity)", {"color": "#6cc24a"}),
            (f"trapq(toolhead,z_velocity)", {"color": "#5aa9ff"}),
        ])
    return descs


class MotionAnalysisResult:
    """Bundle: matplotlib figure + keep-alive refs for temp dir cleanup."""

    def __init__(self, fig, meta: dict, temp_dir: str, skip: float, duration: float) -> None:
        self.fig = fig
        self.meta = meta
        self.temp_dir = temp_dir
        self.skip = skip
        self.duration = duration


def run_motion_analysis(
    jsonl_gz_path: str,
    skip: float = 0.0,
    duration: float = 60.0,
    segment_time: float = 1e-3,     # 1 ms — plenty of fidelity, ~10x faster than motan's 100 µs default
    graph_descs: Optional[list[list[tuple[str, dict]]]] = None,
    progress=None,
) -> MotionAnalysisResult:
    """Convert the given telemetry file to motan format in a temp dir,
    run motan's default analysis set (unless `graph_descs` is provided),
    and return a MotionAnalysisResult wrapping the matplotlib Figure.

    `skip` and `duration` are seconds relative to the first trapq move.
    """
    temp_dir = tempfile.mkdtemp(prefix="ftvmotan_")
    prefix = os.path.join(temp_dir, "log")

    if progress:
        progress("Converting telemetry to motan format...")
    meta = convert_to_motan(jsonl_gz_path, prefix)

    if progress:
        progress(
            f"Running motan analyzers: "
            f"{meta['trapq_event_count']} trapq events, "
            f"{len(meta['trapq_names'])} trapqs"
        )

    # Cap `duration` to the available span of the log so motan doesn't try
    # to analyze gaps past EOF.
    span = max(0.0, meta["last_print_time"] - meta["first_print_time"])
    if duration <= 0 or duration > span:
        duration = span

    lmgr = readlog.LogManager(prefix)
    lmgr.setup_index()
    lmgr.seek_time(skip)
    amgr = analyzers.AnalyzerManager(lmgr, segment_time)
    amgr.set_duration(duration)

    if graph_descs is None:
        graph_descs = default_graph_descs(meta["trapq_names"])

    if progress:
        progress(f"Generating data for {len(graph_descs)} graph(s)...")

    # Collect datasets (mirror of motan_graph.plot_motion but using
    # matplotlib.figure.Figure directly — no pyplot, safe from worker thread).
    for row in graph_descs:
        for dataset, _params in row:
            amgr.setup_dataset(dataset)
    amgr.generate_datasets()
    datasets = amgr.get_datasets()
    times = amgr.get_dataset_times()

    if progress:
        progress("Drawing figure...")

    fig = Figure(figsize=(10, 2.2 * len(graph_descs)), tight_layout=True)
    fig.patch.set_facecolor("#0e1116")
    axes = fig.subplots(nrows=len(graph_descs), sharex=True)
    if len(graph_descs) == 1:
        axes = [axes]
    axes[0].set_title(
        f"Motion Analysis — {Path(jsonl_gz_path).name} "
        f"(+{skip:.1f}s, {duration:.1f}s window)",
        color="#e5e9f0",
    )

    font_small = _fm.FontProperties()
    font_small.set_size("x-small")

    for row, ax in zip(graph_descs, axes):
        graph_units = graph_twin_units = None
        twin_ax = None
        for dataset, plot_params in row:
            label = amgr.get_label(dataset)
            target = ax
            if graph_units is None:
                graph_units = label["units"]
                ax.set_ylabel(graph_units, color="#bec4d1")
            elif label["units"] != graph_units:
                if graph_twin_units is None:
                    target = twin_ax = ax.twinx()
                    graph_twin_units = label["units"]
                    twin_ax.set_ylabel(graph_twin_units, color="#bec4d1")
                elif label["units"] == graph_twin_units:
                    target = twin_ax
            pparams = {"label": label["label"], "alpha": 0.85}
            pparams.update(plot_params)
            target.plot(times, datasets[dataset], **pparams)

        ax.set_facecolor("#171c26")
        ax.tick_params(colors="#8b93a7")
        for spine in ax.spines.values():
            spine.set_color("#252b38")
        ax.grid(True, color="#252b38", alpha=0.5)
        if twin_ax is not None:
            twin_ax.tick_params(colors="#8b93a7")
            for spine in twin_ax.spines.values():
                spine.set_color("#252b38")
            li1, la1 = ax.get_legend_handles_labels()
            li2, la2 = twin_ax.get_legend_handles_labels()
            twin_ax.legend(
                li1 + li2, la1 + la2, loc="best", prop=font_small,
                facecolor="#171c26", edgecolor="#252b38", labelcolor="#e5e9f0",
            )
        else:
            ax.legend(
                loc="best", prop=font_small,
                facecolor="#171c26", edgecolor="#252b38", labelcolor="#e5e9f0",
            )

    axes[-1].set_xlabel("Time (s)", color="#bec4d1")

    return MotionAnalysisResult(
        fig=fig, meta=meta, temp_dir=temp_dir,
        skip=skip, duration=duration,
    )
