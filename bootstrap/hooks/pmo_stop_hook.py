#!/usr/bin/env python3
"""PMO Claude Code Stop hook — routing by @PMO directive (Python, cross-platform).

Behavior mirrors the bash pmo-stop-hook.sh v1.1 (memsys b194ba79):
  - Read the transcript JSONL, find the last assistant text message.
  - Find the LAST line matching the @PMO marker.
  - Extract a resume=<id> (V3 NEXT/ESCALATE) or /resume <id> (V2 form).
  - Wipe input buffer, send the literal text, then send Enter as a SEPARATE keystroke
    (defeats Claude Code's bracketed-paste detection).
  - Idempotency: skip if same cmd was sent within 120s.

Cross-platform via the pmo_mux abstraction (tmux on Mac/Linux, psmux on Windows).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make pmo_mux importable when this hook is installed alongside it in ~/.pmo/.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import pmo_mux  # type: ignore
except ImportError:
    pmo_mux = None  # graceful degrade — we still try TMUX_PANE direct path

TRACE_LOG = Path(os.environ.get("PMO_TRACE_LOG", "/tmp/pmo-stop-hook-trace.log"))
LAST_CMD_FILE = Path(os.environ.get("PMO_LAST_CMD_FILE", "/tmp/pmo-stop-hook-last-cmd"))
LAST_CMD_AGE_MAX = int(os.environ.get("PMO_LAST_CMD_AGE_MAX", "120"))
INITIAL_FLUSH_SETTLE = float(os.environ.get("PMO_FLUSH_SETTLE", "0.5"))
POLL_INTERVAL = 0.1
POLL_MAX_ITERS = 10
ENTER_GAP = 0.5

# Grammar regexes (case-insensitive on @pmo).
DIRECTIVE_LINE_RE = re.compile(r"^\s*@pmo\s+", re.IGNORECASE | re.MULTILINE)
V3_RE = re.compile(
    r"@pmo\s+(?:NEXT|ESCALATE)\s+.*?resume=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
V2_RE = re.compile(r"@pmo\s+/resume\s+([A-Za-z0-9_-]+)", re.IGNORECASE)


def trace(msg: str) -> None:
    try:
        with TRACE_LOG.open("a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")
    except Exception:
        pass


def read_last_assistant_text(transcript_path: Path) -> str:
    """Find the last `"type":"assistant"` entry and extract its text content."""
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if '"type":"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (entry.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
        if text:
            return text
    return ""


def find_directive_cmd(assistant_text: str) -> tuple[str, str]:
    """Return (directive_line, cmd). cmd is '' if no routable directive."""
    matches = [m.group(0).rstrip() for m in re.finditer(r"^\s*@pmo\s+.*$", assistant_text, re.IGNORECASE | re.MULTILINE)]
    if not matches:
        return "", ""
    directive_line = matches[-1]
    m = V3_RE.search(directive_line)
    if m:
        return directive_line, f"/resume {m.group(1)}"
    m = V2_RE.search(directive_line)
    if m:
        return directive_line, f"/resume {m.group(1)}"
    return directive_line, ""


def idempotency_skip(cmd: str, now_ts: int) -> bool:
    if not LAST_CMD_FILE.exists():
        return False
    try:
        line = LAST_CMD_FILE.read_text().splitlines()[0]
        ts_str, last_cmd = line.split("\t", 1)
        last_ts = int(ts_str)
    except (ValueError, IndexError):
        return False
    if last_cmd == cmd and (now_ts - last_ts) < LAST_CMD_AGE_MAX:
        trace(f"SKIP duplicate cmd=[{cmd}] age={now_ts - last_ts}s")
        return True
    return False


def record_last_cmd(cmd: str, now_ts: int) -> None:
    try:
        LAST_CMD_FILE.write_text(f"{now_ts}\t{cmd}\n")
    except OSError:
        pass


def find_pane() -> str | None:
    """Find the mux pane Claude Code runs in. Prefer TMUX_PANE env."""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        return pane
    # Walk parent processes; try to match against `tmux list-panes` pane_pid.
    if not pmo_mux:
        return None
    try:
        mux = pmo_mux.pick_mux()
    except Exception:
        return None
    if mux.name != "tmux":
        # psmux pane discovery is different and not yet implemented here.
        return None
    pid = os.getpid()
    for _ in range(10):
        ppid = _parent_pid(pid)
        if not ppid or ppid == 1:
            break
        pid = ppid
        # `tmux list-panes -a -F '#{pane_pid} #{pane_id}'`
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{pane_id}"],
                capture_output=True, text=True, check=False,
            ).stdout
        except FileNotFoundError:
            return None
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == str(pid):
                return parts[1]
    return None


def _parent_pid(pid: int) -> int | None:
    """Return parent PID of `pid`, cross-platform."""
    # Try /proc (Linux).
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            content = proc_stat.read_text()
            # PPID is the 4th field after the comm field which can contain spaces.
            rp = content.rfind(")")
            fields = content[rp + 2:].split()
            return int(fields[1])
        except (OSError, ValueError, IndexError):
            return None
    # Fallback: `ps -o ppid= -p <pid>` (Mac/BSD/Linux).
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return int(out) if out else None
    except (FileNotFoundError, ValueError):
        return None


def send_keys_to_pane(pane: str, cmd: str) -> tuple[int, int]:
    """Wipe input, send cmd literally, sleep, send Enter as separate keystroke."""
    bin_name = "tmux"
    if pmo_mux:
        try:
            mux = pmo_mux.pick_mux()
            bin_name = mux.BIN if hasattr(mux, "BIN") else mux.name
        except Exception:
            pass

    def _send(*args: str) -> int:
        try:
            return subprocess.run(
                [bin_name, "send-keys", "-t", pane, *args],
                capture_output=True, check=False,
            ).returncode
        except FileNotFoundError:
            return 127

    _send("Escape")
    _send("C-u", "C-u", "C-u")
    time.sleep(0.1)
    rc1 = _send("-l", cmd)
    time.sleep(ENTER_GAP)
    rc2 = _send("Enter")
    # Background pane snapshot for debugging.
    try:
        subprocess.Popen(
            [
                "sh",
                "-c",
                f'sleep 2 && {bin_name} capture-pane -t "{pane}" -p 2>/dev/null | tail -5 | sed "s/^/  pane> /" >> "{TRACE_LOG}"',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
    return rc1, rc2


def main() -> int:
    trace(f"fired pid={os.getpid()} tmux_pane={os.environ.get('TMUX_PANE', 'unset')}")

    # Initial flush settle — Stop event can fire before the final assistant entry
    # is flushed to the JSONL transcript.
    time.sleep(INITIAL_FLUSH_SETTLE)

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    transcript_path_str = payload.get("transcript_path", "")
    if not transcript_path_str:
        print("{}")
        return 0
    transcript_path = Path(transcript_path_str)
    if not transcript_path.is_file():
        print("{}")
        return 0

    # Read with flush-race polling guard.
    assistant_text = read_last_assistant_text(transcript_path)
    if not assistant_text:
        for _ in range(POLL_MAX_ITERS):
            time.sleep(POLL_INTERVAL)
            assistant_text = read_last_assistant_text(transcript_path)
            if assistant_text:
                break

    directive_line, cmd = find_directive_cmd(assistant_text)
    trace(
        f"PRE-GUARD assistant_text_len={len(assistant_text)} "
        f"directive_line=[{directive_line}] cmd=[{cmd}]"
    )
    if not cmd:
        print("{}")
        return 0

    now_ts = int(time.time())
    if idempotency_skip(cmd, now_ts):
        print("{}")
        return 0

    pane = find_pane()
    if not pane:
        print(json.dumps({"systemMessage": "pmo-stop-hook: mux pane not found; skipped"}))
        return 0

    rc1, rc2 = send_keys_to_pane(pane, cmd)
    record_last_cmd(cmd, now_ts)
    trace(f"send-keys rc=text:{rc1},enter:{rc2} pane=[{pane}] cmd=[{cmd}]")
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
