"""Shared fixtures for agentmem tests."""
import os
import tempfile
import pytest
from agent_memory.store import MemoryStore


@pytest.fixture
def tmp_db(tmp_path):
    """Return a temporary DB path (file does not exist yet)."""
    return str(tmp_path / "test_memory.db")


@pytest.fixture
def store(tmp_db):
    """Return a MemoryStore backed by a temporary DB."""
    s = MemoryStore(db_path=tmp_db)
    yield s
    s.close()
