from commands.base import Arg, register


@register("history", "Show scan history for a target", args=[
    Arg(("--target",), dict(default=None, help="Target to show history for")),
])
async def run(args, orchestrator) -> None:
    if args.target:
        history = orchestrator.learning.get_target_history(args.target)
        print(f"\nHistory for {args.target}:")
        print(f"  Scans: {history['scan_count']}")
        print(f"  Findings: {history['total_findings']}")
        print(f"  Accepted: {history['accepted_findings']}")
        print(f"  Bounty earned: ${history['total_bounty_earned']:.2f}")
        print(f"  Total cost: ${history['total_cost']:.2f}")
        print(f"  Net ROI: ${history['net_roi']:.2f}")
        print(f"  Last scanned: {history['last_scanned'] or 'never'}")
        if history['finding_types']:
            print(f"  Finding types: {', '.join(history['finding_types'])}")
    else:
        scans = orchestrator.persistent.get_past_scans(limit=20)
        if scans:
            print("\nRecent scans:")
            for s in scans:
                print(f"  {s['started_at'][:16]} | {s['target'][:40]} | "
                      f"{s['findings_count']} findings | ${s['cost']:.2f}")
        else:
            print("No scan history yet.")
