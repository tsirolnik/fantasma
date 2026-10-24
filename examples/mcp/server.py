"""Harmless local MCP server for verifying the integration."""
from mcp.server.fastmcp import FastMCP

server = FastMCP("SwarmHound demo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def fail() -> str:
    """Return a deliberate tool error for integration tests."""
    raise ValueError("deliberate demo error")


if __name__ == "__main__":
    server.run(transport="stdio")
