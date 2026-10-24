"""Agent system — Director/Worker pattern with LLM-powered communication."""

from agents.messaging import AgentMessage, MessageBus, TaskAssignment, TaskResult, DirectorDecision
from agents.base import BaseAgent
from agents.director import Director
from agents.pipeline import AgentPipeline
from agents.llm_config import LLMConfigMap, AgentLLMConfig, create_llm_for_agent
from agents.workers import (
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
