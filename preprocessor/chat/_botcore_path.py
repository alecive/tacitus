"""Puts the in-repo botcore package on sys.path (no install needed)."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2] / "botcore")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
