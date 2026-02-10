"""
Inter-agent message bus.

Manages asynchronous message passing between agents with file-based persistence.
Supports peer-based collaboration between FDA, Librarian, and Executor agents.
"""

import json
from pathlib import Path
from typing import Any, Optional
from datetime import datetime
import uuid
import fcntl

from fda.config import MESSAGE_BUS_PATH


# Peer message types for collaboration
class MessageTypes:
    """Standard message types for peer agent communication."""

    # Request types (agent asking another agent to do something)
    SEARCH_REQUEST = "search_request"          # Ask Librarian to search files
    INDEX_REQUEST = "index_request"            # Ask Librarian to index a file
    EXECUTE_REQUEST = "execute_request"        # Ask Executor to run a command
    FILE_REQUEST = "file_request"              # Ask Executor to create/edit file
    KNOWLEDGE_REQUEST = "knowledge_request"    # Ask Librarian for information
    STATUS_REQUEST = "status_request"          # Ask any agent for status
    CLAUDE_CODE_REQUEST = "claude_code_request"  # Ask Executor to run Claude Code

    # Response types (agent reporting results)
    SEARCH_RESULT = "search_result"            # Librarian returns search results
    INDEX_COMPLETE = "index_complete"          # Librarian finished indexing
    EXECUTE_RESULT = "execute_result"          # Executor returns command output
    FILE_COMPLETE = "file_complete"            # Executor finished file operation
    KNOWLEDGE_RESULT = "knowledge_result"      # Librarian returns knowledge
    STATUS_RESPONSE = "status_response"        # Agent reports status
    CLAUDE_CODE_RESULT = "claude_code_result"  # Executor returns Claude Code output

    # Collaboration types (peer-to-peer communication)
    DISCOVERY = "discovery"                    # Agent shares something it found
    SUGGESTION = "suggestion"                  # Agent suggests an action
    QUESTION = "question"                      # Agent asks for clarification
    BLOCKER = "blocker"                        # Agent reports being blocked

    # Legacy types (for backward compatibility)
    TASK = "task"
    ALERT = "alert"
    REQUEST = "request"
    REVIEW_REQUEST = "review_request"
    REVIEW_RESPONSE = "review_response"


# Agent names as constants
class Agents:
    """Standard agent names."""
    FDA = "fda"
    LIBRARIAN = "librarian"
    EXECUTOR = "executor"
    TELEGRAM = "telegram"
    DISCORD = "discord"


class MessageBus:
    """
    Message bus for inter-agent communication.

    Supports sending messages between agents with persistence to disk
    and thread-safe file operations using fcntl locking.
    """

    def __init__(self, bus_path: Path = MESSAGE_BUS_PATH):
        """
        Initialize the message bus.

        Args:
            bus_path: Path to the message_bus.json file.
        """
        self.bus_path = bus_path
        self.bus_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.bus_path.exists():
            self._initialize_bus()

    def _initialize_bus(self) -> None:
        """
        Initialize an empty message bus file.
        """
        initial_data = {"messages": [], "created_at": datetime.now().isoformat()}
        with open(self.bus_path, "w") as f:
            json.dump(initial_data, f, indent=2)

    def _read_bus(self) -> dict[str, Any]:
        """
        Read the message bus data.

        Returns:
            Dictionary containing messages and metadata.
        """
        with open(self.bus_path, "r") as f:
            return json.load(f)

    def _write_bus(self, data: dict[str, Any]) -> None:
        """
        Write data to the message bus.

        Args:
            data: Dictionary to write to the bus file.
        """
        with open(self.bus_path, "w") as f:
            json.dump(data, f, indent=2)

    def send(
        self,
        from_agent: str,
        to_agent: str,
        msg_type: str,
        subject: str,
        body: str,
        priority: str = "medium",
        reply_to: Optional[str] = None,
    ) -> str:
        """
        Send a message to another agent.

        Args:
            from_agent: Sender agent name.
            to_agent: Recipient agent name.
            msg_type: Message type (e.g., "task", "alert", "request").
            subject: Message subject.
            body: Message body.
            priority: Priority level ("low", "medium", "high").
            reply_to: Optional message ID this is a reply to.

        Returns:
            Message ID of the sent message.
        """
        msg_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        # Determine thread_id - either from reply_to or start new thread
        thread_id = msg_id
        if reply_to:
            # Look up the thread_id from the original message
            fh = self._acquire_lock()
            try:
                bus_data = json.load(fh)
                for msg in bus_data["messages"]:
                    if msg["id"] == reply_to:
                        thread_id = msg.get("thread_id", reply_to)
                        break
            finally:
                self._release_lock(fh)

        message = {
            "id": msg_id,
            "from": from_agent,
            "to": to_agent,
            "type": msg_type,
            "subject": subject,
            "body": body,
            "priority": priority,
            "timestamp": timestamp,
            "read": False,
            "thread_id": thread_id,
            "reply_to": reply_to,
        }

        fh = self._acquire_lock()
        try:
            fh.seek(0)
            bus_data = json.load(fh)
            bus_data["messages"].append(message)
            fh.seek(0)
            fh.truncate()
            json.dump(bus_data, fh, indent=2)
        finally:
            self._release_lock(fh)

        return msg_id

    def get_pending(self, agent_name: str) -> list[dict[str, Any]]:
        """
        Get all pending messages for an agent.

        Args:
            agent_name: Name of the agent.

        Returns:
            List of pending message dictionaries.
        """
        fh = self._acquire_lock()
        try:
            fh.seek(0)
            bus_data = json.load(fh)
        finally:
            self._release_lock(fh)

        pending = [
            msg
            for msg in bus_data["messages"]
            if msg["to"] == agent_name and not msg["read"]
        ]

        # Sort by priority (high first) then by timestamp
        priority_order = {"high": 0, "medium": 1, "low": 2}
        pending.sort(
            key=lambda m: (priority_order.get(m["priority"], 1), m["timestamp"])
        )

        return pending

    def get_all_for_agent(self, agent_name: str) -> list[dict[str, Any]]:
        """
        Get all messages for an agent (both sent and received).

        Args:
            agent_name: Name of the agent.

        Returns:
            List of message dictionaries.
        """
        fh = self._acquire_lock()
        try:
            fh.seek(0)
            bus_data = json.load(fh)
        finally:
            self._release_lock(fh)

        return [
            msg
            for msg in bus_data["messages"]
            if msg["to"] == agent_name or msg["from"] == agent_name
        ]

    def mark_read(self, msg_id: str) -> None:
        """
        Mark a message as read.

        Args:
            msg_id: ID of the message to mark as read.
        """
        fh = self._acquire_lock()
        try:
            fh.seek(0)
            bus_data = json.load(fh)
            for msg in bus_data["messages"]:
                if msg["id"] == msg_id:
                    msg["read"] = True
                    msg["read_at"] = datetime.now().isoformat()
                    break
            fh.seek(0)
            fh.truncate()
            json.dump(bus_data, fh, indent=2)
        finally:
            self._release_lock(fh)

    def get_thread(self, msg_id: str) -> list[dict[str, Any]]:
        """
        Get all messages in a conversation thread.

        Args:
            msg_id: ID of a message in the thread.

        Returns:
            List of messages in the thread, ordered by timestamp.
        """
        fh = self._acquire_lock()
        try:
            fh.seek(0)
            bus_data = json.load(fh)
        finally:
            self._release_lock(fh)

        # First find the thread_id for this message
        thread_id = None
        for msg in bus_data["messages"]:
            if msg["id"] == msg_id:
                thread_id = msg.get("thread_id", msg_id)
                break

        if thread_id is None:
            return []

        # Get all messages in this thread
        thread_messages = [
            msg
            for msg in bus_data["messages"]
            if msg.get("thread_id") == thread_id
        ]

        # Sort by timestamp
        thread_messages.sort(key=lambda m: m["timestamp"])

        return thread_messages

    def _acquire_lock(self) -> Any:
        """
        Acquire a file lock on the message bus.

        Returns:
            File handle with lock acquired.
        """
        fh = open(self.bus_path, "r+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _release_lock(self, fh: Any) -> None:
        """
        Release a file lock.

        Args:
            fh: File handle to unlock.
        """
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def cleanup_old_messages(self, days: int = 30) -> int:
        """
        Remove messages older than specified days.

        Args:
            days: Number of days to keep messages.

        Returns:
            Number of messages removed.
        """
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        fh = self._acquire_lock()
        try:
            fh.seek(0)
            bus_data = json.load(fh)
            original_count = len(bus_data["messages"])
            bus_data["messages"] = [
                msg
                for msg in bus_data["messages"]
                if msg["timestamp"] >= cutoff_str
            ]
            removed_count = original_count - len(bus_data["messages"])
            fh.seek(0)
            fh.truncate()
            json.dump(bus_data, fh, indent=2)
        finally:
            self._release_lock(fh)

        return removed_count

    # Peer communication helpers

    def request_search(
        self,
        from_agent: str,
        query: str,
        path: Optional[str] = None,
        priority: str = "medium",
    ) -> str:
        """
        Send a search request to the Librarian.

        Args:
            from_agent: Agent making the request.
            query: Search query (pattern, keyword, or natural language).
            path: Optional path to search within.
            priority: Request priority.

        Returns:
            Message ID for tracking the request.
        """
        body = json.dumps({"query": query, "path": path})
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.LIBRARIAN,
            msg_type=MessageTypes.SEARCH_REQUEST,
            subject=f"Search: {query[:50]}",
            body=body,
            priority=priority,
        )

    def request_execute(
        self,
        from_agent: str,
        command: str,
        cwd: Optional[str] = None,
        priority: str = "medium",
    ) -> str:
        """
        Send an execution request to the Executor.

        Args:
            from_agent: Agent making the request.
            command: Command to execute.
            cwd: Working directory for the command.
            priority: Request priority.

        Returns:
            Message ID for tracking the request.
        """
        body = json.dumps({"command": command, "cwd": cwd})
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.EXECUTOR,
            msg_type=MessageTypes.EXECUTE_REQUEST,
            subject=f"Execute: {command[:50]}",
            body=body,
            priority=priority,
        )

    def request_file_operation(
        self,
        from_agent: str,
        operation: str,
        path: str,
        content: Optional[str] = None,
        priority: str = "medium",
    ) -> str:
        """
        Send a file operation request to the Executor.

        Args:
            from_agent: Agent making the request.
            operation: Operation type ('create', 'edit', 'delete', 'read').
            path: File path.
            content: File content (for create/edit).
            priority: Request priority.

        Returns:
            Message ID for tracking the request.
        """
        body = json.dumps({"operation": operation, "path": path, "content": content})
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.EXECUTOR,
            msg_type=MessageTypes.FILE_REQUEST,
            subject=f"File {operation}: {path}",
            body=body,
            priority=priority,
        )

    def request_knowledge(
        self,
        from_agent: str,
        question: str,
        context: Optional[dict[str, Any]] = None,
        priority: str = "medium",
    ) -> str:
        """
        Send a knowledge request to the Librarian.

        Args:
            from_agent: Agent making the request.
            question: Natural language question.
            context: Optional context for the question.
            priority: Request priority.

        Returns:
            Message ID for tracking the request.
        """
        body = json.dumps({"question": question, "context": context})
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.LIBRARIAN,
            msg_type=MessageTypes.KNOWLEDGE_REQUEST,
            subject=f"Knowledge: {question[:50]}",
            body=body,
            priority=priority,
        )

    def request_claude_code(
        self,
        from_agent: str,
        prompt: str,
        cwd: Optional[str] = None,
        allow_edits: bool = False,
        timeout: int = 300,
        priority: str = "medium",
    ) -> str:
        """
        Send a Claude Code request to the Executor.

        This delegates coding tasks to Claude Code CLI, which uses the user's
        Max subscription instead of API credits.

        Args:
            from_agent: Agent making the request.
            prompt: The task/question to send to Claude Code.
            cwd: Working directory for Claude Code.
            allow_edits: If True, allow Claude Code to edit files.
            timeout: Timeout in seconds (default 5 minutes).
            priority: Request priority.

        Returns:
            Message ID for tracking the request.
        """
        body = json.dumps({
            "prompt": prompt,
            "cwd": cwd,
            "allow_edits": allow_edits,
            "timeout": timeout,
        })
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.EXECUTOR,
            msg_type=MessageTypes.CLAUDE_CODE_REQUEST,
            subject=f"Claude Code: {prompt[:50]}",
            body=body,
            priority=priority,
        )

    def send_result(
        self,
        from_agent: str,
        to_agent: str,
        msg_type: str,
        result: Any,
        success: bool = True,
        error: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """
        Send a result response to another agent.

        Args:
            from_agent: Agent sending the result.
            to_agent: Agent that made the original request.
            msg_type: Result message type.
            result: The result data.
            success: Whether the operation succeeded.
            error: Error message if failed.
            reply_to: Original request message ID.

        Returns:
            Message ID of the response.
        """
        body = json.dumps({
            "success": success,
            "result": result,
            "error": error,
        })
        return self.send(
            from_agent=from_agent,
            to_agent=to_agent,
            msg_type=msg_type,
            subject=f"Result: {'Success' if success else 'Failed'}",
            body=body,
            priority="medium",
            reply_to=reply_to,
        )

    def share_discovery(
        self,
        from_agent: str,
        discovery_type: str,
        description: str,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Share a discovery with all peers (broadcast).

        Args:
            from_agent: Agent that made the discovery.
            discovery_type: Type of discovery (file, pattern, tool, etc.).
            description: Human-readable description.
            details: Additional details.

        Returns:
            Message ID of the discovery broadcast to FDA.
        """
        body = json.dumps({
            "discovery_type": discovery_type,
            "description": description,
            "details": details,
        })
        # Send to FDA as the central coordinator for visibility
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.FDA,
            msg_type=MessageTypes.DISCOVERY,
            subject=f"Discovery: {description[:50]}",
            body=body,
            priority="low",
        )

    def report_blocker(
        self,
        from_agent: str,
        blocker_description: str,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Report a blocker to FDA.

        Args:
            from_agent: Agent that encountered the blocker.
            blocker_description: Description of what's blocking.
            context: Additional context.

        Returns:
            Message ID of the blocker report.
        """
        body = json.dumps({
            "description": blocker_description,
            "context": context,
        })
        return self.send(
            from_agent=from_agent,
            to_agent=Agents.FDA,
            msg_type=MessageTypes.BLOCKER,
            subject=f"Blocker: {blocker_description[:50]}",
            body=body,
            priority="high",
        )

    def get_pending_by_type(
        self,
        agent_name: str,
        msg_type: str,
    ) -> list[dict[str, Any]]:
        """
        Get pending messages of a specific type for an agent.

        Args:
            agent_name: Name of the agent.
            msg_type: Message type to filter by.

        Returns:
            List of matching pending messages.
        """
        pending = self.get_pending(agent_name)
        return [msg for msg in pending if msg["type"] == msg_type]

    def wait_for_response(
        self,
        agent_name: str,
        request_id: str,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ) -> Optional[dict[str, Any]]:
        """
        Wait for a response to a specific request.

        Args:
            agent_name: Agent waiting for the response.
            request_id: ID of the original request.
            timeout_seconds: Maximum time to wait.
            poll_interval: Time between checks.

        Returns:
            Response message or None if timeout.
        """
        import time

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            # Check for messages that are replies to our request
            pending = self.get_pending(agent_name)
            for msg in pending:
                if msg.get("reply_to") == request_id:
                    return msg
            time.sleep(poll_interval)

        return None
