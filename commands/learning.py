from commands.base import register


@register("learning", "Show learning memory insights")
async def run(args, orchestrator) -> None:
    print(orchestrator.show_learning())
