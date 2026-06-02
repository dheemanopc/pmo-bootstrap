"""Unified bootstrap CLI (MCP-over-stdio transport).

Usage:
  python -m bootstrap install --project-name "My New Project"
  python -m bootstrap seed
  python -m bootstrap hooks
  python -m bootstrap project --name "My New Project"

Memsys transport:
  Bootstrap talks to memsys as an MCP server over stdio. It reads Claude Code's
  MCP config to find the server's spawn command. By default it looks for an
  entry named "memsys" in:
      $PWD/.mcp.json
      $PWD/.claude/settings.json
      ~/.claude.json
      ~/.config/claude/settings.json
  Override the name with --memsys-mcp-name (env PMO_MCP_SERVER_NAME), or pass an
  inline command with --memsys-mcp-cmd "mem-mcp serve".

Environment:
  PMO_FW_TEAM            (required)  framework team UUID
  PMO_PROJ_TEAM          (optional)  project team UUID; defaults to PMO_FW_TEAM
  PMO_MCP_SERVER_NAME    (optional)  name of memsys server in Claude Code config (default 'memsys')
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import init_project, install_hooks, seed_framework
from .mcp_client import MCPStdioClient, MCPError
from .mcp_config import (
    DEFAULT_SERVER_NAME,
    find_memsys_server,
    normalize_server_config,
    parse_inline_command,
)
from .memsys_client import MemsysClient, MemsysError


# ---- pretty I/O ---------------------------------------------------------

_SUPPORTS_UNICODE = sys.stdout.encoding and "utf" in sys.stdout.encoding.lower()
OK = "✓" if _SUPPORTS_UNICODE else "OK"
FAIL = "✗" if _SUPPORTS_UNICODE else "X"
ARROW = "→" if _SUPPORTS_UNICODE else "->"


def _header(text: str) -> None:
    print(f"\n{text}")
    print("-" * len(text))


def _step(idx: int, total: int, text: str) -> None:
    print(f"\n[{idx}/{total}] {text}")


def _line(symbol: str, text: str) -> None:
    print(f"  {symbol}  {text}")


def _prompt_yn(question: str, default_yes: bool = False) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    if not sys.stdin.isatty():
        return default_yes
    while True:
        try:
            ans = input(f"{question} {suffix} ").strip().lower()
        except EOFError:
            return default_yes
        if not ans:
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


# ---- config resolution -------------------------------------------------

def _resolve_teams(args) -> dict[str, str]:
    fw = args.fw_team or os.environ.get("PMO_FW_TEAM", "")
    proj = args.proj_team or os.environ.get("PMO_PROJ_TEAM", "") or fw
    return {"PMO_FW_TEAM": fw, "PMO_PROJ_TEAM": proj}


def _resolve_server_config(args) -> tuple[dict, str]:
    """Return (normalized_server_config, source_label)."""
    if args.memsys_mcp_cmd:
        return parse_inline_command(args.memsys_mcp_cmd), "--memsys-mcp-cmd"
    name = args.memsys_mcp_name or os.environ.get("PMO_MCP_SERVER_NAME") or DEFAULT_SERVER_NAME
    raw, path = find_memsys_server(name=name)
    if raw is None:
        searched = "\n    ".join(
            str(p) for p in (
                Path.cwd() / ".mcp.json",
                Path.cwd() / ".claude" / "settings.json",
                Path.home() / ".claude.json",
                Path.home() / ".config" / "claude" / "settings.json",
            )
        )
        raise MemsysError(
            f"no MCP server named '{name}' found in Claude Code config. Searched:\n    {searched}\n"
            f"Either add a stdio entry under mcpServers, set PMO_MCP_SERVER_NAME, "
            f"or pass --memsys-mcp-cmd \"<command>\"."
        )
    try:
        cfg = normalize_server_config(raw)
    except ValueError as e:
        raise MemsysError(f"server '{name}' in {path}: {e}") from None
    return cfg, str(path)


def _require_teams(teams: dict[str, str]) -> None:
    if not teams["PMO_FW_TEAM"]:
        print(f"{FAIL} missing PMO_FW_TEAM (set env or pass --fw-team)", file=sys.stderr)
        sys.exit(2)


# ---- phases -------------------------------------------------------------

def phase_verify(client: MemsysClient, teams: dict[str, str]) -> None:
    """Verify auth + that FW/PROJ teams exist; prompt to create if missing."""
    _line("...", "checking auth + team(s) via MCP")
    try:
        team_list = client.list_my_teams()
    except MemsysError as e:
        print(f"{FAIL}  memsys MCP call failed: {e}", file=sys.stderr)
        sys.exit(3)
    by_id = {t.get("id") or t.get("team_id"): t for t in team_list}
    by_name = {t.get("name", ""): t for t in team_list}
    needed = [("FW_TEAM", teams["PMO_FW_TEAM"])]
    if teams["PMO_PROJ_TEAM"] and teams["PMO_PROJ_TEAM"] != teams["PMO_FW_TEAM"]:
        needed.append(("PROJ_TEAM", teams["PMO_PROJ_TEAM"]))
    for label, value in needed:
        if value in by_id or value in by_name:
            _line(OK, f"team {label}={value!s} exists")
            continue
        if not _prompt_yn(f"  Team {label}={value!r} not found. Create it?", default_yes=False):
            print(f"{FAIL}  refusing to continue without team {label}", file=sys.stderr)
            sys.exit(4)
        try:
            result = client.team_create(name=value)
            new_id = (result or {}).get("id") or (result or {}).get("team_id")
            _line(OK, f"team {label} created (id={new_id})")
            if label == "FW_TEAM":
                teams["PMO_FW_TEAM"] = new_id or value
            elif label == "PROJ_TEAM":
                teams["PMO_PROJ_TEAM"] = new_id or value
        except MemsysError as e:
            print(f"{FAIL}  could not create team {label}: {e}", file=sys.stderr)
            sys.exit(5)


def phase_seed(client: MemsysClient, fw_team: str, dry_run: bool = False) -> dict:
    def on_progress(slug, status, info):
        sym = OK if status in ("written", "skipped", "superseded") else (
            "..." if "dry-run" in str(status) else FAIL
        )
        info_str = f" (id={info})" if info else ""
        _line(sym, f"{slug:<48} {status}{info_str}")

    summary = seed_framework.seed(client, fw_team, dry_run=dry_run, on_progress=on_progress)
    _line(
        OK if summary["errors"] == 0 else FAIL,
        f"summary: written={summary['written']} skipped={summary['skipped']} "
        f"superseded={summary['superseded']} errors={summary['errors']}",
    )
    return summary


def phase_hooks(server_config: dict, teams: dict[str, str]) -> dict:
    result = install_hooks.install_all(
        server_config=server_config,
        fw_team=teams["PMO_FW_TEAM"],
        proj_team=teams["PMO_PROJ_TEAM"],
    )
    for path in result["copy"]["copied"]:
        _line(OK, f"copied {path}")
    _line(OK, f"linked {result['link']['linked']} ({result['link']['method']})")
    _line(OK, f"config written {result['config']}")
    wire = result["wire"]
    _line(
        OK,
        f"settings.json {wire['settings_path']} "
        f"(stop_added={wire['stop_added']}, session_start_added={wire['session_start_added']})",
    )
    if wire.get("backup"):
        _line("...", f"backup at {wire['backup']}")
    nuke = result["nuke_project"]
    _line(OK, f"project Stop-hook check: {nuke['action']} ({nuke['path']})")
    return result


def phase_project(
    client: MemsysClient, teams: dict[str, str], project_name: str, intent: str
) -> init_project.ProjectResult:
    result = init_project.init_project(
        client,
        project_name=project_name,
        intent=intent,
        proj_team=teams["PMO_PROJ_TEAM"],
        fw_team=teams["PMO_FW_TEAM"],
    )
    _line(OK, f"slug              = {result.slug}")
    _line(OK, f"manifest_id       = {result.manifest_id}")
    _line(OK, f"registry_id       = {result.registry_id}")
    _line(OK, f"user_intent_id    = {result.intent_id}")
    return result


def _read_intent_interactively(project_name: str) -> str:
    if not sys.stdin.isatty():
        return f"Project '{project_name}' (no intent captured — non-interactive run)."
    print(f"\nDescribe the project '{project_name}' in 1-3 sentences (end with blank line):")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    return "\n".join(lines) or f"Project '{project_name}' (no intent provided)."


# ---- commands -----------------------------------------------------------

def _open_client_and_run(args, fn) -> int:
    """Common pattern: resolve server config, open MCP client, hand to fn."""
    teams = _resolve_teams(args)
    _require_teams(teams)
    try:
        server_config, source = _resolve_server_config(args)
    except MemsysError as e:
        print(f"{FAIL}  {e}", file=sys.stderr)
        return 2
    _line(OK, f"memsys MCP: {' '.join(server_config['command'])}  (from {source})")
    try:
        with MCPStdioClient(
            command=server_config["command"],
            env=server_config.get("env"),
            cwd=server_config.get("cwd"),
        ) as mcp:
            client = MemsysClient(mcp=mcp)
            return fn(client, server_config, teams)
    except MCPError as e:
        print(f"{FAIL}  MCP error: {e}", file=sys.stderr)
        return 3


def cmd_install(args) -> int:
    def run(client: MemsysClient, server_config: dict, teams: dict[str, str]) -> int:
        _step(0, 4, "Verify memsys auth + teams")
        phase_verify(client, teams)

        steps_left = sum(1 for x in (args.skip_framework, args.skip_hooks, args.skip_project) if not x)
        idx = 0

        if not args.skip_framework:
            idx += 1
            _step(idx, steps_left, "Seed framework into memsys")
            phase_seed(client, teams["PMO_FW_TEAM"], dry_run=args.dry_run)

        if not args.skip_hooks:
            idx += 1
            _step(idx, steps_left, "Install Claude Code hooks + pmo CLI")
            if args.dry_run:
                _line("...", "dry-run: would copy substrate, link CLI, edit ~/.claude/settings.json")
            else:
                phase_hooks(server_config, teams)

        if not args.skip_project:
            idx += 1
            if not args.project_name:
                print(f"{FAIL}  --project-name required (or use --skip-project)", file=sys.stderr)
                return 2
            intent = args.intent or _read_intent_interactively(args.project_name)
            _step(idx, steps_left, f"Scaffold project '{args.project_name}'")
            if args.dry_run:
                _line("...", "dry-run: would write manifest, registry, user-intent")
            else:
                try:
                    phase_project(client, teams, args.project_name, intent)
                except MemsysError as e:
                    print(f"{FAIL}  {e}", file=sys.stderr)
                    return 6

        _header("Setup complete.")
        if not args.skip_project and not args.dry_run and args.project_name:
            slug = init_project.kebab(args.project_name)
            print(f"  Next: spawn the PM role to start project dialogue.")
            print(f"        pmo spawn {slug} pm --window")
            print()
        return 0
    return _open_client_and_run(args, run)


def cmd_seed(args) -> int:
    def run(client, server_config, teams):
        _step(1, 1, "Seed framework")
        phase_seed(client, teams["PMO_FW_TEAM"], dry_run=args.dry_run)
        return 0
    return _open_client_and_run(args, run)


def cmd_hooks(args) -> int:
    teams = _resolve_teams(args)
    _require_teams(teams)
    try:
        server_config, source = _resolve_server_config(args)
    except MemsysError as e:
        print(f"{FAIL}  {e}", file=sys.stderr)
        return 2
    _line(OK, f"memsys MCP: {' '.join(server_config['command'])}  (from {source})")
    _step(1, 1, "Install hooks + CLI")
    if args.dry_run:
        _line("...", "dry-run mode — nothing written")
        return 0
    phase_hooks(server_config, teams)
    return 0


def cmd_project(args) -> int:
    def run(client, server_config, teams):
        intent = args.intent or _read_intent_interactively(args.name)
        _step(1, 1, f"Scaffold project '{args.name}'")
        try:
            phase_project(client, teams, args.name, intent)
        except MemsysError as e:
            print(f"{FAIL}  {e}", file=sys.stderr)
            return 6
        return 0
    return _open_client_and_run(args, run)


# ---- argparse -----------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memsys-mcp-cmd", help='inline server command, e.g. "mem-mcp serve"')
    p.add_argument(
        "--memsys-mcp-name",
        help=f"name of memsys server in Claude Code mcp config (default: {DEFAULT_SERVER_NAME})",
    )
    p.add_argument("--fw-team", help="framework team UUID (env PMO_FW_TEAM)")
    p.add_argument("--proj-team", help="project team UUID (env PMO_PROJ_TEAM; defaults to fw-team)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmo-bootstrap", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("install", help="one-command: verify + seed + hooks + project")
    _add_common(pi)
    pi.add_argument("--project-name", help="project name; derives kebab slug")
    pi.add_argument("--intent", help="project intent (1-3 sentences). If omitted, prompts.")
    pi.add_argument("--skip-framework", action="store_true")
    pi.add_argument("--skip-hooks", action="store_true")
    pi.add_argument("--skip-project", action="store_true")
    pi.set_defaults(fn=cmd_install)

    ps = sub.add_parser("seed", help="seed framework artifacts into FW_TEAM")
    _add_common(ps)
    ps.set_defaults(fn=cmd_seed)

    ph = sub.add_parser("hooks", help="install Claude Code hooks + pmo CLI locally")
    _add_common(ph)
    ph.set_defaults(fn=cmd_hooks)

    pp = sub.add_parser("project", help="scaffold a new project in PROJ_TEAM")
    _add_common(pp)
    pp.add_argument("--name", required=True, help="project name")
    pp.add_argument("--intent", help="project intent")
    pp.set_defaults(fn=cmd_project)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
