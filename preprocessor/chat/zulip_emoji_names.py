"""Moved to botcore.chat.zulip_emoji_names; this name is the same module object."""
import sys

from . import _botcore_path  # noqa: F401
from botcore.chat import zulip_emoji_names as _m

sys.modules[__name__] = _m
