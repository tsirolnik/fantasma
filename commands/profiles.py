"""Offline profile inspection through the installed CLI."""
from commands.base import Arg, register


@register("profiles", "Inspect profiles and list mechanisms without starting a run", args=[
    Arg(("action",), dict(choices=["inspect", "mechanisms"])),
    Arg(("profile",), dict(nargs="?", help="Profile name or TOML path for inspect")),
])
async def run(args, orchestrator):
    from profiles.__main__ import main
    argv = [args.action]
    if args.profile is not None:
        argv.append(args.profile)
    main(argv)
