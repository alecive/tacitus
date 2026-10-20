#!/usr/bin/env python3
"""The bot's old name. The chat bot is ea-chat-bot.py; this file keeps a
system unit or a script that still names ea-slack-bot.py working until
deploy/rename-chat-bot.sh has moved the unit over. It replaces itself
with the real bot, so the process shows the new name."""
import os
import sys
from pathlib import Path

os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve().with_name("ea-chat-bot.py"))] + sys.argv[1:])
