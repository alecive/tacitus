"""Lives in botcore.chat.shadow_frontend; this name is the same module object."""
import sys

from . import _botcore_path  # noqa: F401
from botcore.chat import shadow_frontend as _m

sys.modules[__name__] = _m
