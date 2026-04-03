"""MCP server for am-memory.

Exposes am_search, am_save, am_state_get, am_state_set, am_session_checkpoint,
am_namespace_list as MCP tools.

Supports two transports:
  - stdio (default): am mcp — used by Claude Code
  - sse: am serve --transport sse --port 3333 — used by Cursor, Windsurf, etc.
"""
import json
import sys

from agent_memory.store import MemoryStore

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def _create_server(store: MemoryStore, read_only: bool = False) -> "Server":
    """Create and configure the MCP server with all tools."""
    server = Server("am-memory")

    @server.list_tools()
    async def list_tools():
        tools = [
            types.Tool(
                name="am_search",
                description=(
                    "Search persistent memory for relevant documents and past session knowledge. "
                    "Call this before answering questions about past work, architecture decisions, "
                    "debugging solutions, or technical constraints."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query — use technical terms, keywords, or concepts",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 5,
                            "description": "Max results to return",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="am_namespace_list",
                description=(
                    "List all known namespaces (projects) with document counts. "
                    "Useful for understanding what knowledge is stored across projects."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="am_state_get",
                description="Get a value from the current session state (e.g. active_work, scratchpad).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "State key"},
                    },
                    "required": ["key"],
                },
            ),
        ]

        if not read_only:
            tools.extend([
                types.Tool(
                    name="am_save",
                    description=(
                        "Save a piece of knowledge to persistent memory. "
                        "Call this when you discover something non-obvious: config constraints, "
                        "architectural decisions, debug solutions, or gotchas worth remembering. "
                        "Choose source based on content type — it determines TTL automatically: "
                        "architectural_decision=never expires, debug_solution/technical_insight=90d, "
                        "session_note/routine=30d."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "One-line title — used as primary search signal",
                            },
                            "content": {
                                "type": "string",
                                "description": "Full content — facts, decisions, context",
                            },
                            "source": {
                                "type": "string",
                                "enum": [
                                    "architectural_decision",
                                    "debug_solution",
                                    "technical_insight",
                                    "session_note",
                                    "routine",
                                ],
                                "description": (
                                    "architectural_decision: core design choices, never expires. "
                                    "debug_solution: non-obvious fixes, 90d. "
                                    "technical_insight: analysis/trade-offs/lessons, 90d. "
                                    "session_note: ephemeral context, 30d. "
                                    "routine: standard ops, 30d."
                                ),
                            },
                        },
                        "required": ["title", "content", "source"],
                    },
                ),
                types.Tool(
                    name="am_session_checkpoint",
                    description=(
                        "Checkpoint the current session: extract knowledge and promote to persistent memory "
                        "without ending the session. Safe to call multiple times (upserts)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                types.Tool(
                    name="am_state_set",
                    description="Set a value in the current session state.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {"description": "Any JSON-serializable value"},
                        },
                        "required": ["key", "value"],
                    },
                ),
            ])

        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name == "am_search":
            query = arguments["query"]
            limit = int(arguments.get("limit", 5))
            project = store.state.get("current_project")
            results = store.search(query, max_results=limit, project=project)
            output = store.inject(results, max_tokens=1500)
            return [types.TextContent(type="text", text=output or "No results found.")]

        elif name == "am_save":
            if read_only:
                return [types.TextContent(type="text", text="Error: server is in read-only mode")]
            title = arguments["title"]
            content = arguments["content"]
            source = arguments["source"]
            project = store.state.get("current_project")
            doc_id = store.save(title=title, content=content, source=source, project=project)
            return [types.TextContent(type="text", text=f"Saved as doc_id={doc_id}")]

        elif name == "am_session_checkpoint":
            if read_only:
                return [types.TextContent(type="text", text="Error: server is in read-only mode")]
            session_id = store.state.get("current_session_id")
            if not session_id:
                return [types.TextContent(type="text", text="No active session")]
            result = store.session.checkpoint(session_id)
            summary = result.get("summary", "")
            doc_id = result.get("doc_id")
            text = f"Checkpoint done. doc_id={doc_id}" if doc_id else f"Checkpoint done (not promoted: {summary[:50]})"
            return [types.TextContent(type="text", text=text)]

        elif name == "am_namespace_list":
            namespaces = store.namespace_list()
            text = json.dumps(namespaces, indent=2)
            return [types.TextContent(type="text", text=text)]

        elif name == "am_state_get":
            key = arguments["key"]
            val = store.state.get(key)
            text = json.dumps(val) if val is not None else "null"
            return [types.TextContent(type="text", text=text)]

        elif name == "am_state_set":
            if read_only:
                return [types.TextContent(type="text", text="Error: server is in read-only mode")]
            key = arguments["key"]
            value = arguments["value"]
            store.state.set(key, value)
            return [types.TextContent(type="text", text="ok")]

        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


def run(transport: str = "stdio", port: int = 3333, read_only: bool = False):
    """Start the MCP server.

    Args:
        transport: "stdio" (default, for Claude Code) or "sse" (for Cursor, Windsurf)
        port: HTTP port for SSE transport (default 3333)
        read_only: if True, write tools (am_save, am_state_set, am_session_checkpoint) are disabled
    """
    if not _MCP_AVAILABLE:
        print(
            "ERROR: mcp package not installed. Run: pip install 'am-memory[mcp]'",
            file=sys.stderr,
        )
        sys.exit(1)

    store = MemoryStore()
    server = _create_server(store, read_only=read_only)

    import asyncio

    if transport == "sse":
        asyncio.run(_serve_sse(server, port))
    else:
        asyncio.run(_serve_stdio(server))


async def _serve_stdio(server):
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


async def _serve_sse(server, port: int):
    """Serve MCP over SSE (Server-Sent Events) for cross-tool access."""
    try:
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Route, Mount
        import uvicorn
    except ImportError:
        print(
            "ERROR: SSE transport requires starlette and uvicorn. "
            "Run: pip install starlette uvicorn",
            file=sys.stderr,
        )
        sys.exit(1)

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1],
                server.create_initialization_options()
            )

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

    print(f"am-memory MCP server (SSE) listening on http://localhost:{port}")
    print(f"  SSE endpoint: http://localhost:{port}/sse")
    print(f"  Messages endpoint: http://localhost:{port}/messages/")

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server_instance = uvicorn.Server(config)
    await server_instance.serve()
