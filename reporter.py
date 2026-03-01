"""Remote reporter for Claude Code Dashboard.

Runs on remote machines, periodically pushes session data to the central dashboard.

Usage:
    python reporter.py --server http://dashboard-host:8420 [--api-key SECRET] [--interval 10]
"""

import argparse
import os
import socket
import time

import requests

from parser import parse_all_sessions

CLAUDE_DIR = os.path.expanduser("~/.claude")


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Dashboard Remote Reporter"
    )
    parser.add_argument(
        "--server",
        required=True,
        help="Dashboard server URL (e.g. http://192.168.1.10:8420)",
    )
    parser.add_argument("--api-key", default="", help="Shared secret for auth")
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Report interval in seconds (default: 10)",
    )
    args = parser.parse_args()

    hostname = socket.gethostname()
    server_url = args.server.rstrip("/")
    report_url = f"{server_url}/api/report"

    print(f"Reporter started: machine={hostname}, server={server_url}, interval={args.interval}s")

    while True:
        try:
            sessions = parse_all_sessions(CLAUDE_DIR, machine=hostname)
            payload = {
                "machine": hostname,
                "api_key": args.api_key,
                "sessions": [s.to_dict() for s in sessions],
            }
            resp = requests.post(report_url, json=payload, timeout=10)
            active = sum(1 for s in sessions if s.is_active)
            print(
                f"Reported {len(sessions)} sessions ({active} active) -> {resp.status_code}"
            )
        except requests.RequestException as e:
            print(f"Report failed: {e}")
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
