"""CLI entry point for the AI Bug Bounty Agent.

The only hardcoded thing here is a generic dispatcher: build an argparse
subcommand from each registered Command's declared args, then call its
handler. What commands exist and what they do lives entirely in commands/ —
this file never names a specific command.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from config import override_settings
from agents.orchestrator import Orchestrator
import commands
from commands.base import REGISTRY

# Module-level so other functions can access the run's log path
_log_file_path: str | None = None


def setup_logging(verbose: bool = False):
    global _log_file_path
    level = logging.DEBUG if verbose else logging.INFO

    # Create log directory
    log_dir = os.path.join("data", "logs")
    os.makedirs(log_dir, exist_ok=True)

    # Timestamped log file
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file_path = os.path.join(log_dir, f"{ts}.log")
    _web_log_path = os.path.join(log_dir, f"{ts}_web.log")

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_log_file_path, encoding="utf-8"),
        ],
    )

    # Route noisy web/HTTP loggers to a separate file so the main log stays clean
    web_handler = logging.FileHandler(_web_log_path, encoding="utf-8")
    web_handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    for web_logger_name in ("aiohttp.access", "httpx"):
        wl = logging.getLogger(web_logger_name)
        wl.handlers.clear()
        wl.addHandler(web_handler)
        wl.propagate = False  # don't send to root (main log + console)

    # LLM wire-level debug log — captures full request/response payloads
    _wire_log_path = os.path.join(log_dir, f"{ts}_llm_wire.log")
    wire_handler = logging.FileHandler(_wire_log_path, encoding="utf-8")
    wire_handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    wire_logger = logging.getLogger("llm.wire")
    wire_logger.handlers.clear()
    wire_logger.addHandler(wire_handler)
    wire_logger.setLevel(logging.DEBUG)
    wire_logger.propagate = False  # file-only, not console or dashboard

    logging.getLogger().info("Logging to %s", _log_file_path)
    logging.getLogger().info("Web access logs at %s", _web_log_path)
    logging.getLogger().info("LLM wire debug at %s", _wire_log_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasma",
        description="Configurable AI-assisted pentesting and security research",
    )
    from importlib.metadata import PackageNotFoundError, version
    try:
        installed_version = version("fantasma")
    except PackageNotFoundError:
        installed_version = "development (not installed)"
    parser.add_argument("--version", action="version", version=f"%(prog)s {installed_version}")
    parser.add_argument("--llm", choices=["claude", "gemini", "ollama", "openai", "vertex"], default=None,
                        help="LLM provider to use")
    parser.add_argument("--output-dir", default=None, help="Output directory for reports")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--openai-base-url", default=None,
                        help="Base URL for OpenAI-compatible API (Novita, vLLM, etc.)")
    parser.add_argument("--openai-api-key", default=None,
                        help="API key for OpenAI-compatible provider")
    parser.add_argument("--vertex-project", default=None,
                        help="GCP project ID for Vertex AI")
    parser.add_argument("--vertex-region", default=None,
                        help="GCP region for Vertex AI (default: global)")
    parser.add_argument("--vertex-model", default=None,
                        help="Vertex AI model ID (e.g. claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5)")
    parser.add_argument("--gemini-api-key", default=None,
                        help="Google Gemini API key")
    parser.add_argument("--gemini-model", default=None,
                        help="Gemini model ID (e.g. gemini-2.5-flash-preview-05-20, gemini-2.5-pro-preview-06-05)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Every subcommand is data: each entry in the registry (populated by
    # importing `commands`, where every module self-registers via @register)
    # supplies its own help text and Arg list. Adding, removing, or changing
    # the flags of a command never touches this file.
    for name, cmd in REGISTRY.items():
        sub = subparsers.add_parser(name, help=cmd.help)
        for arg in cmd.args:
            arg.add_to(sub)

    return parser


async def async_main(args: argparse.Namespace) -> None:
    overrides = {}
    if args.llm:
        overrides["llm_provider"] = args.llm
    if args.output_dir:
        overrides["output_dir"] = args.output_dir
    if args.verbose:
        overrides["verbose"] = True
    if getattr(args, "openai_base_url", None):
        overrides["openai_base_url"] = args.openai_base_url
    if getattr(args, "openai_api_key", None):
        overrides["openai_api_key"] = args.openai_api_key
    if getattr(args, "vertex_project", None):
        overrides["vertex_project_id"] = args.vertex_project
    if getattr(args, "vertex_region", None):
        overrides["vertex_region"] = args.vertex_region
    if getattr(args, "vertex_model", None):
        overrides["vertex_model"] = args.vertex_model
    if getattr(args, "gemini_api_key", None):
        overrides["gemini_api_key"] = args.gemini_api_key
    if getattr(args, "gemini_model", None):
        overrides["gemini_model"] = args.gemini_model
    if overrides:
        override_settings(**overrides)

    orchestrator = None if args.command in ("full", "profiles") else Orchestrator(llm_name=args.llm)

    command = REGISTRY[args.command]
    await command.handler(args, orchestrator)


def main():
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] or ["full"])
    if args.command != "profiles":
        setup_logging(args.verbose)

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        # Try to save a checkpoint if we were running a swarm pipeline
        try:
            from memory.checkpoint import list_checkpoints
            from memory.store import PersistentMemory
            store = PersistentMemory()
            checkpoints = list_checkpoints(store)
            store.close()
            if checkpoints:
                latest = checkpoints[0]
                name = latest.get("run_name", "")
                label = name or f"scan #{latest['scan_id']}"
                print(
                    f"\nCheckpoint saved ({label}, "
                    f"{latest['domains']} domains, {latest['findings']} findings)."
                )
                resume_hint = f"--resume-scan {name}" if name else "--resume"
                print(f"Resume with: fantasma full {resume_hint}")
            else:
                print("\nAborted.")
        except Exception:
            print("\nAborted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
