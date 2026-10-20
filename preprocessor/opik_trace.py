"""
Opik tracing integration for EA system.

Provides trace/span helpers used by:
  - run-agent.sh (via CLI: python3 opik_trace.py record-briefing ...)
  - ea-chat-bot.py (inline: trace_slack(...))
  - tests/evals/run_evals.py (inline: trace_eval(...))

Traces are project-scoped:
  - "ea-briefings" for cron-scheduled runs
  - "ea-slack" for real-time Slack interactions
  - "ea-evals" for eval suite runs

Graceful degradation: if Opik is unavailable (not installed, server
down, misconfigured), all functions silently return None. Tracing
never blocks the bot.

Usage (CLI):
  python3 preprocessor/opik_trace.py record-briefing \\
      --agent morning-briefing --briefing-file ~/.ea/state/briefing-latest.json \\
      --output "..." --duration 45.2 --model sonnet --validation ok

Usage (Python):
  from preprocessor.opik_trace import trace_slack, trace_eval
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

_client = None
_available = None  # None = not checked yet


def _get_client(project_name: str = "ea-briefings"):
    """Get or create the Opik client. Returns None if unavailable."""
    global _client, _available
    if _available is False:
        return None
    if _client is not None:
        return _client
    try:
        import opik
        if not _server_reachable(opik):
            log.info("Opik server not reachable; tracing is off for this process")
            _available = False
            return None
        _client = opik.Opik(project_name=project_name)
        _available = True
        return _client
    except Exception as e:
        log.debug(f"Opik unavailable: {e}")
        _available = False
        return None


def _server_reachable(opik_mod, timeout_s: float = 1.0) -> bool:
    """One TCP connect to the configured Opik server. The SDK's client
    queues every trace and retries a dead server for the life of the
    process, logging each failure, so a server that is down turns tracing
    off for the process instead. An unreadable config is left to the SDK."""
    import socket
    from urllib.parse import urlsplit
    try:
        url = str(opik_mod.config.OpikConfig().url_override or "")
    except Exception:
        return True
    parts = urlsplit(url)
    if not parts.hostname:
        return True
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def trace_briefing(agent: str, briefing_data: dict, output: str,
                   duration_s: float, model: str, validation: str,
                   tool_calls: list | None = None) -> str | None:
    """Record a cron briefing run as an Opik trace.

    Returns trace_id on success, None on failure.
    """
    client = _get_client("ea-briefings")
    if not client:
        return None

    try:
        delta = briefing_data.get("delta_summary", {})
        trace = client.trace(
            name=f"{agent} {time.strftime('%Y-%m-%d %H:%M')}",
            input={
                "agent": agent,
                "new_inbox": delta.get("new_inbox_messages", 0),
                "total_delta": delta.get("total_delta_items", 0),
                "needs_response": len(briefing_data.get("new_needs_response", [])),
                "dropped_threads": len(briefing_data.get("new_dropped_threads", [])),
                "scheduling_requests": len(briefing_data.get("new_scheduling_requests", [])),
            },
            output={
                "text_preview": output[:500] if output else "(empty)",
                "text_length": len(output) if output else 0,
                "validation": validation,
            },
            metadata={
                "model": model,
                "duration_s": round(duration_s, 2),
                "tool_call_count": len(tool_calls) if tool_calls else 0,
            },
            tags=[agent],
        )

        trace.span(
            name="preprocessor",
            input={"mode": briefing_data.get("mode", "unknown")},
            output={
                "new_inbox": delta.get("new_inbox_messages", 0),
                "total_delta": delta.get("total_delta_items", 0),
                "events_next_14d": delta.get("events_next_14d", 0),
            },
        )

        trace.span(
            name="claude-execution",
            input={"model": model},
            output={
                "text_length": len(output) if output else 0,
                "tool_calls": [tc.get("tool", "") for tc in (tool_calls or [])],
            },
            metadata={"duration_s": round(duration_s, 2)},
        )

        trace.span(
            name="validation",
            input={"agent": agent},
            output={"result": validation},
        )

        trace.end()
        return trace.id
    except Exception as e:
        log.debug(f"Opik trace_briefing failed: {e}")
        return None


def trace_slack(msg: str, is_thread: bool, skills: list[str],
                modules: set[str], complexity: str, use_mcp: bool,
                dispatch_path: str, output: str, duration_s: float,
                model: str) -> str | None:
    """Record a Slack bot interaction as an Opik trace.

    Returns trace_id on success, None on failure.
    """
    client = _get_client("ea-slack")
    if not client:
        return None

    try:
        tags = [complexity]
        tags.extend(sorted(modules))
        if skills:
            tags.extend(skills)

        trace = client.trace(
            name=f"slack: {msg[:60]}",
            input={
                "message": msg[:200],
                "is_thread_reply": is_thread,
            },
            output={
                "text_preview": output[:500] if output else "(empty)",
                "text_length": len(output) if output else 0,
                "dispatch_path": dispatch_path,
            },
            metadata={
                "model": model,
                "duration_s": round(duration_s, 2),
                "complexity": complexity,
                "needs_mcp": use_mcp,
                "skills": skills,
                "modules": sorted(modules),
            },
            tags=tags,
        )

        trace.span(
            name="routing",
            input={"message": msg[:200]},
            output={
                "skills": skills,
                "modules": sorted(modules),
                "complexity": complexity,
                "needs_mcp": use_mcp,
            },
        )

        trace.span(
            name="claude-execution",
            input={"dispatch_path": dispatch_path, "model": model},
            output={"text_length": len(output) if output else 0},
            metadata={"duration_s": round(duration_s, 2)},
        )

        trace.end()
        return trace.id
    except Exception as e:
        log.debug(f"Opik trace_slack failed: {e}")
        return None


def trace_eval(fixture_info: dict, assertion_results: list,
               output: str, mode: str) -> str | None:
    """Record an eval run with feedback scores.

    Each assertion becomes a feedback score on the trace, visible
    in the Opik dashboard for quality tracking over time.

    Returns trace_id on success, None on failure.
    """
    client = _get_client("ea-evals")
    if not client:
        return None

    try:
        trace = client.trace(
            name=f"eval: {fixture_info.get('name', fixture_info.get('id', 'unknown'))}",
            input={
                "fixture_id": fixture_info.get("id", ""),
                "fixture_name": fixture_info.get("name", ""),
                "category": fixture_info.get("category", ""),
                "mode": mode,
            },
            output={
                "text_length": len(output) if output else 0,
                "output_preview": output[:300] if output else "(empty)",
                "all_pass": all(r.get("pass", False) for r in assertion_results),
                "assertion_count": len(assertion_results),
            },
            metadata={
                "mode": mode,
                "fixture_id": fixture_info.get("id", ""),
                "category": fixture_info.get("category", ""),
                "bug_ref": fixture_info.get("bug_ref", ""),
            },
            tags=[fixture_info.get("category", "unknown"), mode],
        )

        for ar in assertion_results:
            trace.log_feedback_score(
                name=ar.get("assertion", "unknown"),
                value=ar.get("score", 0.0),
                reason=ar.get("reason", ""),
                category_name=fixture_info.get("category", "unknown"),
            )

        trace.end()
        return trace.id
    except Exception as e:
        log.debug(f"Opik trace_eval failed: {e}")
        return None


def _cmd_record_briefing(args):
    """CLI handler for record-briefing subcommand."""
    briefing_data = {}
    if args.briefing_file and Path(args.briefing_file).exists():
        try:
            briefing_data = json.loads(Path(args.briefing_file).read_text())
        except (json.JSONDecodeError, OSError):
            pass

    trace_id = trace_briefing(
        agent=args.agent,
        briefing_data=briefing_data,
        output=args.output or "",
        duration_s=args.duration or 0.0,
        model=args.model or "sonnet",
        validation=args.validation or "unknown",
    )

    if trace_id:
        print(f"Opik trace logged: {trace_id}")
    else:
        print("Opik trace skipped (unavailable or failed)")


def main():
    parser = argparse.ArgumentParser(description="Opik tracing for EA system")
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("record-briefing", help="Record a briefing run")
    rec.add_argument("--agent", required=True, help="Agent name (morning-briefing, etc.)")
    rec.add_argument("--briefing-file", help="Path to briefing JSON")
    rec.add_argument("--output", help="Claude output text")
    rec.add_argument("--duration", type=float, help="Duration in seconds")
    rec.add_argument("--model", default="sonnet", help="Model used")
    rec.add_argument("--validation", default="ok", help="Validation result")
    rec.set_defaults(func=_cmd_record_briefing)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
