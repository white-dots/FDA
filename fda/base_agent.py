"""
Base Agent class with shared functionality.

Provides common utilities for all FDA system agents including
Claude API integration, message bus access, and state management.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from anthropic import Anthropic

import os

from fda.state.project_state import ProjectState
from fda.comms.message_bus import MessageBus
from fda.journal.writer import JournalWriter
from fda.journal.retriever import JournalRetriever
from fda.config import STATE_DB_PATH, MESSAGE_BUS_PATH, JOURNAL_DIR, ANTHROPIC_API_KEY_ENV

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Abstract base class for all FDA system agents.

    Provides shared functionality for Claude API calls, state access,
    inter-agent messaging, and journal operations.
    """

    def __init__(
        self,
        name: str,
        model: str,
        system_prompt: str,
        project_state_path: Optional[Path] = None,
    ):
        """
        Initialize the base agent.

        Args:
            name: Agent name (used for messaging and logging).
            model: Claude model to use.
            system_prompt: System prompt for the agent.
            project_state_path: Optional path to project state database.
        """
        self.name = name
        self.model = model
        self.system_prompt = system_prompt

        # Initialize shared components first (needed to get API key from DB)
        self.state = ProjectState(project_state_path or STATE_DB_PATH)

        # Initialize Anthropic client with API key from env or database
        api_key = os.environ.get(ANTHROPIC_API_KEY_ENV) or self.state.get_context("anthropic_api_key")
        if not api_key:
            raise ValueError(
                "Anthropic API key not found. Set ANTHROPIC_API_KEY environment variable "
                "or configure via 'fda setup' web interface."
            )
        self.client = Anthropic(api_key=api_key)
        self.message_bus = MessageBus(MESSAGE_BUS_PATH)
        self.journal_writer = JournalWriter(JOURNAL_DIR)
        self.journal_retriever = JournalRetriever(JOURNAL_DIR)

        # Conversation history for multi-turn interactions
        self.conversation_history: list[dict[str, Any]] = []

        # Running state
        self._running = False

    def chat(
        self,
        message: str,
        include_history: bool = True,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        """
        Send a message to Claude and get a response.

        Args:
            message: The user message to send.
            include_history: Whether to include conversation history.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            Claude's response text.
        """
        # Build messages list
        messages = []

        if include_history:
            messages.extend(self.conversation_history)

        messages.append({"role": "user", "content": message})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=self.system_prompt,
                messages=messages,
                temperature=temperature,
            )

            assistant_message = response.content[0].text

            # Update conversation history
            if include_history:
                self.conversation_history.append({"role": "user", "content": message})
                self.conversation_history.append(
                    {"role": "assistant", "content": assistant_message}
                )

            return assistant_message

        except Exception as e:
            logger.error(f"Error calling Claude API: {e}")
            raise

    def chat_with_context(
        self,
        message: str,
        context: dict[str, Any],
        max_tokens: int = 4096,
        model_override: Optional[str] = None,
    ) -> str:
        """
        Send a message with additional context to Claude.

        Args:
            message: The user message.
            context: Dictionary of context to include.
            max_tokens: Maximum tokens in response.
            model_override: Optional model to use instead of the agent's default.

        Returns:
            Claude's response text.
        """
        # Format context as part of the message
        context_str = self._format_context(context)
        full_message = f"{context_str}\n\n{message}"

        if model_override:
            return self._chat_with_model(full_message, model_override, max_tokens)
        return self.chat(full_message, include_history=False, max_tokens=max_tokens)

    def _chat_with_model(
        self,
        message: str,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        """
        Send a message using a specific model (doesn't update history).

        Args:
            message: The user message.
            model: Model to use for this request.
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            Claude's response text.
        """
        try:
            response = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=self.system_prompt,
                messages=[{"role": "user", "content": message}],
                temperature=temperature,
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"Error calling Claude API with model {model}: {e}")
            raise

    def _format_context(self, context: dict[str, Any]) -> str:
        """Format context dictionary into a string for the prompt."""
        lines = ["## Current Context\n"]

        for key, value in context.items():
            if isinstance(value, list):
                lines.append(f"### {key.replace('_', ' ').title()}")
                for item in value[:10]:  # Limit to 10 items
                    if isinstance(item, dict):
                        item_str = ", ".join(f"{k}: {v}" for k, v in item.items())
                        lines.append(f"- {item_str}")
                    else:
                        lines.append(f"- {item}")
                if len(value) > 10:
                    lines.append(f"... and {len(value) - 10} more")
            elif isinstance(value, dict):
                lines.append(f"### {key.replace('_', ' ').title()}")
                for k, v in value.items():
                    lines.append(f"- {k}: {v}")
            else:
                lines.append(f"**{key.replace('_', ' ').title()}**: {value}")

        return "\n".join(lines)

    def clear_history(self) -> None:
        """Clear the conversation history."""
        self.conversation_history = []

    def send_message(
        self,
        to_agent: str,
        msg_type: str,
        subject: str,
        body: str,
        priority: str = "medium",
    ) -> str:
        """
        Send a message to another agent.

        Args:
            to_agent: Name of the recipient agent.
            msg_type: Message type (e.g., "task", "alert", "request").
            subject: Message subject.
            body: Message body.
            priority: Priority level.

        Returns:
            Message ID.
        """
        return self.message_bus.send(
            from_agent=self.name,
            to_agent=to_agent,
            msg_type=msg_type,
            subject=subject,
            body=body,
            priority=priority,
        )

    def get_pending_messages(self) -> list[dict[str, Any]]:
        """
        Get pending messages for this agent.

        Returns:
            List of pending message dictionaries.
        """
        return self.message_bus.get_pending(self.name)

    def process_message(self, message: dict[str, Any]) -> Optional[str]:
        """
        Process a received message and optionally generate a response.

        Args:
            message: The message to process.

        Returns:
            Optional response message ID if a reply was sent.
        """
        # Mark message as read
        self.message_bus.mark_read(message["id"])

        # Default implementation - subclasses can override
        logger.info(
            f"[{self.name}] Received message from {message['from']}: {message['subject']}"
        )

        return None

    def log_to_journal(
        self,
        summary: str,
        content: str,
        tags: Optional[list[str]] = None,
        relevance_decay: str = "medium",
    ) -> Path:
        """
        Write an entry to the project journal.

        Args:
            summary: Brief summary of the entry.
            content: Full content of the entry.
            tags: Optional list of tags.
            relevance_decay: Decay rate for the entry.

        Returns:
            Path to the written journal file.
        """
        return self.journal_writer.write_entry(
            author=self.name,
            tags=tags or [self.name.lower()],
            summary=summary,
            content=content,
            relevance_decay=relevance_decay,
        )

    def search_journal(
        self,
        query: str,
        tags: Optional[list[str]] = None,
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Search the project journal.

        Args:
            query: Search query text.
            tags: Optional tags to filter by.
            top_n: Maximum number of results.

        Returns:
            List of matching journal entries.
        """
        return self.journal_retriever.retrieve(
            query_tags=tags,
            query_text=query,
            top_n=top_n,
        )

    def add_alert(
        self,
        level: str,
        message: str,
    ) -> str:
        """
        Add an alert to the project state.

        Args:
            level: Alert level (info, warning, critical).
            message: Alert message.

        Returns:
            Alert ID.
        """
        return self.state.add_alert(
            level=level,
            message=message,
            source=self.name,
        )

    def get_project_context(self) -> dict[str, Any]:
        """
        Get current project context for prompts.

        Returns:
            Dictionary with project state information.
        """
        tasks = self.state.get_tasks()
        alerts = self.state.get_alerts(acknowledged=False)

        # Group tasks by status
        tasks_by_status = {}
        for task in tasks:
            status = task.get("status", "unknown")
            if status not in tasks_by_status:
                tasks_by_status[status] = []
            tasks_by_status[status].append(task)

        return {
            "timestamp": datetime.now().isoformat(),
            "tasks_summary": {
                status: len(tasks) for status, tasks in tasks_by_status.items()
            },
            "in_progress_tasks": tasks_by_status.get("in_progress", []),
            "blocked_tasks": tasks_by_status.get("blocked", []),
            "pending_tasks": tasks_by_status.get("pending", [])[:5],
            "unacknowledged_alerts": alerts,
        }

    @abstractmethod
    def run_event_loop(self) -> None:
        """
        Run the agent's main event loop.

        This must be implemented by each agent subclass.
        """
        pass

    def start(self) -> None:
        """Start the agent's event loop."""
        self._running = True
        logger.info(f"[{self.name}] Starting agent...")
        self.run_event_loop()

    def stop(self) -> None:
        """Stop the agent's event loop."""
        self._running = False
        logger.info(f"[{self.name}] Stopping agent...")

    def is_running(self) -> bool:
        """Check if the agent is currently running."""
        return self._running
