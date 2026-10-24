"""Run security tools inside Docker containers with host isolation."""

import asyncio
import json
import logging
import os
import shutil
from uuid import uuid4

logger = logging.getLogger(__name__)

DOCKER_BIN = shutil.which("docker") or "docker"

# Default timeout for containerized tool runs (5 minutes)
DEFAULT_TIMEOUT = 300

# Bridge networking separates container localhost from host localhost, but is
# NOT an egress firewall. Host/LAN addresses may still be reachable.
# Benchmarks opt into a network containing the local target containers.
CONTAINER_NETWORK = os.environ.get("SWARMHOUND_TOOL_NETWORK", "bridge")


def _apply_host_rewrite(args: list[str]) -> list[str]:
    """Rewrite host:port tokens in tool args, when SWARMHOUND_TOOL_HOST_REWRITE
    is set (benchmark only). Lets agents use one URL (e.g. localhost:3000) while
    sandboxed tools transparently hit the target's in-network container name.

    Format: "from1=to1,from2=to2" e.g. "localhost:3000=bench-juice-shop:3000".
    No-op when the env var is unset, so normal runs are unaffected.
    """
    mapping = os.environ.get("SWARMHOUND_TOOL_HOST_REWRITE", "")
    if not mapping:
        return args
    pairs = [p.split("=", 1) for p in mapping.split(",") if "=" in p]
    rewritten = []
    for a in args:
        for frm, to in pairs:
            a = a.replace(frm, to)
        rewritten.append(a)
    return rewritten


async def run_container(
    image: str,
    args: list[str],
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """Run a Docker container with security isolation.

    Security constraints:
    - bridge network (not a host/LAN egress firewall)
    - no-new-privileges: prevents privilege escalation
    - memory and CPU limits
    - no volume mounts (cannot access host filesystem)
    - dropped all capabilities except NET_RAW (needed for nmap/ping)

    Args:
        image: Docker image name (e.g. "bugbounty-tools:latest")
        args: Command-line arguments to pass to the container entrypoint
        timeout: Max seconds to wait before killing

    Returns:
        Tuple of (exit_code, stdout, stderr)
    """
    args = _apply_host_rewrite(args)
    container_name = "swarmhound-tool-" + uuid4().hex
    cmd = [
        DOCKER_BIN, "run", "--rm", "--name", container_name,
        # Bridge provides internet access; it does not block host/LAN IPs
        "--network", CONTAINER_NETWORK,
        # Disable the convenience hostname (not direct gateway IP access)
        "--add-host", "host.docker.internal:0.0.0.0",
        # Resource limits
        "--memory", "512m",
        "--cpus", "1",
        "--pids-limit", "256",
        # Security: prevent privilege escalation
        "--security-opt", "no-new-privileges:true",
        # Drop all capabilities, add back only what's needed
        "--cap-drop", "ALL",
        "--cap-add", "NET_RAW",     # nmap, ping
        # No host filesystem access (no -v mounts)
        image,
        *args,
    ]

    logger.info("docker run %s %s", image, " ".join(args))

    proc = None
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    try:
        # Keep the process handle available even if cancellation races startup.
        try:
            proc = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            proc = await spawn
            raise
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (proc.returncode or 0, stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"))
    except asyncio.TimeoutError:
        return (-1, "", f"Timed out after {timeout}s")
    except FileNotFoundError:
        return (-1, "", "Docker not found. Install Docker to use containerized tools.")
    except Exception as e:
        logger.exception("Failed to run container %s", image)
        return (-1, "", str(e))
    finally:
        if proc is not None:
            cleanup = asyncio.create_task(_cleanup_container(proc, container_name))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise


async def _cleanup_container(proc, name: str) -> None:
    """Stop the CLI and explicitly remove its daemon-managed container."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
    cleanup_proc = None
    try:
        cleanup_proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN, "rm", "--force", name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(cleanup_proc.communicate(), timeout=15)
        if cleanup_proc.returncode and b"No such container" not in stderr:
            logger.error("Could not remove tool container %s: %s", name, stderr.decode(errors="replace"))
    except Exception:
        logger.exception("Container cleanup failed for %s; check Docker manually", name)
    finally:
        if cleanup_proc is not None and cleanup_proc.returncode is None:
            cleanup_proc.kill()
            await cleanup_proc.communicate()


async def ensure_image(image: str) -> bool:
    """Pull a Docker image if not already present. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN, "image", "inspect", image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            return True

        logger.info("Pulling Docker image %s...", image)
        proc = await asyncio.create_subprocess_exec(
            DOCKER_BIN, "pull", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        return proc.returncode == 0
    except Exception as e:
        logger.error("Failed to ensure image %s: %s", image, e)
        return False


def parse_jsonl(output: str) -> list[dict]:
    """Parse newline-delimited JSON output from tools like katana/nuclei."""
    results = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return results
