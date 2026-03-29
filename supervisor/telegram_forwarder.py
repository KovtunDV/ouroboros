"""
Telegram Message Forwarder — Brdige Telegram-messages into the local chat flow.

Runs as a separate worker process. Handles:
- Long polling from Telegram
- Forwarding messages to message_bus (as local user 1)
- Sending responses from message_bus back to Telegram
"""

from __future__ import annotations
import logging
log = logging.getLogger(__name__)

import sys
import time
import queue
import threading
from typing import Dict, Optional

# Import TelegramBridge for API access
from supervisor.telegram_bridge import TelegramBridge


class TelegramForwarder:
    """Bridge between Telegram and local message_bus."""
    
    def __init__(
        self,
        bot_token: str,
        chat_id: int,
        local_owner_id: int = 1,
        local_chat_id: int = 1,
        polling_timeout: int = 30,
        max_retries: int = 3,
        retry_delay: int = 5,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.local_owner_id = local_owner_id
        self.local_chat_id = local_chat_id
        self.polling_timeout = polling_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        self.bridge = TelegramBridge(bot_token, chat_id)
        
        # Queue for outgoing messages
        self.outgoing_queue: queue.Queue[tuple[int, str]] = queue.Queue()
        
        # Control flags
        self._running = False
        self._stop_event = threading.Event()
        
        # Track offset for polling
        self.update_offset = 0
        
        log.info(
            f"TelegramForwarder initialized: bot_token=***, chat_id={chat_id}, "
            f"local_owner_id={local_owner_id}, local_chat_id={local_chat_id}"
        )
    
    def _get_user_id(self, telegram_user_id: int) -> int:
        """Map Telegram user_id to local owner_id."""
        # For now: all Telegram users map to local owner_id 1
        return self.local_owner_id
    
    def _polling_loop(self) -> None:
        """Main polling loop in background thread."""
        log.info("Telegram polling loop started")
        
        while not self._stop_event.is_set():
            try:
                # Get updates with long polling
                updates = self.bridge.get_updates(offset=self.update_offset or 1, timeout=self.polling_timeout)
                
                for update in updates:
                    update_id = int(update.get("update_id", 0))
                    self.update_offset = max(self.update_offset, update_id + 1)
                    
                    # Extract message
                    message = update.get("message", {})
                    if not message:
                        continue
                    
                    # Get user and text
                    from_user = message.get("from", {})
                    telegram_user_id = int(from_user.get("id", 0))
                    text = message.get("text", "")
                    
                    if not text:
                        continue
                    
                    # Log incoming message
                    log.info(
                        f"Telegram message received: user={telegram_user_id}, "
                        f"chat={self.chat_id}, text={text[:50]}..."
                    )
                    
                    # Put in outgoing queue (format: (local_user_id, local_chat_id, text))
                    self.outgoing_queue.put((self.local_owner_id, self.local_chat_id, text))
                    
            except Exception as e:
                log.error(f"Telegram polling error: {e}", exc_info=True)
                time.sleep(self.retry_delay)
        
        log.info("Telegram polling loop stopped")
    
    def get_queued_messages(self) -> list[tuple[int, int, str]]:
        """Get all queued messages (user_id, chat_id, text)."""
        messages = []
        while not self.outgoing_queue.empty():
            try:
                msg = self.outgoing_queue.get_nowait()
                messages.append(msg)
            except queue.Empty:
                break
        return messages
    
    def send_response(self, message: str) -> bool:
        """Send response to Telegram chat."""
        try:
            success = self.bridge.send_message(self.chat_id, message)
            if success:
                log.info(f"Response sent to Telegram chat {self.chat_id}")
                return True
            else:
                log.error(f"Failed to send response to Telegram chat {self.chat_id}")
                return False
        except Exception as e:
            log.error(f"Error sending Telegram response: {e}", exc_info=True)
            return False
    
    def start(self) -> None:
        """Start the forwarder."""
        if self._running:
            log.warning("TelegramForwarder already running")
            return
        
        self._running = True
        self._stop_event.clear()
        
        # Start polling thread
        self._polling_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._polling_thread.start()
        
        log.info("TelegramForwarder started")
    
    def stop(self) -> None:
        """Stop the forwarder."""
        if not self._running:
            return
        
        self._running = False
        self._stop_event.set()
        
        # Wait for polling thread to finish
        if hasattr(self, "_polling_thread"):
            self._polling_thread.join(timeout=5)
        
        log.info("TelegramForwarder stopped")


def run_forwarder(
    bot_token: str,
    chat_id: int,
    local_owner_id: int = 1,
    local_chat_id: int = 1,
    polling_timeout: int = 30,
    max_retries: int = 3,
    retry_delay: int = 5,
) -> TelegramForwarder:
    """
    Run Telegram forwarder in main loop.
    
    Returns the forwarder instance for control.
    """
    forwarder = TelegramForwarder(
        bot_token=bot_token,
        chat_id=chat_id,
        local_owner_id=local_owner_id,
        local_chat_id=local_chat_id,
        polling_timeout=polling_timeout,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )
    
    try:
        forwarder.start()
        
        # Run until stopped
        while forwarder._running:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("TelegramForwarder interrupted by user")
    finally:
        forwarder.stop()
    
    return forwarder


if __name__ == "__main__":
    import dotenv
    import os
    
    # Load environment
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    dotenv.load_dotenv(env_path)
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    
    if not bot_token or chat_id == 0:
        sys.stderr.write("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required\n")
        sys.exit(1)
    
    run_forwarder(
        bot_token=bot_token,
        chat_id=chat_id,
        polling_timeout=30,
    )