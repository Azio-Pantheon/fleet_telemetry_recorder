"""
Convert fleet-telemetry `.jsonl.gz` files into Klipper motan's two-file log
format (`.json.gz` + `.index.gz`) so motan's analyzer pipeline can run
against them.

Motan format details (reverse-engineered from motan/data_logger.py and
motan/readlog.py):

  Both `.json.gz` and `.index.gz` are raw zlib streams with windowBits=31
  (gzip-compatible). Messages are serialized as JSON then joined by b'\\x03'
  framing bytes. The `.json.gz` file holds the webhook message stream; each
  frame looks like `{"q":"trapq:<name>","params":{"data":[...moves...]}}`.
  The `.index.gz` file holds periodic `get_status()` checkpoints; we write a
  single checkpoint with `status.toolhead.estimated_print_time` (used as the
  analysis time origin) plus a `subscriptions` dict enumerating the
  subscriptions present in the log.

Only trapq events are converted — motan's motion analyzers (velocity,
acceleration, jerk, kinematics) are exactly what trapq enables. Status
events are lossy in our format (forward-filled), so we skip them; motan's
status-field analyzers aren't useful here.
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path
from typing import Optional


class _MotanLogWriter:
    """Byte-compatible replica of motan/data_logger.py's LogWriter.

    Uses Z_BEST_SPEED (level 1) — the output file is a temp scratch
    consumed by motan's readlog and then discarded, so compression ratio
    matters much less than throughput (roughly 5× faster encode than
    Z_DEFAULT_COMPRESSION).
    """

    def __init__(self, filename: str) -> None:
        self.file = open(filename, "wb")
        self.comp = zlib.compressobj(
            zlib.Z_BEST_SPEED, zlib.DEFLATED, 31
        )

    def write_msg(self, raw: bytes) -> None:
        d = self.comp.compress(raw + b"\x03")
        if d:
            self.file.write(d)

    def close(self) -> None:
        self.file.write(self.comp.flush(zlib.Z_FINISH))
        self.file.close()


def _extract_first_number(line: str, start: int) -> Optional[float]:
    """Read a numeric literal starting at `start`, stop at next comma/bracket."""
    end = start
    n = len(line)
    while end < n and line[end] not in ",]":
        end += 1
    try:
        return float(line[start:end])
    except ValueError:
        return None


def convert_to_motan(
    jsonl_gz_path: str, out_prefix: str, progress=None,
) -> dict:
    """Read our `.jsonl.gz`, write `{out_prefix}.json.gz` and
    `{out_prefix}.index.gz` in motan's format.

    Hot path uses string slicing instead of json.loads/json.dumps — a full
    JSON round-trip on ~50k × 2 KB messages dominates the conversion cost.
    Our lines are produced by our own recorder with a deterministic
    `{"t":…,"kind":"trapq","q":"NAME","data":[[…]]}` shape, so direct
    substring extraction is safe.

    Returns:
      - trapq_names: sorted list of trapq names present in the log
      - first_print_time: Klipper print_time of the first move (analysis origin)
      - last_print_time: approx Klipper print_time of the last move
      - trapq_event_count: number of trapq events written
    """
    trapq_names: set[str] = set()
    first_pt: Optional[float] = None
    last_pt: Optional[float] = None
    trapq_events = 0

    # Pre-encoded prefix/suffix to reduce string concatenation work per line.
    PREFIX = b'{"q":"trapq:'
    MID    = b'","params":{'
    SUFFIX = b'}}'

    json_writer = _MotanLogWriter(out_prefix + ".json.gz")
    try:
        with gzip.open(jsonl_gz_path, "rt", encoding="utf-8") as f:
            for line in f:
                # Fast skip on non-trapq lines.
                if '"kind":"trapq"' not in line:
                    continue
                # Extract q name: find `"q":"` then next `"`.
                q_idx = line.find('"q":"')
                if q_idx < 0:
                    continue
                q_start = q_idx + 5
                q_end = line.find('"', q_start)
                if q_end < 0:
                    continue
                name = line[q_start:q_end]
                # Extract data section: from `"data":` to just before the
                # outer object's closing `}`. We strip a trailing newline
                # so rindex("}") hits the true outer closer.
                d_idx = line.find('"data":', q_end)
                if d_idx < 0:
                    continue
                line_s = line.rstrip()
                last_brace = line_s.rfind("}")
                if last_brace < 0 or last_brace <= d_idx:
                    continue
                data_kv = line_s[d_idx:last_brace]   # `"data":[[...]]`

                # First-move print_time — digits right after `"data":[[`.
                if first_pt is None:
                    dd = data_kv.find("[[")
                    if dd >= 0:
                        first_pt = _extract_first_number(data_kv, dd + 2)

                # Approximate last_print_time: the first scalar of the LAST
                # move in this event. Between-moves separator is `]],[`
                # (inner `[dx,dy,dz]` closes with `]`, then the outer move
                # closes with `]`, comma, then `[` opens the next move).
                # The lone `],[` pattern appears *inside* a single move
                # between its two inner arrays, so matching that gives
                # wrong results.
                pos = data_kv.rfind("]],[")
                if pos >= 0:
                    v = _extract_first_number(data_kv, pos + 4)
                    if v is not None and (last_pt is None or v > last_pt):
                        last_pt = v
                else:
                    # Single-move event: fall back to its first scalar.
                    dd = data_kv.find("[[")
                    if dd >= 0:
                        v = _extract_first_number(data_kv, dd + 2)
                        if v is not None and (last_pt is None or v > last_pt):
                            last_pt = v

                trapq_names.add(name)
                trapq_events += 1

                # Build motan message by direct concatenation — no JSON
                # re-encoding of the data payload.
                json_writer.write_msg(
                    PREFIX + name.encode("utf-8") + MID
                    + data_kv.encode("utf-8") + SUFFIX
                )

                if progress is not None and (trapq_events & 0x3FFF) == 0:
                    progress(trapq_events)
    finally:
        json_writer.close()

    if not trapq_names:
        raise RuntimeError(
            "no trapq events found — motion analysis requires "
            "FTR_INCLUDE_TRAPQ=1 (default) on the recorder side"
        )
    if first_pt is None:
        first_pt = 0.0
    if last_pt is None:
        last_pt = first_pt + 1.0

    # Write the one-message index file. motan's LogManager.setup_index reads
    # exactly one message here — status + subscriptions + file_position.
    # seek_time() will then ask for additional messages to advance the
    # snapshot; since we only have one, it stops at that, which is fine for
    # whole-file analysis.
    subs = {f"trapq:{n}": {"name": n} for n in trapq_names}
    initial = {
        "status": {
            "toolhead": {
                "estimated_print_time": first_pt,
            },
        },
        "subscriptions": subs,
        "file_position": 0,
    }
    idx_writer = _MotanLogWriter(out_prefix + ".index.gz")
    try:
        idx_writer.write_msg(
            json.dumps(initial, separators=(",", ":")).encode("utf-8")
        )
    finally:
        idx_writer.close()

    return {
        "trapq_names": sorted(trapq_names),
        "first_print_time": first_pt,
        "last_print_time": last_pt,
        "trapq_event_count": trapq_events,
    }
