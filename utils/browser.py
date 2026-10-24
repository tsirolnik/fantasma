"""Persistent headless browser session for agent use.

Wraps Playwright to give agents the ability to navigate JS-rendered pages,
interact with dynamic content, and extract data that plain HTTP requests miss.

Each agent gets its own BrowserContext (isolated cookies, storage, history)
from a shared Browser instance managed by BrowserPool.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-imported at runtime so the module can be imported even if playwright
# is not installed (graceful degradation).
_playwright_module = None


def _get_playwright():
    global _playwright_module
    if _playwright_module is None:
        try:
            from playwright import async_api as pw
            _playwright_module = pw
        except ImportError:
            raise ImportError(
                "playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
    return _playwright_module


class BrowserSession:
    """A single browser context — isolated cookies, storage, and history.

    Created by BrowserPool.create_session(). Each agent gets its own session
    so logins and navigation don't interfere across agents.
    """

    def __init__(self, context, owner: str = "") -> None:
        self._context = context
        self._page = None
        self.owner = owner

    async def _ensure_page(self):
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout: int = 30_000,
    ) -> dict:
        """Navigate to a URL. Returns {url, title, status}."""
        page = await self._ensure_page()
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout)
            status = response.status if response else None
        except Exception as exc:
            logger.warning("browser navigate to %s failed: %s", url, exc)
            return {"url": url, "title": "", "status": None, "error": str(exc)}
        return {
            "url": page.url,
            "title": await page.title(),
            "status": status,
        }

    async def back(self) -> dict:
        page = await self._ensure_page()
        await page.go_back()
        return {"url": page.url, "title": await page.title()}

    async def reload(self) -> dict:
        page = await self._ensure_page()
        await page.reload()
        return {"url": page.url, "title": await page.title()}

    # ------------------------------------------------------------------
    # Content extraction
    # ------------------------------------------------------------------

    async def get_text(self, selector: str | None = None) -> str:
        """Get visible text of the page or a specific element."""
        page = await self._ensure_page()
        if selector:
            try:
                el = await page.wait_for_selector(selector, timeout=5000)
                return await el.inner_text() if el else ""
            except Exception:
                return ""
        return await page.inner_text("body")

    async def get_links(self) -> list[dict]:
        """Extract all links as [{text, href}, ...]."""
        page = await self._ensure_page()
        return await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                text: (a.textContent || '').trim().slice(0, 200),
                href: a.href,
            }))
        """)

    async def get_html(self, selector: str | None = None) -> str:
        """Get HTML content of the page or a specific element."""
        page = await self._ensure_page()
        if selector:
            try:
                el = await page.wait_for_selector(selector, timeout=5000)
                return await el.inner_html() if el else ""
            except Exception:
                return ""
        return await page.content()

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    async def click(self, selector: str) -> dict:
        """Click an element. selector can be CSS or 'text=...' for text content."""
        page = await self._ensure_page()
        try:
            await page.click(selector, timeout=10_000)
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception as exc:
            return {"success": False, "error": str(exc), "url": page.url}
        return {"success": True, "url": page.url}

    async def type_text(self, selector: str, text: str) -> dict:
        """Type text into an input field."""
        page = await self._ensure_page()
        try:
            await page.fill(selector, text, timeout=10_000)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True}

    async def select(self, selector: str, value: str) -> dict:
        """Select an option in a <select> element."""
        page = await self._ensure_page()
        try:
            await page.select_option(selector, value, timeout=10_000)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True}

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def screenshot(self) -> str:
        """Take a PNG screenshot, return as base64 string."""
        page = await self._ensure_page()
        buf = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(buf).decode("ascii")

    async def screenshot_jpeg(self, quality: int = 60) -> str:
        """Take a JPEG screenshot, return as base64 string."""
        page = await self._ensure_page()
        buf = await page.screenshot(type="jpeg", quality=quality, full_page=False)
        return base64.b64encode(buf).decode("ascii")

    async def get_title(self) -> str:
        """Get current page title (async)."""
        if self._page and not self._page.is_closed():
            try:
                return await self._page.title()
            except Exception:
                return ""
        return ""

    async def wait_for(self, selector: str, timeout: int = 5000) -> bool:
        """Wait for an element to appear. Returns True if found."""
        page = await self._ensure_page()
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            return False

    async def evaluate(self, js: str) -> Any:
        """Run arbitrary JavaScript in the page context and return the result."""
        page = await self._ensure_page()
        return await page.evaluate(js)

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def current_url(self) -> str:
        if self._page and not self._page.is_closed():
            return self._page.url
        return ""

    @property
    def page_title(self) -> str:
        # title() is async; provide sync access to last known URL instead
        if self._page and not self._page.is_closed():
            return self._page.url  # best we can do synchronously
        return ""

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close this session's browser context."""
        try:
            await self._context.close()
        except Exception:
            pass


class BrowserPool:
    """Manages a shared Playwright Browser with per-agent isolated sessions.

    Usage:
        pool = BrowserPool()
        await pool.start()
        session = await pool.create_session("scanner")
        await session.navigate("https://example.com")
        ...
        await pool.stop()
    """

    def __init__(self) -> None:
        self._pw = None       # Playwright instance
        self._browser = None  # Browser instance
        self._sessions: dict[str, BrowserSession] = {}

    async def start(
        self,
        headless: bool = True,
        cdp_url: str | None = None,
    ) -> None:
        """Launch (or connect to) a browser."""
        pw = _get_playwright()
        self._pw = await pw.async_playwright().start()

        if cdp_url:
            # Connect to an external browser (e.g. Lightpanda, remote Chrome)
            self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
            logger.info("Connected to external browser at %s", cdp_url)
        else:
            self._browser = await self._pw.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            logger.info("Launched headless Chromium (pid=%s)", self._browser.contexts)

    async def create_session(self, owner: str = "") -> BrowserSession:
        """Create an isolated browser context for an agent.

        Each session has its own cookies, localStorage, and history.
        If a session already exists for this owner, returns the existing one.
        """
        if owner in self._sessions:
            return self._sessions[owner]

        if self._browser is None:
            raise RuntimeError("BrowserPool not started. Call start() first.")

        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        session = BrowserSession(context, owner=owner)
        if owner:
            self._sessions[owner] = session
        logger.info("Created browser session for '%s'", owner or "anonymous")
        return session

    async def get_session(self, owner: str) -> BrowserSession | None:
        """Get an existing session by owner name."""
        return self._sessions.get(owner)

    async def close_session(self, owner: str) -> None:
        """Close and remove a specific agent's session."""
        session = self._sessions.pop(owner, None)
        if session:
            await session.close()

    async def stop(self) -> None:
        """Close all sessions and the browser."""
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

        logger.info("BrowserPool stopped")
