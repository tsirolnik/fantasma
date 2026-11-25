"""Agent system — Director/Worker pattern with LLM-powered communication."""

from fantasma.agents.messaging import AgentMessage, MessageBus, TaskAssignment, TaskResult, DirectorDecision
from fantasma.agents.base import BaseAgent
from fantasma.agents.director import Director
from fantasma.agents.pipeline import AgentPipeline
from fantasma.agents.llm_config import LLMConfigMap, AgentLLMConfig, create_llm_for_agent
from fantasma.agents.workers import (
    DiscoverWorker,
    ReconWorker,
    ScanWorker,
    AnalyzeWorker,
    ReportWorker,
)

__all__ = [
    "AgentMessage",
    "MessageBus",
    "TaskAssignment",
    "TaskResult",
    "DirectorDecision",
    "BaseAgent",
    "Director",
    "AgentPipeline",
    "LLMConfigMap",
    "AgentLLMConfig",
    "create_llm_for_agent",
    "DiscoverWorker",
    "ReconWorker",
    "ScanWorker",
    "AnalyzeWorker",
    "ReportWorker",
]
