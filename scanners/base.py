"""Abstract scanner interface."""

from abc import ABC, abstractmethod
from models.finding import VulnFinding


class ScannerBase(ABC):
    """Base class for vulnerability scanners."""

    scanner_name: str = ""

    @abstractmethod
    async def scan(self, target: str, recon_data: dict | None = None) -> list[VulnFinding]:
        """Scan a target and return findings."""
        ...
