#!/usr/bin/env python3
"""stdio→HTTP shim — lets MCP-over-stdio clients (e.g. pmo-bootstrap) talk to
the local mem-mcp HTTP server.

Reads JSON-RPC requests line-by-line from stdin, forwards each as a POST to
http://localhost:8080/mcp (or MEM_MCP_HTTP_URL) with Authorization: Bearer
$MEM_MCP_TOKEN. Writes responses line-by-line to stdout.

initialize / initialized are handled locally — no need to round-trip them.

Usage:
    MEM_MCP_TOKEN=$(scripts/dev-token.sh | sed 's/.*=//') \\
    python /codes/ai-work/pmo-bootstrap/scripts/mem-mcp-stdio-shim.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


HTTP_URL = os.environ.get("MEM_MCP_HTTP_URL", "http://localhost:8080/mcp")
TOKEN = os.environ.get("MEM_MCP_TOKEN", "")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _post(payload: dict) -> dict:
    if not TOKEN:
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32001, "message": "MEM_MCP_TOKEN env not set"},
        }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        HTTP_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)
        except Exception:
            err = {"error": body}
        _log(f"shim: HTTP {e.code} from {HTTP_URL}: {err}")
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32002, "message": f"HTTP {e.code}", "data": err},
        }
    except Exception as e:
        _log(f"shim: transport error: {type(e).__name__}: {e}")
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32003, "message": f"shim transport error: {e}"},
        }


def _handle_initialize(req: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req.get("id"),
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "mem-mcp-stdio-shim", "version": "0.1.0"},
        },
    }


def main() -> int:
    _log(f"shim: starting; HTTP_URL={HTTP_URL}; TOKEN={'set' if TOKEN else 'MISSING'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"shim: bad JSON from client: {e}; line={line[:200]}")
            continue
        method = req.get("method", "")
        if "id" not in req:
            continue  # notification — no response
        if method == "initialize":
            resp = _handle_initialize(req)
        else:
            resp = _post(req)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
