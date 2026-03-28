#!/usr/bin/env python3
"""CLI entry point for agentmem.

Usage:
  am init                                   # one-time setup (MCP + hooks + CLAUDE.md)
  am mcp                                    # start MCP server (stdio)
  am doc save --title T --content C [--priority P1] [--source hook]
  am search --query Q [--max-tokens 1500] [--format inject|json]
  am state set --key K --value V
  am state get --key K
  am session start --project P --topic T   # prints session_id
  am session end --session-id S [--summary "..."]
  am session message --session-id S --role R --content C
  am session resume --session-id S [--max-tokens 2000]
"""
import sys
import json
import argparse
from agent_memory.store import MemoryStore

_store = None


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


def cmd_doc(args):
    if args.action == "save":
        content = args.content or sys.stdin.read()
        doc_id = get_store().save(
            title=args.title,
            content=content,
            priority=getattr(args, "priority", "P1"),
            source=getattr(args, "source", "hook"),
            file_path=getattr(args, "file_path", None) or None,
        )
        print(doc_id)

    elif args.action == "prune":
        deleted = get_store().prune_expired()
        print(f"Pruned {deleted} expired document(s)")
        return

    elif args.action == "enhance":
        import sqlite3
        import json as _json
        from agent_memory.db import DB_PATH
        from agent_memory.llm_extract import llm_extract
        from agent_memory.vector import embed_doc, vec_to_blob

        source_filter = getattr(args, "source", None) or None
        force = getattr(args, "force", False)

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        q = "SELECT doc_id, title, raw_content, generator FROM documents WHERE raw_content IS NOT NULL"
        params = []
        if not force:
            q += " AND generator != 'llm'"
        if source_filter:
            q += " AND source = ?"
            params.append(source_filter)

        docs = conn.execute(q, params).fetchall()
        total = len(docs)
        print(f"Enhancing {total} docs", flush=True)

        ok = fb = 0
        for i, row in enumerate(docs, 1):
            content = row["raw_content"] or ""
            title   = row["title"] or ""
            doc_id  = row["doc_id"]
            llm_fields = llm_extract(content, title_hint=title)
            if llm_fields:
                conn.execute(
                    "UPDATE documents SET title=?, summary=?, key_facts=?, decisions=?, generator='llm' WHERE doc_id=?",
                    (llm_fields["title"] or title, llm_fields["summary"],
                     _json.dumps(llm_fields["key_facts"]), _json.dumps(llm_fields["decisions"]), doc_id),
                )
                use = llm_fields
                ok += 1
            else:
                use = {"title": title, "summary": "", "key_facts": []}
                fb += 1
            vec = embed_doc(use.get("title") or title, use.get("summary") or "", use.get("key_facts") or [])
            if vec:
                conn.execute("UPDATE documents SET embedding=? WHERE doc_id=?", (vec_to_blob(vec), doc_id))
            conn.commit()
            print(f"[{i}/{total}] {'llm' if llm_fields else 'rule'} {title[:55]}", flush=True)

        print(f"Done: {ok} llm  {fb} fallback")
        conn.close()


def cmd_search(args):
    results = get_store().search(args.query, max_results=5)
    fmt = getattr(args, "format", "inject")
    if fmt == "json":
        print(json.dumps([{
            "id": r.id, "type": r.type,
            "l1": r.l1, "l2": r.l2,
            "score": r.score, "priority": r.priority,
        } for r in results]))
    else:
        # max_tokens governs inject() token budget, not search()
        print(get_store().inject(results,
              max_tokens=getattr(args, "max_tokens", 3000)))


def cmd_state(args):
    s = get_store()
    if args.action == "set":
        try:
            value = json.loads(args.value)
        except (json.JSONDecodeError, TypeError):
            value = args.value
        s.state.set(args.key, value)
    elif args.action == "get":
        val = s.state.get(args.key)
        if val is None:
            sys.exit(1)
        print(json.dumps(val) if not isinstance(val, str) else val)


def cmd_session(args):
    s = get_store()
    if args.action == "start":
        sid = s.session.start(
            project=getattr(args, "project", ""),
            topic=getattr(args, "topic", ""),
            source=getattr(args, "source", ""),
        )
        print(sid)
    elif args.action == "latest":
        sid = s.session.get_latest_session_id(
            source=getattr(args, "source", None) or None,
            project=getattr(args, "project", None) or None,
        )
        if sid:
            print(sid)
        else:
            sys.exit(1)
    elif args.action == "end":
        s.session.end(
            session_id=args.session_id,
            summary=getattr(args, "summary", None),
        )
    elif args.action == "message":
        s.session.save_message(args.session_id, args.role, args.content)
    elif args.action == "resume":
        ctx = s.session.get_resume_context(
            args.session_id,
            max_tokens=getattr(args, "max_tokens", 2000),
        )
        print(json.dumps(ctx))
    elif args.action == "delete":
        s.session.delete(args.session_id)
    elif args.action == "checkpoint":
        result = s.session.checkpoint(args.session_id)
        print(json.dumps(result))
    elif args.action == "list":
        rows = s.session.list_for_dashboard(
            limit=getattr(args, "limit", 100),
            include_cli=getattr(args, "include_cli", False),
        )
        print(json.dumps(rows))


def cmd_mcp(_args):
    from agent_memory.mcp_server import run
    run()


def cmd_init(_args):
    from agent_memory.init_cmd import run
    run()


def cmd_status(args):
    from agent_memory.watch import status_line
    status_line(
        event=getattr(args, "event", "idle"),
        detail=getattr(args, "detail", ""),
    )


def main():
    from agent_memory import __version__
    p = argparse.ArgumentParser(prog="am")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd")

    # doc save
    doc_p = sub.add_parser("doc")
    doc_p.add_argument("action", choices=["save", "enhance", "prune"])
    doc_p.add_argument("--title", default="")
    doc_p.add_argument("--content", default=None)
    doc_p.add_argument("--priority", default="P1")
    doc_p.add_argument(
        "--source",
        default="hook",
        choices=[
            "architectural_decision", "debug_solution", "technical_insight",
            "session_note", "routine",
            "hook", "session_extract", "explicit",
        ],
    )
    doc_p.add_argument("--file-path", default=None, dest="file_path")
    doc_p.add_argument("--force", action="store_true", help="Re-enhance already-llm docs")

    # search
    s_p = sub.add_parser("search")
    s_p.add_argument("--query", required=True)
    s_p.add_argument("--max-tokens", type=int, default=1500, dest="max_tokens")
    s_p.add_argument("--format", default="inject", choices=["inject", "json"])

    # state
    st_p = sub.add_parser("state")
    st_p.add_argument("action", choices=["set", "get"])
    st_p.add_argument("--key", required=True)
    st_p.add_argument("--value", default=None)

    # session
    se_p = sub.add_parser("session")
    se_p.add_argument("action", choices=["start", "end", "delete", "message", "resume", "latest", "list", "checkpoint"])
    se_p.add_argument("--project", default="")
    se_p.add_argument("--topic", default="")
    se_p.add_argument("--session-id", default=None, dest="session_id")
    se_p.add_argument("--role", default="user")
    se_p.add_argument("--content", default="")
    se_p.add_argument("--summary", default=None)
    se_p.add_argument("--max-tokens", type=int, default=2000, dest="max_tokens")
    se_p.add_argument("--source", default="", dest="source")
    se_p.add_argument("--limit", type=int, default=100)
    se_p.add_argument("--include-cli", action="store_true", dest="include_cli")

    # status (called by hooks to show inline feedback)
    status_p = sub.add_parser("status", help="Print one-line colored status (used by hooks)")
    status_p.add_argument("--event", default="idle",
                          choices=["session", "checkpoint", "message", "save", "search", "prune", "error", "idle"])
    status_p.add_argument("--detail", default="")

    sub.add_parser("init", help="One-time setup: MCP registration, hooks, CLAUDE.md")
    sub.add_parser("mcp", help="Start MCP server (stdio transport)")
    args = p.parse_args()

    if args.cmd == "init":
        cmd_init(args)
    elif args.cmd == "mcp":
        cmd_mcp(args)
    elif args.cmd == "doc":
        cmd_doc(args)
    elif args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "state":
        cmd_state(args)
    elif args.cmd == "session":
        cmd_session(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
