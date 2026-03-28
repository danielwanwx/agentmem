"""MCP server for agentmem.

Exposes am_search, am_save, am_state_get, am_state_set as MCP tools.
Started via: am mcp  (stdio transport)
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


def run():
    if not _MCP_AVAILABLE:
        print(
            "ERROR: mcp package not installed. Run: pip install 'agentmem[mcp]'",
            file=sys.stderr,
        )
        sys.exit(1)

    store = MemoryStore()
    server = Server("agentmem")

    @server.list_tools()
    async def list_tools():
        return [
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
                    "without ending the session. Call during long sessions to ensure cross-session visibility. "
                    "Safe to call multiple times (upserts)."
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
        ]

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
            title = arguments["title"]
            content = arguments["content"]
            source = arguments["source"]
            project = store.state.get("current_project")
            doc_id = store.save(title=title, content=content, source=source, project=project)
            return [types.TextContent(type="text", text=f"Saved as doc_id={doc_id}")]

        elif name == "am_session_checkpoint":
            session_id = store.state.get("current_session_id")
            if not session_id:
                return [types.TextContent(type="text", text="No active session")]
            result = store.session.checkpoint(session_id)
            summary = result.get("summary", "")
            doc_id = result.get("doc_id")
            text = f"Checkpoint done. doc_id={doc_id}" if doc_id else f"Checkpoint done (not promoted: {summary[:50]})"
            return [types.TextContent(type="text", text=text)]

        elif name == "am_state_get":
            key = arguments["key"]
            val = store.state.get(key)
            text = json.dumps(val) if val is not None else "null"
            return [types.TextContent(type="text", text=text)]

        elif name == "am_state_set":
            key = arguments["key"]
            value = arguments["value"]
            store.state.set(key, value)
            return [types.TextContent(type="text", text="ok")]

        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    import asyncio
    asyncio.run(_serve(server))


async def _serve(server):
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())
