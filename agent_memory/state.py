"""StateManager: key-value store over SQLite state table."""
import json
import time
import sqlite3


class StateManager:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def set(self, key: str, value) -> None:
        """Store value (dict, list, or string) under key."""
        self._conn.execute(
            "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?,?,?)",
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()

    def get(self, key: str):
        """Return stored value or None if not found."""
        row = self._conn.execute(
            "SELECT value FROM state WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]
