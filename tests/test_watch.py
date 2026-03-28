"""Tests for status line output."""
from agent_memory.watch import status_line, _get_counts, _THEME


class TestStatus:
    def test_all_events_defined(self):
        expected = {"session", "checkpoint", "message", "save", "search", "prune", "error", "idle"}
        assert set(_THEME.keys()) == expected

    def test_theme_has_icon_and_color(self):
        for name, entry in _THEME.items():
            assert len(entry) == 2, f"{name} should be (icon, color_rgb)"
            icon, color = entry
            assert isinstance(icon, str)
            assert len(color) == 3

    def test_status_line_prints(self, tmp_path, capsys):
        from agent_memory.db import init_db
        db_path = str(tmp_path / "s.db")
        init_db(db_path)
        status_line(event="session", detail="myproject", db_path=db_path)
        out = capsys.readouterr().out
        assert "session started" in out
        assert "myproject" in out

    def test_status_line_shows_counts(self, tmp_path, capsys):
        from agent_memory.db import init_db
        db_path = str(tmp_path / "s.db")
        conn = init_db(db_path)
        conn.execute("INSERT INTO documents (title, source) VALUES ('d1', 'explicit')")
        conn.commit()
        conn.close()
        status_line(event="idle", db_path=db_path)
        out = capsys.readouterr().out
        assert "1 docs" in out

    def test_get_counts(self, tmp_path):
        from agent_memory.db import init_db
        db_path = str(tmp_path / "s.db")
        init_db(db_path)
        d, s = _get_counts(db_path)
        assert d == 0
        assert s == 0
