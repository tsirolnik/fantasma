"""Message types and MessageBus for inter-agent communication."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    """Core inter-agent message with LLM-generated reasoning."""

    sender: str
    recipient: str  # "director", agent name, or "all"
    msg_type: str  # task_result, concern, observation, question, answer, decision
    content: str  # LLM-generated natural language
    data: dict[str, Any] = field(default_factory=dict)
    priority: str = "info"  # info, warning, critical
    in_reply_to: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        prefix = f"[{self.sender} → {self.recipient}]"
        prio = f" ({self.priority})" if self.priority != "info" else ""
        content = self.content
        if self.msg_type in ("task_result", "debrief"):
            # Show full content for substantive outputs
            return f"{prefix} ({self.msg_type}{prio}) {content}"
        return f"{prefix} ({self.msg_type}{prio}) {content[:200]}"


@dataclass
class TaskAssignment:
    """Director assigns work to a worker."""

    agent_name: str
    task_type: str  # discover, recon, scan, analyze, report
    parameters: dict[str, Any] = field(default_factory=dict)
    context: str = ""  # Director's reasoning for this assignment


@dataclass
class TaskResult:
    """Result from a worker's execute() call."""

    agent_name: str
    task_type: str
    data: Any = None  # The actual result (findings, programs, etc.)
    reflection: str = ""  # LLM-generated self-assessment
    success: bool = True
    error: str = ""


@dataclass
class DirectorDecision:
    """Director's strategic decision after evaluating a phase."""

    decision: str  # continue, skip, abort, redirect
    reasoning: str  # LLM-generated explanation
    parameters: dict[str, Any] = field(default_factory=dict)


class MessageBus:
    """Central message bus for agent communication and observability."""

    def __init__(self) -> None:
        self._log: list[AgentMessage] = []
        self._subscribers: dict[str, list[Callable]] = {}
        self._agents: dict[str, Any] = {}  # name -> agent, for routing questions

    def register_agent(self, name: str, agent: Any) -> None:
        """Register an agent so the bus can route questions to it."""
        self._agents[name] = agent

    def get_agent(self, name: str):
        """Look up a currently registered agent for interactive routing."""
        return self._agents.get(name)

    def send(self, message: AgentMessage) -> None:
        """Log and dispatch a message to subscribers."""
        self._log.append(message)
        logger.info(str(message))

        # Notify subscribers for this message type
        for handler in self._subscribers.get(message.msg_type, []):
            try:
                handler(message)
            except Exception:
                logger.exception("Subscriber error for %s", message.msg_type)

        # Notify wildcard subscribers
        for handler in self._subscribers.get("*", []):
            try:
                handler(message)
            except Exception:
                logger.exception("Wildcard subscriber error")

    def subscribe(self, msg_type: str, handler: Callable) -> None:
        """Register a callback for a message type. Use '*' for all."""
        self._subscribers.setdefault(msg_type, []).append(handler)

    async def ask(
        self,
        sender: str,
        recipient: str,
        question: str,
        context: dict[str, Any] | None = None,
    ) -> AgentMessage:
        """Synchronous question/answer between agents.

        Routes the question to the recipient's handle_question() method
        and returns the answer message.
        """
        q_msg = AgentMessage(
            sender=sender,
            recipient=recipient,
            msg_type="question",
            content=question,
            data=context or {},
        )
        self.send(q_msg)

        agent = self._agents.get(recipient)
        if agent is None:
            # No agent registered — return a stub answer
            answer = AgentMessage(
                sender=recipient,
                recipient=sender,
                msg_type="answer",
                content=f"Agent '{recipient}' is not available.",
                in_reply_to=q_msg.id,
            )
            self.send(answer)
            return answer

        answer = await agent.handle_question(q_msg)
        answer.in_reply_to = q_msg.id
        self.send(answer)
        return answer

    def get_conversation(self, message_id: str) -> list[AgentMessage]:
        """Get all messages in a thread (by in_reply_to chain)."""
        thread_ids = {message_id}
        result = []
        for msg in self._log:
            if msg.id in thread_ids or msg.in_reply_to in thread_ids:
                thread_ids.add(msg.id)
                result.append(msg)
        return result

    def get_log(
        self,
        agent: str | None = None,
        msg_type: str | None = None,
    ) -> list[AgentMessage]:
        """Get message history, optionally filtered."""
        msgs = self._log
        if agent:
            msgs = [m for m in msgs if m.sender == agent or m.recipient == agent]
        if msg_type:
            msgs = [m for m in msgs if m.msg_type == msg_type]
        return msgs

    def format_log(
        self,
        agent: str | None = None,
        msg_type: str | None = None,
    ) -> str:
        """Human-readable formatted log."""
        msgs = self.get_log(agent=agent, msg_type=msg_type)
        if not msgs:
            return "(no messages)"
        return "\n\n".join(str(m) for m in msgs)
