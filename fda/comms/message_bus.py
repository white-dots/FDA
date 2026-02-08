"""
Inter-agent message bus.

Manages asynchronous message passing between agents with file-based persistence.
"""

import json
from pathlib import Path
from typing import Any, Optional
from datetime import datetime
import uuid
import fcntl

from fda.config import MESSAGE_BUS_PATH


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
