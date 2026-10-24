#!/usr/bin/env python3
"""Minimal browser CLI — screenshot, text, links, headers.

Usage:
    python tools/browse.py screenshot <url> [-o path.png]
    python tools/browse.py text <url>
    python tools/browse.py links <url>
    python tools/browse.py headers <url>
"""
import argparse, asyncio, json, sys, tempfile
from playwright.async_api import async_playwright


async def run(cmd, url, output=None, **_):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0",
        )).new_page()

        hdrs, status = {}, None
        async def on_resp(r):
            nonlocal hdrs, status
            if r.url.rstrip("/") == url.rstrip("/"):
                hdrs, status = await r.all_headers(), r.status
        page.on("response", on_resp)

        try:
            r = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if r and not status: status, hdrs = r.status, await r.all_headers()
        except Exception as e:
            print(json.dumps({"error": str(e)})); await browser.close(); return

        await page.wait_for_timeout(2000)

        if cmd == "screenshot":
            out = output or tempfile.mktemp(suffix=".png", prefix="browse_")
            await page.screenshot(path=out, full_page=False)
            print(json.dumps({"screenshot": out, "url": page.url, "title": await page.title(), "status": status}))
        elif cmd == "text":
            text = await page.inner_text("body")
            if len(text) > 10000: text = text[:10000] + "\n...[truncated]"
            print(json.dumps({"url": page.url, "title": await page.title(), "status": status, "text": text}))
        elif cmd == "links":
            links = await page.eval_on_selector_all("a[href]", "els => els.map(e => ({href: e.href, text: e.textContent.trim().slice(0,80)})).slice(0,100)")
            print(json.dumps({"url": page.url, "status": status, "links": links}))
        elif cmd == "headers":
            print(json.dumps({"url": page.url, "status": status, "headers": hdrs}))

        await browser.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["screenshot", "text", "links", "headers"])
    ap.add_argument("url")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    asyncio.run(run(**vars(a)))
