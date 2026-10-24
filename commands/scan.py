from commands.base import Arg, register


@register("scan", "Scan a specific target", args=[
    Arg(("--target",), dict(required=True, help="Target domain to scan")),
])
async def run(args, orchestrator) -> None:
    findings = await orchestrator.scan_target(args.target)
    print(f"\nScan complete: {len(findings)} findings")
    for f in findings:
        print(f"  [{f.effective_severity.value.upper()}] {f.title}")
    print(f"\nReports saved to: {orchestrator.settings.output_dir}")
