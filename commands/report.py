from commands.base import Arg, register


@register("report", "Generate reports from findings JSON", args=[
    Arg(("--findings",), dict(required=True, help="Path to findings JSON file")),
])
async def run(args, orchestrator) -> None:
    await orchestrator.report_from_findings(args.findings)
    print(f"Reports saved to: {orchestrator.settings.output_dir}")
