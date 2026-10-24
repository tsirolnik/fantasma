"""Declarative MCP connections. Credentials are environment references only."""
from dataclasses import dataclass, field
import math
import re
from urllib.parse import urlsplit


@dataclass
class MCPServer:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    tools: list[str] | None = None
    timeout: float = 60

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", self.name):
            raise ValueError("MCP name must contain 1–24 letters, numbers, underscores or hyphens")
        if self.transport not in ("stdio", "streamable_http", "sse"):
            raise ValueError("MCP transport must be stdio, streamable_http or sse")
        if not isinstance(self.args, list) or any(not isinstance(x, str) for x in self.args):
            raise ValueError("MCP args must be an array of strings")
        for mapping in (self.env, self.headers):
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in mapping.items()):
                raise ValueError("MCP env/headers must map names to environment variable names")
        if self.tools is not None and (not isinstance(self.tools, list) or any(not isinstance(x, str) for x in self.tools)):
            raise ValueError("MCP tools must be an array of exact tool names")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MCP timeout must be a finite positive number")
        if self.transport == "stdio":
            if not isinstance(self.command, str) or not self.command.strip() or self.url or self.headers:
                raise ValueError("stdio requires command and forbids url/headers")
            if self.cwd is not None and not isinstance(self.cwd, str):
                raise ValueError("MCP cwd must be a path string")
        else:
            parsed = urlsplit(self.url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("HTTP MCP requires an http(s) URL without embedded credentials")
            if self.command or self.args or self.env or self.cwd:
                raise ValueError("HTTP MCP does not accept command, args, env or cwd")
