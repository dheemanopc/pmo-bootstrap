"""Find the memsys MCP server config in Claude Code's settings files.

Searches, in order:
  1. $PWD/.mcp.json
  2. $PWD/.claude/settings.json
  3. ~/.claude.json
  4. ~/.config/claude/settings.json

In each, look for a top-level `mcpServers` object, OR a per-project nested
`projects.<cwd>.mcpServers`. Match by server name (default "memsys", override
via env PMO_MCP_SERVER_NAME).

Returns the server config dict: {command, args, env, cwd} — or None if not found.

The same shape MCP clients (Claude Code, Continue, others) use for stdio servers:
    {"command": "mem-mcp", "args": ["serve"], "env": {"MEM_MCP_DSN": "..."}}

HTTP/SSE servers (shape {"type":"http","url":...}) are NOT supported — bootstrap
uses stdio. Tell user to add a stdio entry, or pass --memsys-mcp-cmd to override.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_SERVER_NAME = "memsys"


def _candidate_paths() -> list[Path]:
    home = Path.home()
    cwd = Path.cwd()
    return [
        cwd / ".mcp.json",
        cwd / ".claude" / "settings.json",
        home / ".claude.json",
        home / ".config" / "claude" / "settings.json",
    ]


def _try_load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _extract_servers(config: dict) -> dict:
    """Pull mcpServers from a config blob, including per-project nesting."""
    servers: dict = {}
    top = config.get("mcpServers")
    if isinstance(top, dict):
        servers.update(top)
    projects = config.get("projects")
    if isinstance(projects, dict):
        cwd = str(Path.cwd())
        # Prefer cwd-specific project entry; merge any others as lower priority.
        for key, proj in projects.items():
            if not isinstance(proj, dict):
                continue
            proj_servers = proj.get("mcpServers")
            if not isinstance(proj_servers, dict):
                continue
            if key == cwd:
                # cwd-match wins — overrides previous.
                servers.update(proj_servers)
            else:
                # only add keys not already present
                for k, v in proj_servers.items():
                    servers.setdefault(k, v)
    return servers


def find_memsys_server(
    name: str | None = None,
    extra_paths: list[Path] | None = None,
) -> tuple[dict, Path] | tuple[None, None]:
    """Return (server_config, source_path) or (None, None) if not found.

    server_config is the raw dict from Claude Code's settings — has command/args/env etc.
    """
    target = name or os.environ.get("PMO_MCP_SERVER_NAME") or DEFAULT_SERVER_NAME
    paths = list(extra_paths or []) + _candidate_paths()
    for path in paths:
        data = _try_load(path)
        if not data:
            continue
        servers = _extract_servers(data)
        if target in servers and isinstance(servers[target], dict):
            return servers[target], path
    return None, None


def normalize_server_config(raw: dict) -> dict:
    """Normalize a server config to {command, args, env, cwd}.

    Rejects HTTP/SSE servers with a clear message."""
    if raw.get("type") in ("http", "sse"):
        raise ValueError(
            f"server entry is HTTP/SSE (type={raw.get('type')}); bootstrap requires a stdio entry. "
            f"Add a local stdio entry for memsys (or use --memsys-mcp-cmd to override)."
        )
    cmd = raw.get("command")
    if not cmd:
        raise ValueError("server entry has no `command`")
    args = list(raw.get("args") or [])
    env = dict(raw.get("env") or {})
    cwd = raw.get("cwd")
    return {
        "command": [cmd] + args,
        "env": env,
        "cwd": cwd,
    }


def parse_inline_command(s: str) -> dict:
    """Parse a shell-style command string into {command, env, cwd} for --memsys-mcp-cmd flag."""
    import shlex
    parts = shlex.split(s)
    if not parts:
        raise ValueError("empty command")
    return {"command": parts, "env": {}, "cwd": None}
