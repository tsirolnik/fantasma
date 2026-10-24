"""aiohttp server — REST endpoints + WebSocket handler for the dashboard."""

from __future__ import annotations

import json
import logging
import secrets
from urllib.parse import urlsplit

from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web, WSMsgType

from config import get_settings

if TYPE_CHECKING:
    from web.bridge import PipelineBridge

logger = logging.getLogger(__name__)

FRONTEND_PATH = Path(__file__).parent / "frontend.html"


@web.middleware
async def dashboard_auth(request: web.Request, handler):
    # Reject DNS-rebinding hostnames even on the unauthenticated login page.
    host = urlsplit("//" + request.host).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "::1", get_settings().web_host}
    if host not in allowed_hosts:
        raise web.HTTPForbidden(text="Unexpected dashboard host")
    origin = request.headers.get("Origin")
    if origin is not None and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Cross-origin dashboard request rejected")
    if request.path == "/ws" and not origin:
        raise web.HTTPForbidden(text="WebSocket Origin required")

    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    token = token or request.cookies.get("swarmhound_session", "")
    authenticated = secrets.compare_digest(token.encode(), request.app["dashboard_token"].encode())
    if request.path == "/login" and request.method == "POST":
        form = await request.post()
        supplied = str(form.get("token", ""))
        if not secrets.compare_digest(supplied.encode(), request.app["dashboard_token"].encode()):
            raise web.HTTPUnauthorized(text="Invalid dashboard token")
        response = web.Response(status=303, headers={"Location": "/"})
        response.set_cookie("swarmhound_session", supplied, httponly=True,
                            samesite="Strict", secure=request.secure, path="/")
    elif request.path == "/" and request.method == "GET" and not authenticated:
        response = web.Response(content_type="text/html", text='<!doctype html><html lang="en"><meta charset="utf-8"><title>swarmhound login</title>\n<h1>swarmhound dashboard</h1><p>Enter the access token printed in the server terminal.</p>\n<form method="post" action="/login"><label>Access token <input name="token" type="password" required autocomplete="off"></label><button>Sign in</button></form></html>')
    elif not authenticated:
        raise web.HTTPUnauthorized(text="Dashboard authentication required")
    else:
        response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def create_app(bridge: PipelineBridge) -> web.Application:
    app = web.Application(middlewares=[dashboard_auth])
    app["dashboard_token"] = get_settings().web_auth_token or secrets.token_urlsafe(32)
    print(f"Dashboard access token: {app['dashboard_token']}", flush=True)
    app["bridge"] = bridge

    app.router.add_get("/", handle_index)
    app.router.add_post("/login", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/profile", handle_profile)
    app.router.add_get("/api/messages", handle_messages)
    app.router.add_get("/api/costs", handle_costs)
    app.router.add_get("/api/findings", handle_findings)
    app.router.add_get("/api/memory", handle_memory)
    app.router.add_get("/api/graph", handle_graph)
    app.router.add_get("/api/history/findings", handle_history_findings)
    app.router.add_get("/api/history/scans", handle_history_scans)
    app.router.add_get("/api/graph/overview", handle_graph_overview)
    app.router.add_get("/api/graph/domain", handle_graph_domain)
    app.router.add_get("/api/graph/techniques", handle_graph_techniques)
    app.router.add_get("/api/llm-wire", handle_llm_wire)
    app.router.add_get("/ws", handle_websocket)
    app.router.add_post("/api/ingest", handle_ingest)

    return app


# --------------------------------------------------------------------------
# REST endpoints
# --------------------------------------------------------------------------

async def handle_index(request: web.Request) -> web.Response:
    if FRONTEND_PATH.exists():
        return web.Response(
            text=FRONTEND_PATH.read_text(encoding="utf-8"),
            content_type="text/html",
        )
    return web.Response(text="frontend.html not found", status=404)


async def handle_profile(request: web.Request) -> web.Response:
    return web.json_response(request.app["bridge"].get_profile())


async def handle_status(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    return web.json_response(bridge.get_status())


async def handle_messages(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    pipeline = bridge.pipeline
    messages = []
    for msg in pipeline.bus.get_log():
        messages.append({
            "id": msg.id,
            "sender": msg.sender,
            "recipient": msg.recipient,
            "msg_type": msg.msg_type,
            "content": msg.content,
            "priority": msg.priority,
            "timestamp": msg.timestamp.isoformat(),
            "data": msg.data,
        })
    return web.json_response(messages)


async def handle_costs(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    ct = bridge.pipeline.cost_tracker
    store = bridge.pipeline.persistent

    # Current session
    current = {
        "total": ct.get_total(),
        "breakdown": ct.get_breakdown(),
        "per_target": ct.get_per_target_costs(),
        "budget": getattr(bridge.pipeline, "effective_budget", None) or bridge.pipeline.settings.session_budget,
    }

    # Historical (all sessions)
    current["history"] = {
        "stats": store.get_cost_stats(),
        "recent_scans": store.get_cost_history(limit=20),
    }

    return web.json_response(current)


async def handle_findings(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    session = bridge.pipeline.session

    def _finding_dict(f):
        try:
            return f.model_dump(mode="json")
        except Exception:
            return str(f)

    raw = [_finding_dict(f) for f in session.raw_findings]
    analyzed = [_finding_dict(f) for f in session.analyzed_findings]

    # In swarm mode, also include live findings from SharedMemory
    shared_memory = getattr(bridge.pipeline, "shared_memory", None)
    if shared_memory and not analyzed:
        live = shared_memory.get_findings()
        analyzed = [_finding_dict(f) for f in live]
        if not raw:
            raw = analyzed

    return web.json_response({
        "raw": raw,
        "analyzed": analyzed,
    })


async def handle_memory(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    pipeline = bridge.pipeline
    learning_summary = pipeline.learning.summary()

    scans = pipeline.persistent.get_past_scans(limit=20)
    scan_list = []
    for s in scans:
        scan_list.append({
            "target": s.get("target", ""),
            "started_at": s.get("started_at", ""),
            "findings_count": s.get("findings_count", 0),
            "cost": s.get("cost", 0.0),
        })

    best_types = pipeline.learning.get_best_vuln_types(top_n=5)

    return web.json_response({
        "learning_summary": learning_summary,
        "recent_scans": scan_list,
        "best_vuln_types": best_types,
    })


async def handle_graph(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    graph = getattr(bridge.pipeline, "graph", None)
    if not graph or not graph.available:
        return web.json_response({"connected": False, "op_counts": {}, "total_ops": 0})

    return web.json_response({
        "connected": True,
        "total_ops": graph.total_ops,
        "op_counts": graph.op_counts,
    })


# --------------------------------------------------------------------------
# History endpoints — browse past findings and scans from SQLite
# --------------------------------------------------------------------------

async def handle_history_findings(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    store = bridge.pipeline.persistent

    target = request.query.get("target") or None
    vuln_type = request.query.get("vuln_type") or None
    limit = min(int(request.query.get("limit", "100")), 500)

    findings = store.get_past_findings(target=target, vuln_type=vuln_type, limit=limit)

    # Parse JSON data field if present
    for f in findings:
        if isinstance(f.get("data"), str):
            try:
                f["data"] = json.loads(f["data"])
            except (json.JSONDecodeError, TypeError):
                pass

    # Also return programs for filter dropdowns
    programs = store.get_programs()

    return web.json_response({
        "findings": findings,
        "programs": [{"name": p.get("name", ""), "url": p.get("url", "")} for p in programs],
    })


async def handle_history_scans(request: web.Request) -> web.Response:
    bridge: PipelineBridge = request.app["bridge"]
    store = bridge.pipeline.persistent

    target = request.query.get("target") or None
    limit = min(int(request.query.get("limit", "50")), 200)

    scans = store.get_past_scans(target=target, limit=limit)

    return web.json_response({"scans": scans})


# --------------------------------------------------------------------------
# Graph explorer endpoints — browse knowledge graph
# --------------------------------------------------------------------------

async def handle_graph_overview(request: web.Request) -> web.Response:
    """Return all domains with their tech counts and finding counts."""
    bridge: PipelineBridge = request.app["bridge"]
    graph = getattr(bridge.pipeline, "graph", None)

    if graph and graph.available:
        # Use FalkorDB for rich data
        try:
            domains = await graph.query(
                "MATCH (d:Domain) "
                "OPTIONAL MATCH (d)-[:USES_TECH]->(t:Technology) "
                "OPTIONAL MATCH (d)-[:HAS_FINDING]->(f:Finding) "
                "RETURN d.name AS domain, "
                "collect(DISTINCT t.name) AS technologies, "
                "count(DISTINCT f) AS finding_count, "
                "d.last_scanned AS last_scanned "
                "ORDER BY finding_count DESC "
                "LIMIT 200"
            )

            # Stats
            stats_result = await graph.query(
                "MATCH (d:Domain) "
                "OPTIONAL MATCH (d)-[:USES_TECH]->(t:Technology) "
                "OPTIONAL MATCH (d)-[:HAS_FINDING]->(f:Finding) "
                "RETURN count(DISTINCT d) AS total_domains, "
                "count(DISTINCT t) AS total_techs, "
                "count(DISTINCT f) AS total_findings"
            )
            stats = stats_result[0] if stats_result else {
                "total_domains": 0, "total_techs": 0, "total_findings": 0,
            }

            return web.json_response({
                "source": "graph",
                "domains": domains,
                "stats": stats,
            })
        except Exception as exc:
            logger.debug("Graph overview query failed: %s", exc)

    # Fallback: use SQLite data
    store = bridge.pipeline.persistent
    scans = store.get_past_scans(limit=200)
    findings = store.get_past_findings(limit=500)

    # Group by domain
    domain_map: dict[str, dict] = {}
    for s in scans:
        target = s.get("target", "")
        if target not in domain_map:
            domain_map[target] = {
                "domain": target,
                "technologies": [],
                "finding_count": 0,
                "last_scanned": s.get("started_at", ""),
            }
        else:
            existing = domain_map[target].get("last_scanned", "")
            if s.get("started_at", "") > existing:
                domain_map[target]["last_scanned"] = s["started_at"]

    for f in findings:
        target = f.get("target", "")
        if target in domain_map:
            domain_map[target]["finding_count"] += 1
        else:
            domain_map[target] = {
                "domain": target,
                "technologies": [],
                "finding_count": 1,
                "last_scanned": "",
            }

    domains = sorted(domain_map.values(), key=lambda d: d["finding_count"], reverse=True)

    return web.json_response({
        "source": "sqlite",
        "domains": domains,
        "stats": {
            "total_domains": len(domain_map),
            "total_techs": 0,
            "total_findings": len(findings),
        },
    })


async def handle_graph_domain(request: web.Request) -> web.Response:
    """Return full context for a single domain."""
    bridge: PipelineBridge = request.app["bridge"]
    domain = request.query.get("name", "")
    if not domain:
        return web.json_response({"error": "name parameter required"}, status=400)

    graph = getattr(bridge.pipeline, "graph", None)

    if graph and graph.available:
        try:
            context = await graph.get_domain_context(domain)
            if context:
                context["source"] = "graph"
                return web.json_response(context)
        except Exception as exc:
            logger.debug("Graph domain context failed: %s", exc)

    # Fallback: SQLite
    store = bridge.pipeline.persistent
    findings = store.get_past_findings(target=domain, limit=50)
    scans = store.get_past_scans(target=domain, limit=20)

    # Parse data fields
    for f in findings:
        if isinstance(f.get("data"), str):
            try:
                f["data"] = json.loads(f["data"])
            except (json.JSONDecodeError, TypeError):
                pass

    return web.json_response({
        "source": "sqlite",
        "domain": domain,
        "technologies": [],
        "findings": findings,
        "scan_count": len(scans),
        "scans": scans,
        "program": None,
        "endpoints": [],
    })


async def handle_graph_techniques(request: web.Request) -> web.Response:
    """Return effective techniques for a given tech stack."""
    bridge: PipelineBridge = request.app["bridge"]
    techs_str = request.query.get("techs", "")
    if not techs_str:
        return web.json_response({"error": "techs parameter required"}, status=400)

    tech_list = [t.strip() for t in techs_str.split(",") if t.strip()]

    graph = getattr(bridge.pipeline, "graph", None)
    if graph and graph.available:
        try:
            techniques = await graph.get_effective_techniques(tech_list)
            return web.json_response({"techniques": techniques})
        except Exception as exc:
            logger.debug("Graph techniques query failed: %s", exc)

    return web.json_response({"techniques": [], "note": "Graph not available"})


async def handle_llm_wire(request: web.Request) -> web.Response:
    """Return stored LLM input/output exchanges for the wire debug tab."""
    bridge: PipelineBridge = request.app["bridge"]
    agent = request.query.get("agent") or None
    limit = min(int(request.query.get("limit", "50")), 200)
    exchanges = bridge.get_llm_exchanges(agent=agent, limit=limit)
    return web.json_response({"exchanges": exchanges})


# --------------------------------------------------------------------------
# Authenticated external event ingest
# --------------------------------------------------------------------------

async def handle_ingest(request: web.Request) -> web.Response:
    """Accept authenticated external events and broadcast to the dashboard."""
    bridge: PipelineBridge = request.app["bridge"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Wrap in a standard event envelope if not already typed
    if "type" not in data:
        data = {"type": "external_event", **data}
    bridge._broadcast(data)
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------

async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    bridge: PipelineBridge = request.app["bridge"]
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    bridge.add_client(ws)
    logger.info("WebSocket client connected (total: %d)", len(bridge.clients))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    continue
                await bridge.handle_client_message(data, ws)
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        bridge.remove_client(ws)
        logger.info("WebSocket client disconnected (remaining: %d)", len(bridge.clients))

    return ws
