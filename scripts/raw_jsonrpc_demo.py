"""Shows the raw JSON-RPC messages MCP sends over stdio (no SDK client involved)."""

import json
import subprocess
import sys

MESSAGES = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "raw", "version": "0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "greet", "arguments": {"name": "raw"}},
    },
]

proc = subprocess.Popen(  # noqa: S603
    [sys.executable, "-m", "apps.hello_server.server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
if proc.stdin is None or proc.stdout is None:
    raise RuntimeError("subprocess pipes unavailable")
for msg in MESSAGES:
    print(">>> CLIENT:", json.dumps(msg))
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if "id" in msg:
        print("<<< SERVER:", proc.stdout.readline().strip(), "\n")
proc.terminate()
