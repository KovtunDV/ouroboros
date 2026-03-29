#!/usr/bin/env python3
"""Telegram daemon process – runs as separate background worker.

This module integrates TelegramBridge with the Ouroboros supervisor system.
It runs as a daemon process, polling for.telegram messages and routing them
through the message_bus, then sending responses back to Telegram.
"""
import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Add repo to path for imports
repo_dir = "/opt/ouroboros/ouroboros"
sys.path.insert(0, repo_dir)

from supervisor.telegram_bridge import TelegramBridge
from supervisor.message_bus import log_chat

log = logging.getLogger("telegram_daemon")


class TelegramDaemon:
    """Telegram polling daemon that integrates with message_bus.

    This daemon:
    1. Polls for incoming Telegram messages
    2. Logs them through message_bus.log_chat()
    3. Sends responses back to Telegram
    """

    def __init__(
        self,
        bot_token: str,
        group_chat_id: int,
        send_queue: "queue.Queue[Dict[str, Any]]",
        control_q: Optional["queue.Queue[str]] = None,
    ):
        self.bot_token = bot_token
        self.group_chat_id = group_chat_id
        self.send_queue = send_queue
        self.control_q = control_q
        self.bridge = TelegramBridge(bot_token, group_chat_id)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def _poll_loop(self) -> None:
        """Main polling loop – runs in background thread."""
        offset = 0
        while self._running:
            try:
                updates = self.bridge.get_updates(offset=offset, timeout=5)
                for upd in updates:
                    offset = int(upd["update_id"]) + 1
                    msg = upd.get("message") or {}
                    if not msg:
                        continue

                    chat_id = int(msg.get("chat", {}).get("id", self.group_chat_id))
                    user_id = int(msg.get("from", {}).get("id", 0))
                    text = msg.get("text") or ""

                    if not text:
                        continue

                    # Log incoming message through message_bus
                    log_chat("in", chat_id, user_id, text)

                    # Put in send_queue for processing by supervisor
                    self.send_queue.put(
                        {
                            "type": "telegram_message",
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "text": text,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )

            except Exception as exc:
                log.error("Telegram polling error: %s", exc, exc_info=True)
                time.sleep(5)  # Back off on error

    def start(self) -> str:
        """Start polling in background thread."""
        if self._running:
            return "telegram_daemon: already running"

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="telegram_poll",
            daemon=True,
        )
        self._thread.start()
        log.info("Telegram daemon started (chat_id=%d)", self.group_chat_id)
        return "telegram_daemon: started"

    def stop(self) -> str:
        """Stop polling."""
        if not self._running:
            return "telegram_daemon: not running"

        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("Telegram daemon stopped")
        return "telegram_daemon: stopped"

    def send_message(self, text: str) -> bool:
        """Send text message to Telegram."""
        try:
            result = self.bridge.send_message(self.group_chat_id, text)
            if result.get("ok"):
                log_chat("out", self.group_chat_id, 0, text)
                return True
            else:
                log.error("Telegram send failed: %s", result)
                return False
        except Exception as exc:
            log.error("Telegram send error: %s", exc, exc_info=True)
            return False

    def send_photo(self, photo_base64: str, caption: Optional[str] = None) -> bool:
        """Send photo to Telegram."""
        try:
            result = self.bridge.send_photo(self.group_chat_id, photo_base64, caption)
            if result.get("ok"):
                log_chat("out", self.group_chat_id, 0, f"[photo: {caption or 'no caption'}]")
                return True
            else:
                log.error("Telegram send photo failed: %s", result)
                return False
        except Exception as exc:
            log.error("Telegram send photo error: %s", exc, exc_info=True)
            return False


def main() -> int:
    """Entry point for standalone testing."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Use test queue for standalone mode
    send_queue: queue.Queue[Dict[str, Any]] = queue.Queue()

    # TODO: Load from config/state
    daemon = TelegramDaemon(
        bot_token="YOUR_BOT_TOKEN",
        group_chat_id=-5014744032,
        send_queue=send_queue,
    )

    daemon.start()

    # Process messages from queue (in real supervisor, this happens in main loop)
    try:
        while True:
            msg = send_queue.get()
            print(f"Received: {msg}")
            # Echo back for testing
            if msg.get("type") == "telegram_message":
                daemon.send_message(f"Echo: {msg['text']}")
    except KeyboardInterrupt:
        daemon.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())