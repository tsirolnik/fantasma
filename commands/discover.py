from commands.base import Arg, register


@register("discover", "Find high-ROI bug bounty programs", args=[
    Arg(("--top",), dict(type=int, default=10, help="Number of top programs to show")),
    Arg(("--min-bounty",), dict(type=float, default=50.0, help="Minimum bounty threshold in USD")),
])
async def run(args, orchestrator) -> None:
    from agents.program_finder import ProgramFinder
    from cost.tracker import CostTracker
    from memory.store import PersistentMemory

    store = PersistentMemory()
    finder = ProgramFinder(CostTracker(), min_bounty=args.min_bounty, persistent_store=store)
    programs = await finder.discover(top_n=args.top)
    if programs:
        print(f"\nTop {len(programs)} Programs by ROI:\n")
        from cost.roi_model import ROIModel
        roi = ROIModel()
        for i, p in enumerate(programs, 1):
            summary = roi.format_roi_summary(p)
            print(f"  {i}. {p.name} ({p.platform})")
            print(f"     Bounty: ${p.min_bounty}-${p.max_bounty}")
            print(f"     Scope: {len(p.all_domains)} domains")
            print(f"     {summary}")
            print()
    else:
        print("No programs found matching criteria.")
