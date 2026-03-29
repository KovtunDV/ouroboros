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

# ---------------------------------------------------------------------------