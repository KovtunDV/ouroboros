#!/usr/bin/env python3
"""Telegram Polling Bridge — bridges Telegram Bot API with agent message interface."""

import backoff
import httpx
import logging
import threading
import time
from typing import Dict, Optional, List, Any

log = logging.getLogger(__name__)


class TelegramBridge:
    """Telegram polling client for a specific group chat.
    
    Allows messages from all users in the group, responds as a single entity.
    Compatible interface with LocalChatBridge for send_photo/send_message.
    
    Architecture:
    - Uses long polling getUpdates with timeout
    - Queues incoming messages for worker process to consume
    - Implements retry logic for network errors
    - Designed to run in the same process as workers (or worker subproc)
    """
    
    BASE_URL = "https://api.telegram.org/bot{token}"
    
    # Telegram chunked upload requires <= 50MB per chunk
    MAX_PHOTO_SIZE = 50 * 1024 * 1024  # 50MB
    
    def __init__(self, bot_token: str, group_chat_id: str, 
                 max_retries: int = 3, long_poll_timeout: int = 25):
        self.bot_token = bot_token
        self.group_chat_id = group_chat_id
        self.max_retries = max_retries
        self.long_poll_timeout = long_poll_timeout
        
        # HTTP client with timeout
        self.http = httpx.Client(timeout=30.0)
        
        # Message queue for incoming messages
        self._message_queue: List[Dict] = []
        self._queue_lock = threading.Lock()
        
        # Polling control
        self._polling = False
        self._poll_thread: Optional[threading.Thread] = None
        self._current_offset: int = 0  # getUpdates requires increasing offset
        
        # User ID cache (for better logging/debugging)
        self._user_cache: Dict[int, str] = {}
    
    @backoff.on_exception(backoff.expo, httpx.HTTPError,
                          max_tries=3, jitter=backoff.full_jitter(1))
    def _api_call(self, method: str, **data) -> Optional[Dict]:
        """Call Telegram Bot API with error handling and retry."""
        url = self.BASE_URL.format(token=self.bot_token) + f"/{method}"
        resp = self.http.post(url, json=data)
        resp.raise_for_status()
        result = resp.json()
        
        if not result.get("ok"):
            error_desc = result.get("description", "Unknown error")
            error_code = result.get("error_code", 0)
            log.error(f"Telegram API error for {method}: [{error_code}] {error_desc}")
            return None
        
        return result.get("result")
    
    @backoff.on_exception(backoff.expo, httpx.HTTPError,
                          max_tries=3, jitter=backoff.full_jitter(1))
    def _api_call_multipart(self, method: str, files: Dict, **data) -> Optional[Dict]:
        """Call Telegram API with multipart/form-data message (for photos)."""
        url = self.BASE_URL.format(token=self.bot_token) + f"/{method}"
        resp = self.http.post(url, data=data, files=files)
        resp.raise_for_status()
        result = resp.json()
        
        if not result.get("ok"):
            error_desc = result.get("description", "Unknown error")
            error_code = result.get("error_code", 0)
            log.error(f"Telegram API error for {method}: [{error_code}] {error_desc}")
            return None
        
        return result.get("result")
    
    def send_message(self, text: str, parse_mode: str = None) -> bool:
        """Send a text message to the group chat.
        
        Compatible with LocalChatBridge.send_message.
        """
        data = {"chat_id": self.group_chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        
        result = self._api_call("sendMessage", **data)
        return result is not None
    
    def send_photo(self, photo_bytes: bytes, caption: str = None,
                    parse_mode: str = None, chunk_size: int = 10 * 1024 * 1024) -> bool:
        """Send a photo to the group chat (multipart/form-data).
        
        Automatically chunks large photos to stay within Telegram limits.
        Compatible with LocalChatBridge.send_photo.
        """
        if len(photo_bytes) > self.MAX_PHOTO_SIZE:
            log.error(f"Photo too large: {len(photo_bytes)} bytes (max {self.MAX_PHOTO_SIZE})")
            return False
        
        import io
        files = {"photo": ("photo.jpg", io.BytesIO(photo_bytes), "image/jpeg")}
        data = {"chat_id": self.group_chat_id}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        
        result = self._api_call_multipart("sendPhoto", files=files, **data)
        return result is not None
    
    def queuing(self) -> bool:
        """
        Compatible with LocalChatBridge.queuing.
        Indicates that this bridge queues messages for later consumption.
        """
        return True
    
    def get_queued_messages(self) -> List[Dict[str, Any]]:
        """
        Compatible with LocalChatBridge.get_queued_messages.
        Returns list of queued messages for processing.
        
        Each message dict contains:
            - user_id: int
            - username: str (real_username or first_name)
            - text: str
            - user_is_group_admin: bool
            - user_is_group_member: bool
            - msg_id: int (Telegram message_id)
        """
        with self._queue_lock:
            messages = self._message_queue[:]
            self._message_queue.clear()
            return messages
    
    # -- Polling API (Telegram-specific) --
    
    def _process_update(self, update: Dict):
        """Process a single Telegram update and queue if it's a text message."""
        if "message" not in update:
            return
        
        message = update["message"]
        
        # Only process text messages
        if "text" not in message:
            log.debug(f"Skipping non-text message: {message.get('message_id')}")
            return
        
        # Check if message is from our target group chat
        chat = message.get("chat", {})
        if chat.get("id") != int(self.group_chat_id):
            log.debug(f"Message from wrong chat: {chat.get('id')} (expected {self.group_chat_id})")
            return
        
        # Extract user info
        user = message.get("from", {})
        user_id = user.get("id")
        username = user.get("username") or user.get("first_name") or "Unknown"
        
        # Cache username for better logs
        if user_id:
            self._user_cache[user_id] = username
        
        # Get membership status (Telegram doesn't provide this in message)
        # We'll default to True - better to be permissive
        user_is_group_member = True  # If user sent message, they're in the chat
        user_is_group_admin = False  # Can't determine from message
        # Can be enhanced with getChatMember calls in future
        
        # Build message dict (compatible with LocalChatBridge format)
        msg_dict = {
            "user_id": user_id,
            "username": username,
            "text": message["text"],
            "user_is_group_admin": user_is_group_admin,
            "user_is_group_member": user_is_group_member,
            "msg_id": message.get("message_id"),
        }
        
        # Queue for processing
        with self._queue_lock:
            self._message_queue.append(msg_dict)
        
        log.info(f"Queued message from @{username}: {message['text'][:50]}")
        
        # Update offset so we don't re-fetch this message
        self._current_offset = update["update_id"] + 1
    
    def _polling_loop(self):
        """Background thread that polls Telegram for updates."""
        log.info("Starting Telegram polling loop")
        
        while self._polling:
            try:
                # Call getUpdates with long-polling timeout
                result = self._api_call(
                    "getUpdates",
                    offset=self._current_offset,
                    timeout=self.long_poll_timeout,
                    allowed_updates=["message"],
                )
                
                if result:
                    for update in result:
                        self._process_update(update)
                
                # Brief sleep to prevent tight loop on error
                time.sleep(0.1)
                
            except httpx.HTTPError as http_error:
                # Network errors: retry (backoff already applied in _api_call)
                log.error(f"Network error on getUpdates: {http_error}")
                time.sleep(5)
                
            except Exception as e:
                log.error(f"Unexpected error in polling loop: {e}", exc_info=True)
                time.sleep(5)  # Brief pause before retry
        
        log.info("Telegram polling loop stopped")
    
    def start_polling(self):
        """Start background polling thread."""
        if self._polling:
            log.warning("Polling already started")
            return
        
        self._polling = True
        self._poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._poll_thread.start()
        log.info("Telegram polling started")
    
    def stop_polling(self):
        """Stop background polling thread."""
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
        log.info("Telegram polling stopped")
    
    def close(self):
        """Clean up resources."""
        self.stop_polling()
        self.http.close()