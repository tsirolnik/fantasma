"""Build the Docker image that container tools run in, from the bundled Dockerfile."""
import subprocess
from pathlib import Path

from fantasma.commands.base import Arg, register

DOCKERFILE = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.tools"
DEFAULT_IMAGE = "bugbounty-tools:latest"


def build_command(tag: str) -> list[str]:
    # The Dockerfile copies nothing from the build context, so it is fed on stdin.
    return ["docker", "build", "-t", tag, "-"]


@register("tools", "Build the Docker image used by container tools", args=[
    Arg(("action",), dict(choices=["build", "dockerfile"],
                          help="build: run docker build; dockerfile: print the bundled Dockerfile path")),
    Arg(("--tag",), dict(default=DEFAULT_IMAGE, help=f"Image tag (default: {DEFAULT_IMAGE})")),
])
async def run(args, orchestrator):
    if args.action == "dockerfile":
        print(DOCKERFILE)
        return
    with DOCKERFILE.open("rb") as source:
        result = subprocess.run(build_command(args.tag), stdin=source)
    if result.returncode:
        raise SystemExit(result.returncode)
