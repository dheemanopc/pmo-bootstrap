# pmo-bootstrap

**One-command installer** for the PMO on a fresh memsys: seeds the framework
(roles, matrix, grammar, vocab, area-E substrate plan), installs the Claude Code
hooks + the `pmo` CLI on the operator's machine, and scaffolds a new project.

The PMO is a multi-role project-orchestration layer that runs **on top of
[memsys](https://memsys.dheemantech.in) (the Personal Memory MCP)**. Role
agents (PM, PA, DM, DA, Developer, Reviewer) coordinate entirely through memory
reads/writes and a small directive grammar; a `tmux`/`psmux` substrate routes
the operator's terminal between role sessions.

## Repository layout

| Directory       | What's in it                                                               |
|-----------------|----------------------------------------------------------------------------|
| [`bootstrap/`](./bootstrap)     | The installer. `python -m bootstrap install …` is the one command. |
| [`roles/`](./roles)             | The six role-prompt templates (PM/PA/DM/DA/Developer/Reviewer).        |
| [`config/`](./config)           | Permission matrix, role-configs, directive grammar, vocab, resolvers.    |
| [`architecture/`](./architecture) | Project-manifest spec, Area E substrate plan, routing config, stop-hook reference. |
| [`backlog/`](./backlog)         | v2 workstreams (project-setup generator, token optimization).             |
| [`_source/memories.json`](./_source/memories.json) | Verbatim export of the framework memories from memsys (source-of-truth for the seeder). |
| [`tools/build_pmo_repo.py`](./tools/build_pmo_repo.py) | Regenerator: rebuilds `roles/`, `config/`, `architecture/`, `backlog/` from `_source/memories.json`. |

A full catalog is in [`INDEX.md`](./INDEX.md).

## Quick start

```bash
# 1. Export your memsys creds
export MEMSYS_URL=https://memsys.example.com
export MEMSYS_TOKEN=...
export PMO_FW_TEAM=<framework-team-uuid>
export PMO_PROJ_TEAM=<project-team-uuid>     # optional; defaults to PMO_FW_TEAM

# 2. Install (verify auth → seed framework → install hooks → scaffold project)
python -m bootstrap install --project-name "My New Project"

# 3. Spawn the PM and start the project dialogue
pmo spawn my-new-project pm --window
```

That's it. See [`bootstrap/README.md`](./bootstrap/README.md) for sub-commands,
flags, and troubleshooting.

## What gets installed where

- **memsys (FW_TEAM):** 18 framework memories (6 roles, matrix, vocab, grammar, resolvers, area-E plan, etc.) — written with canonical slugs so role sessions resolve them at runtime.
- **memsys (PROJ_TEAM):** 3 project memories per project (manifest with POINTERS to FW_TEAM, session-registry, verbatim user-intent).
- **`~/.pmo/`:** the `pmo` Python CLI, `pmo_mux.py` (tmux/psmux abstraction), the two Python hooks (`pmo_stop_hook.py`, `pmo_session_start_hook.py`), `memsys_client.py`, and `config.json` (mode 0600 — contains your memsys token).
- **`~/.local/bin/pmo`:** symlinked to `~/.pmo/pmo`.
- **`~/.claude/settings.json`:** registers the Stop + SessionStart hooks (backup made before edit).

## Platform support

- **Mac/Linux + tmux** — validated path.
- **Windows + psmux** — supported via the mux adapter; mux is on PATH. Not yet tested on a real psmux box.
- Setup tool itself is platform-agnostic (stdlib Python, HTTP to memsys, JSON file edits).

## Refreshing the framework from memsys

memsys is authoritative for the framework content. To pull updates:

```bash
# 1. Re-export `current`-tagged memories in the pmo team to _source/memories.json
#    (use memsys's export tool / API)
# 2. Regenerate the mirror tree:
python tools/build_pmo_repo.py
# 3. (Optional) Push updates to another memsys instance:
python -m bootstrap seed
```

The seeder is idempotent: it compares content hashes against existing slugs and
either skips (unchanged), supersedes (drift), or writes fresh (new).

## License

Apache-2.0 — see [`LICENSE`](./LICENSE).
