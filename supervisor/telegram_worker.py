#!/usr/bin/env python3
"""Telegram Worker — worker process for handling Telegram messages."""

import logging
import os
import sys
import json
import threading
import time
from typing import Dict, Optional

# Add repo to path
repo_dir = os.environ.get("OUROBOROS_REPO_DIR", os.path.dirname(os.path.dirname(__file__)))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

from supervisor.telegram_bridge import TelegramBridge
from supervisor.telegram_dispatcher import TelegramDispatcher

log = logging.getLogger(__name__)


def main():
    """Main worker process."""
    import signal
    import sys
    
    log.info("Telegram worker started")
    
    # Load configuration from environment (set by workers.py)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    group_chat_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID")
    
    if not bot_token or not group_chat_id:
        log.error("Telegram worker: missing TELEGRAM_BOT_TOKEN or TELEGRAM_GROUP_CHAT_ID")
        sys.exit(1)
    
    # Create Telegram bridge
    bridge = TelegramBridge(
        bot_token=bot_token,
        group_chat_id=group_chat_id,
        max_retries=3,
        long_poll_timeout=25
    )
    
    # Create dispatcher
    dispatcher = TelegramDispatcher(bridge=bridge, poll_interval=0.5)
    
    # Start polling
    bridge.start_polling()
    
    # Start dispatcher
    dispatcher.start()
    
    log.info(f"Telegram worker listening to group: {group_chat_id}")
    
    # Keep running until killed
    # The parent (supervisor/workers.py) will SIGTERM us
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        dispatcher.stop()
        bridge.stop_polling()
        bridge.close()
        log.info("Telegram worker stopped")


if __name__ == "__main__":
    main()