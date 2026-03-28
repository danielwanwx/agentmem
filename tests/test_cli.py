"""Tests for CLI command functions."""
import json
import sys
import io
import pytest
from unittest.mock import patch
from agent_memory.store import MemoryStore


@pytest.fixture
def cli_store(tmp_path):
    """Store for CLI tests."""
    s = MemoryStore(db_path=str(tmp_path / "cli_test.db"))
    yield s
    s.close()


class TestCliDoc:
    def test_cmd_doc_save(self, cli_store, capsys):
        from agent_memory.cli import cmd_doc
        import argparse
        args = argparse.Namespace(
            action="save", title="Test CLI save", content="CLI content",
            priority="P1", source="explicit", file_path=None,
        )
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_doc(args)
        output = capsys.readouterr().out.strip()
        assert output.isdigit()  # prints doc_id

    def test_cmd_doc_prune(self, cli_store, capsys):
        from agent_memory.cli import cmd_doc
        import argparse
        args = argparse.Namespace(action="prune")
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_doc(args)
        output = capsys.readouterr().out.strip()
        assert "Pruned" in output


class TestCliSearch:
    def test_cmd_search_inject_format(self, cli_store, capsys):
        cli_store.save(title="CLI search test", content="test content", source="explicit")
        from agent_memory.cli import cmd_search
        import argparse
        args = argparse.Namespace(query="CLI search test", format="inject", max_tokens=1500)
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_search(args)
        output = capsys.readouterr().out
        assert "CLI search test" in output

    def test_cmd_search_json_format(self, cli_store, capsys):
        cli_store.save(title="JSON test doc", content="json content", source="explicit")
        from agent_memory.cli import cmd_search
        import argparse
        args = argparse.Namespace(query="JSON test doc", format="json", max_tokens=1500)
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_search(args)
        output = capsys.readouterr().out.strip()
        data = json.loads(output)
        assert isinstance(data, list)


class TestCliState:
    def test_cmd_state_set_and_get(self, cli_store, capsys):
        from agent_memory.cli import cmd_state
        import argparse
        # Set
        args_set = argparse.Namespace(action="set", key="test_key", value='"hello"')
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_state(args_set)
        # Get
        args_get = argparse.Namespace(action="get", key="test_key")
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_state(args_get)
        output = capsys.readouterr().out.strip()
        assert "hello" in output

    def test_cmd_state_get_nonexistent_exits(self, cli_store):
        from agent_memory.cli import cmd_state
        import argparse
        args = argparse.Namespace(action="get", key="nonexistent_key_xyz")
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            with pytest.raises(SystemExit):
                cmd_state(args)


class TestCliSession:
    def test_cmd_session_start(self, cli_store, capsys):
        from agent_memory.cli import cmd_session
        import argparse
        args = argparse.Namespace(
            action="start", project="testproj", topic="testing",
            source="cli:testproj", session_id=None, role="user",
            content="", summary=None, max_tokens=2000,
            limit=100, include_cli=False,
        )
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_session(args)
        output = capsys.readouterr().out.strip()
        assert len(output) > 0  # prints session_id

    def test_cmd_session_list(self, cli_store, capsys):
        cli_store.session.start(project="listproj")
        from agent_memory.cli import cmd_session
        import argparse
        args = argparse.Namespace(
            action="list", project="", topic="", source="",
            session_id=None, role="user", content="",
            summary=None, max_tokens=2000,
            limit=100, include_cli=True,
        )
        with patch("agent_memory.cli.get_store", return_value=cli_store):
            cmd_session(args)
        output = capsys.readouterr().out.strip()
        data = json.loads(output)
        assert isinstance(data, list)
