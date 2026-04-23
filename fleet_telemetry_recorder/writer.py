"""Per-print gzipped JSONL writer.

File naming lifecycle:
  1. On print start: open ".inprogress__{timestamp}__{stub}.jsonl.gz"
  2. On print end  : close and rename to "{moonraker_job_id}__{stub}.jsonl.gz"
  3. If the process crashes mid-print: on next startup, leftover
     ".inprogress__*" files are renamed to "{timestamp}__crash.jsonl.gz" so
     fleet_daemon's sweep eventually marks those jobs 'unavailable'.

The writer stays small: gzip file + append one JSON object per line.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("fleet_telemetry_recorder.writer")

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_stub(filename: Optional[str]) -> str:
    """Make a print filename safe for use as a file-name stub."""
    if not filename:
        return "print"
    # Strip directory and extension
    base = filename.rsplit("/", 1)[-1]
    base = base.rsplit(".", 1)[0]
    safe = _SAFE_RE.sub("_", base)[:80].strip("_")
    return safe or "print"


class PrintLogWriter:
    def __init__(
        self,
        log_dir: Path,
        filename: Optional[str],
        printer_hostname: str,
        klippy_version: Optional[str] = None,
    ) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir
        self.filename = filename
        self.stub = _sanitize_stub(filename)
        self.started_at = time.time()
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
        # Leading dot makes the in-progress file easy to spot and ignore.
        self.inprogress_path = log_dir / f".inprogress__{ts}__{self.stub}.jsonl.gz"
        self._fh = gzip.open(self.inprogress_path, "wt", encoding="utf-8")
        self._closed = False

        self._write_raw({
            "kind": "header",
            "printer": printer_hostname,
            "klippy_version": klippy_version or "",
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)
            ),
            "filename": filename or "",
        })
        log.info(f"[writer] opened {self.inprogress_path.name}")

    def _write_raw(self, obj: dict) -> None:
        if self._closed:
            return
        try:
            self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except Exception as e:
            log.warning(f"[writer] write failed: {e}")

    def write_event(self, kind: str, payload: dict) -> None:
        """Append one event. `payload` must already contain the useful fields;
        we add `t` (seconds since print start) and `kind`."""
        if self._closed:
            return
        rec = {"t": round(time.time() - self.started_at, 3), "kind": kind}
        rec.update(payload)
        self._write_raw(rec)

    def close_and_finalize(
        self, moonraker_job_id: Optional[str]
    ) -> Optional[Path]:
        """Close the gzip file and rename it to its final form.

        If `moonraker_job_id` is None (couldn't be resolved), the file is
        renamed to a timestamp-based name anyway so crash-recovery doesn't
        later interpret it as abandoned."""
        if self._closed:
            return None
        self._closed = True
        try:
            self._fh.close()
        except Exception as e:
            log.warning(f"[writer] close failed: {e}")

        if moonraker_job_id:
            final = self.log_dir / f"{moonraker_job_id}__{self.stub}.jsonl.gz"
        else:
            ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
            final = self.log_dir / f"unresolved__{ts}__{self.stub}.jsonl.gz"
        try:
            self.inprogress_path.rename(final)
            log.info(f"[writer] finalized -> {final.name}")
            return final
        except Exception as e:
            log.error(f"[writer] rename failed: {e}")
            return None
