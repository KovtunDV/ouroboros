"""
Ouroboros Agent Server — Self-editable entry point.

This file lives in REPO_DIR and can be modified by the agent.
It runs as a subprocess of the launcher, serving the web UI and
coordinating the supervisor/worker system.

Starlette + uvicorn on localhost:{PORT}.
"""

import asyncio
import json
import logging
import os
import pathlib
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from multiprocessing import Process as Proc
from queue import Empty, Queue
from threading import Thread, Event
from typing import Any, Coroutine, Dict, List, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.websockets import WebSocket, WebSocketState
from starlette.middleware.cors import CORSMiddleware

# These imports are from this repo, not from installed packages
from supervisor.state import load_state, save_state, append_jsonl
from supervisor.events import DirectMessage, RequestLog
from supervisor.message_bus import (
    init as bus_init,
    send_with_budget,
    get_budget,
    get_total_budget,
    get_budget_limits,
    get_message_offset,
    advance_message_offset,
    update_budget_from_usage,
    get_chat_bridges,
    get_telegram_bridges,
)

# Directory structure
DATA_DIR = "/home/ouroboros/Ouroboros/data"
REPO_DIR = "/opt/ouroboros/ouroboros"
LOG_DIR = f"{DATA_DIR}/logs"

# Web UI is served from REPO_DIR/docs
DOCS_DIR = os.path.join(REPO_DIR, "docs")

# Cache for memoized values
_memove_cache: dict[str, Any] = {"last_modified": 0}

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s](%(process)d)[%(name)s][%(levelname).1s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger(__name__)


async def health(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse(
        {
            "status": "healthy",
            "time": datetime.now(timezone.utc).isoformat(),
            "drive_root": DATA_DIR,
        }
    )


async def get_state(request: Request) -> JSONResponse:
    """Return current state (excluding sensitive data)."""
    state = load_state()
    # Filter out sensitive data
    if "telegram" in state and "bot_token" in state["telegram"]:
        state_safe = {
            **state,
            "telegram": {**state["telegram"], "bot_token": "***REDACTED***"},
        }
    else:
        state_safe = state
    return JSONResponse(state_safe)


async def api_send(request: Request) -> JSONResponse:
    """Send a direct message to the agent (owner → agent)."""
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        if not text:
            return JSONResponse({"error": "_message_empty"}, status_code=400)

        # Send via message_bus with budget tracking
        try:
            send_with_budget(text, sender_id=None, channel="api")
            return JSONResponse({"status": "queued"})
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=503)

    except Exception as e:
        log.exception("Error in /send")
        return JSONResponse({"error": "internal_error"}, status_code=500)


async def api_events(request: Request) -> JSONResponse:
    """Get events since given offset."""
    try:
        state = load_state()
        offset = int(request.query_params.get("offset", 0))
        current_offset = get_message_offset()

        if offset > current_offset:
            # No new events
            return JSONResponse(
                {
                    "events": [],
                    "offset": current_offset,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            )

        # Read events from chat.jsonl (owner dialogue)
        events: List[Dict[str, Any]] = []
        for line in open(f"{LOG_DIR}/chat.jsonl"):
            try:
                event = json.loads(line)
                seq = int(event.get("seq", 0))
                if seq > offset:
                    events.append(event)
            except (json.JSONDecodeError, ValueError, KeyError):
                continue

        # Update offset to latest read
        advance_message_offset(current_offset)

        return JSONResponse(
            {
                "events": events,
                "offset": current_offset,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

    except Exception as e:
        log.exception("Error in /events")
        return JSONResponse({"error": "internal_error"}, status_code=500)


async def api_budget(request: Request) -> JSONResponse:
    """Get budget information."""
    try:
        info = get_budget_limits()
        return JSONResponse(info)
    except Exception as e:
        log.exception("Error in /budget")
        return JSONResponse({"error": "internal_error"}, status_code=500)


async def api_ssh_hosts(request: Request) -> JSONResponse:
    """Get configured SSH hosts."""
    try:
        state = load_state()
        hosts = state.get("ssh_hosts", {})
        # Filter out private keys for security
        hosts_safe = {
            k: {**v, "private_key_path": "***REDACTED***"}
            if v.get("private_key_path")
            else v
            for k, v in hosts.items()
        }
        return JSONResponse(hosts_safe)
    except Exception as e:
        log.exception("Error in /ssh-hosts")
        return JSONResponse({"error": "internal_error"}, status_code=500)


async def api_update_ssh_host(request: Request) -> JSONResponse:
    """Add or update an SSH host configuration."""
    try:
        data = await request.json()
        host_id = data.get("host_id")
        host = data.get("host")
        port = int(data.get("port", 22))
        username = data.get("username")
        private_key_path = data.get("private_key_path")

        if not host_id or not host or not username:
            return JSONResponse({"error": "missing_fields"}, status_code=400)

        state = load_state()
        if "ssh_hosts" not in state:
            state["ssh_hosts"] = {}

        state["ssh_hosts"][host_id] = {
            "host": host,
            "port": port,
            "username": username,
            "private_key_path": private_key_path,
        }

        save_state(state)
        return JSONResponse({"status": "updated", "host_id": host_id})
    except Exception as e:
        log.exception("Error in /update-ssh-host")
        return JSONResponse({"error": "internal_error"}, status_code=500)


# WebSocket support
_websocket_clients: List[WebSocket] = []
_broadcast_queue: Queue = Queue()


async def ws_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time updates."""
    await websocket.accept()
    _websocket_clients.append(websocket)
    client_id = id(websocket)
    log.info("WebSocket client %d connected", client_id)

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            typ = msg.get("type")

            if typ == "ping":
                await websocket.send_json({"type": "pong"})
            elif typ == "send":
                # Forward to agent as direct message
                text = msg.get("text", "").strip()
                if text:
                    try:
                        send_with_budget(
                            text, sender_id=f"ws:{client_id}", channel="web"
                        )
                    except (ValueError, RuntimeError):
                        await websocket.send_json({"type": "error", "error": "agent_busy"})
            else:
                await websocket.send_json({"type": "error", "error": "unknown_type"})

    except Exception:
        pass  # Connection closed
    finally:
        if websocket in _websocket_clients:
            _websocket_clients.remove(websocket)
        log.info("WebSocket client %d disconnected", client_id)


def broadcast_ws_sync(event: Dict[str, Any]) -> None:
    """Synchronous broadcast to all WebSocket clients (called from worker)."""
    for ws in _websocket_clients.copy():
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_json(event), _event_loop
            ).result(timeout=1.0)
        except Exception:
            _websocket_clients.remove(ws)


def _broadcast_loop() -> None:
    """Background thread: broadcast events to WebSocket clients."""
    global _event_loop

    while True:
        try:
            event = _broadcast_queue.get(timeout=1.0)
            broadcast_ws_sync(event)
        except Empty:
            continue
        except Exception as e:
            log.error("Error in broadcast loop: %s", e)


# Routes
routes = [
    Route("/health", health),
    Route("/api/state", get_state, methods=["GET"]),
    Route("/api/send", api_send, methods=["POST"]),
    Route("/api/events", api_events, methods=["GET"]),
    Route("/api/budget", api_budget, methods=["GET"]),
    Route("/api/ssh-hosts", api_ssh_hosts, methods=["GET"]),
    Route("/api/update-ssh-host", api_update_ssh_host, methods=["POST"]),
    Route("/ws", ws_endpoint),
]

app = Starlette(
    routes=routes,
    lifespan=Lifespan(
        on_startup=[
            _start_supervisor,
            _start_broadcast_loop,
            _start_telegram_forwarders,
        ],
        on_shutdown=[
            _stop_supervisor,
            _stop_broadcast_loop,
            _stop_telegram_forwarders,
        ],
    ),
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Globals
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_telegram_forwarder_processes: List[Proc] = []  # Global: Telegram forwarder processes


def _run_supervisor(settings: dict) -> None:
    """Initialize and run the supervisor loop. Called in a background thread."""
    global _telegram_forwarder_processes

    _apply_settings_to_env(settings)

    try:
        from supervisor.message_bus import init as bus_init
        from supervisor.message_bus import LocalChatBridge

        bridge = LocalChatBridge()
        bridge._broadcast_fn = broadcast_ws_sync

        from ouroboros.utils import set_log_sink
        set_log_sink(bridge.push_log)

        bus_init(
            drive_root=DATA_DIR,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 1000.0)),
            budget_report_every=10,
            chat_bridge=bridge,
        )

        from supervisor.state import init as state_init, init_state, rotate_chat_log_if_needed
        state_init(DATA_DIR, float(settings.get("TOTAL_BUDGET", 1000.0)))
        init_state()

        # Telegram forwarder initialization
        global _telegram_forwarder_processes
        try:
            from supervisor.workers import start_telegram_forwarders
            _telegram_forwarder_processes = start_telegram_forwarders()
            if _telegram_forwarder_processes:
                log.info("Started Telegram forwarder processes")
        except Exception as e:
            log.error("Failed to start Telegram forwarder: %s", e, exc_info=True)

        from supervisor.git_ops import init as git_ops_init, ensure_repo_present, safe_restart
        git_ops_init(
            repo_dir=REPO_DIR, drive_root=DATA_DIR, remote_url="",
            branch_dev="ouroboros", branch_stable="ouroboros-stable",
        )

        _checkpoint_thread = Thread(target=_checkpoint_loop, daemon=True)
        _checkpoint_thread.start()

        from supervisor.runners import run_supervisor_loop
        run_supervisor_loop()
    except Exception as e:
        log.error("Supervisor initialization failed: %s", e, exc_info=True)


def _start_supervisor(app: Starlette) -> Coroutine[Any, Any, None]:
    """Lifespan callback: start supervisor thread."""

    async def inner() -> None:
        global _event_loop
        _event_loop = asyncio.get_running_loop()

        # Load settings from environment
        settings = {
            "TOTAL_BUDGET": int(os.getenv("TOTAL_BUDGET", "1000")),
            "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
        }

        # Start supervisor in a background thread
        _supervisor_thread = Thread(target=_run_supervisor, args=(settings,), daemon=True)
        _supervisor_thread.start()

    return inner()


def _stop_supervisor(app: Starlette) -> Coroutine[Any, Any, None]:
    """Lifespan callback: stop supervisor and Telegram forwarders."""

    async def inner() -> None:
        global _telegram_forwarder_processes

        # Stop Telegram forwarders
        if _telegram_forwarder_processes:
            from supervisor.workers import stop_telegram_forwarders
            try:
                stop_telegram_forwarders(_telegram_forwarder_processes)
                log.info("Telegram forwarders stopped")
            except Exception:
                pass
            _telegram_forwarder_processes = []

        # Kill workers
        try:
            from supervisor.workers import kill_workers
            kill_workers(force=True)
        except Exception:
            pass

    return inner()


def _start_broadcast_loop(app: Starlette) -> Coroutine[Any, Any, None]:
    """Lifespan callback: start WebSocket broadcast loop."""

    async def inner() -> None:
        _broadcast_thread = Thread(target=_broadcast_loop, daemon=True)
        _broadcast_thread.start()

    return inner()


def _stop_broadcast_loop(app: Starlette) -> Coroutine[Any, Any, None]:
    """Lifespan callback: stop WebSocket broadcast loop."""

    async def inner() -> None:
        pass  # Thread is daemon, will be cleaned up on exit

    return inner()


def _start_telegram_forwarders(app: Starlette) -> Coroutine[Any, Any, None]:
    """Lifespan callback: start Telegram forwarder processes."""
    # Already done in _run_supervisor()
    async def inner() -> None:
        pass

    return inner()


def _stop_telegram_forwarders(app: Starlette) -> Coroutine[Any, Any, None]:
    """Lifespan callback: stop Telegram forwarder processes."""
    # Already done in _stop_supervisor()
    async def inner() -> None:
        pass

    return inner()


def _apply_settings_to_env(settings: dict) -> None:
    """Apply settings to environment variables."""
    for k, v in settings.items():
        os.environ[k] = str(v)


def _checkpoint_loop() -> None:
    """Periodically rotate log files."""
    while True:
        time.sleep(300)  # 5 minutes
        try:
            from supervisor.state import rotate_chat_log_if_needed
            rotate_chat_log_if_needed()
        except Exception as e:
            log.error("Failed to rotate logs: %s", e)


def main() -> None:
    """Entry point: start the Starlette server."""
    # Ensure data directories exist
    for d in [DATA_DIR, LOG_DIR, f"{DATA_DIR}/state", f"{DATA_DIR}/memory"]:
        pathlib.Path(d).mkdir(parents=True, exist_ok=True)

    # Ensure index.html exists in docs
    if not os.path.exists(os.path.join(DOCS_DIR, "index.html")):
        log.warning("No index.html found in %s", DOCS_DIR)

    port = int(os.getenv("PORT", "8000"))
    log.info("Starting server on port %d, serving docs from %s", port, DOCS_DIR)

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()