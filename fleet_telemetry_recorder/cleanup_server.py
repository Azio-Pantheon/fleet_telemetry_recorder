"""Tiny HTTP server that lets fleet_daemon request post-archive deletion of
a specific telemetry file. Exists because Moonraker's `logs` file root is
read-only by default (`register_data_folder("logs")` without
`full_access=True`), so fleet_daemon can't DELETE via Moonraker.

Binds to ``FTR_CLEANUP_HOST:FTR_CLEANUP_PORT`` (defaults 0.0.0.0:7130).
No authentication — same trust model as Moonraker's HTTP port on the LAN.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from aiohttp import web

from . import config

log = logging.getLogger("fleet_telemetry_recorder.cleanup")

# Accept only filenames we ourselves produce: <jobid>__<stub>.jsonl.gz,
# unresolved__<ts>__<stub>.jsonl.gz, crash__<ts>__<stub>.jsonl.gz.
# No directory separators. No ".." segments. No leading dot.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*\.jsonl\.gz$")


async def _handle_delete(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    if not _SAFE_NAME.match(name):
        log.warning(f"[cleanup] rejected bad name: {name!r}")
        return web.json_response({"error": "invalid filename"}, status=400)

    log_dir: Path = config.LOG_DIR.resolve()
    candidate = (log_dir / name).resolve()
    # Defense in depth — candidate must be inside log_dir.
    try:
        candidate.relative_to(log_dir)
    except ValueError:
        log.warning(f"[cleanup] path-traversal attempt: {name!r}")
        return web.json_response({"error": "invalid path"}, status=400)

    if not candidate.exists():
        return web.json_response({"error": "not found"}, status=404)

    try:
        candidate.unlink()
    except Exception as e:   # noqa: BLE001
        log.error(f"[cleanup] unlink failed for {name}: {e}")
        return web.json_response({"error": str(e)}, status=500)

    log.info(f"[cleanup] deleted {name} after fleet_daemon archive confirm")
    return web.json_response({"status": "deleted", "filename": name})


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def start_server() -> web.AppRunner:
    """Start the cleanup HTTP server and return its AppRunner (so the caller
    can keep a reference to shut it down cleanly). If CLEANUP_PORT is 0 the
    server is disabled."""
    if config.CLEANUP_PORT <= 0:
        log.info("[cleanup] disabled (FTR_CLEANUP_PORT=0)")
        return None

    app = web.Application()
    app.add_routes([
        web.delete("/telemetry/{name}", _handle_delete),
        web.get("/health", _handle_health),
    ])

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=config.CLEANUP_HOST, port=config.CLEANUP_PORT)
    await site.start()
    log.info(
        f"[cleanup] listening on {config.CLEANUP_HOST}:{config.CLEANUP_PORT} "
        f"— DELETE /telemetry/{{name}} to remove archived files"
    )
    return runner
