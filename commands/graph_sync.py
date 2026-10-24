from commands.base import register


@register("graph-sync", "Rebuild FalkorDB graph from SQLite data")
async def run(args, orchestrator) -> None:
    from config import get_settings
    from memory.graph import GraphMemory
    from memory.store import PersistentMemory

    settings = get_settings()
    graph = GraphMemory(host=settings.falkordb_host, port=settings.falkordb_port)
    if not await graph.ensure_container():
        print("Could not start FalkorDB container. Is Docker running?")
        return
    if not await graph.connect():
        print("Could not connect to FalkorDB.")
        return

    await graph.init_indices()
    store = PersistentMemory()
    try:
        counts = await graph.sync_from_sqlite(store)
        print(f"Graph sync complete:")
        print(f"  Programs: {counts.get('programs', 0)}")
        print(f"  Scans:    {counts.get('scans', 0)}")
        print(f"  Findings: {counts.get('findings', 0)}")
    finally:
        store.close()
        await graph.close()
