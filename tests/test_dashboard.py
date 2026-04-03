"""Tests for dashboard API endpoints."""
import json
import threading
import time
from http.client import HTTPConnection

import pytest
from agent_memory.store import MemoryStore
from agent_memory import dashboard


@pytest.fixture
def dash_server(tmp_path):
    """Start dashboard server in background thread for testing."""
    db_path = str(tmp_path / "dash_test.db")
    store = MemoryStore(db_path=db_path)
    # Save some test data
    store.save(title="Test doc 1", content="- **key1**: value1", source="debug_solution", project="proj-a")
    store.save(title="Test doc 2", content="- **key2**: value2", source="session_note", project="proj-b")
    store.save(title="Global arch", content="Use REST", source="architectural_decision")

    # Override the module-level store
    dashboard._store = store
    dashboard._allow_edits = True

    from http.server import HTTPServer
    server = HTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield port

    server.shutdown()
    store.close()


class TestDashboardAPI:
    def test_overview(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/api/overview")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["total_documents"] == 3
        assert data["p0_count"] >= 1  # arch decision
        conn.close()

    def test_documents_list(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/api/documents")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert len(data["documents"]) == 3
        conn.close()

    def test_documents_filter_by_project(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/api/documents?project=proj-a")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert all(d["project"] == "proj-a" for d in data["documents"])
        conn.close()

    def test_document_detail(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/api/document?id=1")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "title" in data
        assert "key_facts" in data
        conn.close()

    def test_namespaces(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/api/namespaces")
        resp = conn.getresponse()
        data = json.loads(resp.read())
        projects = {n["project"] for n in data["namespaces"]}
        assert "proj-a" in projects
        conn.close()

    def test_sessions(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/api/sessions")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "sessions" in data
        conn.close()

    def test_html_served(self, dash_server):
        conn = HTTPConnection("127.0.0.1", dash_server)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read().decode()
        assert "am-memory" in body
        conn.close()
