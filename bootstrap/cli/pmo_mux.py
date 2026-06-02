#!/usr/bin/env python3
"""
pmo_mux.py — Area E substrate core (one-window, orchestrator-driven).

Implements the v1 substrate from the Area E plan (memsys 3c5900e5):
  - ONE window per project; roles are resumable Claude Code sessions, not panes.
  - A multiplexer ADAPTER so tmux and psmux are interchangeable (owner uses psmux;
    tmux is the dev default). The substrate only needs: ensure a session exists,
    run a command in the window, and send-keys (for the hook's prefill).
  - The session-registry (role -> claude_session_id) is a memsys memory, slug
    pmo-project-<slug>-session-registry. memsys is canonical; this module shells
    out to a thin `mcp` CLI to read/write it (wire MCP_CLI to your endpoint).

This module is intentionally dependency-free (stdlib only) so it runs anywhere
a terminal + python3 exist.

Refs (memsys): Area E v3 3c5900e5 | directive grammar 10852807 | routing config
08b19eee | role defs pmo-role-<role>-v1 (PM 127793ef, etc).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# --- configuration ---------------------------------------------------------

PMO_TEAM = os.environ.get("PMO_TEAM", "edc1a7f0-9f5a-4dd7-b045-ccd9a418b2d9")
# A thin CLI that talks to the memsys MCP. Must accept:
#   MCP_CLI memory-get  --team <t> --type decision --slug <slug>   -> JSON memory or ""
#   MCP_CLI memory-write --team <t> --type decision --slug <slug> --supersede <id|""> --json <payload>
# If absent, registry ops degrade to a local cache file (demo fallback) + a warning.
MCP_CLI = os.environ.get("PMO_MCP_CLI", "")
LOCAL_REGISTRY_DIR = os.path.expanduser(os.environ.get("PMO_HOME", "~/.pmo"))

ROLES = ["pm", "pa", "dm", "da", "developer", "reviewer"]
ROLE_SLUG = {r: f"pmo-role-{r}-v1" for r in ROLES}


# --- multiplexer adapter ---------------------------------------------------

class Mux:
    """Abstract multiplexer. Concrete: TmuxMux, PsmuxMux."""
    name = "abstract"

    def available(self) -> bool: raise NotImplementedError
    def has_session(self, session: str) -> bool: raise NotImplementedError
    def new_session(self, session: str, start_cmd: str) -> None: raise NotImplementedError
    def new_window(self, session: str, win_name: str, start_cmd: str) -> None: raise NotImplementedError
    def attach(self, session: str) -> None: raise NotImplementedError
    def send_keys(self, session: str, text: str, enter: bool) -> None: raise NotImplementedError
    def capture(self, session: str) -> str: raise NotImplementedError


class TmuxMux(Mux):
    name = "tmux"
    # one window, one pane: target is just the session name (window 0, pane 0)
    def available(self): return shutil.which("tmux") is not None
    def has_session(self, session):
        return subprocess.run(["tmux", "has-session", "-t", session],
                              capture_output=True).returncode == 0
    def new_session(self, session, start_cmd):
        # single window, single pane, no splits
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "work", start_cmd],
                       check=True)
    def new_window(self, session, win_name, start_cmd):
        # spawn a role into its own window in the existing session
        subprocess.run(["tmux", "new-window", "-t", session, "-n", win_name, start_cmd],
                       check=True)
    def attach(self, session):
        os.execvp("tmux", ["tmux", "attach", "-t", session])
    def send_keys(self, session, text, enter):
        args = ["tmux", "send-keys", "-t", f"{session}:work.0", text]
        if enter: args.append("Enter")
        subprocess.run(args, check=True)
    def capture(self, session):
        r = subprocess.run(["tmux", "capture-pane", "-p", "-t", f"{session}:work.0"],
                         capture_output=True, text=True)
        return r.stdout


class PsmuxMux(Mux):
    """psmux adapter. psmux mirrors tmux's CLI grammar closely; if your psmux
    differs, this is the ONE place to adjust. Owner uses psmux on Windows."""
    name = "psmux"
    BIN = os.environ.get("PSMUX_BIN", "psmux")
    def available(self): return shutil.which(self.BIN) is not None
    def has_session(self, session):
        return subprocess.run([self.BIN, "has-session", "-t", session],
                              capture_output=True).returncode == 0
    def new_session(self, session, start_cmd):
        subprocess.run([self.BIN, "new-session", "-d", "-s", session, "-n", "work", start_cmd],
                       check=True)
    def new_window(self, session, win_name, start_cmd):
        subprocess.run([self.BIN, "new-window", "-t", session, "-n", win_name, start_cmd],
                       check=True)
    def attach(self, session):
        os.execvp(self.BIN, [self.BIN, "attach", "-t", session])
    def send_keys(self, session, text, enter):
        args = [self.BIN, "send-keys", "-t", f"{session}:work.0", text]
        if enter: args.append("Enter")
        subprocess.run(args, check=True)
    def capture(self, session):
        r = subprocess.run([self.BIN, "capture-pane", "-p", "-t", f"{session}:work.0"],
                          capture_output=True, text=True)
        return r.stdout


def pick_mux(prefer: str | None = None) -> Mux:
    """Choose a multiplexer. prefer ∈ {tmux, psmux} or env PMO_MUX; else autodetect."""
    prefer = prefer or os.environ.get("PMO_MUX")
    candidates = {"tmux": TmuxMux(), "psmux": PsmuxMux()}
    if prefer:
        m = candidates.get(prefer)
        if m and m.available():
            return m
        raise RuntimeError(f"requested mux '{prefer}' not available")
    for m in (TmuxMux(), PsmuxMux()):
        if m.available():
            return m
    raise RuntimeError("no multiplexer found (need tmux or psmux on PATH)")


# --- session registry (memsys-backed, local fallback) ----------------------

@dataclass
class Registry:
    project_slug: str
    rows: dict = field(default_factory=dict)  # role -> {claude_session_id, registered_at}

    @property
    def slug(self) -> str:
        return f"pmo-project-{self.project_slug}-session-registry"

    @property
    def _local_path(self) -> str:
        os.makedirs(LOCAL_REGISTRY_DIR, exist_ok=True)
        return os.path.join(LOCAL_REGISTRY_DIR, f"{self.slug}.json")

    @classmethod
    def load(cls, project_slug: str) -> "Registry":
        reg = cls(project_slug=project_slug)
        if MCP_CLI:
            try:
                out = subprocess.run(
                    [MCP_CLI, "memory-get", "--team", PMO_TEAM,
                     "--type", "decision", "--slug", reg.slug],
                    capture_output=True, text=True, check=True).stdout.strip()
                if out:
                    mem = json.loads(out)
                    body = mem.get("content") or mem.get("body") or "{}"
                    reg.rows = json.loads(body).get("rows", {}) if body.strip().startswith("{") else {}
                    reg._last_id = mem.get("id", "")
                return reg
            except Exception as e:
                sys.stderr.write(f"[pmo] registry memsys-read failed ({e}); using local cache\n")
        # local fallback
        if os.path.exists(reg._local_path):
            reg.rows = json.load(open(reg._local_path)).get("rows", {})
        return reg

    def set_role(self, role: str, session_id: str) -> None:
        from datetime import datetime, timezone
        self.rows[role] = {
            "claude_session_id": session_id,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_session_id(self, role: str) -> str | None:
        r = self.rows.get(role)
        return r["claude_session_id"] if r else None

    def _save(self) -> None:
        payload = json.dumps({"rows": self.rows})
        if MCP_CLI:
            try:
                last = getattr(self, "_last_id", "")
                subprocess.run(
                    [MCP_CLI, "memory-write", "--team", PMO_TEAM, "--type", "decision",
                     "--slug", self.slug, "--supersede", last, "--json", payload],
                    check=True)
                return
            except Exception as e:
                sys.stderr.write(f"[pmo] registry memsys-write failed ({e}); writing local cache\n")
        json.dump({"rows": self.rows}, open(self._local_path, "w"), indent=2)


# --- the resume command the window uses -----------------------------------

def claude_start_cmd(role: str, project_slug: str, resume_id: str | None = None) -> str:
    """The command that launches/continues a role's Claude Code session in the window.
    Fresh start loads the role prompt; resume continues an existing session.
    The role prompt's ON-RESUME block does the warm-start (load plate, etc).

    The launch sets PMO_ROLE + PMO_PROJECT_SLUG in the session's environment so the
    SessionStart self-registration hook knows which role/project this session is, and
    writes the registry row itself (no id-capture needed by the spawner)."""
    env = f"PMO_ROLE={role} PMO_PROJECT_SLUG={project_slug}"
    if resume_id:
        return f"{env} claude --resume {resume_id}"
    # fresh: launch claude with the role prompt as the system/first instruction.
    # The role-def is fetched from memsys by slug at session start; here we pass a
    # bootstrap line telling the session who it is and to run its on-resume block.
    boot = (f"You are the {role.upper()} for project {project_slug}. "
            f"Load your role definition from memsys (slug {ROLE_SLUG[role]}) and run your "
            f"ON-RESUME block now.")
    # -p passes an initial prompt; quoting kept simple for the demo substrate.
    safe = boot.replace('"', '\\"')
    return f'{env} claude -p "{safe}"'
