"""Claude Code Dashboard Server.

FastAPI server with WebSocket real-time updates and filesystem watching.

Usage:
    python server.py [--host HOST] [--port PORT] [--open]
"""

import argparse
import asyncio
import json
import os
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from parser import (
    SessionSummary,
    find_session_file,
    get_running_session_ids,
    is_session_active,
    parse_all_sessions,
    parse_session_file,
    read_session_messages,
)
from remote_monitor import fetch_remote_messages, load_config, poll_remote_machine

CLAUDE_DIR = os.path.expanduser("~/.claude")
API_KEY = os.environ.get("DASHBOARD_API_KEY", "")


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
            print(f"Error processing {path}: {e}")


# --- Periodic active status refresh ---


async def periodic_active_refresh():
    while True:
        await asyncio.sleep(5)
        # Use process-based detection: which sessions have a live claude process?
        running = await asyncio.to_thread(get_running_session_ids, CLAUDE_DIR)
        changed = False
        for sid, s in list(state.sessions.items()):
            if s.get("machine", "local") != "local":
                continue  # remote sessions handled by periodic_remote_poll
            was_active = s.get("is_active", False)
            now_active = sid in running
            if was_active != now_active:
                s["is_active"] = now_active
                changed = True
                await manager.broadcast({"type": "session_update", "data": s})
        if changed:
            await manager.broadcast(
                {"type": "stats_update", "data": state.get_aggregate_stats()}
            )


async def periodic_remote_poll():
    """Periodically SSH into remote machines to detect active sessions."""
    config = load_config()
    machines = config.get("remote_machines", [])
    interval = config.get("poll_interval_seconds", 15)
    timeout = config.get("ssh_timeout_seconds", 10)

    if not machines:
        return

    print(f"Remote monitoring: {machines} (every {interval}s)")

    # Per-machine backoff: counts consecutive failures.
    # Skip polling a machine for min(2^(fails-1), 12) cycles after each failure.
    consecutive_fails: dict[str, int] = {h: 0 for h in machines}
    skip_remaining: dict[str, int] = {h: 0 for h in machines}

    while True:
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
                    print(f"[remote:{host}] Unreachable (will retry in {backoff * interval}s)")
                elif fc % 5 == 0:
                    print(f"[remote:{host}] Still unreachable after {fc} attempts (retry in {backoff * interval}s)")
                continue

            # SSH succeeded — reset backoff
            if consecutive_fails[host] > 0:
                print(f"[remote:{host}] Reconnected after {consecutive_fails[host]} failures")
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

    print(f"Loaded {len(sessions)} sessions ({active_count} active)")

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

    yield

    observer.stop()
    observer.join()
    refresh_task.cancel()
    remote_task.cancel()


# --- FastAPI app ---

app = FastAPI(lifespan=lifespan)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


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
        return {"session_id": session_id, "messages": messages}

    filepath = find_session_file(CLAUDE_DIR, session_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Session file not found")
    messages = read_session_messages(filepath)
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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json(
            {
                "type": "initial_state",
                "sessions": list(state.sessions.values()),
                "stats": state.get_aggregate_stats(),
                "machines": list(state.machines.values()),
            }
        )
        while True:
            data = await ws.receive_text()
            await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(ws)


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

    print(f"Starting Claude Code Dashboard at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
