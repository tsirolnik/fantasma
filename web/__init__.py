"""Web dashboard for real-time pipeline monitoring and god mode control."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.pipeline import AgentPipeline

logger = logging.getLogger(__name__)


class WebUI:
    """Entry point for the web dashboard.

    Wraps the aiohttp server and the pipeline bridge so the caller
    only needs ``await ui.start()`` to get everything running.
    """

    def __init__(self, pipeline: AgentPipeline) -> None:
        from web.bridge import PipelineBridge
        from web.server import create_app

        self.pipeline = pipeline
        self.bridge = PipelineBridge(pipeline)
        self.app = create_app(self.bridge)
        self._runner: object | None = None

    async def start(self) -> None:
        """Start the HTTP/WebSocket server (non-blocking)."""
        from aiohttp import web

        settings = self.pipeline.settings
        host = settings.web_host
        port = settings.web_port

        self.bridge.start()

        runner = web.AppRunner(self.app)
        await runner.setup()
        self._runner = runner

        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info("Dashboard running at http://%s:%d", host, port)

    async def shutdown(self) -> None:
        """Gracefully stop the server and bridge."""
        await self.bridge.stop()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Dashboard shut down")
