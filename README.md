# Claude Pulse

A real-time monitoring dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions. Track active sessions, token usage, costs, and conversation history across local and remote machines from a single browser tab.

## Why

When you're running multiple Claude Code sessions — across projects, terminals, or machines — it's hard to know what's happening where. Pulse gives you a live overview: which sessions are active, what they're working on, how much they cost, and what they're waiting for. It reads Claude Code's existing JSONL session files with zero configuration needed on the machines being monitored.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Browser (Frontend)                         │
│                        static/index.html                            │
│                     Vanilla JS  ·  WebSocket                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ WS /ws  +  REST /api/*
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      FastAPI Server (server.py)                      │
│                                                                      │
│  ┌──────────────┐  ┌──────────────────┐  ┌────────────────────────┐ │
│  │  Filesystem   │  │  Active Session  │  │   WebSocket Manager    │ │
│  │  Watcher      │  │  Detector        │  │   (broadcast to all    │ │
│  │  (watchdog)   │  │  (ps + lsof)     │  │    connected clients)  │ │
│  └──────┬───────┘  └────────┬─────────┘  └───────────┬────────────┘ │
│         │                   │                        │              │
│         ▼                   ▼                        │              │
│  ┌─────────────────────────────────────┐             │              │
│  │        Dashboard State              │─────────────┘              │
│  │  sessions · machines · file_offsets │                             │
│  └──────────────┬──────────────────────┘                            │
│                 │                                                    │
│    ┌────────────┴────────────┐                                      │
│    ▼                         ▼                                      │
│  ┌──────────────┐   ┌─────────────────┐                             │
│  │  Local Parse  │   │  Remote Monitor │                            │
│  │  (parser.py)  │   │  (SSH polling)  │                            │
│  └──────┬───────┘   └────────┬────────┘                             │
│         │                    │                                      │
└─────────┼────────────────────┼──────────────────────────────────────┘
          │                    │
          ▼                    ▼
  ~/.claude/projects/    Remote machines
  *.jsonl session files  (via SSH + inline Python)

                  ── Remote Reporter (Push Mode) ──

  ┌──────────────────┐     POST /api/report     ┌──────────────────┐
  │  Remote Machine   │ ──────────────────────▶  │  Pulse Server    │
  │  reporter.py      │                          │                  │
  │  (parser.py)      │   periodic push of       │  merges into     │
  │                   │   session summaries       │  dashboard state │
  └──────────────────┘                           └──────────────────┘
```

### File Structure

```
pulse                  # Bash entry point — starts server, handles tunneling
├── server.py          # FastAPI app: REST API, WebSocket, filesystem watcher
├── parser.py          # JSONL parsing, session summarization, cost estimation
├── remote_monitor.py  # SSH-based remote machine polling (inline Python scripts)
├── reporter.py        # Push-based reporter agent (runs on remote machines)
├── config.json        # Remote machine list + polling config
├── static/
│   └── index.html     # Single-file frontend (vanilla JS, no framework)
└── requirements.txt   # Python dependencies
```

### Data Flow

| Path | How it works |
|------|-------------|
| **Local sessions** | `watchdog` detects file changes &#8594; incremental parse (byte-offset tracking) &#8594; WebSocket broadcast |
| **Remote sessions (pull)** | Periodic SSH into each machine &#8594; runs inline Python parser on remote &#8594; JSON over stdout &#8594; merge into state |
| **Remote sessions (push)** | `reporter.py` on remote machine &#8594; `POST /api/report` &#8594; merge into state |

## Installation

### Prerequisites

- Python 3.11+
- `pip` (or `pip3`)
- SSH access to remote machines (optional, for multi-machine monitoring)
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (optional, for public tunnel access)

### Setup

```bash
# Clone the repo
git clone https://github.com/hgaurav2k/pulse.git
cd pulse

# Install Python dependencies
pip install -r requirements.txt

# Make the entry script executable (if not already)
chmod +x pulse
```

Dependencies will also be auto-installed the first time you run `./pulse` if they're missing.

## Usage

### Basic

```bash
./pulse
```

Starts the dashboard at `http://127.0.0.1:8420` and opens your browser.

### All Options

```bash
# Default: localhost:8420, auto-open browser
./pulse

# Custom port
./pulse --port 9000

# Custom host (e.g., bind to all interfaces)
./pulse --host 0.0.0.0

# Don't auto-open browser
./pulse --no-open

# Combine options
./pulse --host 0.0.0.0 --port 9000 --no-open

# Expose via Cloudflare Tunnel (local cloudflared)
./pulse --tunnel

# Expose via Cloudflare Tunnel through a remote SSH host
# (useful if your local network blocks Cloudflare)
./pulse --tunnel my-remote-server

# Show help
./pulse --help
```

### Remote Reporter (Push Mode)

Run on any remote machine to push session data to your dashboard:

```bash
# Basic — report every 10 seconds
python reporter.py --server http://dashboard-host:8420

# With authentication and custom interval
python reporter.py --server http://dashboard-host:8420 --api-key SECRET --interval 5
```

Set the `DASHBOARD_API_KEY` environment variable on the server side to require authentication:

```bash
DASHBOARD_API_KEY=SECRET ./pulse
```

### Remote Monitoring (Pull Mode)

Configure SSH-accessible machines in `config.json`:

```json
{
  "remote_machines": [],
  "poll_interval_seconds": 5,
  "ssh_timeout_seconds": 10
}
```

Machine names should match your `~/.ssh/config` hosts. The server will SSH in periodically and run an inline Python script to discover active sessions — no setup needed on the remote machines.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sessions` | GET | All session summaries |
| `/api/sessions/{id}` | GET | Single session summary |
| `/api/sessions/{id}/messages` | GET | Full conversation (local or SSH-fetched) |
| `/api/stats` | GET | Aggregate stats (total cost, tokens, active count) |
| `/api/machines` | GET | Connected machine info |
| `/api/report` | POST | Receive push reports from remote reporters |
| `/ws` | WebSocket | Real-time updates (`initial_state`, `session_update`, `stats_update`) |

## Cost Estimation

Pulse estimates costs using hardcoded per-model pricing (input, output, cache creation, cache read tokens):

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|-------------|------------|
| Opus 4.5 / 4.6 | $15.00/M | $75.00/M | $18.75/M | $1.50/M |
| Sonnet 4.5 / 4.6 | $3.00/M | $15.00/M | $3.75/M | $0.30/M |
| Haiku 4.5 | $0.80/M | $4.00/M | $1.00/M | $0.08/M |

Unknown models fall back to Sonnet-tier pricing.

## Tech Stack

- **Backend**: Python, FastAPI, uvicorn, watchdog
- **Frontend**: Vanilla JS, CSS custom properties, single HTML file
- **Remote monitoring**: asyncio SSH subprocess with inline Python scripts
- **Tunneling**: Cloudflare `cloudflared` (local or via SSH remote)
