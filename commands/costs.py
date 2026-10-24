from commands.base import register


@register("costs", "Show session cost breakdown")
async def run(args, orchestrator) -> None:
    print(orchestrator.show_costs())
    from memory.store import PersistentMemory
    store = PersistentMemory()
    stats = store.get_cost_stats()
    history = store.get_cost_history(limit=15)
    store.close()
    print(f"\n=== All-Time Costs ===")
    print(f"Total spent:  ${stats['total_cost']:.2f} across {stats['total_scans']} scans")
    print(f"Avg per scan: ${stats['avg_cost']:.2f}")
    print(f"Max scan:     ${stats['max_cost']:.2f}")
    print(f"Total findings: {stats['total_findings']}")
    if history:
        print(f"\nRecent scans:")
        for h in history:
            date = h["started_at"].split("T")[0] if h.get("started_at") else "?"
            target = h["target"][:35]
            print(f"  #{h['id']:>3} | {date} | {target:<35} | ${h['cost']:.2f} | {h['findings_count']} findings")
