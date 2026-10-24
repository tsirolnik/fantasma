"""Orchestrator — ties the full bug bounty pipeline together."""

import json
import logging
from pathlib import Path

from config import get_settings
from cost.tracker import CostTracker
from llm import get_provider
from agents.recon import ReconAgent
from agents.scanner import ScannerAgent
from agents.analyzer import AnalyzerAgent
from agents.reporter import ReporterAgent
from models.finding import VulnFinding
from fuzzing.fuzzer import Fuzzer, FuzzConfig
from fuzzing.crash_triage import CrashTriage
from fuzzing.binary_analyzer import BinaryAnalyzer
from memory.store import PersistentMemory
from memory.session import SessionMemory
from memory.learning import LearningMemory

logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates the full bug bounty pipeline."""

    def __init__(self, llm_name: str | None = None):
        self.settings = get_settings()
        self.cost_tracker = CostTracker()
        self.llm = get_provider(llm_name)

        # Memory systems
        self.persistent = PersistentMemory()
        self.session = SessionMemory()
        self.learning = LearningMemory(self.persistent)

        # Adjust ROI model from historical data
        self.learning.adjust_roi_model(None)

    async def scan_target(self, target: str) -> list[VulnFinding]:
        """Scan a specific target domain."""
        return await self.scan_target_domains([target])

    async def scan_target_domains(self, domains: list[str]) -> list[VulnFinding]:
        """Full scan pipeline for a list of domains."""
        # Record scan in persistent memory
        scan_id = self.persistent.start_scan(
            target=",".join(domains),
            metadata={"domain_count": len(domains)},
        )

        # Recon
        logger.info("=== Phase 2: Reconnaissance ===")
        recon = ReconAgent()
        recon_data = await recon.run(domains)
        self.session.recon_data.update(recon_data)

        # Tag targets with technologies for session memory
        for tech in recon_data.get("technologies", []):
            for domain in domains:
                self.session.add_tags(domain, [tech])

        # Scan
        logger.info("=== Phase 3: Vulnerability Scanning ===")
        scanner = ScannerAgent(max_concurrency=self.settings.max_concurrency)
        targets = recon_data.get("subdomains", domains)
        if len(targets) > 50:
            logger.info(f"Limiting scan targets from {len(targets)} to 50")
            targets = targets[:50]
        findings = await scanner.scan(targets, recon_data)
        self.session.raw_findings.extend(findings)

        for domain in domains:
            self.session.mark_scanned(domain)

        if not findings:
            logger.info("No findings from scanning phase")
            self.persistent.complete_scan(scan_id, 0, self.cost_tracker.get_total())
            return []

        # Analyze
        logger.info("=== Phase 4: AI Analysis ===")
        analyzer = AnalyzerAgent(self.llm)
        findings = await analyzer.analyze(findings)
        self.session.analyzed_findings.extend(findings)

        # Persist findings
        for f in findings:
            self.persistent.save_finding(scan_id, f.model_dump(mode="json"))

        # Report
        logger.info("=== Phase 5: Report Generation ===")
        reporter = ReporterAgent()
        reports = await reporter.generate_reports(findings)
        logger.info(f"Generated {len(reports)} reports")

        self.persistent.complete_scan(scan_id, len(findings), self.cost_tracker.get_total())
        return findings

    async def fuzz_binary(self, binary_path: str, corpus_dir: str,
                           output_dir: str = "./fuzz_output",
                           timeout: int = 3600) -> list[VulnFinding]:
        """Run the binary fuzzing pipeline."""
        logger.info(f"=== Fuzzing: {binary_path} ===")
        logger.info("[EXPERIMENTAL] Binary fuzzing pipeline")

        config = FuzzConfig(
            binary_path=binary_path,
            corpus_dir=corpus_dir,
            output_dir=output_dir,
            timeout=timeout,
        )

        fuzzer = Fuzzer(config)
        crashes = await fuzzer.run()

        if not crashes:
            logger.info("No crashes found")
            return []

        triage = CrashTriage()
        unique_crashes = triage.triage(crashes)
        logger.info(f"Unique crashes: {len(unique_crashes)}")

        analyzer = BinaryAnalyzer(self.llm)
        findings = await analyzer.analyze_crashes(unique_crashes)

        reporter = ReporterAgent()
        await reporter.generate_reports(findings)

        return findings

    async def report_from_findings(self, findings_path: str) -> None:
        """Generate reports from a JSON findings file."""
        path = Path(findings_path)
        if not path.exists():
            logger.error(f"Findings file not found: {findings_path}")
            return

        data = json.loads(path.read_text())
        findings = [VulnFinding(**f) for f in data]
        logger.info(f"Loaded {len(findings)} findings from {findings_path}")

        reporter = ReporterAgent()
        reports = await reporter.generate_reports(findings)
        logger.info(f"Generated {len(reports)} reports")

    def show_costs(self) -> str:
        """Display cost breakdown."""
        return self.cost_tracker.summary()

    def show_learning(self) -> str:
        """Display learning memory insights."""
        return self.learning.summary()
