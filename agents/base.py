"""BaseAgent ABC — foundation for all worker agents."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from llm.base import LLMProvider

from agents.messaging import (
    AgentMessage,
    MessageBus,
    TaskAssignment,
    TaskResult,
)

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Abstract base for all worker agents in the Director/Worker system.

    Subclasses implement ``run()`` for actual work.  The base class handles
    LLM-powered reflection, inter-agent communication, and message routing.
    """

    name: str = "base"
    capability: str = "generic"

    def __init__(
        self,
        bus: MessageBus,
        llm: LLMProvider | None = None,
        name: str | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        self.bus = bus
        self.llm = llm
        self._recent_results: list[Any] = []  # track for handle_question context
        self._help_callback = None  # async fn(help_id, description, context) -> str
        bus.register_agent(self.name, self)

    # ------------------------------------------------------------------
    # Core execution flow
    # ------------------------------------------------------------------

    async def execute(self, assignment: TaskAssignment) -> TaskResult:
        """Full execution: run the work, then reflect on the result."""
        try:
            data = await self.run(assignment)
        except Exception as exc:
            logger.exception("%s failed during run()", self.name)
            return TaskResult(
                agent_name=self.name,
                task_type=assignment.task_type,
                success=False,
                error=str(exc),
            )

        # Keep recent results for handle_question context
        self._recent_results.append(data)
        if len(self._recent_results) > 3:
            self._recent_results.pop(0)

        reflection = await self.reflect(data, assignment)

        # Send reflection to director
        self.bus.send(
            AgentMessage(
                sender=self.name,
                recipient="director",
                msg_type="task_result",
                content=reflection,
                data={"task_type": assignment.task_type},
            )
        )

        return TaskResult(
            agent_name=self.name,
            task_type=assignment.task_type,
            data=data,
            reflection=reflection,
        )

    @abstractmethod
    async def run(self, assignment: TaskAssignment) -> Any:
        """Subclasses implement the actual work here."""
        ...

    # ------------------------------------------------------------------
    # LLM-powered reflection
    # ------------------------------------------------------------------

    async def reflect(self, result: Any, assignment: TaskAssignment) -> str:
        """Use the worker's LLM to analyse its own results.

        Falls back to ``summarize()`` when no LLM is configured.
        """
        if self.llm is None:
            return self.summarize(result)

        prompt = self._build_reflect_prompt(result, assignment)
        try:
            return await self.llm.analyze(prompt)
        except Exception:
            logger.exception("%s: LLM reflection failed, using fallback", self.name)
            return self.summarize(result)

    def _build_reflect_prompt(self, result: Any, assignment: TaskAssignment) -> str:
        """Build the reflection prompt.  Override in subclasses for richer prompts."""
        return (
            f"You are the {self.name} agent ({self.capability}) on an authorized "
            "bug bounty security research team. "
            f"You just completed a '{assignment.task_type}' task.\n\n"
            f"Task context: {assignment.context}\n"
            f"Result summary: {self.summarize(result)}\n\n"
            "Analyze your results from a SECURITY perspective: What security-relevant "
            "findings did you discover? What attack surface is interesting? "
            "Any concerns or recommendations for the Director about where to focus "
            "for finding exploitable vulnerabilities?"
        )

    def summarize(self, result: Any) -> str:
        """One-line fallback summary when no LLM is available."""
        if result is None:
            return "No results produced."
        if isinstance(result, list):
            return f"Produced {len(result)} results."
        if isinstance(result, dict):
            return f"Produced results with keys: {', '.join(result.keys())}"
        return str(result)[:200]

    # ------------------------------------------------------------------
    # Communication helpers
    # ------------------------------------------------------------------

    def raise_concern(
        self,
        content: str,
        priority: str = "warning",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Send a concern to the Director with reasoning."""
        self.bus.send(
            AgentMessage(
                sender=self.name,
                recipient="director",
                msg_type="concern",
                content=content,
                data=data or {},
                priority=priority,
            )
        )

    def observe(self, content: str, data: dict[str, Any] | None = None) -> None:
        """Send a non-urgent observation to the Director."""
        self.bus.send(
            AgentMessage(
                sender=self.name,
                recipient="director",
                msg_type="observation",
                content=content,
                data=data or {},
            )
        )

    async def consult(
        self,
        agent_name: str,
        question: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Ask another worker a question via the bus.  Returns the answer text."""
        answer = await self.bus.ask(self.name, agent_name, question, context)
        return answer.content

    async def handle_question(self, question_msg: AgentMessage) -> AgentMessage:
        """Receive a question from another agent and respond.

        Uses LLM to reason about the question in the context of recent work.
        Falls back to a basic summary if no LLM.
        """
        context_summary = "; ".join(
            self.summarize(r) for r in self._recent_results[-2:]
        )

        if self.llm is not None:
            prompt = (
                f"You are the {self.name} agent ({self.capability}) on an "
                "authorized bug bounty security research team.\n"
                f"{question_msg.sender} asks: {question_msg.content}\n\n"
                f"Your recent work context: {context_summary}\n\n"
                "Answer the question from a security research perspective. "
                "Focus on information relevant to finding exploitable vulnerabilities."
            )
            try:
                answer_text = await self.llm.analyze(prompt)
            except Exception:
                logger.exception("%s: LLM failed answering question", self.name)
                answer_text = f"Based on my recent work: {context_summary}"
        else:
            answer_text = f"Based on my recent work: {context_summary}"

        return AgentMessage(
            sender=self.name,
            recipient=question_msg.sender,
            msg_type="answer",
            content=answer_text,
        )

    async def request_human_help(
        self,
        description: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Request human assistance. Blocks until user responds via dashboard.

        Sends a help_needed message on the bus and, if a help callback is
        wired (by the pipeline bridge), waits for the human response.
        Returns the human's free-text response, or empty string if no
        dashboard is connected.
        """
        import uuid

        help_id = uuid.uuid4().hex[:12]
        msg = AgentMessage(
            sender=self.name,
            recipient="human",
            msg_type="help_needed",
            content=description,
            data=context or {},
            priority="critical",
        )
        self.bus.send(msg)

        if self._help_callback:
            try:
                return await self._help_callback(help_id, description, context or {})
            except Exception:
                logger.exception("%s: help callback failed", self.name)
                return ""
        return ""
