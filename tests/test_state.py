"""Tests for StateManager K/V store."""


class TestState:
    def test_set_and_get_string(self, store):
        store.state.set("key1", "value1")
        assert store.state.get("key1") == "value1"

    def test_set_and_get_dict(self, store):
        data = {"ticket": "JIRA-123", "topic": "kafka"}
        store.state.set("active_work", data)
        result = store.state.get("active_work")
        assert result["ticket"] == "JIRA-123"

    def test_set_and_get_list(self, store):
        store.state.set("tags", ["a", "b", "c"])
        assert store.state.get("tags") == ["a", "b", "c"]

    def test_get_nonexistent_returns_none(self, store):
        assert store.state.get("nonexistent") is None

    def test_set_overwrites_existing(self, store):
        store.state.set("key", "old")
        store.state.set("key", "new")
        assert store.state.get("key") == "new"
