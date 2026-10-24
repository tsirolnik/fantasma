"""Run-scoped MCP sessions shared by tool agents, including later replacements.

Enter/exit on the same task: SDK transports own AnyIO cancellation scopes.
Context variables isolate concurrent runs and propagate to their child tasks.
"""
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import timedelta
import hashlib
import os
import re

from agents.tools import Tool, ToolRegistry

_current = ContextVar("mcp_tools", default=None)


def attach_mcp_tools(registry):
    extra = _current.get()
    if extra is not None:
        for tool in extra.list_tools():
            if tool.name in registry:
                raise ValueError(f"MCP tool collision: {tool.name}")
            registry.register(tool)


def tool_name(server, name):
    # Provider-safe, <=64 chars; hash keeps punctuation/truncation distinct.
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:20]
    return f"mcp_{server}_{slug}_{hashlib.sha256(name.encode()).hexdigest()[:10]}"


def environment_refs(mapping):
    values = {}
    for key, variable in mapping.items():
        if variable not in os.environ:
            raise ValueError(f"Missing MCP environment variable: {variable}")
        values[key] = os.environ[variable]
    return values


def result_payload(result):
    blocks = []
    for block in result.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "resource" and hasattr(block.resource, "text"):
            blocks.append({"type": "resource", "uri": str(block.resource.uri), "text": block.resource.text})
        else:
            blocks.append({"type": block.type, "omitted": "Non-text content is not forwarded"})
    return {"isError": result.isError, "content": blocks, "structuredContent": result.structuredContent}


class MCPRuntime:
    def __init__(self):
        self.stack = AsyncExitStack()
        self.tools = ToolRegistry()

    async def __aenter__(self):
        await self.stack.__aenter__()
        self.token = _current.set(self.tools)
        return self

    async def __aexit__(self, *exc):
        try:
            # Close resources without wrapping application errors in SDK task groups.
            await self.stack.aclose()
            return False
        finally:
            _current.reset(self.token)

    async def connect(self, servers):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client

        for spec in servers:
            if spec.transport == "stdio":
                transport = stdio_client(StdioServerParameters(
                    command=spec.command, args=spec.args, cwd=spec.cwd,
                    env=environment_refs(spec.env),
                ))
            else:
                factory = sse_client if spec.transport == "sse" else streamablehttp_client
                transport = factory(spec.url, headers=environment_refs(spec.headers),
                                    timeout=spec.timeout, sse_read_timeout=spec.timeout)
            streams = await self.stack.enter_async_context(transport)
            session = await self.stack.enter_async_context(ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=spec.timeout)))
            await session.initialize()
            discovered, cursor, cursors = {}, None, set()
            while True:
                page = await session.list_tools(cursor=cursor)
                for remote in page.tools:
                    if remote.name in discovered:
                        raise ValueError(f"Duplicate MCP tool from {spec.name}: {remote.name}")
                    discovered[remote.name] = remote
                cursor = page.nextCursor
                if not cursor:
                    break
                if cursor in cursors:
                    raise ValueError(f"Repeated MCP pagination cursor from {spec.name}")
                cursors.add(cursor)
            if spec.tools is not None and set(spec.tools) - discovered.keys():
                raise ValueError(f"Unknown tools in MCP allowlist for {spec.name}: {sorted(set(spec.tools) - discovered.keys())}")
            for remote in discovered.values():
                if spec.tools is not None and remote.name not in spec.tools:
                    continue
                async def invoke(arguments, session=session, name=remote.name):
                    result = await session.call_tool(name, arguments)
                    payload = result_payload(result)
                    if result.isError:
                        raise RuntimeError(str(payload))
                    return payload
                self.tools.register(Tool(
                    name=tool_name(spec.name, remote.name),
                    description=f"MCP {spec.name}/{remote.name}: {remote.description or ''}",
                    parameters=remote.inputSchema, handler=invoke,
                    category="mcp", risk_level="high",
                ))
