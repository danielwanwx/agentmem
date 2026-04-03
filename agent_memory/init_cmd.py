"""am init — one-time setup for am-memory.

Sets up:
  1. ~/.am-memory/memory.db  (created on first MemoryStore() call)
  2. MCP server registration in ~/.claude/mcp.json
  3. SessionStart and Stop hooks in ~/.claude/hooks/
  4. Memory instruction block appended to ~/.claude/CLAUDE.md
"""
import json
import os
import shutil
import stat
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
HOOKS_DIR = CLAUDE_DIR / "hooks"
MCP_JSON = CLAUDE_DIR / "mcp.json"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"

_CLAUDE_MD_BLOCK = """
## Persistent Memory (am-memory)

MCP tools: `am_search` · `am_save` · `am_session_checkpoint` · `am_state_get` · `am_state_set`

**am_search** — call BEFORE answering questions about past work, prior sessions, architecture decisions,
debugging solutions, or technical constraints. Also call before making recommendations that may overlap
with prior decisions. Skip: self-contained questions, pure code gen, already searched this turn.

**am_save** — call when you learn something a future session needs: config constraints, architectural
decisions, debug solutions, gotchas. TTL is automatic based on source type:
- `architectural_decision` — never expires (core design choices)
- `debug_solution` / `technical_insight` — 90 days
- `session_note` / `routine` — 30 days
Skip: obvious facts, anything already in the codebase.

**am_session_checkpoint** — call during long sessions (10+ turns) to promote current session knowledge
to persistent memory without ending the session. Safe to call multiple times (upserts).
This ensures knowledge is searchable cross-session even if the session crashes or times out.

<!-- END:am-memory -->
"""

_SESSION_START_HOOK = """\
#!/bin/bash
# am-memory: session lifecycle (SessionStart hook)
CLI="am"
# Auto-detect project: git repo name > CLAUDE_PROJECT_DIR > PWD basename
GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -n "$GIT_ROOT" ]; then
  PROJECT=$(basename "$GIT_ROOT")
else
  PROJECT=$(basename "${CLAUDE_PROJECT_DIR:-$(pwd)}")
fi
SESSION_ID=$($CLI session start --project "$PROJECT" --source "cli:${PROJECT}" 2>/dev/null)
if [ -n "$SESSION_ID" ]; then
  $CLI state set --key "current_session_id" --value "\\"$SESSION_ID\\"" 2>/dev/null
  $CLI state set --key "current_project" --value "\\"$PROJECT\\"" 2>/dev/null
  $CLI status --event session --detail "$PROJECT" 2>/dev/null
fi
exit 0
"""

_STOP_HOOK = """\
#!/bin/bash
# am-memory: checkpoint per turn (Stop hook)
CLI="am"
SESSION_ID=$($CLI state get --key current_session_id 2>/dev/null | tr -d '"')
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ]; then
  SCRATCHPAD=$($CLI state get --key scratchpad 2>/dev/null)
  if [ -n "$SCRATCHPAD" ] && [ "$SCRATCHPAD" != "null" ]; then
    $CLI session message --session-id "$SESSION_ID" --role assistant --content "$SCRATCHPAD" 2>/dev/null
    $CLI state set --key scratchpad --value "null" 2>/dev/null
  fi
  $CLI session checkpoint --session-id "$SESSION_ID" 2>/dev/null
  $CLI status --event checkpoint 2>/dev/null
fi
exit 0
"""

_USER_PROMPT_HOOK = """\
#!/bin/bash
# am-memory: capture user messages (UserPromptSubmit hook)
CLI="am"
SESSION_ID=$($CLI state get --key current_session_id 2>/dev/null | tr -d '"')
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "null" ] && [ -n "$CLAUDE_USER_PROMPT" ]; then
  $CLI session message --session-id "$SESSION_ID" --role user --content "$CLAUDE_USER_PROMPT" 2>/dev/null
  $CLI status --event message 2>/dev/null
fi
exit 0
"""


def run():
    print("Setting up am-memory...\n")

    # 1. Init DB (creates ~/.am-memory/memory.db)
    try:
        from agent_memory.store import MemoryStore
        MemoryStore()
        _ok("Created ~/.am-memory/memory.db")
    except Exception as e:
        _err(f"DB init failed: {e}")
        sys.exit(1)

    # 2. Register MCP server
    _setup_mcp()

    # 3. Install hooks
    _setup_hooks()

    # 4. Append to CLAUDE.md
    _setup_claude_md()

    print("\nDone! Restart Claude Code to activate persistent memory.")
    print("\nQuick test:")
    print("  am search --query 'test'")
    print("  am doc save --title 'hello' --content 'first memory'")


def _setup_mcp():
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    config = {}
    if MCP_JSON.exists():
        try:
            config = json.loads(MCP_JSON.read_text())
        except Exception:
            config = {}

    mcp_servers = config.setdefault("mcpServers", {})
    if "am-memory" in mcp_servers:
        _skip("MCP server already registered in ~/.claude/mcp.json")
        return

    am_path = shutil.which("am")
    if not am_path:
        _warn("'am' not found in PATH — using 'am' as command name anyway")
        am_path = "am"

    mcp_servers["am-memory"] = {"command": am_path, "args": ["mcp"]}
    MCP_JSON.write_text(json.dumps(config, indent=2))
    _ok("Registered MCP server in ~/.claude/mcp.json")


def _setup_hooks():
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    hooks = {
        "SessionStart": _SESSION_START_HOOK,
        "Stop": _STOP_HOOK,
        "UserPromptSubmit": _USER_PROMPT_HOOK,
    }
    for name, content in hooks.items():
        hook_dir = HOOKS_DIR / name
        hook_dir.mkdir(exist_ok=True)
        hook_file = hook_dir / "am-memory.sh"
        if hook_file.exists():
            _skip(f"Hook already exists: ~/.claude/hooks/{name}/am-memory.sh")
            continue
        hook_file.write_text(content)
        hook_file.chmod(hook_file.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        _ok(f"Installed hook: ~/.claude/hooks/{name}/am-memory.sh")


def _setup_claude_md():
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    if CLAUDE_MD.exists():
        existing = CLAUDE_MD.read_text()
        if "END:am-memory" in existing:
            _skip("Memory instructions already in ~/.claude/CLAUDE.md")
            return
        CLAUDE_MD.write_text(existing + _CLAUDE_MD_BLOCK)
    else:
        CLAUDE_MD.write_text(_CLAUDE_MD_BLOCK.lstrip())
    _ok("Appended memory instructions to ~/.claude/CLAUDE.md")


def _ok(msg):   print(f"  \033[32m✓\033[0m {msg}")
def _skip(msg): print(f"  \033[33m–\033[0m {msg}")
def _warn(msg): print(f"  \033[33m!\033[0m {msg}")
def _err(msg):  print(f"  \033[31m✗\033[0m {msg}")
