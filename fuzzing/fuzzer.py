"""AFL++/honggfuzz integration for binary fuzzing."""

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FuzzConfig:
    binary_path: str
    corpus_dir: str
    output_dir: str = "./fuzz_output"
    timeout: int = 3600  # seconds
    fuzzer: str = "afl"  # afl or honggfuzz
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CrashInfo:
    crash_file: str
    crash_input: bytes
    signal: int = 0
    stderr: str = ""


class Fuzzer:
    """Interface for AFL++/honggfuzz fuzzing."""

    def __init__(self, config: FuzzConfig):
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self.crashes: list[CrashInfo] = []

    def _find_fuzzer(self) -> str | None:
        """Locate the fuzzer binary."""
        if self.config.fuzzer == "afl":
            for name in ["afl-fuzz", "afl-fuzz++"]:
                if shutil.which(name):
                    return name
        elif self.config.fuzzer == "honggfuzz":
            if shutil.which("honggfuzz"):
                return "honggfuzz"
        return None

    async def run(self) -> list[CrashInfo]:
        """Run the fuzzer and collect crashes."""
        fuzzer_bin = self._find_fuzzer()
        if not fuzzer_bin:
            logger.error(f"Fuzzer '{self.config.fuzzer}' not found in PATH")
            return []

        output_path = Path(self.config.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        if self.config.fuzzer == "afl":
            cmd = [
                fuzzer_bin,
                "-i", self.config.corpus_dir,
                "-o", str(output_path),
                *self.config.extra_args,
                "--", self.config.binary_path,
            ]
        else:
            cmd = [
                fuzzer_bin,
                "-i", self.config.corpus_dir,
                "-o", str(output_path),
                *self.config.extra_args,
                "--", self.config.binary_path,
            ]

        logger.info(f"Starting {self.config.fuzzer}: {' '.join(cmd)}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(
                    self._process.communicate(),
                    timeout=self.config.timeout,
                )
            except asyncio.TimeoutError:
                logger.info("Fuzzing timeout reached, stopping fuzzer")
                await self.stop()
        except FileNotFoundError:
            logger.error(f"Failed to start fuzzer: {cmd[0]} not found")
            return []

        self.crashes = await self.collect_crashes()
        return self.crashes

    async def stop(self) -> None:
        """Stop the fuzzer process."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._process.kill()

    async def collect_crashes(self) -> list[CrashInfo]:
        """Collect crash files from the fuzzer output directory."""
        crashes = []
        crash_dir = Path(self.config.output_dir)

        # AFL stores crashes in output/default/crashes/
        afl_crash_dir = crash_dir / "default" / "crashes"
        # Also check direct output dir
        search_dirs = [afl_crash_dir, crash_dir / "crashes", crash_dir]

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for crash_file in search_dir.iterdir():
                if crash_file.is_file() and crash_file.name.startswith("id:"):
                    try:
                        crash_input = crash_file.read_bytes()
                        crashes.append(CrashInfo(
                            crash_file=str(crash_file),
                            crash_input=crash_input,
                        ))
                    except OSError as e:
                        logger.debug(f"Failed to read crash file {crash_file}: {e}")

        logger.info(f"Collected {len(crashes)} crashes")
        return crashes
