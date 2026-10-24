from commands.base import Arg, register


@register("fuzz", "Fuzz a binary target (experimental)", args=[
    Arg(("--binary",), dict(required=True, help="Path to target binary")),
    Arg(("--corpus",), dict(required=True, help="Path to seed corpus directory")),
    Arg(("--timeout",), dict(type=int, default=3600, help="Fuzzing timeout in seconds")),
    Arg(("--output",), dict(default="./fuzz_output", help="Fuzzing output directory")),
])
async def run(args, orchestrator) -> None:
    print("[EXPERIMENTAL] Starting binary fuzzing pipeline...")
    findings = await orchestrator.fuzz_binary(
        binary_path=args.binary,
        corpus_dir=args.corpus,
        output_dir=args.output,
        timeout=args.timeout,
    )
    print(f"\nFuzzing complete: {len(findings)} unique crashes analyzed")
