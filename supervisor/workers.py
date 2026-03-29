"""
Supervisor — Worker lifecycle management.

Multiprocessing workers, worker health, direct chat handling.
Queue operations moved to supervisor.queue.
"""

from __future__ import annotations
import logging
log = logging.getLogger(__name__)

import datetime
import json
import multiprocessing as mp
import os
import pathlib
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from supervisor.state import load_state, append_jsonl
from supervisor import git_ops
from supervisor.message_bus import send_with_budget

# Telegram support
from supervisor.telegram_bridge import TelegramBridge
from supervisor.telegram_forwarder import run_forwarder

# ---------------------------------------------------------------------------


@dataclass
class WorkerInfo:
    """Basic per-worker metadata."""
    id: uuid.UUID
    start_time: float
    process: mp.Process
    killed: bool = False


class WorkerManager:
    """
    Manage multiprocessing workers for long-running task execution.

    Not used for Telegram – Telegram runs in a separate background thread
    via telegram_daemon.TelegramDaemon.
    """

    def __init__(self, max_workers: int = 4):
        self.workers: Dict[uuid.UUID, WorkerInfo] = {}
        self.max_workers = max_workers
        self._lock = threading.Lock()
        self._running = True

    def start_worker(self, target_fn: Any, args: Tuple = (), kwargs: Dict = None) -> uuid.UUID:
        """Start a new worker process and return its ID."""
        kwargs = kwargs or {}
        with self._lock:
            if len(self.workers) >= self.max_workers:
                log.warning("Max workers reached (%d), cannot spawn more", self.max_workers)
                return None

            worker_id = uuid.uuid4()
            proc = mp.Process(
                target=target_fn,
                args=args,
                kwargs=kwargs,
                name=f"worker-{worker_id}"
            )
            proc.start()

            self.workers[worker_id] = WorkerInfo(
                id=worker_id,
                start_time=time.time(),
                process=proc
            )
            log.info("Started worker %s as PID %d", worker_id, proc.pid)
            return worker_id

    def stop_worker(self, worker_id: uuid.UUID) -> bool:
        """Stop a specific worker by ID."""
        with self._lock:
            info = self.workers.get(worker_id)
            if not info:
                return False

            if info.process.is_alive():
                info.process.terminate()
                info.process.join(timeout=5)
                if info.process.is_alive():
                    info.process.kill()
                    info.process.join()

                info.killed = True
                info.process.close()
                log.info("Stopped worker %s", worker_id)
                return True
            return False

    def stop_all(self) -> None:
        """Stop all running workers."""
        with self._lock:
            worker_ids = list(self.workers.keys())
            for worker_id in worker_ids:
                self.stop_worker(worker_id)

    def cleanup_dead_workers(self) -> None:
        """Remove dead worker entries."""
        with self._lock:
            to_remove = []
            for worker_id, info in self.workers.items():
                if not info.process.is_alive():
                    info.process.close()
                    to_remove.append(worker_id)
                    log.info("Worker %s exited naturally", worker_id)

            for worker_id in to_remove:
                del self.workers[worker_id]

    def get_status(self) -> Dict[uuid.UUID, Dict[str, Any]]:
        """Get status of all workers."""
        with self._lock:
            status = {}
            for worker_id, info in self.workers.items():
                status[worker_id] = {
                    "start_time": datetime.datetime.fromtimestamp(info.start_time).isoformat(),
                    "alive": info.process.is_alive(),
                    "pid": info.process.pid if info.process.is_alive() else None,
                    "killed": info.killed
                }
            return status


# Global singleton
_worker_manager: Optional[WorkerManager] = None


def get_manager() -> WorkerManager:
    """Get or create the global worker manager."""
    global _worker_manager
    if _worker_manager is None:
        _worker_manager = Manager(max_workers=4)
    return _worker_manager


def start_telegram_forwarders() -> List[mp.Process]:
    """
    Start Telegram forwarder processes for all enabled Telegram workers.

    Returns list of started processes.
    """
    state = load_state()
    processes = []
    telegram_config = state.get("telegram", {})

    if not telegram_config.get("enabled", False):
        log.info("Telegram not enabled, skipping forwarder")
        return processes

    bot_token = telegram_config.get("bot_token")
    if not bot_token:
        log.warning("Telegram enabled but no bot_token configured")
        return processes

    # Get all target chat IDs (group + optional owner)
    chat_ids = []
    group_chat_id = telegram_config.get("group_chat_id")
    if group_chat_id:
        chat_ids.append(int(group_chat_id))

    owner_chat_id = telegram_config.get("owner_chat_id")
    if owner_chat_id:
        chat_ids.append(int(owner_chat_id))

    if not chat_ids:
        log.warning("Telegram enabled but no chat_ids configured")
        return processes

    log.info("Starting Telegram forwarder for %d chat(s): %s", len(chat_ids), chat_ids)

    # Start a forwarder process
    forwarder_proc = mp.Process(
        target=run_forwarder,
        kwargs={
            "bot_token": bot_token,
            "chat_ids": chat_ids,
            "polling_timeout": telegram_config.get("polling_timeout", 30),
            "max_retries": telegram_config.get("max_retries", 3),
            "retry_delay": telegram_config.get("retry_delay", 5),
        },
        name="telegram-forwarder"
    )
    forwarder_proc.start()
    processes.append(forwarder_proc)
    log.info("Started Telegram forwarder process PID %d", forwarder_proc.pid)

    return processes


def stop_telegram_forwarders(processes: List[mp.Process]) -> None:
    """Stop all Telegram forwarder processes gracefully."""
    for proc in processes:
        if proc.is_alive():
            log.info("Stopping Telegram forwarder PID %d", proc.pid)
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join()

        proc.close()


# Fix: Change Manager to WorkerManager
def get_manager() -> WorkerManager:
    """Get or create the global worker manager."""
    global _worker_manager
    if _worker_manager is None:
        _worker_manager = WorkerManager(max_workers=4)
    return _worker_manager