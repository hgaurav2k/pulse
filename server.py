"""Claude Code Dashboard Server.

FastAPI server with WebSocket real-time updates and filesystem watching.

Usage:
    python server.py [--host HOST] [--port PORT] [--open]
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import signal
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# --- Logging ---
logger = logging.getLogger("pulse")
logger.setLevel(logging.INFO)
_fh = logging.handlers.RotatingFileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pulse.log"),
    maxBytes=5 * 1024 * 1024, backupCount=3,
)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_sh)

from parser import (
    SessionSummary,
    find_session_file,
    get_running_session_ids,
    get_running_session_map,
    is_session_active,
    parse_all_sessions,
    parse_session_file,
    read_session_messages,
)
from remote_monitor import fetch_remote_messages, kill_remote_session, load_config, poll_remote_machine, scan_remote_machine

CLAUDE_DIR = os.path.expanduser("~/.claude")
API_KEY = os.environ.get("DASHBOARD_API_KEY", "")

# --- Auth ---

PULSE_PASSWORD = os.environ.get("PULSE_PASSWORD") or load_config().get("password", "")
COOKIE_SECRET = secrets.token_hex(32)


def make_auth_token() -> str:
    return hmac.new(COOKIE_SECRET.encode(), PULSE_PASSWORD.encode(), hashlib.sha256).hexdigest()


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pulse — Login</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #1a1a2e; color: #e0e0e0; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; }
  .card { background: #16213e; border-radius: 12px; padding: 2.5rem;
          box-shadow: 0 8px 32px rgba(0,0,0,0.3); width: 340px; text-align: center; }
  h1 { font-size: 1.5rem; margin-bottom: 0.3rem; color: #e94560; }
  .subtitle { font-size: 0.85rem; color: #888; margin-bottom: 1.5rem; }
  input[type="password"] { width: 100%; padding: 0.7rem 1rem; border: 1px solid #333;
         border-radius: 8px; background: #0f3460; color: #e0e0e0; font-size: 1rem;
         margin-bottom: 1rem; outline: none; }
  input[type="password"]:focus { border-color: #e94560; }
  button { width: 100%; padding: 0.7rem; border: none; border-radius: 8px;
           background: #e94560; color: #fff; font-size: 1rem; cursor: pointer;
           font-weight: 600; }
  button:hover { background: #c73652; }
  .error { color: #e94560; font-size: 0.85rem; margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="card">
  <h1>Pulse</h1>
  <p class="subtitle">Enter password to continue</p>
  {error}
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Log in</button>
  </form>
</div>
</body>
</html>"""


# --- WebSocket connection manager ---


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, data: dict):
        for ws in self.active_connections[:]:
            try:
                await ws.send_json(data)
            except Exception:
                if ws in self.active_connections:
                    self.active_connections.remove(ws)


# --- Dashboard state ---


class DashboardState:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.machines: dict[str, dict] = {}
        self.file_offsets: dict[str, int] = {}
        self.session_project_map: dict[str, str] = {}
        self.managed_sessions: dict[str, "ManagedSession"] = {}

    def get_aggregate_stats(self) -> dict:
        sessions = list(self.sessions.values())
        return {
            "total_sessions": len(sessions),
            "active_sessions": sum(1 for s in sessions if s.get("is_active")),
            "total_cost_usd": round(
                sum(s.get("estimated_cost_usd", 0) for s in sessions), 4
            ),
            "total_input_tokens": sum(
                s.get("total_input_tokens", 0) for s in sessions
            ),
            "total_output_tokens": sum(
                s.get("total_output_tokens", 0) for s in sessions
            ),
            "total_messages": sum(s.get("message_count", 0) for s in sessions),
            "machines_online": len(self.machines),
        }


manager = ConnectionManager()
state = DashboardState()


# --- Managed Session (take-over + control) ---


class ManagedSession:
    def __init__(self, session_id: str | None, project_path: str, ssh_host: str | None = None):
        self.session_id = session_id  # None for new sessions (discovered from output)
        self.project_path = project_path
        self.ssh_host = ssh_host  # None = local, otherwise SSH hostname
        self.process: asyncio.subprocess.Process | None = None
        self.status: str = "starting"  # starting | running | waiting | stopped | error
        self.output_buffer: list[dict] = []
        self.subscribers: set[WebSocket] = set()
        self._read_task: asyncio.Task | None = None
        # Pending turn messages (not yet in JSONL)
        self._pending_user: dict | None = None
        self._pending_assistant: dict | None = None
        # Event fired when session_id is discovered (for new sessions)
        self._session_id_event: asyncio.Event = asyncio.Event()
        self.stopped_at: float | None = None
        self.waiting_for: str = ""  # "", "plan_approval", "question", "result"

    async def start(self, resume: bool = False):
        config = load_config()
        claude_bin = config.get("claude_binary", "claude")

        claude_cmd = f"CLAUDECODE= {claude_bin} -p --verbose --output-format stream-json --input-format stream-json --permission-mode bypassPermissions"
        if resume and self.session_id:
            claude_cmd += f" --resume {self.session_id}"

        if self.ssh_host:
            # Remote: wrap claude command in SSH
            # Prepend common install paths since non-login SSH shells may lack them
            remote_cmd = f"export PATH=\"$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH\" && cd {self.project_path} && {claude_cmd}"
            cmd = [
                "ssh",
                "-T",
                "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                self.ssh_host,
                remote_cmd,
            ]
            cwd = None
            env = None  # SSH inherits remote env
            print(f"[managed:{self.session_id}] Starting remote on {self.ssh_host}, resume={resume}, cwd={self.project_path}")
        else:
            # Local: run claude directly
            cmd = [claude_bin, "-p", "--verbose", "--output-format", "stream-json", "--input-format", "stream-json", "--permission-mode", "bypassPermissions"]
            if resume and self.session_id:
                cmd.extend(["--resume", self.session_id])
            cwd = self.project_path
            # Must unset CLAUDECODE to avoid nested session detection
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            print(f"[managed:{self.session_id}] Starting local, resume={resume}, cwd={self.project_path}")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            self.status = "running"
            print(f"[managed:{self.session_id}] Process started, pid={self.process.pid}")
            await self._broadcast_status()
            self._read_task = asyncio.create_task(self._read_output_loop())
            self._stderr_task = asyncio.create_task(self._read_stderr_loop())
        except Exception as e:
            self.status = "error"
            await self._broadcast_status()
            print(f"[managed:{self.session_id}] Failed to start: {e}")

    async def send_message(self, text: str):
        if not self.process or not self.process.stdin:
            print(f"[managed:{self.session_id}] send_message: no process or no stdin")
            return
        if self.process.returncode is not None:
            print(f"[managed:{self.session_id}] send_message: process already exited ({self.process.returncode})")
            return
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": text}})
        try:
            self.process.stdin.write((msg + "\n").encode())
            await self.process.stdin.drain()
            self.status = "running"
            self.waiting_for = ""
            # Track pending turn (not yet in JSONL)
            self._pending_user = {
                "role": "user",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": [{"type": "text", "text": text}],
            }
            self._pending_assistant = None
            await self._broadcast_status()
        except Exception as e:
            print(f"[managed:{self.session_id}] Failed to send: {e}")

    async def stop(self):
        if not self.process:
            self.status = "stopped"
            self.stopped_at = time.time()
            await self._broadcast_status()
            return
        try:
            self.process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        except ProcessLookupError:
            pass
        if self.process and self.process.stdin:
            try:
                self.process.stdin.close()
            except Exception:
                pass
        self.status = "stopped"
        self.stopped_at = time.time()
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if hasattr(self, '_stderr_task') and self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        await self._broadcast_status()

    async def interrupt(self):
        if self.process:
            try:
                self.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    async def _read_output_loop(self):
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                self.output_buffer.append(event)
                # Keep buffer bounded
                if len(self.output_buffer) > 500:
                    self.output_buffer = self.output_buffer[-300:]

                # Discover session_id from stream output (for new sessions)
                if not self.session_id:
                    discovered_id = self._extract_session_id(event)
                    if discovered_id:
                        self.session_id = discovered_id
                        self._session_id_event.set()
                        print(f"[managed:new] Discovered session_id: {discovered_id}")

                old_status = self.status
                self._detect_waiting(event)
                if self.status != old_status:
                    await self._broadcast_status()

                # Track pending assistant response for serving via messages API
                etype = event.get("type", "")
                if etype == "assistant":
                    self._extract_pending_assistant(event)

                # Broadcast to all connections (not just subscribers)
                await manager.broadcast({
                    "type": "managed_output",
                    "session_id": self.session_id,
                    "event": event,
                })
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[managed:{self.session_id}] Read loop error: {e}")

        # Process exited or stdout closed
        self.status = "stopped"
        self.stopped_at = time.time()
        await self._broadcast_status()

    def _extract_session_id(self, event: dict) -> str | None:
        """Try to extract session_id from a stream-json event."""
        # system init events contain session_id
        if event.get("type") == "system" and event.get("session_id"):
            return event["session_id"]
        # Some events carry session_id at top level
        if event.get("session_id"):
            return event["session_id"]
        # result events also carry session_id
        if event.get("type") == "result" and event.get("session_id"):
            return event["session_id"]
        return None

    def _detect_waiting(self, event: dict):
        etype = event.get("type", "")
        if etype == "assistant":
            # While assistant is producing output, we're running
            self.status = "running"
            self.waiting_for = ""
            # Check if assistant is requesting user input via tool_use
            message = event.get("message", {})
            stop_reason = message.get("stop_reason") or event.get("stop_reason")
            if stop_reason == "tool_use":
                content = message.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "")
                            if name == "ExitPlanMode":
                                self.status = "waiting"
                                self.waiting_for = "plan_approval"
                                return
                            elif name == "AskUserQuestion":
                                self.status = "waiting"
                                self.waiting_for = "question"
                                return
        elif etype == "result":
            # A result event means one turn is complete.
            # In interactive mode (--input-format stream-json), the process
            # stays alive waiting for the next user message.
            if event.get("is_error"):
                self.status = "error"
                self.waiting_for = ""
            else:
                self.status = "waiting"
                self.waiting_for = "result"

    def _extract_pending_assistant(self, event: dict):
        """Extract assistant content from a stream-json event for pending display."""
        msg = event.get("message", {})
        raw_content = msg.get("content", [])
        model = msg.get("model", "")
        usage = msg.get("usage", {})
        blocks = []
        if isinstance(raw_content, list):
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type", "")
                if bt == "text" and block.get("text", "").strip():
                    blocks.append({"type": "text", "text": block["text"]})
                elif bt == "tool_use":
                    name = block.get("name", "unknown")
                    inp = block.get("input", {})
                    preview = ""
                    tool_block = {"type": "tool_use", "name": name}
                    if isinstance(inp, dict):
                        if name == "ExitPlanMode":
                            tool_block["plan"] = inp.get("plan", "")
                        elif name == "Bash":
                            preview = inp.get("command", "")
                        elif name in ("Read", "Write", "Edit"):
                            preview = inp.get("file_path", "")
                        elif name in ("Grep", "Glob"):
                            preview = inp.get("pattern", "")
                        else:
                            preview = str(inp)[:200]
                    tool_block["input_preview"] = preview
                    blocks.append(tool_block)
                elif bt == "thinking":
                    text = block.get("thinking", "")
                    if text.strip():
                        blocks.append({"type": "thinking", "text": text[:2000]})
        if blocks:
            self._pending_assistant = {
                "role": "assistant",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": blocks,
                "model": model,
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                },
            }

    def get_pending_messages(self) -> list[dict]:
        """Return pending turn messages not yet reflected in JSONL."""
        result = []
        if self._pending_user is not None:
            result.append(self._pending_user)
        if self._pending_assistant is not None:
            result.append(self._pending_assistant)
        return result

    async def _read_stderr_loop(self):
        try:
            while self.process and self.process.stderr:
                line = await self.process.stderr.readline()
                if not line:
                    break
                print(f"[managed:{self.session_id}:stderr] {line.decode().rstrip()}")
        except asyncio.CancelledError:
            return
        except Exception:
            pass

    async def _broadcast_status(self):
        msg = {
            "type": "managed_status",
            "session_id": self.session_id,
            "status": self.status,
            "waiting_for": self.waiting_for,
        }
        # Broadcast to subscribers
        for ws in list(self.subscribers):
            try:
                await ws.send_json(msg)
            except Exception:
                self.subscribers.discard(ws)
        # Also broadcast to all connections so dashboard updates
        await manager.broadcast(msg)


# --- Filesystem watcher ---


class ClaudeFileHandler(FileSystemEventHandler):
    def __init__(self, dashboard_state: DashboardState, ws_manager: ConnectionManager, loop: asyncio.AbstractEventLoop):
        self.state = dashboard_state
        self.ws_manager = ws_manager
        self.loop = loop
        self._pending: dict[str, float] = {}
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith(".jsonl"):
            return
        self._schedule(event.src_path)

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._schedule(event.src_path)

    def _schedule(self, path: str):
        with self._lock:
            self._pending[path] = time.time()
        threading.Timer(0.2, self._maybe_process, args=[path]).start()

    def _maybe_process(self, path: str):
        with self._lock:
            scheduled_at = self._pending.get(path, 0)
            if time.time() - scheduled_at < 0.19:
                return
            self._pending.pop(path, None)

        try:
            offset = self.state.file_offsets.get(path, 0)
            session_id = os.path.basename(path).replace(".jsonl", "")
            project_path = self.state.session_project_map.get(session_id)

            existing = None
            if session_id in self.state.sessions:
                # Rebuild a SessionSummary from stored dict for incremental update
                existing = SessionSummary(**{
                    k: v for k, v in self.state.sessions[session_id].items()
                    if k in SessionSummary.__dataclass_fields__
                })

            summary, new_offset = parse_session_file(
                path,
                machine="local",
                project_path=project_path,
                offset=offset,
                existing_summary=existing,
            )
            self.state.file_offsets[path] = new_offset

            # File was just written to → mark active immediately;
            # periodic_active_refresh will correct if process exits.
            summary.is_active = True

            if summary.message_count > 0:
                data = summary.to_dict()
                self.state.sessions[summary.session_id] = data
                asyncio.run_coroutine_threadsafe(
                    self.ws_manager.broadcast(
                        {"type": "session_update", "data": data}
                    ),
                    self.loop,
                )
                asyncio.run_coroutine_threadsafe(
                    self.ws_manager.broadcast(
                        {
                            "type": "stats_update",
                            "data": self.state.get_aggregate_stats(),
                        }
                    ),
                    self.loop,
                )
        except Exception as e:
            logger.error("Error processing %s: %s", path, e)


# --- Periodic active status refresh ---


async def periodic_active_refresh():
    while True:
        try:
            # Use process-based detection: which sessions have a live claude process?
            running = await asyncio.to_thread(get_running_session_ids, CLAUDE_DIR)
            changed = False
            for sid, s in list(state.sessions.items()):
                if s.get("machine", "local") != "local":
                    continue  # remote sessions handled by periodic_remote_poll
                was_active = s.get("is_active", False)
                # Managed sessions are always considered active while not stopped
                ms = state.managed_sessions.get(sid)
                if ms and ms.status not in ("stopped", "error"):
                    now_active = True
                else:
                    now_active = sid in running
                if was_active != now_active:
                    s["is_active"] = now_active
                    changed = True
                    await manager.broadcast({"type": "session_update", "data": s})
            if changed:
                await manager.broadcast(
                    {"type": "stats_update", "data": state.get_aggregate_stats()}
                )
        except Exception:
            logger.error("periodic_active_refresh error", exc_info=True)
        await asyncio.sleep(5)


async def periodic_remote_poll():
    """Periodically SSH into remote machines to detect active sessions."""
    config = load_config()
    machines = config.get("remote_machines", [])
    interval = config.get("poll_interval_seconds", 15)
    timeout = config.get("ssh_timeout_seconds", 10)

    if not machines:
        return

    logger.info("Remote monitoring: %s (every %ds)", machines, interval)

    # Per-machine backoff: counts consecutive failures.
    # Skip polling a machine for min(2^(fails-1), 12) cycles after each failure.
    consecutive_fails: dict[str, int] = {h: 0 for h in machines}
    skip_remaining: dict[str, int] = {h: 0 for h in machines}

    while True:
        try:
            # Determine which machines to poll this cycle
            to_poll = []
            for host in machines:
                if skip_remaining[host] > 0:
                    skip_remaining[host] -= 1
                else:
                    to_poll.append(host)

            if not to_poll:
                await asyncio.sleep(interval)
                continue

            results = await asyncio.gather(
                *[poll_remote_machine(host, timeout=timeout) for host in to_poll],
                return_exceptions=True,
            )

            changed = False
            active_remote_sids: set[str] = set()

            for host, result in zip(to_poll, results):
                # Handle failures
                if isinstance(result, Exception):
                    consecutive_fails[host] += 1
                    fc = consecutive_fails[host]
                    backoff = min(2 ** (fc - 1), 12)
                    skip_remaining[host] = backoff
                    if fc == 1:
                        logger.warning("[remote:%s] Unreachable (will retry in %ds)", host, backoff * interval)
                    elif fc % 5 == 0:
                        logger.warning("[remote:%s] Still unreachable after %d attempts (retry in %ds)", host, fc, backoff * interval)
                    continue

                # SSH succeeded — reset backoff
                if consecutive_fails[host] > 0:
                    logger.info("[remote:%s] Reconnected after %d failures", host, consecutive_fails[host])
                    consecutive_fails[host] = 0
                    skip_remaining[host] = 0

                for s_data in result:
                    sid = s_data.get("session_id", "")
                    if not sid:
                        continue
                    active_remote_sids.add(sid)
                    s_data["is_active"] = True
                    existing = state.sessions.get(sid)
                    if existing != s_data:
                        state.sessions[sid] = s_data
                        changed = True
                        await manager.broadcast({"type": "session_update", "data": s_data})

                # Update machine heartbeat
                active_count = sum(1 for s in result if s.get("is_active"))
                state.machines[host] = {
                    "hostname": host,
                    "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                    "session_count": len(result),
                    "active_session_count": active_count,
                }

            # Mark remote sessions that are no longer active
            for sid, s in list(state.sessions.items()):
                machine = s.get("machine", "local")
                if machine in machines and sid not in active_remote_sids:
                    if s.get("is_active", False):
                        s["is_active"] = False
                        changed = True
                        await manager.broadcast({"type": "session_update", "data": s})

            if changed:
                await manager.broadcast(
                    {"type": "stats_update", "data": state.get_aggregate_stats()}
                )
        except Exception:
            logger.error("periodic_remote_poll error", exc_info=True)
        await asyncio.sleep(interval)


async def periodic_remote_scan():
    """Periodically SSH into remote machines to discover ALL sessions (including inactive)."""
    config = load_config()
    machines = config.get("remote_machines", [])
    interval = config.get("scan_interval_seconds", 120)

    if not machines:
        return

    # Initial delay to let the active poll run first
    await asyncio.sleep(10)

    logger.info("Remote scan: %s (every %ds)", machines, interval)

    while True:
        try:
            results = await asyncio.gather(
                *[scan_remote_machine(host) for host in machines],
                return_exceptions=True,
            )

            changed = False
            for host, result in zip(machines, results):
                if isinstance(result, Exception) or not result:
                    continue

                for s_data in result:
                    sid = s_data.get("session_id", "")
                    if not sid:
                        continue
                    # Don't overwrite sessions that are currently active
                    # (the active poll has authority over active status)
                    existing = state.sessions.get(sid)
                    if existing and existing.get("is_active", False):
                        continue
                    s_data["is_active"] = False
                    s_data["machine"] = host
                    if existing != s_data:
                        state.sessions[sid] = s_data
                        changed = True
                        await manager.broadcast({"type": "session_update", "data": s_data})

            if changed:
                await manager.broadcast(
                    {"type": "stats_update", "data": state.get_aggregate_stats()}
                )
        except Exception:
            logger.error("periodic_remote_scan error", exc_info=True)
        await asyncio.sleep(interval)


async def periodic_state_gc():
    """Periodically evict stale sessions and managed session entries."""
    config = load_config()
    interval = config.get("gc_interval_seconds", 3600)
    max_age_days = config.get("gc_max_age_days", 7)
    managed_ttl = config.get("gc_managed_ttl_seconds", 600)

    while True:
        try:
            now = time.time()
            cutoff_ts = now - max_age_days * 86400
            evicted_sids = []

            # Evict old inactive sessions with no running managed session
            for sid, s in list(state.sessions.items()):
                if s.get("is_active", False):
                    continue
                ms = state.managed_sessions.get(sid)
                if ms and ms.status not in ("stopped", "error"):
                    continue
                last_ts = s.get("last_timestamp", "")
                if not last_ts:
                    continue
                try:
                    t = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                    if t.timestamp() < cutoff_ts:
                        evicted_sids.append(sid)
                except (ValueError, TypeError):
                    continue

            for sid in evicted_sids:
                del state.sessions[sid]

            # Remove matching file_offsets
            if evicted_sids:
                evicted_set = set(evicted_sids)
                for path in list(state.file_offsets.keys()):
                    basename = os.path.basename(path).replace(".jsonl", "")
                    if basename in evicted_set:
                        del state.file_offsets[path]

            # Remove stopped/error managed sessions past TTL
            managed_evicted = 0
            for sid, ms in list(state.managed_sessions.items()):
                if ms.status in ("stopped", "error") and ms.stopped_at:
                    if now - ms.stopped_at > managed_ttl:
                        del state.managed_sessions[sid]
                        managed_evicted += 1

            if evicted_sids or managed_evicted:
                logger.info("GC: evicted %d sessions, %d managed entries", len(evicted_sids), managed_evicted)
                await manager.broadcast(
                    {"type": "stats_update", "data": state.get_aggregate_stats()}
                )
        except Exception:
            logger.error("periodic_state_gc error", exc_info=True)
        await asyncio.sleep(interval)


# --- App lifespan ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: scan all local sessions
    from parser import build_session_project_map

    state.session_project_map = build_session_project_map(CLAUDE_DIR)

    sessions = parse_all_sessions(CLAUDE_DIR, machine="local")

    # Use process-based detection to set initial active flags
    running = get_running_session_ids(CLAUDE_DIR)
    for s in sessions:
        s.is_active = s.session_id in running
        state.sessions[s.session_id] = s.to_dict()

    active_count = len(running)
    state.machines["local"] = {
        "hostname": "local",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "session_count": len(sessions),
        "active_session_count": active_count,
    }

    logger.info("Loaded %d sessions (%d active)", len(sessions), active_count)

    # Start filesystem watcher
    loop = asyncio.get_event_loop()
    observer = Observer()
    handler = ClaudeFileHandler(state, manager, loop)
    projects_dir = os.path.join(CLAUDE_DIR, "projects")
    if os.path.isdir(projects_dir):
        observer.schedule(handler, projects_dir, recursive=True)
    observer.start()

    # Periodic refresh tasks
    refresh_task = asyncio.create_task(periodic_active_refresh())
    remote_task = asyncio.create_task(periodic_remote_poll())
    scan_task = asyncio.create_task(periodic_remote_scan())
    gc_task = asyncio.create_task(periodic_state_gc())

    yield

    # Stop all managed sessions
    for ms in list(state.managed_sessions.values()):
        await ms.stop()

    observer.stop()
    observer.join()
    refresh_task.cancel()
    remote_task.cancel()
    scan_task.cancel()
    gc_task.cancel()


# --- FastAPI app ---

app = FastAPI(lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not PULSE_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/login" and request.method == "POST":
        return await call_next(request)
    if path == "/api/report" and request.method == "POST":
        return await call_next(request)
    cookie = request.cookies.get("pulse_auth", "")
    if hmac.compare_digest(cookie, make_auth_token()):
        return await call_next(request)
    return HTMLResponse(LOGIN_HTML.replace("{error}", ""))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if not hmac.compare_digest(str(password), PULSE_PASSWORD):
        html = LOGIN_HTML.replace("{error}", '<p class="error">Incorrect password</p>')
        return HTMLResponse(html, status_code=401)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="pulse_auth",
        value=make_auth_token(),
        max_age=86400,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/api/sessions")
async def get_sessions():
    return list(state.sessions.values())


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    if session_id not in state.sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    return state.sessions[session_id]


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    # Check if this is a remote session
    session_data = state.sessions.get(session_id)
    machine = session_data.get("machine", "local") if session_data else "local"

    if machine != "local":
        # Fetch messages from remote machine via SSH
        messages = await fetch_remote_messages(machine, session_id)
    else:
        filepath = find_session_file(CLAUDE_DIR, session_id)
        if not filepath:
            # For managed/new sessions, file may not exist yet
            if session_id in state.managed_sessions:
                messages = []
            else:
                raise HTTPException(status_code=404, detail="Session file not found")
        else:
            messages = read_session_messages(filepath)

    # For managed sessions, append pending messages from the current turn
    # that may not yet be flushed to the JSONL file
    ms = state.managed_sessions.get(session_id)
    if ms and ms._pending_user is not None:
        pending = ms.get_pending_messages()
        if pending:
            # Check if the JSONL already has this user message (avoid duplicates)
            pending_text = ms._pending_user["content"][0]["text"]
            last_user_text = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    for b in m.get("content", []):
                        if b.get("type") == "text":
                            last_user_text = b.get("text", "")
                            break
                    break
            if last_user_text != pending_text:
                messages.extend(pending)
            else:
                # JSONL caught up — clear pending
                ms._pending_user = None
                ms._pending_assistant = None

    return {"session_id": session_id, "messages": messages}


@app.get("/api/stats")
async def get_stats():
    return state.get_aggregate_stats()


@app.get("/api/machines")
async def get_machines():
    return list(state.machines.values())


class ReportPayload(BaseModel):
    machine: str
    api_key: str = ""
    sessions: list[dict]


@app.post("/api/report")
async def receive_report(payload: ReportPayload):
    if API_KEY and payload.api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

    for s_data in payload.sessions:
        s_data["machine"] = payload.machine
        sid = s_data.get("session_id", "")
        if sid:
            state.sessions[sid] = s_data

    state.machines[payload.machine] = {
        "hostname": payload.machine,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "session_count": len(payload.sessions),
        "active_session_count": sum(
            1 for s in payload.sessions if s.get("is_active")
        ),
    }

    await manager.broadcast(
        {
            "type": "machine_report",
            "machine": payload.machine,
            "stats": state.get_aggregate_stats(),
        }
    )
    return {"status": "ok"}


class SendPayload(BaseModel):
    message: str


@app.post("/api/sessions/{session_id}/take-over")
async def take_over_session(session_id: str):
    if session_id in state.managed_sessions:
        ms = state.managed_sessions[session_id]
        if ms.status in ("running", "waiting"):
            return {"status": "already_managed", "managed_status": ms.status}

    session_data = state.sessions.get(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail="Session not found")

    project_path = session_data.get("project_path", "")
    if not project_path:
        raise HTTPException(status_code=400, detail="Unknown project path")

    machine = session_data.get("machine", "local")
    ssh_host = None if machine == "local" else machine

    # Find and kill the running PID
    if ssh_host:
        await kill_remote_session(ssh_host, session_id)
        await asyncio.sleep(0.5)
    else:
        session_map = await asyncio.to_thread(get_running_session_map, CLAUDE_DIR)
        pid = session_map.get(session_id)
        if pid:
            try:
                os.kill(int(pid), signal.SIGINT)
            except (ProcessLookupError, ValueError):
                pass
            await asyncio.sleep(0.5)

    # Start managed session with resume
    ms = ManagedSession(session_id, project_path, ssh_host=ssh_host)
    state.managed_sessions[session_id] = ms
    await ms.start(resume=True)

    return {"status": "ok", "managed_status": ms.status}


class NewSessionPayload(BaseModel):
    project_path: str
    prompt: str
    machine: str | None = None


@app.post("/api/sessions/new")
async def create_new_session(payload: NewSessionPayload):
    project_path = payload.project_path
    if not project_path:
        raise HTTPException(status_code=400, detail="project_path is required")

    ssh_host = payload.machine if payload.machine and payload.machine != "local" else None

    # Create managed session without a session_id (will be discovered)
    ms = ManagedSession(session_id=None, project_path=project_path, ssh_host=ssh_host)
    await ms.start(resume=False)

    # Send the initial prompt
    if ms.status in ("running", "waiting"):
        await ms.send_message(payload.prompt)

    # Wait for session_id to be discovered from stream output
    try:
        await asyncio.wait_for(ms._session_id_event.wait(), timeout=30.0)
    except asyncio.TimeoutError:
        # If we never got a session_id, stop and report error
        await ms.stop()
        raise HTTPException(status_code=500, detail="Timed out waiting for session to start")

    session_id = ms.session_id
    state.managed_sessions[session_id] = ms

    # Create a minimal session entry so the frontend can see it
    if session_id not in state.sessions:
        project_name = os.path.basename(project_path)
        session_data = {
            "session_id": session_id,
            "project_path": project_path,
            "project_name": project_name,
            "machine": payload.machine or "local",
            "is_active": True,
            "message_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "estimated_cost_usd": 0,
            "models_used": [],
            "duration_seconds": 0,
            "last_activity": datetime.now(timezone.utc).isoformat(),
        }
        state.sessions[session_id] = session_data
        await manager.broadcast({"type": "session_update", "data": session_data})
        await manager.broadcast({"type": "stats_update", "data": state.get_aggregate_stats()})

    return {"status": "ok", "session_id": session_id, "managed_status": ms.status}


@app.get("/api/projects")
async def get_projects():
    """Return list of known project paths from session data."""
    projects = set()
    for s in state.sessions.values():
        pp = s.get("project_path", "")
        if pp:
            projects.add(pp)
    # Sort alphabetically
    return sorted(projects)


@app.post("/api/sessions/{session_id}/send")
async def send_to_session(session_id: str, payload: SendPayload):
    ms = state.managed_sessions.get(session_id)
    if not ms:
        raise HTTPException(status_code=400, detail="Session is not managed")
    if ms.status == "stopped":
        raise HTTPException(status_code=400, detail="Session has stopped")

    await ms.send_message(payload.message)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    ms = state.managed_sessions.get(session_id)
    if ms:
        await ms.stop()
        return {"status": "ok", "managed_status": ms.status}

    # Not managed — try to kill the PID directly
    session_data = state.sessions.get(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail="Session not found")

    machine = session_data.get("machine", "local")
    if machine != "local":
        killed = await kill_remote_session(machine, session_id)
        if killed:
            return {"status": "ok"}
        raise HTTPException(status_code=400, detail="No running process found on remote machine")

    session_map = await asyncio.to_thread(get_running_session_map, CLAUDE_DIR)
    pid = session_map.get(session_id)
    if pid:
        try:
            os.kill(int(pid), signal.SIGINT)
        except (ProcessLookupError, ValueError):
            pass
        return {"status": "ok"}

    raise HTTPException(status_code=400, detail="No running process found")


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    ms = state.managed_sessions.get(session_id)
    if ms and ms.process:
        await ms.interrupt()
        return {"status": "ok"}

    # Not managed — send SIGINT to the PID
    session_data = state.sessions.get(session_id)
    if not session_data:
        raise HTTPException(status_code=404, detail="Session not found")

    machine = session_data.get("machine", "local")
    if machine != "local":
        killed = await kill_remote_session(machine, session_id)
        if killed:
            return {"status": "ok"}
        raise HTTPException(status_code=400, detail="No running process found on remote machine")

    session_map = await asyncio.to_thread(get_running_session_map, CLAUDE_DIR)
    pid = session_map.get(session_id)
    if pid:
        try:
            os.kill(int(pid), signal.SIGINT)
        except (ProcessLookupError, ValueError):
            pass
        return {"status": "ok"}

    raise HTTPException(status_code=400, detail="No running process found")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if PULSE_PASSWORD:
        cookie = ws.cookies.get("pulse_auth", "")
        if not hmac.compare_digest(cookie, make_auth_token()):
            await ws.close(code=1008, reason="Unauthorized")
            return
    await manager.connect(ws)
    try:
        # Include managed session statuses in initial state
        managed_statuses = {
            sid: {"status": ms.status, "waiting_for": ms.waiting_for}
            for sid, ms in state.managed_sessions.items()
            if ms.status != "stopped"
        }
        await ws.send_json(
            {
                "type": "initial_state",
                "sessions": list(state.sessions.values()),
                "stats": state.get_aggregate_stats(),
                "machines": list(state.machines.values()),
                "managed_statuses": managed_statuses,
            }
        )
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                await ws.send_json({"type": "pong"})
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "pong"})
                continue

            msg_type = data.get("type", "")

            if msg_type == "subscribe_managed":
                sid = data.get("session_id", "")
                ms = state.managed_sessions.get(sid)
                if ms:
                    ms.subscribers.add(ws)
                    await ws.send_json({
                        "type": "managed_status",
                        "session_id": sid,
                        "status": ms.status,
                    })

            elif msg_type == "unsubscribe_managed":
                sid = data.get("session_id", "")
                ms = state.managed_sessions.get(sid)
                if ms:
                    ms.subscribers.discard(ws)

            elif msg_type == "send_message":
                sid = data.get("session_id", "")
                message = data.get("message", "")
                ms = state.managed_sessions.get(sid)
                if ms and message:
                    await ms.send_message(message)

            else:
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(ws)
        # Clean up subscriber references
        for ms in state.managed_sessions.values():
            ms.subscribers.discard(ws)


# --- Main ---


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Claude Code Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8420, help="Port (default: 8420)")
    parser.add_argument("--open", action="store_true", help="Open browser on start")
    args = parser.parse_args()

    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()

    logger.info("Starting Claude Code Dashboard at http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
