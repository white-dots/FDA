"""
Tests for the inter-agent MessageBus.
"""

import json
import pytest


class TestMessageBus:
    """Tests for MessageBus — send, receive, threading."""

    def test_init_creates_file(self, message_bus, tmp_bus_path):
        assert tmp_bus_path.exists()
        data = json.loads(tmp_bus_path.read_text())
        assert "messages" in data
        assert data["messages"] == []

    def test_send_message(self, message_bus):
        msg_id = message_bus.send(
            from_agent="fda",
            to_agent="worker",
            msg_type="TASK_REQUEST",
            subject="Fix bug",
            body='{"task": "fix it"}',
        )
        assert msg_id is not None
        assert isinstance(msg_id, str)

    def test_get_pending(self, message_bus):
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="TASK", subject="T1", body="b1",
        )
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="TASK", subject="T2", body="b2",
        )
        # Message to a different agent
        message_bus.send(
            from_agent="fda", to_agent="librarian",
            msg_type="SEARCH", subject="S1", body="b3",
        )

        pending = message_bus.get_pending("worker")
        assert len(pending) == 2

    def test_mark_read(self, message_bus):
        msg_id = message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="TASK", subject="T", body="b",
        )
        message_bus.mark_read(msg_id)
        pending = message_bus.get_pending("worker")
        assert len(pending) == 0

    def test_priority_ordering(self, message_bus):
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="T", subject="Low", body="b", priority="low",
        )
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="T", subject="High", body="b", priority="high",
        )
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="T", subject="Medium", body="b", priority="medium",
        )
        pending = message_bus.get_pending("worker")
        assert pending[0]["subject"] == "High"

    def test_thread_tracking(self, message_bus):
        original_id = message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="TASK", subject="Original", body="b",
        )
        reply_id = message_bus.send(
            from_agent="worker", to_agent="fda",
            msg_type="RESULT", subject="Reply", body="r",
            reply_to=original_id,
        )
        thread = message_bus.get_thread(original_id)
        assert len(thread) == 2
        assert thread[0]["subject"] == "Original"
        assert thread[1]["subject"] == "Reply"

    def test_case_insensitive_agent_names(self, message_bus):
        message_bus.send(
            from_agent="FDA", to_agent="Worker",
            msg_type="T", subject="Test", body="b",
        )
        pending = message_bus.get_pending("worker")
        assert len(pending) == 1

    def test_get_all_for_agent(self, message_bus):
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="T", subject="Sent", body="b",
        )
        message_bus.send(
            from_agent="worker", to_agent="fda",
            msg_type="T", subject="Received", body="b",
        )
        all_msgs = message_bus.get_all_for_agent("worker")
        assert len(all_msgs) == 2

    def test_cleanup_old_messages(self, message_bus):
        # Send a message (will be recent)
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="T", subject="Recent", body="b",
        )
        removed = message_bus.cleanup_old_messages(days=30)
        assert removed == 0  # Message is fresh, nothing to remove

    def test_get_pending_by_type(self, message_bus):
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="TASK_REQUEST", subject="T", body="b",
        )
        message_bus.send(
            from_agent="fda", to_agent="worker",
            msg_type="STATUS_REQUEST", subject="S", body="b",
        )
        tasks = message_bus.get_pending_by_type("worker", "TASK_REQUEST")
        assert len(tasks) == 1
        assert tasks[0]["type"] == "TASK_REQUEST"

    def test_helper_request_search(self, message_bus):
        msg_id = message_bus.request_search("fda", "find config files")
        assert msg_id
        pending = message_bus.get_pending("librarian")
        assert len(pending) == 1
        assert pending[0]["type"] == "search_request"

    def test_helper_request_execute(self, message_bus):
        msg_id = message_bus.request_execute("fda", "ls -la")
        assert msg_id
        pending = message_bus.get_pending("executor")
        assert len(pending) == 1

    def test_helper_share_discovery(self, message_bus):
        msg_id = message_bus.share_discovery(
            "librarian", "file", "Found a new config pattern",
        )
        assert msg_id
        pending = message_bus.get_pending("fda")
        assert len(pending) == 1
        assert pending[0]["type"] == "discovery"

    def test_helper_report_blocker(self, message_bus):
        msg_id = message_bus.report_blocker("executor", "SSH key expired")
        assert msg_id
        pending = message_bus.get_pending("fda")
        assert len(pending) == 1
        assert pending[0]["priority"] == "high"
