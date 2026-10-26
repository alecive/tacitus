"""Moved to botcore.chat.files; this name is the same module object."""
import sys

from . import _botcore_path  # noqa: F401
from botcore.chat import files as _m

sys.modules[__name__] = _m
