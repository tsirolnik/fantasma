"""OllamaPool — spawn and manage multiple Ollama instances for parallel inference.

Used in swarm mode when the model architecture doesn't support OLLAMA_NUM_PARALLEL
(e.g. Qwen3.5's hybrid DeltaNet+Attention arch). Each swarm agent gets its own
Ollama instance on a separate port for true parallel inference.

Usage:
    pool = OllamaPool(num_instances=3, model="qwen3.5:9b")
    urls = await pool.start()   # ["http://127.0.0.1:11434", "http://127.0.0.1:11435", ...]
    ...
    pool.stop()
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_PORT = 11434  # All instances start here (11434, 11435, ...)


class OllamaPool:
    """Manages multiple Ollama server instances on different ports."""

    def __init__(
        self,
        num_instances: int,
        model: str = "",
        base_port: int = DEFAULT_BASE_PORT,
    ) -> None:
        self.num_instances = num_instances
        self.model = model
        self.base_port = base_port
        self._processes: list[subprocess.Popen] = []

    async def start(self) -> list[str]:
        """Start Ollama instances and return a list of base URLs (one per agent).

        Spawns all instances (including the first on the default port).
        If an instance is already running on a port, it is reused.
        """
        urls: list[str] = []

        for i in range(self.num_instances):
            port = self.base_port + i
            url = f"http://127.0.0.1:{port}"

            # Reuse if already running on this port
            if await self._is_alive(url):
                logger.info("Ollama already running on port %d — reusing", port)
                urls.append(url)
                continue

            env = {
                **os.environ,
                "OLLAMA_HOST": f"127.0.0.1:{port}",
                "OLLAMA_FLASH_ATTENTION": os.environ.get("OLLAMA_FLASH_ATTENTION", "1"),
            }
            proc = subprocess.Popen(
                ["ollama", "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._processes.append(proc)
            logger.info("Spawned Ollama on port %d (pid %d)", port, proc.pid)
            urls.append(url)

        # Wait for all instances to be ready
        for url in urls:
            ready = await self._wait_ready(url, timeout=30)
            if not ready:
                logger.warning("Ollama instance at %s failed to start", url)

        # Preload model on all instances so first agent request is fast
        if self.model:
            await self._preload_all(urls)

        logger.info(
            "Ollama pool ready: %d instances on ports %d-%d",
            len(urls), self.base_port, self.base_port + len(urls) - 1,
        )
        return urls

    def stop(self) -> None:
        """Terminate all spawned Ollama instances (not pre-existing ones)."""
        for proc in self._processes:
            if proc.poll() is None:
                logger.info("Stopping Ollama pid %d", proc.pid)
                proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 5
        for proc in self._processes:
            remaining = max(0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._processes.clear()

    async def _is_alive(self, url: str) -> bool:
        """Check if an Ollama instance is responding."""
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False

    async def _wait_ready(self, url: str, timeout: float = 30) -> bool:
        """Poll until an Ollama instance is ready."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._is_alive(url):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _preload_all(self, urls: list[str]) -> None:
        """Send a lightweight request to each instance to preload the model."""
        async def _preload(url: str) -> None:
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    await client.post(
                        f"{url}/api/generate",
                        json={"model": self.model, "prompt": "hi", "options": {"num_predict": 1}},
                    )
                    logger.info("Preloaded %s on %s", self.model, url)
            except Exception as exc:
                logger.warning("Failed to preload model on %s: %s", url, exc)

        await asyncio.gather(*[_preload(url) for url in urls])
