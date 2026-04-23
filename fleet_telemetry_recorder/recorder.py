"""Main recorder service.

Connects to the local Klippy webhook unix socket, subscribes to motion +
temperature + print_stats objects (and optionally motion_report/dump_trapq),
and writes one gzipped JSONL file per print. Files are stored under
`~/printer_data/logs/telemetry/` so Moonraker serves them via
`/server/files/logs/telemetry/...` — the central fleet_daemon pulls them
after the print completes.

Pattern borrowed conceptually from klipper/scripts/motan/data_logger.py;
reimplemented here with asyncio + our on-print-boundary file rotation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

from . import config
from .writer import PrintLogWriter

log = logging.getLogger("fleet_telemetry_recorder")

ETX = b"\x03"

TERMINAL_STATES = {"complete", "cancelled", "error", "standby"}
PRINTING_STATES = {"printing", "paused"}  # paused stays in the same file


class KlippySocket:
    """Thin asyncio wrapper around Klippy's webhook unix socket.

    Protocol: JSON messages separated by \\x03 bytes. Queries carry an
    `id` and get one `result` (or `error`) reply. Subscribes get a
    single acknowledge reply plus repeated async pushes keyed by `q`.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        # Retry connect until Klipper is up. Fresh boots can take 20–60s.
        delay = 1.0
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self.path)
                log.info(f"[sock] connected to {self.path}")
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                log.debug(f"[sock] connect failed ({e}); retrying in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 10.0)

    async def send(self, msg: dict) -> None:
        assert self._writer is not None
        data = json.dumps(msg, separators=(",", ":")).encode() + ETX
        self._writer.write(data)
        await self._writer.drain()

    async def messages(self):
        """Async iterator yielding decoded JSON messages."""
        assert self._reader is not None
        buf = b""
        while True:
            chunk = await self._reader.read(8192)
            if not chunk:
                return
            buf += chunk
            while ETX in buf:
                part, buf = buf.split(ETX, 1)
                if not part:
                    continue
                try:
                    yield json.loads(part)
                except json.JSONDecodeError:
                    log.warning(f"[sock] malformed message discarded ({len(part)}B)")

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None


class Recorder:
    def __init__(self) -> None:
        self.sock = KlippySocket(config.KLIPPY_SOCKET)
        self.hostname = socket.gethostname()
        self.klippy_version: str = ""
        self._query_futures: dict[str, asyncio.Future] = {}
        self._query_seq = 0

        # Current print state
        self._writer: Optional[PrintLogWriter] = None
        self._last_print_state: Optional[str] = None
        self._last_filename: Optional[str] = None

        # Subscribed trapq names (for log keying)
        self._trapqs: list[str] = []

    # ------------------------------------------------------------------
    # Query / response plumbing
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._query_seq += 1
        return f"{prefix}:{self._query_seq}"

    async def _query(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        mid = self._next_id("q")
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._query_futures[mid] = fut
        await self.sock.send({"id": mid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._query_futures.pop(mid, None)

    async def _subscribe(self, q_tag: str, method: str, params: dict) -> None:
        # Klippy subscribes: the reply + every push both carry `q: q_tag`.
        params = dict(params)
        params["response_template"] = {"q": q_tag}
        mid = self._next_id("s")
        await self.sock.send({"id": mid, "method": method, "params": params})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        await self._recover_crash_files()

        # Prune loop runs independently of the socket lifecycle.
        asyncio.create_task(self._prune_loop(), name="ftr.prune")

        # Reconnect-forever outer loop. Klipper restarts are routine
        # (firmware flash, config reload) and our service must follow.
        while True:
            try:
                await self._session_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"[main] session crashed: {e}", exc_info=True)
            # Best-effort: close any open writer so we don't leave a
            # half-written gzip if we lost Klipper mid-print.
            if self._writer:
                self._writer.close_and_finalize(moonraker_job_id=None)
                self._writer = None
                self._last_print_state = None
            await self.sock.close()
            await asyncio.sleep(2.0)

    async def _session_once(self) -> None:
        """One connection lifetime against Klippy."""
        await self.sock.connect()

        # Wait for Klippy "ready". `info` returns state=startup/ready/error/shutdown.
        while True:
            info = await self._query("info", {"client_info": {
                "program": "fleet_telemetry_recorder",
                "version": "0.1.0",
            }})
            state = (info.get("result") or {}).get("state", "")
            self.klippy_version = (info.get("result") or {}).get("software_version", "")
            if state == "ready":
                break
            log.info(f"[main] klippy state={state}, waiting...")
            await asyncio.sleep(2.0)

        # Enumerate objects and build our subscription set.
        obj_list_resp = await self._query("objects/list", {})
        all_objects: list[str] = (obj_list_resp.get("result") or {}).get("objects", [])
        subscribe_set: dict[str, None] = {}
        for name in all_objects:
            for prefix in config.BASELINE_OBJECT_PREFIXES:
                if name == prefix.rstrip() or name.startswith(prefix):
                    subscribe_set[name] = None
                    break
        if not subscribe_set:
            log.warning("[main] no matching objects to subscribe")
            return
        log.info(f"[main] subscribing to {len(subscribe_set)} objects")

        # Subscribe baseline. The initial status snapshot arrives as the
        # subscribe ack; we handle it in _handle_subscribe_ack.
        await self._subscribe(
            "status", "objects/subscribe", {"objects": subscribe_set}
        )

        # Drain messages; the handler will fulfil status_resp_fut on first reply.
        async for msg in self.sock.messages():
            await self._handle_message(msg)

        log.warning("[main] klippy socket closed")

    async def _handle_message(self, msg: dict) -> None:
        q_tag = msg.get("q")
        if q_tag is not None:
            # Async push for a subscription
            await self._handle_async(q_tag, msg.get("params", {}) or {})
            return
        mid = msg.get("id")
        if mid and mid in self._query_futures:
            fut = self._query_futures[mid]
            if not fut.done():
                fut.set_result(msg)
            return
        # Subscribe acknowledge (id="s:N") — carries initial snapshot.
        if mid and isinstance(mid, str) and mid.startswith("s:"):
            await self._handle_subscribe_ack(msg)
            return

    async def _handle_subscribe_ack(self, msg: dict) -> None:
        result = msg.get("result") or {}
        status = result.get("status") or {}
        # First-time ack for objects/subscribe: harvest the trapq list and
        # optionally subscribe to per-trapq dumps.
        if not self._trapqs and config.INCLUDE_TRAPQ:
            motion_report = status.get("motion_report", {}) or {}
            trapqs = motion_report.get("trapq") or []
            for q in trapqs:
                tag = f"trapq:{q}"
                self._trapqs.append(q)
                try:
                    await self._subscribe(
                        tag, "motion_report/dump_trapq", {"name": q}
                    )
                    log.info(f"[main] subscribed to dump_trapq for {q}")
                except Exception as e:
                    log.warning(f"[main] dump_trapq subscribe failed for {q}: {e}")

        # Also treat the ack as a first status push so we pick up an already-
        # running print on recorder restart.
        if status:
            await self._on_status_update(status)

    async def _handle_async(self, q_tag: str, params: dict) -> None:
        if q_tag == "status":
            await self._on_status_update(params.get("status", {}) or {})
            return
        if q_tag.startswith("trapq:") and self._writer is not None:
            self._writer.write_event("trapq", {
                "q": q_tag.split(":", 1)[1],
                "data": params.get("data"),
            })

    # ------------------------------------------------------------------
    # Print-state tracking
    # ------------------------------------------------------------------

    async def _on_status_update(self, status: dict) -> None:
        ps = status.get("print_stats") or {}
        state = ps.get("state")
        filename = ps.get("filename")

        # Track the most recent filename we've seen so we can name the file
        # even if print_stats.state transitions before filename updates.
        if filename:
            self._last_filename = filename

        # State transitions
        if state and state != self._last_print_state:
            prev = self._last_print_state
            self._last_print_state = state
            log.info(f"[print] state: {prev} -> {state}")

            if state in PRINTING_STATES and self._writer is None:
                # Start
                self._writer = PrintLogWriter(
                    log_dir=config.LOG_DIR,
                    filename=self._last_filename,
                    printer_hostname=self.hostname,
                    klippy_version=self.klippy_version,
                )
            elif state in TERMINAL_STATES and self._writer is not None:
                # End — resolve job_id, close, rename
                writer = self._writer
                self._writer = None
                asyncio.create_task(
                    self._finalize(writer, filename=self._last_filename),
                    name="ftr.finalize",
                )

        # Regardless of transition, log one `status` event per update so
        # we capture the full stream (temps change every ~1s, motion_report
        # updates ~every 250ms, etc).
        if self._writer is not None:
            self._writer.write_event("status", self._project_status(status))

    def _project_status(self, status: dict) -> dict:
        """Flatten the big status dict to the fields we actually care about."""
        out: dict[str, Any] = {}
        th = status.get("toolhead")
        if th is not None:
            out["th"] = {
                "pos": th.get("position"),
                "homed": th.get("homed_axes"),
            }
        mr = status.get("motion_report")
        if mr is not None:
            out["mr"] = {
                "pos": mr.get("live_position"),
                "vel": mr.get("live_velocity"),
                "evel": mr.get("live_extruder_velocity"),
            }
        ex = status.get("extruder")
        if ex is not None:
            out["ex"] = {
                "T": ex.get("temperature"),
                "set": ex.get("target"),
                "pwr": ex.get("power"),
            }
        bed = status.get("heater_bed")
        if bed is not None:
            out["bed"] = {
                "T": bed.get("temperature"),
                "set": bed.get("target"),
                "pwr": bed.get("power"),
            }
        fan = status.get("fan")
        if fan is not None:
            out["fan"] = fan.get("speed")
        ps = status.get("print_stats")
        if ps is not None:
            out["ps"] = {
                "state": ps.get("state"),
                "pd": ps.get("print_duration"),
                "fil": ps.get("filament_used"),
                "layer": (ps.get("info") or {}).get("current_layer"),
            }
        # Generic extras — all temperature_sensor / temperature_fan / heater_fan
        extras: dict[str, Any] = {}
        for k, v in status.items():
            if not isinstance(v, dict):
                continue
            if k.startswith(("temperature_sensor ", "temperature_fan ",
                             "heater_fan ", "controller_fan ")):
                entry = {}
                if "temperature" in v:
                    entry["T"] = v["temperature"]
                if "speed" in v:
                    entry["spd"] = v["speed"]
                if entry:
                    extras[k] = entry
        if extras:
            out["extras"] = extras
        return out

    # ------------------------------------------------------------------
    # Finalize (resolve moonraker_job_id, rename)
    # ------------------------------------------------------------------

    async def _finalize(self, writer: PrintLogWriter, filename: Optional[str]) -> None:
        job_id = await self._resolve_job_id(filename, started_at=writer.started_at)
        writer.close_and_finalize(moonraker_job_id=job_id)

    async def _resolve_job_id(
        self, filename: Optional[str], started_at: float
    ) -> Optional[str]:
        """Poll Moonraker's history for the just-ended job and return its id.

        Moonraker writes history on print end; give it a few seconds of
        retries to handle order-of-events jitter."""
        url = config.MOONRAKER_URL.rstrip("/") + "/server/history/list"
        for attempt in range(10):
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        url, params={"limit": 5, "order": "desc"}
                    ) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(2.0)
                            continue
                        data = await resp.json()
                        jobs = (data.get("result") or {}).get("jobs") or []
                        best = self._pick_job(jobs, filename, started_at)
                        if best:
                            jid = str(best.get("job_id") or "")
                            if jid:
                                log.info(
                                    f"[finalize] resolved job_id={jid} for "
                                    f"{filename} (after {attempt} retries)"
                                )
                                return jid
            except Exception as e:
                log.debug(f"[finalize] history fetch error: {e}")
            await asyncio.sleep(2.0)
        log.warning(
            f"[finalize] could not resolve job_id for {filename} — file will be "
            "named 'unresolved__...' and flagged by fleet_daemon after grace"
        )
        return None

    def _pick_job(
        self, jobs: list, filename: Optional[str], started_at: float
    ) -> Optional[dict]:
        """Pick the job that matches our filename with the closest start_time."""
        best = None
        best_delta = 1e9
        for j in jobs:
            if filename and j.get("filename") != filename:
                continue
            jst = j.get("start_time") or 0
            delta = abs(jst - started_at)
            # Must be reasonably close — allow 5 minutes of skew.
            if delta < best_delta and delta < 300:
                best = j
                best_delta = delta
        return best

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    async def _recover_crash_files(self) -> None:
        """On startup, finalize any `.inprogress*` files left by a crashed
        prior run. They get a timestamp-based name so fleet_daemon's sweep
        will eventually mark the corresponding job 'unavailable' (no job_id
        prefix means it won't match any fleet_print_history row)."""
        try:
            for path in config.LOG_DIR.glob(".inprogress__*.jsonl.gz"):
                new = path.with_name(
                    path.name.replace(".inprogress__", "crash__").lstrip(".")
                )
                try:
                    path.rename(new)
                    log.warning(f"[recover] finalized abandoned file -> {new.name}")
                except Exception as e:
                    log.warning(f"[recover] rename failed for {path.name}: {e}")
        except Exception as e:
            log.warning(f"[recover] sweep failed: {e}")

    async def _prune_loop(self) -> None:
        """Delete files older than PRUNE_DAYS. Runs once on start then daily."""
        while True:
            try:
                cutoff = time.time() - config.PRUNE_DAYS * 86400
                pruned = 0
                for path in config.LOG_DIR.glob("*.jsonl.gz"):
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                            pruned += 1
                    except Exception:
                        pass
                if pruned:
                    log.info(f"[prune] removed {pruned} files older than {config.PRUNE_DAYS}d")
            except Exception as e:
                log.warning(f"[prune] error: {e}")
            await asyncio.sleep(config.PRUNE_INTERVAL_SECS)


async def _amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    os.nice(10)  # don't steal cycles from klippy
    r = Recorder()
    try:
        await r.run()
    except asyncio.CancelledError:
        pass


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
