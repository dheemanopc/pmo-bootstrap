#!/usr/bin/env python3
"""PMO Claude Code SessionStart hook — role self-registration (Python, cross-platform).

When a spawned role session starts, write its session-id into the project's
session-registry. Reads session_id from the hook payload (Claude Code provides it)
and role + project from env (PMO_ROLE, PMO_PROJECT_SLUG) set at spawn.

Registry writes:
  1. Always: local JSON cache at $PMO_HOME/pmo-project-<slug>-session-registry.json.
  2. If ~/.pmo/config.json has a MEMSYS_MCP block: spawn the memsys MCP server
     subprocess, supersede the project's session-registry memory in memsys.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# These are co-located in ~/.pmo/ after install.
try:
    from memsys_client import MemsysClient, MemsysError  # type: ignore
except ImportError:
    MemsysClient = None  # graceful — local cache only
    MemsysError = Exception  # type: ignore


PMO_HOME = Path(os.environ.get("PMO_HOME", str(Path.home() / ".pmo")))
CONFIG_PATH = PMO_HOME / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def update_local_registry(slug: str, role: str, sid: str) -> tuple[Path, dict]:
    PMO_HOME.mkdir(parents=True, exist_ok=True)
    reg_path = PMO_HOME / f"pmo-project-{slug}-session-registry.json"
    try:
        data = json.loads(reg_path.read_text()) if reg_path.exists() else {"rows": {}}
    except (OSError, json.JSONDecodeError):
        data = {"rows": {}}
    data.setdefault("rows", {})[role] = {
        "claude_session_id": sid,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    reg_path.write_text(json.dumps(data, indent=2))
    return reg_path, data


def update_memsys_registry(slug: str, rows: dict, cfg: dict) -> str | None:
    """Spawn memsys via MCP, supersede/write the session-registry memory."""
    mcp_cfg = cfg.get("MEMSYS_MCP") or {}
    if not MemsysClient or not mcp_cfg.get("command"):
        return None
    team_id = cfg.get("PMO_PROJ_TEAM") or cfg.get("PMO_FW_TEAM")
    if not team_id:
        return None
    server_config = {
        "command": list(mcp_cfg["command"]),
        "env": dict(mcp_cfg.get("env") or {}),
        "cwd": mcp_cfg.get("cwd"),
    }
    slug_clue = f"pmo-project-{slug}-session-registry"
    content = json.dumps({"rows": rows}, indent=2)
    try:
        with MemsysClient(server_config=server_config) as client:
            existing = client.slug_lookup(team_id=team_id, resource_type="decision", slug=slug_clue)
            if existing and existing.get("memory_id"):
                client.memory_supersede(id=existing["memory_id"], content=content)
                return existing["memory_id"]
            result = client.memory_write(
                team_id=team_id,
                type="decision",
                slug_clue=slug_clue,
                content=content,
                tags=["pmo", "pmo-session-registry", f"pmo-project-{slug}", "current"],
                indexable=False,
            )
            return result.get("id") if isinstance(result, dict) else None
    except MemsysError:
        return None
    except Exception:
        return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}

    sid = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "")
    role = os.environ.get("PMO_ROLE", "")
    slug = os.environ.get("PMO_PROJECT_SLUG", "")

    if not (sid and role and slug):
        print("{}")
        return 0

    reg_path, data = update_local_registry(slug, role, sid)
    rows = data.get("rows", {})
    cfg = load_config()
    update_memsys_registry(slug, rows, cfg)

    print(json.dumps({"systemMessage": f"pmo: registered {role} -> {sid} for {slug}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
