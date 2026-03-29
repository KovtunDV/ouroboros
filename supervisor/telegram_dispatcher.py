#!/usr/bin/env python3
"""Telegram Dispatcher — reads queued messages and forwards to agent."""

import logging
import os
import sys
import threading
import time

# Add repo to path
repo_dir = os.environ.get("OUROBOROS_REPO_DIR", os.path.dirname(os.path.dirname(__file__)))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from supervisor.telegram_bridge import TelegramBridge

log = logging.getLogger(__name__)


class TelegramDispatcher:
    """Dispatcher that forwards Telegram messages to the agent.
    
    Runs in a background thread, polling TelegramBridge.get_queued_messages()
    at regular intervals and forwarding messages via handle_chat_direct().
    
    Architecture:
    - TelegramBridge (in telegram_worker process) does HTTP polling
    - Queues messages internally with metadata (user_id, username, etc.)
    - TelegramDispatcher (in supervisor process) reads queue and forwards
    - Responses go back via TelegramBridge.send_message/send_photo
    """

    def __init__(self, bridge: TelegramBridge, poll_interval: float = 0.5):
        """Initialize dispatcher with a TelegramBridge instance.
        
        Args:
            bridge: TelegramBridge instance (must have polling started)
            poll_interval: how often to check for new messages (default 0.5s)
        """
        self.bridge = bridge
        self.poll_interval = poll_interval
        
        # Control flags
        self._running = False
        self._thread: threading.Thread = None
        
        # Import handle_chat_direct from workers.py
        # We need to do this at runtime, not at module level, to avoid circular imports
        self._handle_chat_direct = None
        
    def _import_handler(self):
        """Import handle_chat_direct from workers.py (lazy import)."""
        if self._handle_chat_direct is None:
            from supervisor.workers import handle_chat_direct
            self._handle_chat_direct = handle_chat_direct
            
    def _dispatch_loop(self):
        """Main dispatch loop: poll for messages and forward to agent."""
        log.info("Telegram dispatcher loop started")
        
        while self._running:
            try:
                # Get queued messages from TelegramBridge
                messages = self.bridge.get_queued_messages()
                
                if messages:
                    log.info(f"Dispatching {len(messages)} Telegram message(s)")
                    
                for msg in messages:
                    self._dispatch_message(msg)
                
                # Sleep before next poll
                time.sleep(self.poll_interval)
                
            except Exception as e:
                log.error(f"Error in dispatch loop: {e}", exc_info=True)
                time.sleep(1)  # Brief pause on error
        
        log.info("Telegram dispatcher loop stopped")
    
    def _dispatch_message(self, msg: dict):
        """Dispatch a single message to the agent.
        
        Args:
            msg: dict with keys (user_id, username, text, msg_id, etc.)
        """
        self._import_handler()
        
        user_id = msg.get("user_id")
        username = msg.get("username", "Unknown")
        text = msg.get("text", "")
        msg_id = msg.get("msg_id")
        
        log.info(f"Dispatching message from @{username} (user_id={user_id}, msg_id={msg_id})")
        
        try:
            # Forward to agent via handle_chat_direct
            # We use the same chat_id for all messages in the group
            chat_id = int(self.bridge.group_chat_id)
            
            # Prefix message with username for context
            prefixed_text = f"@{username}: {text}"
            
            self._handle_chat_direct(
                chat_id=chat_id,
                text=prefixed_text,
                image_data=None
            )
            
        except Exception as e:
            log.error(f"Error dispatching message: {e}", exc_info=True)
    
    def start(self):
        """Start the dispatcher background thread."""
        if self._running:
            log.warning("Telegram dispatcher already running")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._thread.start()
        log.info("Telegram dispatcher started")
    
    def stop(self):
        """Stop the dispatcher background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        log.info("Telegram dispatcher stopped")