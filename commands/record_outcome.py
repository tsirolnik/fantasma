from commands.base import Arg, register


@register("record-outcome", "Record a finding submission outcome", args=[
    Arg(("--finding-id",), dict(type=int, required=True, help="Finding ID from database")),
    Arg(("--accepted",), dict(action="store_true", help="Was the finding accepted?")),
    Arg(("--bounty",), dict(type=float, default=0.0, help="Actual bounty received in USD")),
])
async def run(args, orchestrator) -> None:
    orchestrator.learning.record_outcome(
        finding_id=args.finding_id,
        accepted=args.accepted,
        bounty=args.bounty,
    )
    print(f"Outcome recorded for finding #{args.finding_id}")

    from config import get_settings
    settings = get_settings()
    if settings.falkordb_enabled:
        from memory.graph import GraphMemory
        graph = GraphMemory(host=settings.falkordb_host, port=settings.falkordb_port)
        if await graph.ensure_container() and await graph.connect():
            await graph.record_outcome(args.finding_id, args.accepted, args.bounty)
            await graph.close()
            print("Graph memory updated")
