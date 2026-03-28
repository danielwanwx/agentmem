"""am status — one-line colored status for hooks output.

Prints a single colored status line to stdout, designed to appear
inline in the Claude Code terminal when called from hooks.
"""
import time
import sqlite3
from .db import DB_PATH

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _fg(r, g, b):
    return f"\033[38;2;{r};{g};{b}m"


_THEME = {
    "session":    ("🚀", (255, 180, 100)),
    "checkpoint": ("📋", (255, 210, 100)),
    "message":    ("💬", (100, 180, 255)),
    "save":       ("💾", (120, 220, 140)),
    "search":     ("🔍", (100, 180, 255)),
    "prune":      ("🗑",  (200, 170, 140)),
    "error":      ("❌", (255, 100, 100)),
    "idle":       ("🧠", (180, 160, 210)),
}


def _get_counts(db_path):
    """Read doc/session counts from DB."""
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        sess = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        return docs, sess
    except Exception:
        return 0, 0


def status_line(event="idle", detail="", db_path=None):
    """Print one colored status line."""
    db_path = db_path or str(DB_PATH)
    icon, color = _THEME.get(event, _THEME["idle"])
    r, g, b = color
    c = _fg(r, g, b)
    docs, sess = _get_counts(db_path)

    parts = [f"{icon} {c}{BOLD}"]

    if event == "session":
        parts.append(f"session started")
        if detail:
            parts.append(f" → {detail}")
    elif event == "checkpoint":
        parts.append(f"checkpoint saved")
    elif event == "message":
        parts.append(f"message captured")
    elif event == "save":
        parts.append(f"saved")
        if detail:
            parts.append(f": {detail[:40]}")
    elif event == "search":
        parts.append(f"searched")
        if detail:
            parts.append(f": {detail[:40]}")
    elif event == "prune":
        parts.append(f"pruned expired docs")
    elif event == "error":
        parts.append(f"error")
        if detail:
            parts.append(f": {detail[:40]}")
    else:
        parts.append(f"ready")

    parts.append(f"{RESET}")
    parts.append(f"  {DIM}{docs} docs · {sess} sess{RESET}")

    print("".join(parts))
