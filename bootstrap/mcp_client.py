"""Minimal MCP stdio client. Stdlib only.

Spawns an MCP server as a subprocess and talks line-delimited JSON-RPC over its
stdin/stdout. Implements just enough of the protocol for the bootstrap tool:

  - initialize handshake (+ initialized notification)
  - tools/call with optional argument map
  - clean shutdown on exit

Usage:
    with MCPStdioClient(["mem-mcp", "serve"], env={...}) as client:
        result = client.call_tool("memory_write", {"team_id": ..., ...})

Notes:
  - MCP tool results have shape {"content": [{"type":"text","text":"<json>"}], "isError": bool}.
    `call_tool` unwraps the first text block and JSON-decodes it; if not JSON, returns the raw string.
  - Notifications from the server (e.g. logging) are silently consumed.
  - Server stderr is captured separately and surfaced in error messages.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any


class MCPError(RuntimeError):
    pass


PROTOCOL_VERSION = "2024-11-05"


class MCPStdioClient:
    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        client_name: str = "pmo-bootstrap",
        client_version: str = "0.1.0",
        startup_timeout: float = 15.0,
    ):
        if not command:
            raise MCPError("MCP server command is empty")
        self.command = command
        self.env = env
        self.cwd = cwd
        self.client_name = client_name
        self.client_version = client_version
        self.startup_timeout = startup_timeout
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None

    # ---- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "MCPStdioClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        merged_env = os.environ.copy()
        if self.env:
            merged_env.update({k: str(v) for k, v in self.env.items()})
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                cwd=self.cwd,
                text=True,
                bufsize=1,  # line-buffered
            )
        except (FileNotFoundError, PermissionError) as e:
            raise MCPError(
                f"could not spawn MCP server {self.command!r}: {e}. "
                f"Is it installed and on PATH?"
            ) from None

        # Drain stderr to a buffer in the background so it doesn't block, and surfaces in errors.
        def _drain_stderr() -> None:
            assert self._proc is not None and self._proc.stderr is not None
            for line in self._proc.stderr:
                self._stderr_lines.append(line.rstrip("\n"))

        self._stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        self._stderr_thread.start()

        # initialize handshake.
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
                timeout=self.startup_timeout,
            )
        except MCPError as e:
            self.close()
            raise MCPError(f"initialize failed: {e}{self._stderr_tail()}") from None
        self._notification("notifications/initialized", {})

    def close(self) -> None:
        if not self._proc:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    # ---- protocol ---------------------------------------------------------

    def _send(self, msg: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPError("server not started")
        try:
            line = json.dumps(msg) + "\n"
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"send failed: {e}{self._stderr_tail()}") from None

    def _read_response(self, expected_id: int, timeout: float | None = None) -> dict:
        if not self._proc or not self._proc.stdout:
            raise MCPError("server not started")
        # If timeout is set, we'd need a watchdog thread. Skipping for v1 — relies on
        # the MCP server responding promptly. Add later if real-world hangs surface.
        for raw_line in self._proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Some servers log non-JSON to stdout; skip.
                continue
            if "id" not in msg:
                # Notification; consume and move on.
                continue
            if msg.get("id") != expected_id:
                # Out-of-order response (shouldn't happen with sequential req/resp); skip.
                continue
            if "error" in msg:
                err = msg["error"]
                raise MCPError(f"JSON-RPC error {err.get('code')}: {err.get('message')}")
            return msg.get("result", {})
        raise MCPError(f"server closed without responding to request id={expected_id}{self._stderr_tail()}")

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        self._req_id += 1
        rid = self._req_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self._read_response(rid, timeout=timeout)

    def _notification(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _stderr_tail(self, n: int = 10) -> str:
        if not self._stderr_lines:
            return ""
        tail = self._stderr_lines[-n:]
        return "\n  [server stderr]\n    " + "\n    ".join(tail)

    # ---- high-level -------------------------------------------------------

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """Call an MCP tool. Returns the parsed result (JSON if response is JSON text,
        else the raw text/object)."""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            content = result.get("content", [])
            msg = "; ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            ) or "tool returned isError=true"
            raise MCPError(f"tool {name}: {msg}")
        content = result.get("content", [])
        if not content:
            return result
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
        return result
