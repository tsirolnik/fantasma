"""Crash deduplication and classification."""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from fuzzing.fuzzer import CrashInfo

logger = logging.getLogger(__name__)


class CrashType(str, Enum):
    HEAP_BUFFER_OVERFLOW = "heap-buffer-overflow"
    STACK_BUFFER_OVERFLOW = "stack-buffer-overflow"
    USE_AFTER_FREE = "use-after-free"
    DOUBLE_FREE = "double-free"
    NULL_DEREF = "null-dereference"
    INTEGER_OVERFLOW = "integer-overflow"
    SEGFAULT = "segfault"
    UNKNOWN = "unknown"


@dataclass
class TriagedCrash:
    original: CrashInfo
    crash_type: CrashType = CrashType.UNKNOWN
    stack_hash: str = ""
    stack_frames: list[str] = field(default_factory=list)
    asan_report: str = ""
    deduplicated: bool = False
    exploitability: str = "unknown"  # high, medium, low, unknown


ASAN_TYPE_MAP = {
    "heap-buffer-overflow": CrashType.HEAP_BUFFER_OVERFLOW,
    "stack-buffer-overflow": CrashType.STACK_BUFFER_OVERFLOW,
    "heap-use-after-free": CrashType.USE_AFTER_FREE,
    "double-free": CrashType.DOUBLE_FREE,
    "null-dereference": CrashType.NULL_DEREF,
    "SEGV": CrashType.SEGFAULT,
}


class CrashTriage:
    """Parses ASAN output, deduplicates crashes, classifies crash types."""

    def __init__(self):
        self.seen_hashes: set[str] = set()

    def triage(self, crashes: list[CrashInfo]) -> list[TriagedCrash]:
        """Triage and deduplicate a list of crashes."""
        triaged = []
        for crash in crashes:
            result = self._analyze_crash(crash)
            if result.stack_hash in self.seen_hashes:
                result.deduplicated = True
            else:
                self.seen_hashes.add(result.stack_hash)
            triaged.append(result)

        unique = [t for t in triaged if not t.deduplicated]
        logger.info(f"Triaged {len(crashes)} crashes -> {len(unique)} unique")
        return unique

    def _analyze_crash(self, crash: CrashInfo) -> TriagedCrash:
        """Analyze a single crash."""
        result = TriagedCrash(original=crash, asan_report=crash.stderr)

        # Parse ASAN output
        result.crash_type = self._classify_type(crash.stderr)
        result.stack_frames = self._extract_stack(crash.stderr)
        result.stack_hash = self._compute_stack_hash(result.stack_frames)
        result.exploitability = self._estimate_exploitability(result.crash_type)

        return result

    def _classify_type(self, stderr: str) -> CrashType:
        """Classify crash type from ASAN output."""
        for pattern, crash_type in ASAN_TYPE_MAP.items():
            if pattern in stderr:
                return crash_type

        if "SIGSEGV" in stderr or "signal 11" in stderr:
            return CrashType.SEGFAULT

        return CrashType.UNKNOWN

    def _extract_stack(self, stderr: str) -> list[str]:
        """Extract stack frames from ASAN output."""
        frames = []
        # Match ASAN stack frame format: #0 0xaddr in func file:line
        for match in re.finditer(r"#\d+\s+0x[a-f0-9]+\s+in\s+(\S+)\s+(\S+)", stderr):
            func_name = match.group(1)
            location = match.group(2)
            frames.append(f"{func_name} @ {location}")

        # Fallback: simpler stack format
        if not frames:
            for match in re.finditer(r"#\d+\s+(.+)", stderr):
                frames.append(match.group(1).strip())

        return frames[:10]  # Top 10 frames

    def _compute_stack_hash(self, frames: list[str]) -> str:
        """Compute a hash of the top stack frames for dedup."""
        top_frames = frames[:3]  # Hash top 3 frames
        content = "|".join(top_frames)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _estimate_exploitability(self, crash_type: CrashType) -> str:
        """Rough exploitability estimate based on crash type."""
        high = {CrashType.USE_AFTER_FREE, CrashType.HEAP_BUFFER_OVERFLOW, CrashType.DOUBLE_FREE}
        medium = {CrashType.STACK_BUFFER_OVERFLOW, CrashType.INTEGER_OVERFLOW}

        if crash_type in high:
            return "high"
        elif crash_type in medium:
            return "medium"
        return "low"
