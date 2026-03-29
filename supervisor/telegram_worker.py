#!/usr/bin/env python3
"""Telegram worker process — bridges Telegram polling with agent."""

import logging
import os
import pathlib
import sys
import threading
import time
import uuid
from typing import Optional

log = logging.getLogger(__name__)


def telegram_worker_main(bridge_token: str, chat_id: str,
                        repo_dir: str, drive_root: str,
                        event_q) -> None:
    """
    Worker process that polls Telegram and forwards messages to the agent.
    
    Architecture:
    - Runs telegram_bridge.start_polling() in this process
    - For incoming messages: inject into agent via handle_chat_direct()
    - For outgoing messages: agent responds via LocalChatBridge, we don't double-send to Telegram
    """
    
    # Add repo to path
    if not getattr(sys, 'frozen', False):
        sys.path.insert(0, repo_dir)
    
    from supervisor.telegram_bridge import TelegramBridge
    from supervisor.workers import _get_chat_agent, handle_chat_direct
    from supervisor.state import load_state
    
    drive = pathlib.Path(drive_root)
    
    # Initialize Telegram bridge
    log.info(f"Starting Telegram worker for chat_id={chat_id}")
    bridge = TelegramBridge(bridge_token, chat_id)
    
    # Send "я родился !" message
    max_retries = 3
    for attempt in range(max_retries):
        if bridge.send_message("я родился ! 🐍"):
            log.info("Telegram worker boot message sent successfully")
            break
        else:
            log.warning(f"Failed to send boot message (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(2)
    else:
        log.error("Failed to send boot message after all retries")
    
    # Start polling in background thread
    def polling_loop():
        try:
            bridge.start_polling()
        except Exception as e:
            log.error(f"Polling loop crashed: {e}", exc_info=True)
    
    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()
    
    # Message forward loop
    log.info("Telegram worker message forward loop started")
    
    while True:
        try:
            # Check for queued messages from Telegram
            messages = bridge.get_queued_messages()
            
            for msg in messages:
                # Forward to agent as if it came from the local chat
                # Note: chat_id from Telegram is mapped to owner_chat_id
                text = msg['text'].strip()
                if not text:
                    continue
                
                log.info(f"Forwarding Telegram message from @{msg['username']}: {text[:50]}")
                
                # Load state to get proper chat_id
                st = load_state()
                owner_chat_id = st.get('owner_chat_id') or 1
                
                # Forward to agent's direct chat handler
                handle_chat_direct(owner_chat_id, text)
                
                # Log event
                if event_q:
                    event_q.put({
                        "type": "telegram_message",
                        "user_id": msg.get('user_id'),
                        "username": msg.get('username'),
                        "text": text,
                    })
            
            # Sleep briefly between checks
            time.sleep(0.5)
            
        except KeyboardInterrupt:
            log.info("Telegram worker received KeyboardInterrupt, shutting down")
            bridge.stop_polling()
            break
            
        except Exception as e:
            log.error(f"Error in Telegram worker loop: {e}", exc_info=True)
            time.sleep(5)  # Brief pause before retry


if __name__ == "__main__":
    import multiprocessing as mp
    
    # Test mode
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    repo = os.environ.get('OUROBOROS_REPO_DIR', str(pathlib.Path.home() / "Ouroboros" / "repo"))
    data = os.environ.get('OUROBOROS_DRIVE_ROOT', str(pathlib.Path.home() / "Ouroboros" / "data"))
    
    if not token or not chat_id:
        print("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars required")
        print("Usage: export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...; python -m supervisor.telegram_worker")
        sys.exit(1)
    
    # Run in current process for testing
    telegram_worker_main(token, chat_id, repo, data, None)