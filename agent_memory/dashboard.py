"""Web dashboard for am-memory knowledge base.

Single-file HTML+JS frontend served by a minimal Python HTTP handler.
Read-only by default; --allow-edits enables delete/priority changes.

Usage:
    am dashboard                    # read-only, port 8420
    am dashboard --port 9000        # custom port
    am dashboard --allow-edits      # enable mutations
"""

from __future__ import annotations

import json
import os
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .store import MemoryStore


_store: MemoryStore | None = None
_allow_edits: bool = False


def start(port: int = 8420, allow_edits: bool = False) -> None:
    """Start the dashboard server."""
    global _store, _allow_edits
    _store = MemoryStore()
    _allow_edits = allow_edits

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://localhost:{port}"
    print(f"am-memory dashboard: {url}")
    print(f"  Mode: {'read-write' if allow_edits else 'read-only'}")
    print("  Press Ctrl+C to stop\n")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _store.close()


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default logging

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/overview":
            self._api_overview()
        elif path == "/api/documents":
            self._api_documents(params)
        elif path == "/api/document":
            self._api_document(params)
        elif path == "/api/sessions":
            self._api_sessions(params)
        elif path == "/api/namespaces":
            self._api_namespaces()
        elif path == "/api/dream-log":
            self._api_dream_log()
        else:
            self._json_response({"error": "Not found"}, 404)

    def do_POST(self):
        if not _allow_edits:
            self._json_response({"error": "Read-only mode"}, 403)
            return

        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}

        if parsed.path == "/api/document/delete":
            doc_id = body.get("doc_id")
            if doc_id:
                _store.delete_documents([doc_id])
                self._json_response({"ok": True})
            else:
                self._json_response({"error": "doc_id required"}, 400)
        elif parsed.path == "/api/document/priority":
            doc_id = body.get("doc_id")
            priority = body.get("priority")
            if doc_id and priority in ("P0", "P1", "P2"):
                _store._wq.execute(
                    "UPDATE documents SET priority=? WHERE doc_id=?",
                    (priority, doc_id),
                )
                self._json_response({"ok": True})
            else:
                self._json_response({"error": "doc_id and priority (P0/P1/P2) required"}, 400)
        else:
            self._json_response({"error": "Not found"}, 404)

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DASHBOARD_HTML.encode("utf-8"))

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _api_overview(self):
        with _store._read_lock:
            total = _store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            p0 = _store._conn.execute("SELECT COUNT(*) FROM documents WHERE priority='P0'").fetchone()[0]
            p1 = _store._conn.execute("SELECT COUNT(*) FROM documents WHERE priority='P1'").fetchone()[0]
            p2 = _store._conn.execute("SELECT COUNT(*) FROM documents WHERE priority='P2'").fetchone()[0]
            sessions = _store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        namespaces = _store.namespace_list()
        db_path = _store._db_path
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
        self._json_response({
            "total_documents": total,
            "p0_count": p0, "p1_count": p1, "p2_count": p2,
            "total_sessions": sessions,
            "namespaces": namespaces,
            "db_size_mb": round(db_size / 1048576, 2),
            "allow_edits": _allow_edits,
        })

    def _api_documents(self, params):
        page = int(params.get("page", [1])[0])
        per_page = min(int(params.get("per_page", [20])[0]), 100)
        project = params.get("project", [None])[0]
        priority = params.get("priority", [None])[0]
        query = params.get("q", [None])[0]
        offset = (page - 1) * per_page

        if query:
            results = _store.search(query, max_results=per_page, project=project)
            docs = []
            for r in results:
                with _store._read_lock:
                    row = _store._conn.execute(
                        """SELECT doc_id, title, summary, priority, source, project,
                                  created_at, expires_at, last_accessed_at, generator
                           FROM documents WHERE doc_id=?""",
                        (r.id,),
                    ).fetchone()
                if row:
                    docs.append(_row_to_dict(row))
            self._json_response({"documents": docs, "total": len(docs)})
            return

        where_parts = []
        where_params = []
        if project:
            if project == "(global)":
                where_parts.append("project IS NULL")
            else:
                where_parts.append("project = ?")
                where_params.append(project)
        if priority:
            where_parts.append("priority = ?")
            where_params.append(priority)

        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        with _store._read_lock:
            total = _store._conn.execute(
                f"SELECT COUNT(*) FROM documents {where}", where_params
            ).fetchone()[0]
            rows = _store._conn.execute(
                f"""SELECT doc_id, title, summary, priority, source, project,
                           created_at, expires_at, last_accessed_at, generator
                    FROM documents {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?""",
                where_params + [per_page, offset],
            ).fetchall()

        docs = [_row_to_dict(r) for r in rows]
        self._json_response({"documents": docs, "total": total, "page": page, "per_page": per_page})

    def _api_document(self, params):
        doc_id = params.get("id", [None])[0]
        if not doc_id:
            self._json_response({"error": "id required"}, 400)
            return
        with _store._read_lock:
            row = _store._conn.execute(
                """SELECT doc_id, title, summary, key_facts, decisions, code_sigs,
                          metrics, raw_content, priority, source, project, generator,
                          file_path, created_at, expires_at, last_accessed_at
                   FROM documents WHERE doc_id=?""",
                (int(doc_id),),
            ).fetchone()
        if not row:
            self._json_response({"error": "Not found"}, 404)
            return
        doc = dict(row)
        for field in ("key_facts", "decisions", "code_sigs", "metrics"):
            try:
                doc[field] = json.loads(doc[field] or "[]")
            except Exception:
                doc[field] = []
        # Get related documents
        related = _store._get_related_docs(int(doc_id), set())
        doc["related"] = related
        self._json_response(doc)

    def _api_sessions(self, params):
        limit = int(params.get("limit", [50])[0])
        sessions = _store.session.list_for_dashboard(limit=limit, include_cli=True)
        self._json_response({"sessions": sessions})

    def _api_namespaces(self):
        namespaces = _store.namespace_list()
        self._json_response({"namespaces": namespaces})

    def _api_dream_log(self):
        try:
            with _store._read_lock:
                rows = _store._conn.execute(
                    """SELECT * FROM consolidation_log ORDER BY timestamp DESC LIMIT 20"""
                ).fetchall()
            logs = [dict(r) for r in rows]
        except Exception:
            logs = []
        self._json_response({"logs": logs})


def _row_to_dict(row) -> dict:
    d = dict(row)
    now = time.time()
    if d.get("expires_at"):
        remaining = d["expires_at"] - now
        d["ttl_remaining_days"] = round(remaining / 86400, 1) if remaining > 0 else 0
    else:
        d["ttl_remaining_days"] = None  # never expires
    return d


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>am-memory dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
background:#0d1117;color:#c9d1d9;line-height:1.5}
.container{max-width:1200px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:12px;padding:16px 0;border-bottom:1px solid #21262d}
header h1{font-size:20px;color:#f0f6fc}
header .badge{background:#238636;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px}
nav{display:flex;gap:8px;padding:12px 0;border-bottom:1px solid #21262d}
nav button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 16px;
border-radius:6px;cursor:pointer;font-size:14px}
nav button.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
nav button:hover{background:#30363d}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;padding:16px 0}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px}
.card .value{font-size:28px;font-weight:600;color:#f0f6fc}
.card .label{font-size:12px;color:#8b949e}
.filters{display:flex;gap:8px;padding:12px 0;flex-wrap:wrap}
.filters select,.filters input{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;
padding:6px 12px;border-radius:6px;font-size:14px}
.filters input{width:300px}
table{width:100%;border-collapse:collapse;margin:12px 0}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #21262d;font-size:14px}
th{color:#8b949e;font-weight:500;font-size:12px;text-transform:uppercase}
tr:hover{background:#161b22}
.p0{color:#f85149}.p1{color:#d29922}.p2{color:#8b949e}
.detail{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:20px;margin:12px 0}
.detail h2{font-size:18px;color:#f0f6fc;margin-bottom:12px}
.detail .field{margin:8px 0}
.detail .field-label{font-size:12px;color:#8b949e;text-transform:uppercase}
.detail .field-value{color:#c9d1d9}
.detail .facts li,.detail .decisions li{margin:4px 0}
.btn{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:4px 12px;
border-radius:6px;cursor:pointer;font-size:13px}
.btn:hover{background:#30363d}
.btn-danger{color:#f85149;border-color:#f85149}
.btn-danger:hover{background:#f85149;color:#fff}
.related{margin:12px 0;padding:12px;background:#0d1117;border-radius:6px}
.related a{color:#58a6ff;text-decoration:none}
.related a:hover{text-decoration:underline}
.empty{text-align:center;padding:40px;color:#8b949e}
@media(max-width:768px){.filters input{width:100%}.cards{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<div class="container">
<header>
<h1>am-memory</h1>
<span class="badge" id="mode-badge">read-only</span>
</header>
<nav>
<button class="active" onclick="showTab('overview')">Overview</button>
<button onclick="showTab('documents')">Documents</button>
<button onclick="showTab('sessions')">Sessions</button>
<button onclick="showTab('dream')">Dream Log</button>
</nav>
<div id="content"></div>
</div>
<script>
let state={tab:'overview',docs:[],detail:null,page:1,project:'',priority:'',query:'',overview:null};

async function api(path){
  const r=await fetch('/api/'+path);
  return r.json();
}
async function apiPost(path,body){
  const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}

function showTab(t){
  state.tab=t;state.detail=null;
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  render();
}

async function render(){
  const c=document.getElementById('content');
  if(state.tab==='overview')await renderOverview(c);
  else if(state.tab==='documents')await renderDocuments(c);
  else if(state.tab==='sessions')await renderSessions(c);
  else if(state.tab==='dream')await renderDream(c);
}

async function renderOverview(c){
  const d=await api('overview');
  state.overview=d;
  document.getElementById('mode-badge').textContent=d.allow_edits?'read-write':'read-only';
  let ns=d.namespaces.map(n=>`<tr><td>${n.project}</td><td>${n.doc_count}</td></tr>`).join('');
  c.innerHTML=`
  <div class="cards">
    <div class="card"><div class="value">${d.total_documents}</div><div class="label">Documents</div></div>
    <div class="card"><div class="value p0">${d.p0_count}</div><div class="label">P0 (permanent)</div></div>
    <div class="card"><div class="value p1">${d.p1_count}</div><div class="label">P1 (90d)</div></div>
    <div class="card"><div class="value p2">${d.p2_count}</div><div class="label">P2 (30d)</div></div>
    <div class="card"><div class="value">${d.total_sessions}</div><div class="label">Sessions</div></div>
    <div class="card"><div class="value">${d.db_size_mb}</div><div class="label">DB Size (MB)</div></div>
  </div>
  <h3 style="margin:16px 0 8px">Namespaces</h3>
  <table><thead><tr><th>Project</th><th>Documents</th></tr></thead><tbody>${ns||'<tr><td colspan=2 class="empty">No documents yet</td></tr>'}</tbody></table>`;
}

async function renderDocuments(c){
  if(state.detail)return renderDocDetail(c);
  let qs=`page=${state.page}&per_page=20`;
  if(state.project)qs+=`&project=${encodeURIComponent(state.project)}`;
  if(state.priority)qs+=`&priority=${state.priority}`;
  if(state.query)qs+=`&q=${encodeURIComponent(state.query)}`;
  const d=await api('documents?'+qs);
  const ns=state.overview?.namespaces||await api('namespaces').then(r=>r.namespaces);
  let opts=ns.map(n=>`<option value="${n.project}">${n.project} (${n.doc_count})</option>`).join('');
  let rows=d.documents.map(doc=>{
    let ttl=doc.ttl_remaining_days===null?'<span class="p0">never</span>':`${doc.ttl_remaining_days}d`;
    return `<tr onclick="showDoc(${doc.doc_id})" style="cursor:pointer">
      <td>${doc.title||'(untitled)'}</td>
      <td class="${doc.priority.toLowerCase()}">${doc.priority}</td>
      <td>${doc.project||'(global)'}</td>
      <td>${doc.source}</td>
      <td>${ttl}</td></tr>`;
  }).join('');
  let total=d.total||d.documents.length;
  let pages=Math.ceil(total/20);
  let pager='';
  if(pages>1){
    pager='<div style="padding:12px 0">';
    for(let i=1;i<=Math.min(pages,10);i++)
      pager+=`<button class="btn${i===state.page?' active':''}" onclick="goPage(${i})">${i}</button> `;
    pager+='</div>';
  }
  c.innerHTML=`
  <div class="filters">
    <input type="text" placeholder="Search documents..." value="${state.query||''}" onkeyup="if(event.key==='Enter'){state.query=this.value;state.page=1;render()}">
    <select onchange="state.project=this.value;state.page=1;render()">
      <option value="">All projects</option>${opts}
    </select>
    <select onchange="state.priority=this.value;state.page=1;render()">
      <option value="">All priorities</option>
      <option value="P0">P0</option><option value="P1">P1</option><option value="P2">P2</option>
    </select>
  </div>
  <table><thead><tr><th>Title</th><th>Priority</th><th>Project</th><th>Source</th><th>TTL</th></tr></thead>
  <tbody>${rows||'<tr><td colspan=5 class="empty">No documents found</td></tr>'}</tbody></table>
  ${pager}`;
}

async function showDoc(id){
  state.detail=id;render();
}
function goPage(p){state.page=p;render();}

async function renderDocDetail(c){
  const d=await api('document?id='+state.detail);
  if(d.error){c.innerHTML=`<div class="empty">${d.error}</div>`;return;}
  let facts=(d.key_facts||[]).map(f=>`<li>${f}</li>`).join('');
  let decs=(d.decisions||[]).map(f=>`<li>${f}</li>`).join('');
  let sigs=(d.code_sigs||[]).map(f=>`<li><code>${f}</code></li>`).join('');
  let related=(d.related||[]).map(r=>`<a href="#" onclick="showDoc(${r.doc_id});return false">[${r.relation_type}] ${r.title}</a><br>`).join('');
  let ttl=d.expires_at?`${Math.round((d.expires_at-Date.now()/1000)/86400)}d`:'never';
  let editBtns=state.overview?.allow_edits?`
    <div style="margin-top:16px">
      <select onchange="changePriority(${d.doc_id},this.value)">
        <option value="">Change priority...</option>
        <option value="P0">P0</option><option value="P1">P1</option><option value="P2">P2</option>
      </select>
      <button class="btn btn-danger" onclick="deleteDoc(${d.doc_id})">Delete</button>
    </div>`:'';
  c.innerHTML=`
  <div style="padding:8px 0"><button class="btn" onclick="state.detail=null;render()">← Back</button></div>
  <div class="detail">
    <h2>${d.title||'(untitled)'}</h2>
    <div class="field"><div class="field-label">Priority</div><div class="field-value ${d.priority.toLowerCase()}">${d.priority}</div></div>
    <div class="field"><div class="field-label">Source</div><div class="field-value">${d.source} (${d.generator})</div></div>
    <div class="field"><div class="field-label">Project</div><div class="field-value">${d.project||'(global)'}</div></div>
    <div class="field"><div class="field-label">TTL</div><div class="field-value">${ttl}</div></div>
    <div class="field"><div class="field-label">Summary</div><div class="field-value">${d.summary||'—'}</div></div>
    ${facts?`<div class="field"><div class="field-label">Key Facts</div><ul class="facts">${facts}</ul></div>`:''}
    ${decs?`<div class="field"><div class="field-label">Decisions</div><ul class="decisions">${decs}</ul></div>`:''}
    ${sigs?`<div class="field"><div class="field-label">Code Signatures</div><ul>${sigs}</ul></div>`:''}
    ${related?`<div class="related"><div class="field-label">Related Documents</div>${related}</div>`:''}
    ${d.file_path?`<div class="field"><div class="field-label">File Path</div><div class="field-value"><code>${d.file_path}</code></div></div>`:''}
    ${editBtns}
  </div>`;
}

async function renderSessions(c){
  const d=await api('sessions?limit=50');
  let rows=d.sessions.map(s=>{
    let status=s.ended_at?'ended':'active';
    return `<tr>
      <td>${s.topic||s.session_id?.substring(0,8)||'—'}</td>
      <td>${s.project||'—'}</td>
      <td>${status}</td>
      <td>${s.message_count||0}</td>
      <td>${s.source||'—'}</td></tr>`;
  }).join('');
  c.innerHTML=`
  <table><thead><tr><th>Topic</th><th>Project</th><th>Status</th><th>Messages</th><th>Source</th></tr></thead>
  <tbody>${rows||'<tr><td colspan=5 class="empty">No sessions</td></tr>'}</tbody></table>`;
}

async function renderDream(c){
  const d=await api('dream-log');
  let rows=d.logs.map(l=>{
    let dt=new Date(l.timestamp*1000).toLocaleString();
    return `<tr><td>${dt}</td><td>${l.project||'—'}</td><td>${l.action}</td>
      <td>${l.facts_merged}</td><td>${l.contradictions_resolved}</td><td>${l.sessions_count}</td></tr>`;
  }).join('');
  c.innerHTML=`
  <table><thead><tr><th>Time</th><th>Project</th><th>Action</th><th>Facts Merged</th><th>Contradictions</th><th>Sessions</th></tr></thead>
  <tbody>${rows||'<tr><td colspan=6 class="empty">No dream runs yet</td></tr>'}</tbody></table>`;
}

async function deleteDoc(id){
  if(!confirm('Delete this document?'))return;
  await apiPost('document/delete',{doc_id:id});
  state.detail=null;render();
}
async function changePriority(id,p){
  if(!p)return;
  await apiPost('document/priority',{doc_id:id,priority:p});
  render();
}

render();
</script>
</body>
</html>"""
