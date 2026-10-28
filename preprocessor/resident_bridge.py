"""Bridge between the Slack bot and the resident tmux Claude pane.

The resident pane (scripts/ea-resident-pane.sh) hosts ONE long-lived
interactive Claude Code session as ea-bot — the ORCHESTRATOR. It is the
same continuous-conversation experience as a human Claude Code session,
billed against the Max subscription rather than API tokens. The owner can
attach (`tmux attach -t ea-resident` / ~/attach-ea.sh) and watch or type
into the very conversation the EA uses.

Protocol:

  DISPATCH IN  Every Slack message is injected IMMEDIATELY via tmux
               bracketed paste, prefixed with a machine-stable marker:
                   [slack-dispatch thread=<thread_ts> id=<msg_ts>]
               If the orchestrator is mid-turn, the TUI queues the
               input as the next user message — no lock-waiting, no
               spillover. The send itself holds a short flock only so
               two pastes can't interleave keystrokes.

  REPLY OUT    The transcript JSONL is read via `sudo -n -u ea-bot
               tail` (files are 0600). The reply to dispatch <id> is
               the turn-final assistant text in the SEGMENT between
               the user entry containing marker <id> and the next
               [slack-dispatch ...] user entry (or quiescent EOF).

  ASYNC OUT    Long tasks are handed to background subagents by the
               orchestrator (Agent tool). Their completion reports are
               written as a normal turn whose text STARTS with
                   [deliver:<thread_ts>]
               The bot's router thread tails the transcript and posts
               any unseen [deliver:] report to its Slack thread. The
               tag is for async reports ONLY — direct replies are
               matched positionally by segment.

Parallelism model: short tasks run serially in the conversation (they
are fast); long tasks run as background subagents while the
orchestrator stays responsive to new dispatches. The per-thread
--resume path is the fallback for pane-down only, plus threads already
sticky to it.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
try:
    import atomic_io as _atomic
    import owner_trust as _owner_trust
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import resident_io as _rio
import time
from pathlib import Path

# The pane's session record: the pane script writes it as the resident, so
# it stays in the resident's state dir and is read with the resident's
# access.
STATE_FILE = Path("/home/ea-bot/.ea/state/resident-pane.json")
# The bridge's own state and locks: read and written only by owner
# processes (the bot, the owner's crons), so they live in the owner-private
# dir, out of the resident's reach.
_PRIVATE = _owner_trust.private_dir()
SEND_LOCK_FILE = _PRIVATE / "resident-pane.lock"
ROUTER_STATE_FILE = _PRIVATE / "resident-router.json"
PENDING_FILE = _PRIVATE / "resident-pending.json"
INFLIGHT_FILE = _PRIVATE / "resident-inflight.json"
# An earlier version kept the router offsets and the pending registry in
# the EA state dir: that copy is read once, as the resident, when the
# private file does not exist yet, and taken only in the shape the bridge
# writes (see _legacy).

# The resident is ONE serial conversation: two foreground turns at once
# cross, and a dispatch injected mid-turn is woven into the running turn.
# Only ONE foreground turn may hold the resident at a time. A colliding
# same-thread follow-up queues for it; a new thread spills to the
# per-thread lane. Stale claims (a turn that timed out and is being
# delivered late by the router) age out after INFLIGHT_TTL so the lane
# never wedges shut.
INFLIGHT_TTL = 600.0

# Rotation: a session that never ends pays for its whole history on
# EVERY turn, so a large transcript makes routine replies crawl.
# Two tiers, continuity first:
#   - The conversation PERSISTS across days for memory continuity.
#   - `ea-resident-pane.sh rotate-daily` (a quiet-hour cron on an install
#     that runs the pane) resets it only when it has grown past ~300KB.
#   - This mid-day cap is the safety net: if a busy day blows past it
#     before that reset, maybe_rotate() resets before the next dispatch.
# With chat on per-topic sessions (ea-config `chat_sessions`) the bot
# dispatches nothing here; `resident_mode: false` in ea-config.json turns
# the pane and its sentinel checks off, and style_analysis posts its
# digest directly.
# Cross-day memory that must survive a reset lives in the ledger and the
# briefing; in-conversation context loss at rotation is the accepted cost
# of staying fast.
ROTATE_MAX_BYTES = 500_000
# Deferring rotation on a thread follow-up (see ea-chat-bot) protects live
# context, but not without bound: above this size a whole working day in one
# thread would stay slow, so rotate even mid-thread and eat one cold start.
ROTATE_HARD_MAX_BYTES = 2 * ROTATE_MAX_BYTES
PENDING_EXPIRE_S = 12 * 3600
DISPATCH_LEAD = "Slack dispatch from the user, act on it: "
PANE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ea-resident-pane.sh"
TRANSCRIPT_DIR = Path("/home/ea-bot/.claude/projects/-home-ea-bot-ea-system")
TMUX_TARGET = "ea-resident"

QUIESCENT_S = 3.0
POLL_S = 2.0
# A fresh session writes its transcript with its first message, so right
# after a rotation the file does not exist until the paste is submitted.
TRANSCRIPT_APPEAR_S = 60
DEFAULT_TIMEOUT_S = 420

DISPATCH_MARKER_PREFIX = "[slack-dispatch "
_DELIVER_RE = re.compile(r"^\s*\[deliver:([^\s\]]+)\]\s*", re.DOTALL)


def make_marker(thread_ts: str, msg_ts: str) -> str:
    return f"[slack-dispatch thread={thread_ts} id={msg_ts}]"


_MARKER_ID_RE = re.compile(re.escape(DISPATCH_MARKER_PREFIX) + r"thread=\S+ id=([^\]\s]+)\]")


def _newer_id(other: str, dispatch_id: str) -> bool:
    """Whether dispatch `other` was sent after `dispatch_id`. Message ids
    and Slack timestamps both grow with time; ids that are not numbers
    have no order, so any other id counts as newer."""
    try:
        return float(other) > float(dispatch_id)
    except ValueError:
        return other != dispatch_id


def _later_dispatch(text: str, dispatch_id: str) -> bool:
    """True when `text` carries the marker of a dispatch sent after this
    one, the only thing that ends this dispatch's segment. The model's own
    tools can print markers (the conversation index shows each thread's
    last dispatch), so this dispatch's marker or an older one never does."""
    return any(_newer_id(other, dispatch_id) for other in _MARKER_ID_RE.findall(text))


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """resident_mode flag from ea-config.json (default ON)."""
    try:
        import ea_config
        return bool(ea_config.load().get("resident_mode", True))
    except Exception:
        return True


def _state() -> dict:
    try:
        data = json.loads(_rio.read_text(STATE_FILE) or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _lock(path: Path):
    """An open file for flock on an owner-private lock file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                 | getattr(os, "O_CLOEXEC", 0), 0o600)
    return os.fdopen(fd, "r+")


def _read_private(path: Path):
    """The parsed JSON of an owner-private state file; None when missing,
    {} when unreadable."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return {}


def _legacy(name: str):
    """The parsed JSON of the copy an earlier version kept in the state
    dir, read as the resident; None when there is none."""
    try:
        return json.loads(_rio.read_text(_owner_trust.ea_dir() / "state" / name) or "null")
    except Exception:
        return None


def _valid_router(data) -> dict:
    """Router state in the shape the bridge writes: an int offset and a
    list of posted uuids."""
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    if isinstance(data.get("offset"), int) and data["offset"] >= 0:
        out["offset"] = data["offset"]
    posted = data.get("posted")
    if isinstance(posted, list):
        out["posted"] = [u for u in posted if isinstance(u, str)][-300:]
    return out


def _valid_pending(data) -> dict:
    """Pending entries in the shape register_pending writes."""
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for did, meta in data.items():
        if not (isinstance(did, str) and isinstance(meta, dict)):
            continue
        if not (isinstance(meta.get("thread_ts"), str)
                and isinstance(meta.get("channel"), str)):
            continue
        try:
            out[did] = {"thread_ts": meta["thread_ts"], "channel": meta["channel"],
                        "start_offset": int(meta.get("start_offset") or 0),
                        "journal_offset": int(meta.get("journal_offset") or 0),
                        "created": float(meta.get("created") or 0)}
        except (TypeError, ValueError):
            continue
    return out


def transcript_path() -> Path | None:
    sid = _state().get("session_id")
    return (TRANSCRIPT_DIR / f"{sid}.jsonl") if sid else None


def session_id() -> str | None:
    """Current resident session id (None if unknown / mid-rotation).
    The smart-context tracker keys on this so rotation auto-resets it."""
    return _state().get("session_id")


# ---------------------------------------------------------------------------
# Foreground in-flight claim (serialize the single resident conversation)
# ---------------------------------------------------------------------------

def _inflight_fresh(data: dict) -> bool:
    try:
        return (time.time() - float(data.get("ts", 0))) < INFLIGHT_TTL
    except Exception:
        return False


def inflight_thread() -> str:
    """The thread holding a fresh foreground claim, or ''."""
    try:
        cur = json.loads(INFLIGHT_FILE.read_text())
    except Exception:
        return ""
    return str(cur.get("thread_ts") or "") if cur and _inflight_fresh(cur) else ""


def claim_inflight(thread_ts: str) -> bool:
    """Atomically claim the resident's single foreground slot. Returns
    True if claimed (caller may dispatch), False if another turn holds a
    FRESH claim (caller must queue or spill). A stale claim (> INFLIGHT_TTL,
    i.e. a timed-out turn) is taken over."""
    try:
        with _lock(SEND_LOCK_FILE.with_suffix(".inflight-lock")) as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                cur = json.loads(INFLIGHT_FILE.read_text())
            except Exception:
                cur = {}
            if cur and _inflight_fresh(cur):
                return False
            _atomic.write_json(INFLIGHT_FILE, {"thread_ts": thread_ts,
                                                "ts": time.time()}, mode=0o600)
            return True
    except Exception:
        # On any lock/IO error, fail OPEN (allow dispatch) — never wedge
        # the resident shut over a bookkeeping hiccup.
        return True


def release_inflight(thread_ts: str | None = None,
                     claimed_before: float | None = None) -> bool:
    """Free the foreground slot. With `thread_ts` (and `claimed_before`,
    the dispatch's creation time) the release is scoped: it frees the slot
    only if the holder is that thread and claimed it no later than then.
    A late reply for one dispatch must never free the slot a newer
    dispatch is holding, or the next paste lands mid-turn and the TUI
    folds it into a tool result. Returns True when the slot was freed."""
    try:
        with _lock(SEND_LOCK_FILE.with_suffix(".inflight-lock")) as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if thread_ts is not None:
                try:
                    cur = json.loads(INFLIGHT_FILE.read_text())
                except Exception:
                    return False
                if cur.get("thread_ts") != thread_ts:
                    return False
                if claimed_before is not None and float(cur.get("ts", 0)) > claimed_before + 1.0:
                    return False
            INFLIGHT_FILE.unlink(missing_ok=True)
            return True
    except Exception:
        return False


def inflight_busy() -> bool:
    """True if a fresh foreground turn currently holds the resident."""
    try:
        cur = json.loads(INFLIGHT_FILE.read_text())
    except Exception:
        return False
    return bool(cur) and _inflight_fresh(cur)


# ---------------------------------------------------------------------------
# Pane lifecycle / health
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, input_text: str | None = None,
         timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True,
                          text=True, timeout=timeout)


def pane_alive() -> bool:
    try:
        return _run(["tmux", "has-session", "-t", TMUX_TARGET]).returncode == 0
    except Exception:
        return False


def _tui_ready() -> bool:
    """Is the Claude TUI at its input prompt? On RESUME the transcript
    already exists before the UI accepts input, so transcript existence
    is not a readiness signal and a paste sent during boot is silently
    swallowed. This is the one place we look at the pane, and only for
    the prompt chrome, never for data."""
    try:
        r = _run(["tmux", "capture-pane", "-t", TMUX_TARGET, "-p"])
        if r.returncode != 0:
            return False
        tail = r.stdout[-2000:]
        return ("⏵⏵" in tail) or ("❯" in tail)
    except Exception:
        return False


def ensure_healthy(start_grace_s: float = 40.0) -> bool:
    """Idempotently ensure the pane is up AND accepting input."""
    try:
        _run(["bash", str(PANE_SCRIPT), "ensure"], timeout=30)
    except Exception:
        return False
    if not pane_alive():
        return False
    tp = transcript_path()
    if tp is None:
        return False
    deadline = time.monotonic() + start_grace_s
    while time.monotonic() < deadline:
        if _tui_ready():
            return True
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Transcript access (via sudo — files are 0600 ea-bot)
# ---------------------------------------------------------------------------

def _tsize(tp: Path) -> int | None:
    try:
        r = _run(["sudo", "-n", "-u", "ea-bot", "stat", "-c", "%s", str(tp)])
        return int(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:
        return None


def _tread(tp: Path, offset: int) -> str:
    try:
        r = _run(["sudo", "-n", "-u", "ea-bot", "tail", "-c",
                  f"+{offset + 1}", str(tp)], timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Transcript parsing (pure — unit tested)
# ---------------------------------------------------------------------------

def parse_entries(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _user_text(entry: dict) -> str:
    """Text of a user entry; handles both content shapes (plain string
    for typed/pasted input, block list for tool_result entries)."""
    if entry.get("type") != "user":
        return ""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _tool_result_texts(entry: dict) -> str:
    """Every tool_result string in a user entry, joined."""
    if entry.get("type") != "user":
        return ""
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_result":
            continue
        body = c.get("content")
        if isinstance(body, str):
            parts.append(body)
        elif isinstance(body, list):
            parts.extend(b.get("text", "") for b in body if isinstance(b, dict))
    return "\n".join(parts)


def _marker_text(entry: dict) -> str:
    """Text to scan for a dispatch marker. A paste that lands while the
    model is mid-turn is queued by the TUI and attached to the next tool
    result rather than becoming a user turn, so tool_result bodies count
    too."""
    if entry.get("type") != "user":
        return ""
    return _user_text(entry) + "\n" + _tool_result_texts(entry)


def _assistant_text(entry: dict) -> str | None:
    """Pure-text assistant entry -> its text; None for tool turns/meta."""
    if entry.get("type") != "assistant":
        return None
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    kinds = {c.get("type") for c in content if isinstance(c, dict)}
    if "tool_use" in kinds:
        return None
    text = "\n".join(c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text").strip()
    return text or None


def final_text_of(entries: list[dict]) -> str | None:
    """Latest turn-final assistant text (a newer tool_use supersedes)."""
    final = None
    for e in entries:
        if e.get("type") != "assistant":
            continue
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        if "tool_use" in kinds:
            final = None
            continue
        text = "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text").strip()
        if text:
            final = text
    return final


def find_segment_reply(entries: list[dict], dispatch_id: str
                       ) -> tuple[str | None, bool, str | None]:
    """Reply for a dispatch: turn-final text between its marker user
    entry and the marker of the next, newer dispatch.

    Returns (reply_text|None, segment_closed, tagged_entry_uuid|None).
    segment_closed=True when a later dispatch marker exists — the reply
    (if any) is definitively complete, no quiescence wait needed.
    tagged_entry_uuid is set when the reply text carried a [deliver:]
    tag (model misuse on a direct reply) so the caller can mark it
    consumed before the router double-posts it; the tag is stripped
    from the returned text."""
    id_token = f"id={dispatch_id}]"
    start = None
    end = None
    for i, e in enumerate(entries):
        text = _marker_text(e)
        if not text or DISPATCH_MARKER_PREFIX not in text:
            continue
        if start is None:
            if id_token in text:
                start = i
        elif _later_dispatch(text, dispatch_id):
            end = i
            break
    if start is None:
        return None, False, None
    segment = entries[start + 1: end if end is not None else len(entries)]
    reply = None
    uuid = None
    for e in segment:
        text = _assistant_text(e)
        if text is None:
            if e.get("type") == "assistant":
                reply, uuid = None, None  # tool turn supersedes
            continue
        reply, uuid = text, e.get("uuid")
    tagged_uuid = None
    if reply:
        m = _DELIVER_RE.match(reply)
        if m:
            tagged_uuid = uuid
            reply = _DELIVER_RE.sub("", reply, count=1).strip()
    return reply, end is not None, tagged_uuid


_BACKGROUND_LAUNCHED = "Async agent launched successfully"
_TASK_NOTIFICATION = "<task-notification>"
_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9A-Za-z_-]{2,})")
_TASK_ID_RE = re.compile(r"<task-id>([0-9A-Za-z_-]{2,})</task-id>")
# A background launch counts as resolved once the model has its result:
# a task notification, or a blocking read of the task's output.
_TASK_READ_TOOLS = {"TaskOutput", "Monitor", "TaskStop"}
BACKGROUND_HOLD_S = 150.0


def _task_reads(entry: dict) -> set[str]:
    """Task ids the model read back in this assistant entry."""
    if entry.get("type") != "assistant":
        return set()
    content = (entry.get("message") or {}).get("content")
    out: set[str] = set()
    for c in content if isinstance(content, list) else []:
        if isinstance(c, dict) and c.get("type") == "tool_use" \
                and c.get("name") in _TASK_READ_TOOLS:
            tid = (c.get("input") or {}).get("task_id")
            if tid:
                out.add(str(tid))
    return out


def segment_background_pending(entries: list[dict], dispatch_id: str) -> bool:
    """True while a background subagent launched inside this dispatch's
    segment has neither reported back nor been read. The model's turn ends
    when it launches one, so the text it wrote before the launch looks
    like a final reply; the answer that matters (and any draft it
    creates) arrives after the result comes back. A launch the model
    reads with TaskOutput is resolved by that read; a segment closed by a
    later marker is never pending."""
    id_token = f"id={dispatch_id}]"
    started = False
    launched: set[str] = set()
    resolved: set[str] = set()
    unnamed_launches = 0
    for e in entries:
        text = _marker_text(e)
        if DISPATCH_MARKER_PREFIX in text:
            if not started:
                if id_token in text:
                    started = True
                continue
            if _later_dispatch(text, dispatch_id):
                break
        if not started:
            continue
        results = _tool_result_texts(e)
        if _BACKGROUND_LAUNCHED in results:
            ids = _AGENT_ID_RE.findall(results)
            launched.update(ids)
            if not ids:
                unnamed_launches += 1
        if _TASK_NOTIFICATION in text:
            found = _TASK_ID_RE.findall(text)
            resolved.update(found)
            if not found:
                unnamed_launches = max(0, unnamed_launches - 1)
        for tid in _task_reads(e):
            if tid in launched:
                resolved.add(tid)
            else:
                unnamed_launches = max(0, unnamed_launches - 1)
    return bool(launched - resolved) or unnamed_launches > 0


def extract_deliveries(entries: list[dict]) -> list[dict]:
    """Async background-completion reports: pure-text assistant entries
    starting with [deliver:<thread_ts>]. Returns
    [{uuid, thread_ts, text}] with the tag stripped."""
    out = []
    for e in entries:
        text = _assistant_text(e)
        if not text:
            continue
        m = _DELIVER_RE.match(text)
        if not m:
            continue
        out.append({"uuid": e.get("uuid") or "",
                    "thread_ts": m.group(1),
                    "text": _DELIVER_RE.sub("", text, count=1).strip()})
    return out


# ---------------------------------------------------------------------------
# Router posted-set (shared between dispatch path and router thread)
# ---------------------------------------------------------------------------

def _router_state() -> dict:
    data = _read_private(ROUTER_STATE_FILE)
    if data is None:
        return _valid_router(_legacy("resident-router.json"))
    return data if isinstance(data, dict) else {}


def _save_router_state(state: dict) -> None:
    try:
        state["posted"] = (state.get("posted") or [])[-300:]
        _atomic.write_json(ROUTER_STATE_FILE, state, mode=0o600, indent=None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pending-dispatch registry (late-reply recovery)
# ---------------------------------------------------------------------------
# A task that runs past the await window would otherwise leave its answer
# with nowhere to go. Every dispatch registers here; awaits that return
# clear their entry; TIMED-OUT entries remain, and the router thread keeps
# checking their transcript segments and delivers the reply whenever it
# lands. A hang becomes a late answer.

def _pending_locked(fn):
    try:
        with _lock(SEND_LOCK_FILE.with_suffix(".pending-lock")) as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            pending = pending_dispatches()
            result = fn(pending)
            _atomic.write_json(PENDING_FILE, pending, mode=0o600, indent=None)
            return result
    except Exception:
        return None


def register_pending(dispatch_id: str, *, thread_ts: str, channel: str,
                     start_offset: int, journal_offset: int) -> None:
    def _add(p):
        p[dispatch_id] = {"thread_ts": thread_ts, "channel": channel,
                          "start_offset": start_offset,
                          "journal_offset": journal_offset,
                          "created": time.time()}
    _pending_locked(_add)


def clear_pending(dispatch_id: str) -> None:
    _pending_locked(lambda p: p.pop(dispatch_id, None))


def pending_dispatches() -> dict:
    data = _read_private(PENDING_FILE)
    if data is None:
        return _valid_pending(_legacy("resident-pending.json"))
    return data if isinstance(data, dict) else {}


def recover_pending() -> list[dict]:
    """Router tick: late replies for dispatches whose await timed out.

    Returns [{dispatch_id, thread_ts, channel, journal_offset, reply,
    raw}] for entries whose segment reply has now materialized (only
    consulted for entries older than the await window — live awaits own
    their segments until then). Expired entries (>12h) are dropped with
    a log-visible marker entry in the return list (reply=None)."""
    out: list[dict] = []
    pending = pending_dispatches()
    if not pending:
        return out
    tp = transcript_path()
    if tp is None:
        return out
    now = time.time()
    stale_ids: list[str] = []
    for did, meta in pending.items():
        age = now - (meta.get("created") or 0)
        if age < DEFAULT_TIMEOUT_S + 30:
            continue  # the live await still owns this segment
        if age > PENDING_EXPIRE_S:
            stale_ids.append(did)
            out.append({"dispatch_id": did, "reply": None, **meta})
            continue
        raw = _tread(tp, meta.get("start_offset") or 0)
        if not raw:
            continue
        reply, closed, tagged_uuid = find_segment_reply(parse_entries(raw), did)
        if not reply:
            continue
        if tagged_uuid:
            mark_posted(tagged_uuid)
        out.append({"dispatch_id": did, "reply": reply, "raw": raw, **meta})
        stale_ids.append(did)
    if stale_ids:
        _pending_locked(lambda p: [p.pop(d, None) for d in stale_ids])
    return out


# ---------------------------------------------------------------------------
# Session rotation
# ---------------------------------------------------------------------------

def rotation_due(max_bytes: int = ROTATE_MAX_BYTES) -> bool:
    """True if maybe_rotate() would rotate right now: the transcript has
    outgrown the cap and no dispatch is pending. Split out so a caller can
    apply its own policy on top (the Slack bot defers rotation on a thread
    follow-up) and log that decision without triggering the reset."""
    tp = transcript_path()
    if tp is None:
        return False
    size = _tsize(tp)
    if size is None or size < max_bytes:
        return False
    return not pending_dispatches()


def maybe_rotate(max_bytes: int = ROTATE_MAX_BYTES) -> bool:
    """Rotate to a fresh conversation when the transcript has outgrown
    fast-turn territory. Refuses while dispatches are pending (their
    segments live in the current transcript). Returns True if rotated."""
    if not rotation_due(max_bytes):
        return False
    try:
        _run(["bash", str(PANE_SCRIPT), "rotate"], timeout=30)
        _run(["bash", str(PANE_SCRIPT), "ensure"], timeout=30)
        # reset router offset for the new transcript
        _save_router_state({"posted": (_router_state().get("posted") or [])})
        return True
    except Exception:
        return False


def mark_posted(uuid: str) -> None:
    """Record a delivery uuid as already posted (lock-guarded — the
    dispatch path and the router thread share this file)."""
    if not uuid:
        return
    try:
        with _lock(SEND_LOCK_FILE.with_suffix(".router-lock")) as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            state = _router_state()
            posted = state.setdefault("posted", [])
            if uuid not in posted:
                posted.append(uuid)
                _save_router_state(state)
    except Exception:
        pass


def already_posted(uuid: str) -> bool:
    return uuid in (_router_state().get("posted") or [])


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _input_holds(pane_tail: str, head: str) -> bool:
    """Does the TUI input box (the text after the last prompt glyph) still
    hold an unsent paste? A large paste shows there as a "[Pasted text #N
    +M lines]" placeholder, not as its first line."""
    if "❯" not in pane_tail:
        return False
    box = pane_tail.rsplit("❯", 1)[1]
    return (bool(head) and head in box) or "[Pasted text" in box


def send(text: str) -> bool:
    """Inject one message into the pane as a single bracketed paste.
    Short lock so two pastes can't interleave keystrokes; if the
    orchestrator is mid-turn the TUI queues the message — by design."""
    try:
        with _lock(SEND_LOCK_FILE) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Wait briefly for the TUI prompt if the pane just booted —
            # pastes during boot are silently swallowed.
            for _ in range(20):
                if _tui_ready():
                    break
                time.sleep(1.0)
            if _run(["tmux", "load-buffer", "-b", "ea-bridge", "-"],
                    input_text=text).returncode != 0:
                return False
            # A line typed before the paste: the TUI wraps a long paste as
            # pasted content, and a message that is only a paste reads as
            # material from elsewhere rather than the user's request.
            if _run(["tmux", "send-keys", "-t", TMUX_TARGET, "-l",
                     DISPATCH_LEAD]).returncode != 0:
                return False
            if _run(["tmux", "paste-buffer", "-p", "-b", "ea-bridge",
                     "-t", TMUX_TARGET]).returncode != 0:
                return False
            time.sleep(0.6)  # let the TUI ingest the paste before submit
            if _run(["tmux", "send-keys", "-t", TMUX_TARGET,
                     "Enter"]).returncode != 0:
                return False
            # Submit-race guard: if the TUI was busy or still booting, the
            # paste can land AFTER the Enter, leaving the text stranded in
            # the input box until the next paste submits both. Re-Enter
            # while the box still holds it.
            head = (text.splitlines() or [""])[0][:40]
            for _ in range(4):
                time.sleep(1.0)
                try:
                    cap = _run(["tmux", "capture-pane", "-t", TMUX_TARGET,
                                "-p"]).stdout
                except Exception:
                    break
                if _input_holds(cap[-1500:], head):
                    _run(["tmux", "send-keys", "-t", TMUX_TARGET, "Enter"])
                else:
                    break
            return True
    except Exception:
        return False


class ResidentUsageLimitError(Exception):
    """The resident hit a Claude usage/session/weekly limit while processing a
    dispatch, so no reply will come. `.reset` is a human-readable reset hint
    (e.g. '7:10pm (America/Chicago)') when the CLI reported one, else ''."""

    def __init__(self, reset: str = ""):
        self.reset = reset or ""
        super().__init__("resident hit a Claude usage limit"
                         + (f" (resets {self.reset})" if self.reset else ""))


# Phrasings / transcript fields that mean "this turn was blocked by a Claude
# usage limit". Specific enough that a normal reply won't false-trigger them.
_USAGE_LIMIT_SIGNALS = (
    "hit your session limit", "hit your usage limit", "hit your weekly limit",
    "usage limit reached", '"error":"rate_limit"',
)
_RESET_RE = re.compile(r"resets\s+([0-9: ]*[ap]m\s*\([A-Za-z_/]+\))", re.I)


def _usage_limit_hit(text: str) -> str | None:
    """If `text` (a transcript slice or a pane capture) shows a Claude usage
    limit, return the reset-time hint (possibly ''); else None."""
    if not text:
        return None
    low = text.lower()
    if not any(sig in low for sig in _USAGE_LIMIT_SIGNALS):
        return None
    m = _RESET_RE.search(text)
    return m.group(1) if m else ""


def _pane_snapshot() -> str:
    """Best-effort current visible contents of the resident pane."""
    try:
        r = _run(["tmux", "capture-pane", "-t", TMUX_TARGET, "-p"])
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def dispatch_and_await(text: str, dispatch_id: str,
                       timeout_s: float = DEFAULT_TIMEOUT_S,
                       thread_ts: str = "", channel: str = "",
                       journal_offset: int = 0) -> tuple[str, str]:
    """Inject `text` (which must contain make_marker(..., dispatch_id))
    and wait for ITS segment reply. Multiple dispatches may be in
    flight; replies are matched by marker position, not arrival order.

    Every dispatch is registered in the pending file; a TimeoutError
    leaves the entry behind so the router delivers the reply late
    (see recover_pending).

    Returns (reply_text, raw_transcript_since_send). Raises
    TimeoutError when no segment reply appears in time, RuntimeError on
    send failure."""
    tp = transcript_path()
    if tp is None:
        raise RuntimeError("no resident session state")
    start_offset = _tsize(tp) or 0
    register_pending(dispatch_id, thread_ts=thread_ts, channel=channel,
                     start_offset=start_offset,
                     journal_offset=journal_offset)
    if not send(text):
        clear_pending(dispatch_id)  # nothing was injected; no late reply
        raise RuntimeError("tmux send failed (pane gone?)")
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last_size = start_offset
    quiet_since = None
    last_pane_check = 0.0
    seen_file = _tsize(tp) is not None
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        size = _tsize(tp)
        if size is None:
            if not seen_file and time.monotonic() - started < TRANSCRIPT_APPEAR_S:
                continue
            # The caller falls back and answers another way, so this
            # dispatch must never get a late reply: a stale entry would
            # claim the next turn's answer for this thread.
            clear_pending(dispatch_id)
            raise RuntimeError("transcript vanished mid-await" if seen_file
                               else "transcript never appeared")
        seen_file = True
        if size != last_size:
            last_size = size
            quiet_since = time.monotonic()
        raw = _tread(tp, start_offset) if size > start_offset else ""
        reply = None
        if raw:
            entries = parse_entries(raw)
            reply, closed, tagged_uuid = find_segment_reply(entries, dispatch_id)
            if reply and not closed and segment_background_pending(entries, dispatch_id) \
                    and (quiet_since is None or
                         time.monotonic() - quiet_since < BACKGROUND_HOLD_S):
                reply = None  # a background result is still expected
            if reply and (closed or (quiet_since is not None and
                                     time.monotonic() - quiet_since
                                     >= QUIESCENT_S)):
                if tagged_uuid:
                    mark_posted(tagged_uuid)  # don't let the router repost
                clear_pending(dispatch_id)
                return reply, raw
        # Usage-limit detection: surface it instead of silently timing out.
        # Primary signal is the transcript slice for THIS turn. Fallback: if
        # the paste produced no transcript output at all and the pane is sitting
        # on a limit prompt, treat that as blocked too. The pane is consulted
        # ONLY when there is no transcript activity, so a normal reply can never
        # false-trigger it.
        if not reply:
            lim = _usage_limit_hit(raw)
            if (lim is None and not raw
                    and time.monotonic() - started >= 8.0
                    and time.monotonic() - last_pane_check >= 3.0):
                last_pane_check = time.monotonic()
                lim = _usage_limit_hit(_pane_snapshot())
            if lim is not None:
                clear_pending(dispatch_id)  # this turn errored; no late reply
                raise ResidentUsageLimitError(lim)
    # Deliberately NOT clearing the pending entry: the router's
    # recover_pending() keeps watching this segment and posts the reply
    # whenever the orchestrator finishes.
    raise TimeoutError(
        f"no segment reply for dispatch {dispatch_id} within "
        f"{timeout_s:.0f}s — the orchestrator is still working; the "
        f"router will deliver the reply when it lands")


# ---------------------------------------------------------------------------
# Router poll step (called by the bot's router thread)
# ---------------------------------------------------------------------------

def router_poll() -> list[dict]:
    """One router tick: read new transcript, return unposted deliveries
    (caller posts them to Slack, then calls mark_posted per uuid).
    Maintains its own offset in ROUTER_STATE_FILE."""
    tp = transcript_path()
    if tp is None:
        return []
    size = _tsize(tp)
    if size is None:
        return []
    state = _router_state()
    offset = state.get("offset")
    if offset is None or offset > size:
        # First run (or pruned/rotated transcript): start from EOF —
        # never replay history into Slack.
        _save_router_state({**state, "offset": size})
        return []
    if size == offset:
        return []
    raw = _tread(tp, offset)
    _save_router_state({**_router_state(), "offset": size})
    deliveries = extract_deliveries(parse_entries(raw))
    return [d for d in deliveries if not already_posted(d["uuid"])]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps({
            "enabled": enabled(), "pane_alive": pane_alive(),
            "state": _state(), "router": _router_state(),
            "transcript": str(transcript_path() or ""),
        }, indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "send":
        did = f"{time.time():.6f}"
        marker = make_marker("cli-test", did)
        reply, _ = dispatch_and_await(
            f"{marker}\n{sys.argv[2]}", did, timeout_s=180)
        print(reply)
