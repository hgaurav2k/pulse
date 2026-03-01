"""Core JSONL parsing logic for Claude Code session data.

Shared between the dashboard server and the remote reporter.
"""

import json
import os
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

# Pricing per million tokens
MODEL_PRICING = {
    "claude-opus-4-6": {
        "input": 15.0,
        "output": 75.0,
        "cache_create": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-5-20251101": {
        "input": 15.0,
        "output": 75.0,
        "cache_create": 18.75,
        "cache_read": 1.50,
    },
    "claude-sonnet-4-5-20250929": {
        "input": 3.0,
        "output": 15.0,
        "cache_create": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_create": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.0,
        "cache_create": 1.0,
        "cache_read": 0.08,
    },
}

# Default pricing for unknown models
DEFAULT_PRICING = {
    "input": 3.0,
    "output": 15.0,
    "cache_create": 3.75,
    "cache_read": 0.30,
}


@dataclass
class SessionSummary:
    session_id: str
    machine: str = "local"
    project_path: str = ""
    project_name: str = ""
    git_branch: str | None = None
    first_user_message: str = ""
    last_user_message: str = ""
    message_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    models_used: list[str] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    estimated_cost_usd: float = 0.0
    first_timestamp: str = ""
    last_timestamp: str = ""
    duration_seconds: float = 0.0
    is_active: bool = False
    tool_call_count: int = 0
    recent_tools: list[str] = field(default_factory=list)
    daily_stats: list[dict] = field(default_factory=list)
    last_message_role: str = ""  # 'user' or 'assistant'
    last_content_type: str = ""  # 'text', 'tool_use', 'thinking' — what the last assistant msg ended with
    last_tool_name: str = ""    # name of the last tool_use in the last assistant message
    # Waiting state: set when the session is blocked waiting for user input
    # Values: "" (not waiting), "question", "plan_approval", "plan_mode", "permission"
    waiting_for: str = ""
    waiting_tool: str = ""  # the specific tool name being waited on

    def to_dict(self) -> dict:
        return asdict(self)


def get_running_session_ids(claude_dir: str) -> set[str]:
    """Detect which session IDs have a currently-running Claude Code process.

    Strategy:
    1. Find running 'claude' CLI processes via pgrep
    2. Get each process's CWD (via lsof) and start time (via ps)
    3. Match each process to a session ID by finding the history.jsonl entry
       whose project matches the CWD and whose timestamp is closest to
       (and >= ) the process start time.
    """
    # Step 1: Get PIDs of running 'claude' CLI processes
    # Use ps instead of pgrep for reliability (pgrep -x can miss processes)
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,comm,args"],
            capture_output=True, text=True, timeout=5,
        )
        pids = []
        for line in r.stdout.strip().split("\n"):
            parts = line.strip().split(None, 2)
            if (
                len(parts) >= 2
                and parts[1] == "claude"
                and "/Applications/" not in line
            ):
                pids.append(parts[0])
    except Exception:
        return set()

    if not pids:
        return set()

    # Step 2: Get CWD and start time for each PID
    processes: list[tuple[str, str, float]] = []  # (pid, cwd, start_epoch_ms)
    for pid in pids:
        try:
            # CWD via lsof
            lsof_out = subprocess.run(
                ["lsof", "-p", pid],
                capture_output=True, text=True, timeout=5,
            ).stdout
            cwd = None
            for line in lsof_out.split("\n"):
                if " cwd " in line and "DIR" in line:
                    cwd = line.split()[-1]
                    break
            if not cwd:
                continue

            # Start time via ps
            ps_out = subprocess.run(
                ["ps", "-p", pid, "-o", "lstart="],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # e.g. "Sat Feb 28 16:11:41 2026"
            start_dt = datetime.strptime(ps_out, "%a %b %d %H:%M:%S %Y")
            start_ms = start_dt.timestamp() * 1000

            processes.append((pid, cwd, start_ms))
        except Exception:
            continue

    if not processes:
        return set()

    # Step 3: Read history.jsonl and build per-project, per-session latest entries
    history_path = os.path.join(claude_dir, "history.jsonl")
    # project -> {session_id -> (latest_timestamp, last_display_text)}
    project_sessions: dict[str, dict[str, tuple[float, str]]] = defaultdict(dict)
    try:
        with open(history_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts = obj.get("timestamp", 0)
                    proj = obj.get("project", "")
                    sid = obj.get("sessionId", "")
                    display = obj.get("display", "")
                    if ts and proj and sid:
                        prev = project_sessions[proj].get(sid)
                        if prev is None or ts > prev[0]:
                            project_sessions[proj][sid] = (ts, display)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return set()

    # Step 4: For each project with running processes, pick the N most-recently-
    # used sessions (N = number of processes), skipping sessions whose last
    # interaction was "exit".
    cwd_counts: dict[str, int] = defaultdict(int)
    for _, cwd, _ in processes:
        cwd_counts[cwd] += 1

    active_sessions: set[str] = set()
    for cwd, n_procs in cwd_counts.items():
        sessions_for_proj = project_sessions.get(cwd, {})
        # Filter out sessions whose last entry is an explicit exit
        candidates = [
            (sid, ts)
            for sid, (ts, display) in sessions_for_proj.items()
            if not display.strip().lower().startswith("exit")
        ]
        # Sort by most recent last-entry first
        candidates.sort(key=lambda x: x[1], reverse=True)
        # Take top N
        for sid, _ in candidates[:n_procs]:
            active_sessions.add(sid)

    return active_sessions


def is_session_active(last_timestamp: str, filepath: str = "") -> bool:
    """Lightweight mtime-based active check (used during file-change events).

    When a file was just modified, this returns True so the UI updates
    immediately. The authoritative check is get_running_session_ids().
    """
    import time as _time

    if filepath:
        try:
            mtime = os.path.getmtime(filepath)
            return (_time.time() - mtime) < 60
        except OSError:
            pass
    return False


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> float:
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    cost = (
        input_tokens * pricing["input"] / 1_000_000
        + output_tokens * pricing["output"] / 1_000_000
        + cache_creation_tokens * pricing["cache_create"] / 1_000_000
        + cache_read_tokens * pricing["cache_read"] / 1_000_000
    )
    return round(cost, 6)


def build_session_project_map(claude_dir: str) -> dict[str, str]:
    """Build sessionId -> real project path mapping from history.jsonl."""
    mapping: dict[str, str] = {}
    history_path = os.path.join(claude_dir, "history.jsonl")
    if not os.path.exists(history_path):
        return mapping
    try:
        with open(history_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    sid = obj.get("sessionId")
                    proj = obj.get("project")
                    if sid and proj:
                        mapping[sid] = proj
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return mapping


def decode_project_dir(dirname: str) -> str:
    """Best-effort decode of project directory name to path.

    e.g. '-Users-sinnhima-slp' -> '/Users/sinnhima/slp'
    This is ambiguous for paths containing hyphens, so prefer
    build_session_project_map() when possible.
    """
    if dirname.startswith("-"):
        return dirname.replace("-", "/")
    return dirname


def parse_session_file(
    filepath: str,
    machine: str = "local",
    project_path: str | None = None,
    offset: int = 0,
    existing_summary: SessionSummary | None = None,
) -> tuple[SessionSummary, int]:
    """Parse a session JSONL file from a given byte offset.

    Returns (summary, new_byte_offset).
    If existing_summary is provided, updates it incrementally.
    """
    session_id = os.path.basename(filepath).replace(".jsonl", "")

    if project_path is None:
        dirname = os.path.basename(os.path.dirname(filepath))
        project_path = decode_project_dir(dirname)

    if existing_summary is not None:
        summary = existing_summary
    else:
        summary = SessionSummary(
            session_id=session_id,
            machine=machine,
            project_path=project_path,
            project_name=os.path.basename(project_path),
        )

    # Track per-model tokens for cost calculation
    model_tokens: dict[str, dict[str, int]] = {}
    # Track per-day stats: date_str -> {messages, tokens_in, tokens_out, cost, tools}
    daily_buckets: dict[str, dict] = {}
    # Rebuild daily_buckets from existing summary if incremental
    if existing_summary is not None:
        for ds in existing_summary.daily_stats:
            daily_buckets[ds["date"]] = {
                "messages": ds["messages"],
                "tokens_in": ds["tokens_in"],
                "tokens_out": ds["tokens_out"],
                "cost": ds["cost"],
                "tools": ds.get("tools", 0),
            }

    MAX_RECENT_TOOLS = 15

    try:
        with open(filepath, "r") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                timestamp = obj.get("timestamp", "")
                day_key = timestamp[:10] if timestamp else ""

                if msg_type == "user":
                    summary.user_message_count += 1
                    summary.message_count += 1
                    summary.last_message_role = "user"
                    if timestamp:
                        if not summary.first_timestamp:
                            summary.first_timestamp = timestamp
                        summary.last_timestamp = timestamp

                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, str) and content.strip():
                        stripped = content.strip()
                        # Skip system/internal content
                        if not stripped.startswith(("<system", "<!--", "<local-command", "<command-name")):
                            # Skip context continuation prompts
                            if stripped.startswith("This session is being continued from a previous"):
                                pass
                            else:
                                if not summary.first_user_message:
                                    summary.first_user_message = stripped[:200]
                                summary.last_user_message = stripped[:200]

                    branch = obj.get("gitBranch")
                    if branch:
                        summary.git_branch = branch

                    if day_key:
                        if day_key not in daily_buckets:
                            daily_buckets[day_key] = {"messages": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "tools": 0}
                        daily_buckets[day_key]["messages"] += 1

                elif msg_type == "assistant":
                    summary.assistant_message_count += 1
                    summary.message_count += 1
                    summary.last_message_role = "assistant"
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")

                    if model and model not in summary.models_used:
                        summary.models_used.append(model)

                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cache_create = usage.get("cache_creation_input_tokens", 0)
                    cache_read = usage.get("cache_read_input_tokens", 0)

                    summary.total_input_tokens += inp
                    summary.total_output_tokens += out
                    summary.total_cache_creation_tokens += cache_create
                    summary.total_cache_read_tokens += cache_read

                    if model:
                        if model not in model_tokens:
                            model_tokens[model] = {
                                "input": 0, "output": 0,
                                "cache_create": 0, "cache_read": 0,
                            }
                        model_tokens[model]["input"] += inp
                        model_tokens[model]["output"] += out
                        model_tokens[model]["cache_create"] += cache_create
                        model_tokens[model]["cache_read"] += cache_read

                    content = msg.get("content", [])
                    chunk_tools: list[str] = []
                    last_block_type = ""
                    last_tool = ""
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                bt = block.get("type", "")
                                if bt == "tool_use":
                                    summary.tool_call_count += 1
                                    tool_name = block.get("name", "unknown")
                                    chunk_tools.append(tool_name)
                                    last_block_type = "tool_use"
                                    last_tool = tool_name
                                elif bt == "text" and block.get("text", "").strip():
                                    last_block_type = "text"
                                    last_tool = ""
                                elif bt == "thinking":
                                    if last_block_type != "text":
                                        last_block_type = "thinking"
                    if last_block_type:
                        summary.last_content_type = last_block_type
                        summary.last_tool_name = last_tool

                    if chunk_tools:
                        summary.recent_tools = (summary.recent_tools + chunk_tools)[-MAX_RECENT_TOOLS:]

                    if timestamp:
                        if not summary.first_timestamp:
                            summary.first_timestamp = timestamp
                        summary.last_timestamp = timestamp

                    # Daily stats for this assistant message
                    if day_key:
                        if day_key not in daily_buckets:
                            daily_buckets[day_key] = {"messages": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "tools": 0}
                        daily_buckets[day_key]["messages"] += 1
                        daily_buckets[day_key]["tokens_in"] += inp + cache_read
                        daily_buckets[day_key]["tokens_out"] += out
                        daily_buckets[day_key]["tools"] += len(chunk_tools)
                        if model:
                            daily_buckets[day_key]["cost"] += estimate_cost(
                                model, inp, out, cache_create, cache_read
                            )

            new_offset = f.tell()
    except OSError:
        return summary, offset

    # Build sorted daily_stats list
    summary.daily_stats = [
        {"date": k, **v} for k, v in sorted(daily_buckets.items())
    ]

    # Compute derived fields
    if summary.first_timestamp and summary.last_timestamp:
        try:
            t1 = datetime.fromisoformat(
                summary.first_timestamp.replace("Z", "+00:00")
            )
            t2 = datetime.fromisoformat(
                summary.last_timestamp.replace("Z", "+00:00")
            )
            summary.duration_seconds = (t2 - t1).total_seconds()
        except (ValueError, TypeError):
            pass

    summary.is_active = is_session_active(summary.last_timestamp, filepath=filepath)

    # Detect waiting-for-user state:
    # If the last message is from the assistant and ended with tool_use,
    # Claude is blocked waiting for user action.
    _INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}
    if summary.is_active and summary.last_message_role == "assistant" and summary.last_content_type == "tool_use":
        tool = summary.last_tool_name
        if tool == "AskUserQuestion":
            summary.waiting_for = "question"
            summary.waiting_tool = tool
        elif tool == "ExitPlanMode":
            summary.waiting_for = "plan_approval"
            summary.waiting_tool = tool
        elif tool == "EnterPlanMode":
            summary.waiting_for = "plan_mode"
            summary.waiting_tool = tool
        elif tool:
            summary.waiting_for = "permission"
            summary.waiting_tool = tool

    # Calculate cost from this chunk's per-model tokens, add to existing
    chunk_cost = 0.0
    for model, tokens in model_tokens.items():
        chunk_cost += estimate_cost(
            model,
            tokens["input"],
            tokens["output"],
            tokens["cache_create"],
            tokens["cache_read"],
        )
    summary.estimated_cost_usd += chunk_cost

    return summary, new_offset


def parse_all_sessions(
    claude_dir: str, machine: str = "local"
) -> list[SessionSummary]:
    """Scan all projects and parse all session JSONL files."""
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_dir):
        return []

    session_project_map = build_session_project_map(claude_dir)
    sessions: list[SessionSummary] = []

    for proj_dirname in os.listdir(projects_dir):
        proj_path = os.path.join(projects_dir, proj_dirname)
        if not os.path.isdir(proj_path) or proj_dirname.startswith("."):
            continue

        for filename in os.listdir(proj_path):
            if not filename.endswith(".jsonl"):
                continue
            # Skip files inside subdirectories (subagent dirs)
            filepath = os.path.join(proj_path, filename)
            if not os.path.isfile(filepath):
                continue

            session_id = filename.replace(".jsonl", "")
            real_project = session_project_map.get(
                session_id, decode_project_dir(proj_dirname)
            )

            summary, _ = parse_session_file(
                filepath, machine=machine, project_path=real_project
            )
            if summary.message_count > 0:
                sessions.append(summary)

    return sessions


def find_session_file(claude_dir: str, session_id: str) -> str | None:
    """Find the JSONL file for a given session ID."""
    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_dir):
        return None
    for proj_dirname in os.listdir(projects_dir):
        proj_path = os.path.join(projects_dir, proj_dirname)
        if not os.path.isdir(proj_path):
            continue
        candidate = os.path.join(proj_path, f"{session_id}.jsonl")
        if os.path.isfile(candidate):
            return candidate
    return None


def _extract_tool_use_block(block: dict, tool_id_to_name: dict) -> dict:
    """Build a rich content block from a tool_use block.

    For special tools (ExitPlanMode, AskUserQuestion, etc.) we preserve the
    full structured input so the frontend can render it properly.
    """
    tool_name = block.get("name", "unknown")
    tool_id = block.get("id", "")
    tool_id_to_name[tool_id] = tool_name
    inp = block.get("input", {})

    # --- Special tools: keep full structured data ---

    if tool_name == "ExitPlanMode" and isinstance(inp, dict):
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": "(plan submitted for approval)",
            "plan": inp.get("plan", ""),
        }

    if tool_name == "EnterPlanMode":
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": "Entering plan mode...",
        }

    if tool_name == "AskUserQuestion" and isinstance(inp, dict):
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": "",
            "questions": inp.get("questions", []),
        }

    if tool_name == "Bash" and isinstance(inp, dict):
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": cmd,
            "description": desc,
        }

    if tool_name == "Read" and isinstance(inp, dict):
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": inp.get("file_path", ""),
        }

    if tool_name in ("Write", "Edit") and isinstance(inp, dict):
        fp = inp.get("file_path", "")
        if tool_name == "Edit":
            old = inp.get("old_string", "")
            new = inp.get("new_string", "")
            return {
                "type": "tool_use",
                "name": tool_name,
                "input_preview": fp,
                "old_string": old[:2000],
                "new_string": new[:2000],
            }
        else:
            content = inp.get("content", "")
            return {
                "type": "tool_use",
                "name": tool_name,
                "input_preview": fp,
                "file_content": content[:3000],
            }

    if tool_name == "Glob" and isinstance(inp, dict):
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": inp.get("pattern", ""),
        }

    if tool_name == "Grep" and isinstance(inp, dict):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": f"{pattern}" + (f" in {path}" if path else ""),
        }

    if tool_name == "Agent" and isinstance(inp, dict):
        desc = inp.get("description", "")
        prompt = inp.get("prompt", "")
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": desc or prompt[:200],
            "agent_prompt": prompt[:1000],
        }

    if tool_name in ("TaskCreate", "TaskUpdate") and isinstance(inp, dict):
        subject = inp.get("subject", "")
        status = inp.get("status", "")
        return {
            "type": "tool_use",
            "name": tool_name,
            "input_preview": subject or status or str(inp)[:200],
        }

    # --- Default: generic preview ---
    if isinstance(inp, dict):
        preview_parts = []
        for k, v in list(inp.items())[:4]:
            vs = str(v)
            if len(vs) > 200:
                vs = vs[:200] + "..."
            preview_parts.append(f"{k}: {vs}")
        input_preview = "\n".join(preview_parts)
    else:
        input_preview = str(inp)[:400]

    return {
        "type": "tool_use",
        "name": tool_name,
        "input_preview": input_preview,
    }


def _extract_tool_result(
    tool_id: str,
    tool_id_to_name: dict,
    content_block: dict,
    tool_use_result: dict | None,
) -> dict:
    """Build a rich tool_result block using both the content block and toolUseResult."""
    tool_name = tool_id_to_name.get(tool_id, "tool")

    # Start with the content block text
    result_content = content_block.get("content", "")
    if isinstance(result_content, list):
        parts = []
        for rc in result_content:
            if isinstance(rc, dict) and rc.get("type") == "text":
                parts.append(rc.get("text", ""))
        result_content = "\n".join(parts)
    if isinstance(result_content, str):
        result_content = result_content[:5000]

    block: dict = {
        "type": "tool_result",
        "content": result_content,
        "tool_name": tool_name,
    }

    # Enrich with structured toolUseResult data
    if not isinstance(tool_use_result, dict):
        return block

    if tool_name == "ExitPlanMode":
        plan = tool_use_result.get("plan", "")
        if plan:
            block["plan"] = plan
            block["plan_file"] = tool_use_result.get("filePath", "")

    elif tool_name == "AskUserQuestion":
        answers = tool_use_result.get("answers", {})
        questions = tool_use_result.get("questions", [])
        if answers:
            block["answers"] = answers
        if questions:
            block["questions"] = questions

    elif tool_name == "Bash":
        stdout = tool_use_result.get("stdout", "")
        stderr = tool_use_result.get("stderr", "")
        if stdout:
            block["stdout"] = stdout[:5000]
        if stderr:
            block["stderr"] = stderr[:3000]
        block["interrupted"] = tool_use_result.get("interrupted", False)

    elif tool_name == "Read":
        file_info = tool_use_result.get("file", {})
        if isinstance(file_info, dict):
            block["file_path"] = file_info.get("filePath", "")

    elif tool_name == "Write":
        block["file_path"] = tool_use_result.get("filePath", "")
        block["write_type"] = tool_use_result.get("type", "")

    elif tool_name == "Edit":
        block["file_path"] = tool_use_result.get("filePath", "")
        block["old_string"] = tool_use_result.get("oldString", "")[:2000]
        block["new_string"] = tool_use_result.get("newString", "")[:2000]

    elif tool_name == "Agent":
        block["agent_status"] = tool_use_result.get("status", "")
        agent_content = tool_use_result.get("content", [])
        if isinstance(agent_content, list):
            text_parts = []
            for ac in agent_content:
                if isinstance(ac, dict) and ac.get("type") == "text":
                    text_parts.append(ac.get("text", ""))
            if text_parts:
                block["agent_summary"] = "\n".join(text_parts)[:3000]
        elif isinstance(agent_content, str):
            block["agent_summary"] = agent_content[:3000]

    elif tool_name == "Grep":
        block["grep_content"] = tool_use_result.get("content", "")[:3000]
        block["grep_files"] = tool_use_result.get("filenames", [])[:20]

    elif tool_name == "Glob":
        filenames = tool_use_result.get("filenames", [])
        if isinstance(filenames, list):
            block["glob_files"] = filenames[:50]

    elif tool_name == "EnterPlanMode":
        msg = tool_use_result.get("message", "")
        if msg:
            block["content"] = msg

    return block


def read_session_messages(filepath: str) -> list[dict]:
    """Read a session JSONL and return structured messages for conversation view.

    Returns a list of message dicts:
      - role: 'user' | 'assistant' | 'system'
      - timestamp: ISO string
      - content: list of content blocks
      - model: str (assistant only)
      - usage: dict (assistant only)

    Content block types:
      - {type: 'text', text: str}
      - {type: 'tool_use', name: str, input_preview: str, ...extra fields per tool}
      - {type: 'tool_result', content: str, tool_name: str, ...extra fields per tool}
      - {type: 'thinking', text: str}
    """
    messages: list[dict] = []
    tool_id_to_name: dict[str, str] = {}

    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                timestamp = obj.get("timestamp", "")

                # --- System messages ---
                if msg_type == "system":
                    subtype = obj.get("subtype", "")
                    if subtype == "api_error":
                        error = obj.get("error") or obj.get("content") or ""
                        if error:
                            messages.append({
                                "role": "system",
                                "timestamp": timestamp,
                                "content": [{"type": "error", "text": str(error)[:1000]}],
                            })
                    elif subtype in ("compact_boundary", "microcompact_boundary"):
                        label = "Context compacted" if subtype == "compact_boundary" else "Context micro-compacted"
                        messages.append({
                            "role": "system",
                            "timestamp": timestamp,
                            "content": [{"type": "info", "text": label}],
                        })
                    continue

                # --- User messages ---
                if msg_type == "user":
                    raw_content = obj.get("message", {}).get("content", "")
                    tool_use_result = obj.get("toolUseResult")
                    blocks: list[dict] = []

                    if isinstance(raw_content, str):
                        text = raw_content.strip()
                        # Skip internal system content
                        if text.startswith(("<system", "<!--", "<local-command-caveat")):
                            continue
                        # Context continuation messages
                        if text.startswith("This session is being continued from a previous"):
                            blocks.append({"type": "text", "text": "(Session continued from previous conversation)"})
                            messages.append({
                                "role": "user",
                                "timestamp": timestamp,
                                "content": blocks,
                            })
                            continue
                        if text.startswith("<command-name>"):
                            m = re.search(r"<command-name>(.*?)</command-name>", text)
                            cmd = m.group(1) if m else "command"
                            blocks.append({"type": "text", "text": f"/{cmd}"})
                        elif text:
                            blocks.append({"type": "text", "text": text})
                    elif isinstance(raw_content, list):
                        has_real_content = False
                        for block in raw_content:
                            if not isinstance(block, dict):
                                continue
                            bt = block.get("type", "")
                            if bt == "tool_result":
                                tool_id = block.get("tool_use_id", "")
                                result_block = _extract_tool_result(
                                    tool_id, tool_id_to_name, block, tool_use_result,
                                )
                                blocks.append(result_block)
                                has_real_content = True
                            elif bt == "text":
                                text = block.get("text", "").strip()
                                if text and not text.startswith(("<system", "<!--", "<local-command")):
                                    blocks.append({"type": "text", "text": text})
                                    has_real_content = True
                        if not has_real_content:
                            continue

                    if not blocks:
                        continue

                    messages.append({
                        "role": "user",
                        "timestamp": timestamp,
                        "content": blocks,
                    })

                # --- Assistant messages ---
                elif msg_type == "assistant":
                    msg = obj.get("message", {})
                    raw_content = msg.get("content", [])
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    blocks = []

                    if isinstance(raw_content, str):
                        if raw_content.strip():
                            blocks.append({"type": "text", "text": raw_content})
                    elif isinstance(raw_content, list):
                        for block in raw_content:
                            if not isinstance(block, dict):
                                continue
                            bt = block.get("type", "")
                            if bt == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    blocks.append({"type": "text", "text": text})
                            elif bt == "tool_use":
                                blocks.append(
                                    _extract_tool_use_block(block, tool_id_to_name)
                                )
                            elif bt == "thinking":
                                text = block.get("thinking", "")
                                if text.strip():
                                    blocks.append({
                                        "type": "thinking",
                                        "text": text[:2000] + ("..." if len(text) > 2000 else ""),
                                    })

                    if not blocks:
                        continue

                    messages.append({
                        "role": "assistant",
                        "timestamp": timestamp,
                        "content": blocks,
                        "model": model,
                        "usage": {
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                        },
                    })

    except OSError:
        pass

    return messages
