"""CLI-style tool adapter to reduce context token usage.

Instead of sending 33+ individual JSON tool schemas on every LLM call
(~5K tokens), this adapter exposes a single "run" tool and puts a
compact CLI-style help reference in the system prompt (~800 tokens).

The LLM outputs:  run(command="nmap_scan --target example.com --ports 80,443")
The adapter parses the command string and dispatches to the real tool handler.

Token savings: ~4.5K tokens per iteration × 10 iterations = ~45K tokens/run.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from agents.tools import ToolRegistry

logger = logging.getLogger(__name__)


class CLIToolAdapter:
    """Wraps a ToolRegistry to expose tools as CLI-style commands."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def help_text(self) -> str:
        """Generate a compact CLI reference from the registry.

        One line per tool, ~30 tokens each vs ~150 for JSON schema.
        """
        lines = ["## Available Commands\n"]
        for tool in self._registry.list_tools():
            params = tool.parameters.get("properties", {})
            required = set(tool.parameters.get("required", []))
            parts = [tool.name]
            for pname, pschema in params.items():
                ptype = pschema.get("type", "str").upper()
                if ptype == "STRING":
                    ptype = "STR"
                elif ptype == "INTEGER":
                    ptype = "INT"
                elif ptype == "OBJECT":
                    ptype = "JSON"
                elif ptype == "ARRAY":
                    ptype = "LIST"
                elif ptype == "NUMBER":
                    ptype = "NUM"
                elif ptype == "BOOLEAN":
                    ptype = "BOOL"
                if pname in required:
                    parts.append(f"--{pname} {ptype}")
                else:
                    parts.append(f"[--{pname} {ptype}]")
            sig = " ".join(parts)
            # First sentence of description only
            desc = tool.description.split("\n")[0].strip()
            if len(desc) > 100:
                desc = desc[:97] + "..."
            lines.append(f"{sig}")
            lines.append(f"  {desc}\n")
        return "\n".join(lines)

    def tool_schema(self) -> list[dict]:
        """Return a single 'run' tool in Anthropic API format."""
        return [
            {
                "name": "run",
                "description": (
                    "Execute a command. Use the CLI reference in the prompt "
                    "to see available commands and their arguments. "
                    "Format: command_name --arg1 value1 --arg2 value2"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The full command string (e.g. 'nmap_scan --target example.com --ports 80,443')",
                        },
                    },
                    "required": ["command"],
                },
            }
        ]

    def tool_schema_ollama(self) -> list[dict]:
        """Return a single 'run' tool in Ollama API format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "run",
                    "description": (
                        "Execute a command. Use the CLI reference in the prompt "
                        "to see available commands and their arguments. "
                        "Format: command_name --arg1 value1 --arg2 value2"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The full command string",
                            },
                        },
                        "required": ["command"],
                    },
                },
            }
        ]

    def parse(self, command: str) -> tuple[str, dict]:
        """Parse a CLI-style command string into (tool_name, params).

        Handles:
        - --key value pairs
        - Quoted strings (shlex handles this)
        - JSON values ({...} or [...])
        - Integer values
        - Boolean flags (--flag without value)
        """
        tokens = shlex.split(command)
        if not tokens:
            raise ValueError("Empty command")

        tool_name = tokens[0]
        params: dict[str, Any] = {}
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--"):
                key = token[2:]
                # Check if next token exists and is not another flag
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    value = tokens[i + 1]
                    params[key] = self._coerce_value(value)
                    i += 2
                else:
                    # Boolean flag
                    params[key] = True
                    i += 1
            else:
                # Positional arg — treat as first required param
                tool = self._registry.get(tool_name)
                if tool:
                    required = tool.parameters.get("required", [])
                    if required:
                        first_req = required[0]
                        if first_req not in params:
                            params[first_req] = self._coerce_value(token)
                i += 1

        return tool_name, params

    async def execute(self, command: str) -> Any:
        """Parse and execute a CLI command, returning the result.

        Returns a tuple of (tool_name, params, result, error).
        """
        tool_name, params = self.parse(command)

        tool = self._registry.get(tool_name)
        if tool is None:
            return tool_name, params, None, f"Unknown command: {tool_name}"

        try:
            result = await tool.handler(params)
            return tool_name, params, result, None
        except Exception as exc:
            return tool_name, params, None, str(exc)

    @staticmethod
    def _coerce_value(value: str) -> Any:
        """Coerce a string value to its likely Python type."""
        # JSON object or array
        if (value.startswith("{") and value.endswith("}")) or \
           (value.startswith("[") and value.endswith("]")):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass

        # Integer
        try:
            return int(value)
        except ValueError:
            pass

        # Float
        try:
            return float(value)
        except ValueError:
            pass

        # Boolean strings
        if value.lower() in ("true", "yes"):
            return True
        if value.lower() in ("false", "no"):
            return False

        return value
