"""Compatibility surface over chat_read: the EA channel reader for the
briefing. New code imports chat_read."""

try:
    from chat_read import fetch_recent_messages, main, summarize_for_briefing
except ImportError:
    from preprocessor.chat_read import fetch_recent_messages, main, summarize_for_briefing

__all__ = ["fetch_recent_messages", "summarize_for_briefing", "main"]

if __name__ == "__main__":
    import sys
    sys.exit(main())
