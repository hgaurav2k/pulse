# Claude Pulse

Live session monitor and dashboard for Claude Code.

## Architecture

Single-page web app backed by a FastAPI server that monitors `~/.claude/projects/` JSONL session files.

```
pulse                  # Bash entry point — starts server, handles tunneling
├── server.py          # FastAPI app: REST API, WebSocket, filesystem watcher
├── parser.py          # JSONL parsing, session summarization, cost estimation
├── remote_monitor.py  # SSH-based remote machine polling (inline Python scripts)
├── reporter.py        # Push-based reporter agent (runs on remote machines)
├── config.json        # Remote machine list + polling config
├── static/
│   └── index.html     # Single-file frontend (vanilla JS, no framework)
└── requirements.txt   # fastapi, uvicorn, watchdog, requests
```

## How It Works

1. On startup, scans all `~/.claude/projects/*//*.jsonl` files to build session summaries
2. Uses `watchdog` to detect file changes and incrementally re-parse only new bytes (offset tracking)
3. Identifies active sessions by matching running `claude` PIDs (via `ps` + `lsof`) to project CWDs, then mapping those to session IDs via `history.jsonl`
4. Pushes real-time updates to the browser via WebSocket
5. Optionally polls remote machines over SSH (runs an inline Python script on each)
6. Supports bidirectional control: take over sessions, send messages, interrupt, and stop — locally or remotely via SSH

## Key Data Flow

- **Local sessions**: filesystem watcher -> incremental parse -> WebSocket broadcast
- **Remote sessions (pull)**: periodic SSH poll -> parse on remote -> JSON over stdout -> merge into state
- **Remote sessions (push)**: `reporter.py` on remote -> POST to `/api/report` -> merge into state
- **Session control**: take-over -> kill original process -> relaunch with `--resume` + stream-json I/O -> managed by `ManagedSession` in server.py

## API Endpoints

- `GET /api/sessions` — all session summaries
- `GET /api/sessions/{id}` — single session summary
- `GET /api/sessions/{id}/messages` — full conversation (local or SSH-fetched, includes pending managed messages)
- `POST /api/sessions/{id}/take-over` — kill running process and relaunch under dashboard control
- `POST /api/sessions/{id}/send` — send a message to a managed session
- `POST /api/sessions/{id}/interrupt` — send SIGINT to a session (managed or by PID)
- `POST /api/sessions/{id}/stop` — stop a session (managed or by PID)
- `GET /api/stats` — aggregate stats (total cost, tokens, active count)
- `GET /api/machines` — connected machine info
- `POST /api/report` — receive push reports from remote reporters
- `WS /ws` — real-time updates (initial_state, session_update, stats_update, managed_status, managed_output)

## Running

```bash
./pulse                         # Start on localhost:8420, auto-open browser
./pulse --port 9000             # Custom port
./pulse --tunnel                # Deploy to remote host (default: my-remote-host) and start with cloudflared
./pulse --tunnel <ssh-host>     # Deploy to a specific remote host
./pulse --stop [host]           # Stop Pulse on a remote host
./pulse --serve                 # Run server + cloudflared locally with restart loops (used on remote)
```

## Cost Estimation

Pricing is hardcoded in `parser.py:MODEL_PRICING` for Opus 4.5/4.6, Sonnet 4.5/4.6, Haiku 4.5. Unknown models fall back to Sonnet-tier pricing.

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, uvicorn, watchdog
- **Frontend**: Vanilla JS, CSS custom properties, single HTML file
- **Remote monitoring**: asyncio SSH subprocess, inline Python scripts sent via stdin
- **Tunneling**: Cloudflare `cloudflared` (local or via SSH remote)
