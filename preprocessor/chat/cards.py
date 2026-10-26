"""Moved to botcore.chat.cards; this name is the same module object."""
import sys

from . import _botcore_path  # noqa: F401
from botcore.chat import cards as _m

sys.modules[__name__] = _m
