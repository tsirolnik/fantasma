"""Browser smoke checks for both interfaces, with local fixtures and no model calls.

From the repository root: python scripts/check_ui.py
Requires: python -m playwright install chromium
Screenshots are written to the OS temporary directory for visual inspection.
"""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio,importlib.util
from pathlib import Path
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from profiles.loader import load_profile
from agents.interactive import InteractiveSession
from web.interactive import InteractiveWebUI
spec=importlib.util.spec_from_file_location('preview',Path(__file__).with_name('capture_dashboard.py'));demo=importlib.util.module_from_spec(spec);spec.loader.exec_module(demo)

async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch()
  app=web.Application();app.router.add_get('/',demo.index);app.router.add_get('/ws',demo.socket);app.router.add_get('/api/{name}',demo.api)
  async with TestServer(app) as server:
   page=await browser.new_page(viewport={'width':1440,'height':1000})
   errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   await page.goto(str(server.make_url('/')))
   await page.locator('#agent-pentester-3').filter(has_text='thinking').wait_for()
   await page.locator('#eventFilter').fill('authentication')
   assert await page.locator('#stream .event:visible').count()==2
   await page.locator('#eventFilter').fill('nonexistent-message')
   assert await page.locator('#stream .event:visible').count()==0
   await page.locator('#eventFilter').fill('')
   assert await page.locator('#stream .event:visible').count()==8
   await page.get_by_role('button',name='Costs',exact=True).click()
   assert await page.locator('#tab-costs').is_visible()
   await page.get_by_role('button',name='Agents',exact=True).click()
   await page.set_viewport_size({'width':390,'height':844})
   assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Dashboard overflow'
   await page.screenshot(path=str(Path(tempfile.gettempdir())/'fantasma-dashboard-mobile.png'),full_page=True)
   assert not errors,errors
   await page.close()
  profile=load_profile('interactive')
  session=InteractiveSession(profile,profile.llm,SimpleNamespace(budget=1,dry_run=True),[],write=lambda text:None)
  ui=InteractiveWebUI(session);session.write=ui.write
  async with TestServer(ui.app) as server:
   page=await browser.new_page(viewport={'width':1440,'height':1000},extra_http_headers={'Authorization':'Bearer '+ui.app['dashboard_token']})
   errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   await page.goto(str(server.make_url('/')))
   await page.locator('#state').filter(has_text='Ready').wait_for()
   await page.locator('#scopeShortcut').click()
   assert await page.locator('#text').input_value()=='/scope '
   await page.locator('#text').fill('/scope example.test')
   await page.locator('#text').press('Control+Enter')
   await page.locator('#scope').filter(has_text='example.test').wait_for()
   await page.locator('#statusShortcut').click()
   await page.locator('#stop').click()
   await page.locator('#messages').filter(has_text='Stopped. Ready').wait_for()
   await page.screenshot(path=str(Path(tempfile.gettempdir())/'fantasma-interactive-desktop.png'),full_page=True)
   await page.set_viewport_size({'width':390,'height':844})
   assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Interactive overflow'
   await page.screenshot(path=str(Path(tempfile.gettempdir())/'fantasma-interactive-mobile.png'),full_page=True)
   assert not errors,errors
   assert session.agent is None
  await browser.close()
 print('Desktop/mobile layout, activity filtering, tabs, keyboard submission, scope/status/stop controls passed. No model calls.')
asyncio.run(main())
