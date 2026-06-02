# bootstrap

One-command setup: seed the PMO framework into a fresh memsys, install Claude Code
hooks + the `pmo` CLI, and scaffold a new project.

**Transport: MCP-over-stdio.** Bootstrap spawns your memsys MCP server as a
subprocess and talks JSON-RPC to it. No HTTP, no token in env, no URL config.
Just point the tool at your existing memsys MCP server (the same one Claude
Code uses) and run.

## Prerequisites

- Python 3.10+
- A memsys MCP server (e.g. `mem-mcp serve`) reachable via stdio
- The memsys server configured in Claude Code's settings as an `mcpServers` entry
  named `memsys` (or any name — pass `--memsys-mcp-name` to override)

## Environment

```bash
export PMO_FW_TEAM=<framework-team-uuid>
export PMO_PROJ_TEAM=<project-team-uuid>     # optional; defaults to PMO_FW_TEAM
# Optional:
export PMO_MCP_SERVER_NAME=memsys            # name of the MCP server in Claude Code config
```

## One-command install

```bash
python -m bootstrap install --project-name "My New Project"
```

Phases (each idempotent; safe to re-run):

1. **Verify** — open MCP connection + confirm FW_TEAM / PROJ_TEAM exist; prompt to create if missing.
2. **Seed framework** — write the 18 framework memories into FW_TEAM (skips slugs that already exist with identical content; supersedes if drift).
3. **Install hooks + CLI** — copy `pmo`, `pmo_mux.py`, `mcp_client.py`, `memsys_client.py`, and the two Python hooks into `~/.pmo/`; symlink `~/.pmo/pmo` to `~/.local/bin/pmo`; edit `~/.claude/settings.json` to register the Stop + SessionStart hooks (backups created); remove any project-level Stop hook that would double-fire.
4. **Scaffold project** — write 3 memories into PROJ_TEAM: manifest (with POINTERS to FW_TEAM slugs), session-registry, verbatim user-intent.

After install:

```bash
pmo spawn <slug> pm --window
```

starts the PM in a tmux/psmux window. PM does its on-resume warm-start (loads
its role-def + matrix + manifest thread from memsys) and engages you.

## Resolving the memsys MCP server

The setup tool finds the spawn command from Claude Code's MCP config. Searched
in this order:

1. `$PWD/.mcp.json`
2. `$PWD/.claude/settings.json`
3. `~/.claude.json`
4. `~/.config/claude/settings.json`

Looking for a stdio entry like:

```json
{
  "mcpServers": {
    "memsys": {
      "command": "mem-mcp",
      "args": ["serve"],
      "env": {"MEM_MCP_DSN": "postgresql://..."}
    }
  }
}
```

(Both top-level `mcpServers` and per-project `projects.<cwd>.mcpServers`
are supported.)

**Overrides:**

- `--memsys-mcp-name <name>` — pick a different server name (default: `memsys`).
- `--memsys-mcp-cmd "<command>"` — bypass discovery; specify the spawn command inline.
- `PMO_MCP_SERVER_NAME` env — same as `--memsys-mcp-name`.

HTTP/SSE MCP entries (`"type":"http"`) are NOT supported — bootstrap only speaks
stdio. Add a stdio entry, or pass `--memsys-mcp-cmd`.

## Sub-commands

```bash
python -m bootstrap seed        # just seed framework
python -m bootstrap hooks       # just install hooks + CLI
python -m bootstrap project --name "Foo"   # just scaffold a project
```

All accept `--dry-run` to preview without writing.

## What the hooks do

- `pmo_stop_hook.py` — on every Claude Code Stop event, scans the transcript for a
  trailing `@PMO NEXT resume=<id>` or `@pmo /resume <id>` line and, if present,
  sends it via `tmux send-keys` (with the bracketed-paste workaround) so the
  window switches to the target role. Includes idempotency guard (120s).
- `pmo_session_start_hook.py` — on every Claude Code SessionStart, reads
  `PMO_ROLE` + `PMO_PROJECT_SLUG` from env and `session_id` from the hook
  payload, writes the row into both the local registry JSON (`~/.pmo/...`)
  and the memsys session-registry memory (spawns a short-lived memsys MCP
  subprocess per session-start — typical overhead ~100-500ms).

Both are pure Python (stdlib only). Multiplexer auto-detect (tmux on Mac/Linux,
psmux on Windows) via `pmo_mux.py`.

## Files installed under `~/.pmo/`

```
~/.pmo/
  pmo                            # Python CLI: pmo spawn / up / where / resume
  pmo_mux.py                     # mux abstraction (tmux/psmux)
  pmo_stop_hook.py               # Claude Code Stop hook
  pmo_session_start_hook.py      # Claude Code SessionStart hook
  memsys_client.py               # high-level memsys API
  mcp_client.py                  # stdio JSON-RPC transport
  mcp_config.py                  # Claude Code config finder
  config.json                    # MEMSYS_MCP server snapshot + team UUIDs (chmod 0600)
  pmo-project-<slug>-session-registry.json   # local cache (per project)
```

## Idempotency + safety

- Re-running `install` is safe. Each phase is idempotent.
- `~/.claude/settings.json` is backed up before editing (suffix `.pmo-bak.<ts>`).
- `$PWD/.claude/settings.json` Stop hook is removed (F1 prevention from
  `architecture/substrate-stop-hook.md`).
- `config.json` is `0600` because it holds the memsys server `env` (which may
  contain a DB DSN or token).

## Troubleshooting

- **`no MCP server named 'memsys' found in Claude Code config`** — add a stdio
  entry under `mcpServers` in one of the searched files, OR pass
  `--memsys-mcp-cmd "mem-mcp serve"` to bypass discovery.
- **`could not spawn MCP server`** — the `command` field points at something
  not on PATH. Either install it, give an absolute path, or fix your PATH.
- **`initialize failed: ... [server stderr] ...`** — memsys server crashed at
  boot. The stderr tail (last 10 lines) is appended to the error; usually a
  config issue (missing DSN, wrong DB, etc.).
- **`team … not found`** — answer the create-team prompt with `y`, or
  pre-create the team in memsys.
- **Hook fires but window doesn't switch** — check `/tmp/pmo-stop-hook-trace.log`.
  The diagnostic recipes in `architecture/substrate-stop-hook.md` §2 are the playbook.
- **Windows + psmux** — `psmux` must be on `PATH`. The mux adapter mirrors
  tmux's CLI. Untested on real psmux; please report issues.
