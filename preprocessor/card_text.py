"""Moved to botcore.chat.card_text; this name is the same module object."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1] / "botcore")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from botcore.chat import card_text as _m  # noqa: E402

sys.modules[__name__] = _m
