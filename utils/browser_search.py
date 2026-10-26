"""Bounded browser search; no API credentials or challenge bypasses."""
import asyncio
from contextlib import suppress
from urllib.parse import urlencode, urlsplit, parse_qs


async def search(context, query, max_results=5):
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        return {'error': 'query must be a nonempty string of at most 500 characters', 'status': 'invalid_query'}
    if type(max_results) is not int or not 1 <= max_results <= 10:
        return {'error': 'max_results must be an integer from 1 to 10', 'status': 'invalid_limit'}
    query = query.strip()
    page = None
    try:
        async with asyncio.timeout(25):
            # A temporary tab preserves the agent's current page and navigation.
            page = await context.new_page()
            response = await page.goto('https://html.duckduckgo.com/html/?' + urlencode({'q': query}),
                                       wait_until='domcontentloaded', timeout=20000)
            status = response.status if response else None
            data = await page.evaluate('''() => {
                const blocked = !!document.querySelector('#challenge-form, form[action*="anomaly"], iframe[src*="captcha"]');
                const body = document.body?.innerText || '';
                return {
                    blocked: blocked || /unusual traffic|verify you are human|confirm you are human|bots use duckduckgo/i.test(body),
                    no_results: !!document.querySelector('.no-results, .no-results__message'),
                    results: Array.from(document.querySelectorAll('.result')).slice(0, 30).map(row => {
                        const link = row.querySelector('.result__a');
                        return {title: (link?.innerText || '').slice(0, 300),
                                url: (link?.href || '').slice(0, 4096),
                                snippet: (row.querySelector('.result__snippet')?.innerText || '').slice(0, 700)};
                    })
                };
            }''')
            if data['blocked'] or status in (202, 403, 429):
                return {'query': query, 'status': 'blocked', 'error': 'Search engine presented a challenge or rate limit. No retry or bypass attempted.'}
            if status is None or status >= 400:
                return {'query': query, 'status': 'failed', 'error': f'Search page returned HTTP {status}'}
            results, seen = [], set()
            for row in data['results']:
                url = row['url']
                parsed = urlsplit(url)
                if parsed.hostname in ('duckduckgo.com', 'html.duckduckgo.com'):
                    url = parse_qs(parsed.query).get('uddg', [''])[0]
                parsed = urlsplit(url)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or url in seen or not row['title'].strip():
                    continue
                seen.add(url)
                results.append({'title': row['title'].strip(), 'url': url, 'snippet': row['snippet'].strip()})
                if len(results) == max_results:
                    break
            if results:
                return {'query': query, 'status': 'ok', 'results': results}
            if data['no_results']:
                return {'query': query, 'status': 'no_results', 'results': []}
            return {'query': query, 'status': 'unrecognized_page', 'error': 'Search layout was not recognized. This does not establish that there are no results.'}
    except Exception as exc:
        return {'query': query, 'status': 'failed', 'error': f'Browser search failed ({type(exc).__name__}). Check browser installation and network access.'}
    finally:
        if page is not None:
            with suppress(Exception):
                await page.close()
