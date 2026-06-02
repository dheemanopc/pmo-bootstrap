"""Install PMO hooks + CLI into the user's environment.

Copies hooks/* + cli/* + mcp_client + memsys_client into ~/.pmo/, edits
~/.claude/settings.json to register the Stop + SessionStart hooks, and writes a
~/.pmo/config.json snapshot of the memsys MCP server config + team UUIDs so the
session-start hook can spawn its own short-lived MCP connection to update the
memsys registry mirror.

Idempotent. Backs up settings.json before editing. Nukes any project-level
Stop hook (F1 prevention).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PMO_HOME = Path(os.environ.get("PMO_HOME", str(Path.home() / ".pmo")))
LOCAL_BIN = Path(os.environ.get("PMO_LOCAL_BIN", str(Path.home() / ".local" / "bin")))
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


# (src_relative, dst_filename, executable)
HOOK_FILES = [
    ("bootstrap/hooks/pmo_stop_hook.py", "pmo_stop_hook.py", True),
    ("bootstrap/hooks/pmo_session_start_hook.py", "pmo_session_start_hook.py", True),
    ("bootstrap/memsys_client.py", "memsys_client.py", False),
    ("bootstrap/mcp_client.py", "mcp_client.py", False),
    ("bootstrap/mcp_config.py", "mcp_config.py", False),
    ("bootstrap/cli/pmo_mux.py", "pmo_mux.py", False),
    ("bootstrap/cli/pmo", "pmo", True),
]


def _make_executable(path: Path) -> None:
    if platform.system() == "Windows":
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + f".pmo-bak.{int(time.time())}")
    shutil.copy2(path, bak)
    return bak


def copy_substrate() -> dict:
    PMO_HOME.mkdir(parents=True, exist_ok=True)
    copied = []
    for src_rel, dst_name, executable in HOOK_FILES:
        src = REPO_ROOT / src_rel
        if not src.exists():
            raise FileNotFoundError(f"missing source file: {src}")
        dst = PMO_HOME / dst_name
        shutil.copyfile(src, dst)
        if executable:
            _make_executable(dst)
        copied.append(str(dst))
    return {"copied": copied, "pmo_home": str(PMO_HOME)}


def link_cli() -> dict:
    """Symlink ~/.pmo/pmo -> ~/.local/bin/pmo (or copy on Windows)."""
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    src = PMO_HOME / "pmo"
    dst = LOCAL_BIN / "pmo"
    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
        except OSError:
            pass
    if platform.system() == "Windows":
        # Symlinks need admin on Windows; just copy.
        shutil.copyfile(src, dst)
        return {"linked": str(dst), "method": "copy"}
    try:
        dst.symlink_to(src)
        return {"linked": str(dst), "method": "symlink"}
    except OSError:
        shutil.copyfile(src, dst)
        _make_executable(dst)
        return {"linked": str(dst), "method": "copy"}


def write_config(server_config: dict, fw_team: str, proj_team: str) -> Path:
    """Write ~/.pmo/config.json with MCP server spawn config + team UUIDs.

    The session-start hook reads this to update the memsys registry mirror.
    Chmod 0600 because server `env` may contain a DSN / token.
    """
    PMO_HOME.mkdir(parents=True, exist_ok=True)
    cfg_path = PMO_HOME / "config.json"
    cfg = {
        "MEMSYS_MCP": {
            "command": list(server_config["command"]),
            "env": dict(server_config.get("env") or {}),
            "cwd": server_config.get("cwd"),
        },
        "PMO_FW_TEAM": fw_team,
        "PMO_PROJ_TEAM": proj_team,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))
    if platform.system() != "Windows":
        cfg_path.chmod(0o600)
    return cfg_path


def _hook_command_for(name: str) -> str:
    """The command string written into ~/.claude/settings.json."""
    return f'{sys.executable} "{PMO_HOME / name}"'


def _ensure_hook(hooks_section: dict, event: str, command: str) -> bool:
    target_tail = command.split()[-1].strip('"')
    entries = hooks_section.setdefault(event, [])
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("command", "").endswith(target_tail) or h.get("command") == command:
                return False
    entries.append({"hooks": [{"type": "command", "command": command}]})
    return True


def wire_user_hooks() -> dict:
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(CLAUDE_SETTINGS)
    if CLAUDE_SETTINGS.exists():
        try:
            cfg = json.loads(CLAUDE_SETTINGS.read_text())
        except json.JSONDecodeError:
            cfg = {}
    else:
        cfg = {}

    hooks = cfg.setdefault("hooks", {})
    stop_added = _ensure_hook(hooks, "Stop", _hook_command_for("pmo_stop_hook.py"))
    start_added = _ensure_hook(hooks, "SessionStart", _hook_command_for("pmo_session_start_hook.py"))
    CLAUDE_SETTINGS.write_text(json.dumps(cfg, indent=2))
    return {
        "settings_path": str(CLAUDE_SETTINGS),
        "backup": str(backup) if backup else None,
        "stop_added": stop_added,
        "session_start_added": start_added,
    }


def nuke_project_stop_hook(project_dir: Path | None = None) -> dict:
    if project_dir is None:
        project_dir = Path.cwd()
    proj_settings = project_dir / ".claude" / "settings.json"
    if not proj_settings.exists():
        return {"path": str(proj_settings), "action": "skip-noexist"}
    backup = _backup(proj_settings)
    try:
        cfg = json.loads(proj_settings.read_text())
    except json.JSONDecodeError:
        return {"path": str(proj_settings), "action": "skip-unreadable"}
    hooks = cfg.get("hooks") or {}
    if "Stop" in hooks:
        del hooks["Stop"]
        if not hooks:
            cfg.pop("hooks", None)
        else:
            cfg["hooks"] = hooks
        proj_settings.write_text(json.dumps(cfg, indent=2))
        return {"path": str(proj_settings), "action": "removed-stop", "backup": str(backup) if backup else None}
    return {"path": str(proj_settings), "action": "skip-no-stop-hook"}


def install_all(server_config: dict, fw_team: str, proj_team: str) -> dict:
    return {
        "copy": copy_substrate(),
        "link": link_cli(),
        "config": str(write_config(server_config, fw_team, proj_team)),
        "wire": wire_user_hooks(),
        "nuke_project": nuke_project_stop_hook(),
    }
