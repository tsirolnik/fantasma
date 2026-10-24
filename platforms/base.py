"""Abstract platform interface."""

from abc import ABC, abstractmethod

from models.program import BountyProgram


class PlatformBase(ABC):
    """Base class for bug bounty platform integrations."""

    platform_name: str = ""

    @abstractmethod
    async def fetch_programs(self) -> list[BountyProgram]:
        """Fetch available bounty programs from this platform."""
        ...
