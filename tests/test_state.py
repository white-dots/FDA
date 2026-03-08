"""
Tests for ProjectState — SQLite-backed state management.
"""

import pytest
from datetime import datetime


class TestProjectState:
    """Tests for core ProjectState operations."""

    def test_init_creates_db(self, project_state, tmp_state_db):
        assert tmp_state_db.exists()

    def test_set_and_get_context(self, project_state):
        project_state.set_context("user_name", "John")
        assert project_state.get_context("user_name") == "John"

    def test_get_context_missing_key(self, project_state):
        assert project_state.get_context("nonexistent") is None

    def test_context_overwrite(self, project_state):
        project_state.set_context("key", "v1")
        project_state.set_context("key", "v2")
        assert project_state.get_context("key") == "v2"

    def test_add_task(self, project_state):
        task_id = project_state.add_task(
            title="Fix bug",
            description="Something is broken",
            owner="worker",
            priority="high",
        )
        assert task_id is not None

    def test_get_tasks(self, project_state):
        project_state.add_task(title="Task A", description="a", owner="w")
        project_state.add_task(title="Task B", description="b", owner="w")
        tasks = project_state.get_tasks()
        assert len(tasks) == 2

    def test_update_task_status(self, project_state):
        task_id = project_state.add_task(
            title="Task", description="d", owner="w",
        )
        project_state.update_task(task_id, status="completed")
        tasks = project_state.get_tasks()
        completed = [t for t in tasks if t["id"] == task_id]
        assert completed[0]["status"] == "completed"

    def test_add_alert(self, project_state):
        alert_id = project_state.add_alert(
            level="warning",
            message="Disk space low",
            source="monitor",
        )
        assert alert_id is not None

    def test_get_alerts_unacknowledged(self, project_state):
        project_state.add_alert(level="info", message="m1", source="s")
        project_state.add_alert(level="error", message="m2", source="s")
        alerts = project_state.get_alerts(acknowledged=False)
        assert len(alerts) == 2

    def test_acknowledge_alert(self, project_state):
        alert_id = project_state.add_alert(
            level="warning", message="test", source="s",
        )
        project_state.acknowledge_alert(alert_id)
        unacked = project_state.get_alerts(acknowledged=False)
        assert len(unacked) == 0

    def test_add_decision(self, project_state):
        dec_id = project_state.add_decision(
            title="Use Sonnet",
            rationale="Better for code gen",
            decision_maker="fda",
            impact="Improved code quality",
        )
        assert dec_id is not None

    def test_get_decisions(self, project_state):
        project_state.add_decision(
            title="D1", rationale="R1", decision_maker="fda", impact="I1",
        )
        decisions = project_state.get_decisions()
        assert len(decisions) == 1
        assert decisions[0]["title"] == "D1"

    def test_agent_status(self, project_state):
        project_state.update_agent_status("worker", "running")
        status = project_state.get_agent_status("worker")
        assert status is not None
        assert status["status"] == "running"

    def test_agent_heartbeat(self, project_state):
        project_state.update_agent_status("worker", "running")
        project_state.agent_heartbeat("worker")
        status = project_state.get_agent_status("worker")
        assert status["last_heartbeat"] is not None

    def test_telegram_user_registration(self, project_state):
        project_state.register_telegram_user("12345", "TestUser")
        users = project_state.get_telegram_users(active_only=True)
        assert any(u["chat_id"] == "12345" for u in users)

    def test_telegram_user_deactivate(self, project_state):
        project_state.register_telegram_user("12345", "TestUser")
        project_state.deactivate_telegram_user("12345")
        users = project_state.get_telegram_users(active_only=True)
        assert not any(u["chat_id"] == "12345" for u in users)

    def test_task_filters_by_status(self, project_state):
        project_state.add_task(title="A", description="a", owner="w")
        tid = project_state.add_task(title="B", description="b", owner="w")
        project_state.update_task(tid, status="completed")

        all_tasks = project_state.get_tasks()
        assert len(all_tasks) == 2

        pending = project_state.get_tasks(status="pending")
        assert len(pending) == 1
        assert pending[0]["title"] == "A"
