"""Remote machine monitoring via SSH.

Periodically SSHes into configured machines and runs an inline Python script
to detect active Claude Code sessions. No setup needed on the remote machines.
"""

import asyncio
import json
import os

# Self-contained Python script that runs on the remote machine via SSH.
# It detects running claude processes, matches them to sessions via
# history.jsonl, and parses the active session files for summary data.
REMOTE_SCRIPT = r'''
import json, os, subprocess, sys
from collections import defaultdict
from datetime import datetime, timezone

CLAUDE_DIR = os.path.expanduser("~/.claude")

def get_active_sessions():
    # Step 1: Find running claude CLI processes
    try:
        r = subprocess.run(["ps", "-eo", "pid,comm,args"], capture_output=True, text=True, timeout=5)
        pids = []
        for line in r.stdout.strip().split("\n"):
            parts = line.strip().split(None, 2)
            if len(parts) >= 2 and parts[1] == "claude" and "/Applications/" not in line:
                pids.append(parts[0])
    except Exception:
        pids = []

    if not pids:
        print(json.dumps([]))
        return

    # Step 2: Get CWD for each PID
    cwd_counts = defaultdict(int)
    for pid in pids:
        try:
            r = subprocess.run(["lsof", "-p", pid], capture_output=True, text=True, timeout=5)
            for line in r.stdout.split("\n"):
                if " cwd " in line and "DIR" in line:
                    cwd = line.split()[-1]
                    cwd_counts[cwd] += 1
                    break
        except Exception:
            # Linux fallback: /proc/{pid}/cwd
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
                cwd_counts[cwd] += 1
            except Exception:
                pass

    if not cwd_counts:
        print(json.dumps([]))
        return

    # Step 3: Read history.jsonl for session matching
    history_path = os.path.join(CLAUDE_DIR, "history.jsonl")
    project_sessions = defaultdict(dict)
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
        print(json.dumps([]))
        return

    # Step 4: Match processes to sessions
    active_sids = set()
    for cwd, n in cwd_counts.items():
        sessions_for_proj = project_sessions.get(cwd, {})
        candidates = [
            (sid, ts) for sid, (ts, display) in sessions_for_proj.items()
            if not display.strip().lower().startswith("exit")
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        for sid, _ in candidates[:n]:
            active_sids.add(sid)

    if not active_sids:
        print(json.dumps([]))
        return

    # Step 5: Find and parse the active session files
    projects_dir = os.path.join(CLAUDE_DIR, "projects")
    # Build session->project map from history
    sid_project = {}
    for proj, sids in project_sessions.items():
        for sid in sids:
            sid_project[sid] = proj

    results = []
    if not os.path.isdir(projects_dir):
        print(json.dumps([]))
        return

    for proj_dirname in os.listdir(projects_dir):
        proj_path = os.path.join(projects_dir, proj_dirname)
        if not os.path.isdir(proj_path):
            continue
        for fname in os.listdir(proj_path):
            if not fname.endswith(".jsonl"):
                continue
            sid = fname.replace(".jsonl", "")
            if sid not in active_sids:
                continue
            filepath = os.path.join(proj_path, fname)
            summary = parse_session_light(filepath, sid, sid_project.get(sid, ""))
            if summary and summary["message_count"] > 0:
                results.append(summary)

    print(json.dumps(results))


def parse_session_light(filepath, session_id, project_path):
    """Lightweight parser — extracts just enough for the dashboard."""
    summary = {
        "session_id": session_id,
        "project_path": project_path,
        "project_name": os.path.basename(project_path) if project_path else "unknown",
        "git_branch": None,
        "first_user_message": "",
        "last_user_message": "",
        "message_count": 0,
        "user_message_count": 0,
        "assistant_message_count": 0,
        "models_used": [],
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "estimated_cost_usd": 0.0,
        "first_timestamp": "",
        "last_timestamp": "",
        "duration_seconds": 0.0,
        "is_active": True,
        "tool_call_count": 0,
        "recent_tools": [],
        "daily_stats": [],
        "last_message_role": "",
        "last_content_type": "",
        "last_tool_name": "",
    }
    tools = []
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = obj.get("type")
                ts = obj.get("timestamp", "")
                if msg_type == "user":
                    summary["user_message_count"] += 1
                    summary["message_count"] += 1
                    summary["last_message_role"] = "user"
                    if ts:
                        if not summary["first_timestamp"]:
                            summary["first_timestamp"] = ts
                        summary["last_timestamp"] = ts
                    content = obj.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        text = content.strip()
                        if text and not text.startswith(("<system", "<!--", "<local-command", "<command-name", "This session is being continued")):
                            if not summary["first_user_message"]:
                                summary["first_user_message"] = text[:200]
                            summary["last_user_message"] = text[:200]
                    branch = obj.get("gitBranch")
                    if branch:
                        summary["git_branch"] = branch
                elif msg_type == "assistant":
                    summary["assistant_message_count"] += 1
                    summary["message_count"] += 1
                    summary["last_message_role"] = "assistant"
                    msg = obj.get("message", {})
                    model = msg.get("model", "")
                    if model and model not in summary["models_used"]:
                        summary["models_used"].append(model)
                    usage = msg.get("usage", {})
                    summary["total_input_tokens"] += usage.get("input_tokens", 0)
                    summary["total_output_tokens"] += usage.get("output_tokens", 0)
                    if ts:
                        if not summary["first_timestamp"]:
                            summary["first_timestamp"] = ts
                        summary["last_timestamp"] = ts
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                summary["tool_call_count"] += 1
                                tools.append(block.get("name", "unknown"))
    except OSError:
        return None

    summary["recent_tools"] = tools[-15:]
    if summary["first_timestamp"] and summary["last_timestamp"]:
        try:
            t1 = datetime.fromisoformat(summary["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(summary["last_timestamp"].replace("Z", "+00:00"))
            summary["duration_seconds"] = (t2 - t1).total_seconds()
        except (ValueError, TypeError):
            pass
    return summary


get_active_sessions()
'''


async def poll_remote_machine(hostname: str, timeout: int = 10) -> list[dict]:
    """SSH into a remote machine and return its active session summaries."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            hostname,
            "python3", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=REMOTE_SCRIPT.encode()),
            timeout=timeout,
        )
        if proc.returncode != 0:
            err = stderr.decode().strip()
            if err:
                print(f"[remote:{hostname}] SSH error: {err[:200]}")
            return []

        output = stdout.decode().strip()
        if not output:
            return []
        sessions = json.loads(output)
        # Tag each session with the machine name
        for s in sessions:
            s["machine"] = hostname
        return sessions

    except asyncio.TimeoutError:
        print(f"[remote:{hostname}] SSH timed out after {timeout}s")
        return []
    except json.JSONDecodeError as e:
        print(f"[remote:{hostname}] Invalid JSON from remote: {e}")
        return []
    except Exception as e:
        print(f"[remote:{hostname}] Error: {e}")
        return []


REMOTE_MESSAGES_SCRIPT = r'''
import json, os, sys, re

CLAUDE_DIR = os.path.expanduser("~/.claude")
session_id = sys.argv[1]

# Find the session file
projects_dir = os.path.join(CLAUDE_DIR, "projects")
filepath = None
if os.path.isdir(projects_dir):
    for d in os.listdir(projects_dir):
        candidate = os.path.join(projects_dir, d, session_id + ".jsonl")
        if os.path.isfile(candidate):
            filepath = candidate
            break

if not filepath:
    print(json.dumps([]))
    sys.exit(0)


def extract_tool_use(block, tool_id_to_name):
    tname = block.get("name", "unknown")
    tid = block.get("id", "")
    tool_id_to_name[tid] = tname
    inp = block.get("input", {})
    b = {"type": "tool_use", "name": tname}
    if tname == "ExitPlanMode" and isinstance(inp, dict):
        b["input_preview"] = "(plan submitted for approval)"
        b["plan"] = inp.get("plan", "")
    elif tname == "EnterPlanMode":
        b["input_preview"] = "Entering plan mode..."
    elif tname == "AskUserQuestion" and isinstance(inp, dict):
        b["input_preview"] = ""
        b["questions"] = inp.get("questions", [])
    elif tname == "Bash" and isinstance(inp, dict):
        b["input_preview"] = inp.get("command", "")
        b["description"] = inp.get("description", "")
    elif tname == "Read" and isinstance(inp, dict):
        b["input_preview"] = inp.get("file_path", "")
    elif tname == "Edit" and isinstance(inp, dict):
        b["input_preview"] = inp.get("file_path", "")
        b["old_string"] = inp.get("old_string", "")[:2000]
        b["new_string"] = inp.get("new_string", "")[:2000]
    elif tname == "Write" and isinstance(inp, dict):
        b["input_preview"] = inp.get("file_path", "")
        b["file_content"] = inp.get("content", "")[:3000]
    elif tname == "Agent" and isinstance(inp, dict):
        b["input_preview"] = inp.get("description", "") or inp.get("prompt", "")[:200]
        b["agent_prompt"] = inp.get("prompt", "")[:1000]
    elif tname in ("Grep", "Glob") and isinstance(inp, dict):
        b["input_preview"] = inp.get("pattern", "")
    else:
        if isinstance(inp, dict):
            b["input_preview"] = "\n".join(f"{k}: {str(v)[:200]}" for k, v in list(inp.items())[:4])
        else:
            b["input_preview"] = str(inp)[:400]
    return b


def extract_tool_result(tool_id, tool_id_to_name, content_block, tool_use_result):
    tname = tool_id_to_name.get(tool_id, "tool")
    rc = content_block.get("content", "")
    if isinstance(rc, list):
        rc = "\n".join(r.get("text", "") for r in rc if isinstance(r, dict) and r.get("type") == "text")
    if isinstance(rc, str):
        rc = rc[:5000]
    b = {"type": "tool_result", "content": rc, "tool_name": tname}
    if not isinstance(tool_use_result, dict):
        return b
    if tname == "ExitPlanMode":
        if tool_use_result.get("plan"):
            b["plan"] = tool_use_result["plan"]
            b["plan_file"] = tool_use_result.get("filePath", "")
    elif tname == "AskUserQuestion":
        if tool_use_result.get("answers"):
            b["answers"] = tool_use_result["answers"]
        if tool_use_result.get("questions"):
            b["questions"] = tool_use_result["questions"]
    elif tname == "Bash":
        if tool_use_result.get("stdout"):
            b["stdout"] = tool_use_result["stdout"][:5000]
        if tool_use_result.get("stderr"):
            b["stderr"] = tool_use_result["stderr"][:3000]
    elif tname == "Read":
        fi = tool_use_result.get("file", {})
        if isinstance(fi, dict):
            b["file_path"] = fi.get("filePath", "")
    elif tname == "Write":
        b["file_path"] = tool_use_result.get("filePath", "")
    elif tname == "Edit":
        b["file_path"] = tool_use_result.get("filePath", "")
        b["old_string"] = tool_use_result.get("oldString", "")[:2000]
        b["new_string"] = tool_use_result.get("newString", "")[:2000]
    elif tname == "Agent":
        b["agent_status"] = tool_use_result.get("status", "")
        ac = tool_use_result.get("content", [])
        if isinstance(ac, list):
            parts = [x.get("text", "") for x in ac if isinstance(x, dict) and x.get("type") == "text"]
            if parts:
                b["agent_summary"] = "\n".join(parts)[:3000]
    elif tname == "EnterPlanMode":
        b["content"] = tool_use_result.get("message", "")
    return b


messages = []
tool_id_to_name = {}
try:
    with open(filepath) as f:
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
            if msg_type == "system":
                st = obj.get("subtype", "")
                if st == "api_error":
                    err = obj.get("error") or obj.get("content") or ""
                    if err:
                        messages.append({"role": "system", "timestamp": timestamp,
                            "content": [{"type": "error", "text": str(err)[:1000]}]})
                elif st in ("compact_boundary", "microcompact_boundary"):
                    lbl = "Context compacted" if st == "compact_boundary" else "Context micro-compacted"
                    messages.append({"role": "system", "timestamp": timestamp,
                        "content": [{"type": "info", "text": lbl}]})
                continue
            if msg_type == "user":
                raw = obj.get("message", {}).get("content", "")
                tur = obj.get("toolUseResult")
                blocks = []
                if isinstance(raw, str):
                    text = raw.strip()
                    if text.startswith(("<system", "<!--", "<local-command-caveat")):
                        continue
                    if text.startswith("This session is being continued from a previous"):
                        blocks.append({"type": "text", "text": "(Session continued from previous conversation)"})
                        messages.append({"role": "user", "timestamp": timestamp, "content": blocks})
                        continue
                    if text.startswith("<command-name>"):
                        m = re.search(r"<command-name>(.*?)</command-name>", text)
                        cmd = m.group(1) if m else "command"
                        blocks.append({"type": "text", "text": "/" + cmd})
                    elif text:
                        blocks.append({"type": "text", "text": text})
                elif isinstance(raw, list):
                    has_real = False
                    for block in raw:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "tool_result":
                            tid = block.get("tool_use_id", "")
                            blocks.append(extract_tool_result(tid, tool_id_to_name, block, tur))
                            has_real = True
                        elif bt == "text":
                            t = block.get("text", "").strip()
                            if t and not t.startswith(("<system", "<!--", "<local-command")):
                                blocks.append({"type": "text", "text": t})
                                has_real = True
                    if not has_real:
                        continue
                if not blocks:
                    continue
                messages.append({"role": "user", "timestamp": timestamp, "content": blocks})
            elif msg_type == "assistant":
                msg = obj.get("message", {})
                raw = msg.get("content", [])
                usage = msg.get("usage", {})
                model = msg.get("model", "")
                blocks = []
                if isinstance(raw, str):
                    if raw.strip():
                        blocks.append({"type": "text", "text": raw})
                elif isinstance(raw, list):
                    for block in raw:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "text" and block.get("text", "").strip():
                            blocks.append({"type": "text", "text": block["text"]})
                        elif bt == "tool_use":
                            blocks.append(extract_tool_use(block, tool_id_to_name))
                        elif bt == "thinking":
                            t = block.get("thinking", "")
                            if t.strip():
                                blocks.append({"type": "thinking", "text": t[:2000] + ("..." if len(t) > 2000 else "")})
                if not blocks:
                    continue
                messages.append({"role": "assistant", "timestamp": timestamp, "content": blocks,
                    "model": model, "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}})
except OSError:
    pass
print(json.dumps(messages))
'''


async def fetch_remote_messages(hostname: str, session_id: str, timeout: int = 15) -> list[dict]:
    """SSH into a remote machine and fetch conversation messages for a session."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            hostname,
            "python3", "-", session_id,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=REMOTE_MESSAGES_SCRIPT.encode()),
            timeout=timeout,
        )
        if proc.returncode != 0:
            return []
        output = stdout.decode().strip()
        if not output:
            return []
        return json.loads(output)
    except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
        print(f"[remote:{hostname}] Message fetch error: {e}")
        return []


def load_config() -> dict:
    """Load remote machine configuration."""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if not os.path.exists(config_path):
        return {"remote_machines": [], "poll_interval_seconds": 15, "ssh_timeout_seconds": 10}
    try:
        with open(config_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"remote_machines": [], "poll_interval_seconds": 15, "ssh_timeout_seconds": 10}
