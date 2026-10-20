#!/usr/bin/env python3
"""Compatibility entry point for chat_upload.py (same flags, same output).
The LLM grant and older skills still name this file."""

import sys

try:
    from chat_upload import main
except ImportError:
    from preprocessor.chat_upload import main

if __name__ == "__main__":
    sys.exit(main())
