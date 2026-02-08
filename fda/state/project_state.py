"""
Project state management using SQLite.

Maintains persistent state for tasks, context, KPIs, alerts, and decisions.
"""

import sqlite3
import json
import uuid
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.config import STATE_DB_PATH


class ProjectState:
    """
    Manages project state using SQLite database.
    
    Stores project context, tasks, KPI snapshots, alerts, and decisions.
    """

    def __init__(self, db_path: Path = STATE_DB_PATH):
        """
        Initialize the project state manager.
        
        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection: Optional[sqlite3.Connection] = None
        self.init_db()

    def init_db(self) -> None:
        """
        Initialize the database with required tables.

        Creates tables for:
        - context: Project context key-value pairs
        - tasks: Task tracking (id, title, status, owner, etc.)
        - kpi_snapshots: Historical KPI values
        - alerts: System alerts and notifications
        - decisions: Recorded decisions and their rationale
        - meeting_prep: Meeting preparation records
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Context table - key-value store for project configuration
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS context (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Tasks table - task tracking with status and ownership
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                owner TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                due_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # KPI snapshots table - historical metric values
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS kpi_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Alerts table - system alerts and notifications
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                source TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Decisions table - recorded decisions with rationale
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                rationale TEXT NOT NULL,
                decision_maker TEXT NOT NULL,
                impact TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Meeting prep table - meeting preparation records
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meeting_prep (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                brief TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Telegram users table - registered Telegram users
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_users (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Discord sessions table - voice session tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS discord_sessions (
                id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                transcript_path TEXT,
                status TEXT DEFAULT 'active'
            )
        """)

        # Create indexes for common queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_kpi_metric ON kpi_snapshots(metric)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(level)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_meeting_prep_event ON meeting_prep(event_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_telegram_active ON telegram_users(is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_discord_status ON discord_sessions(status)")

        conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get or create database connection.

        Returns:
            SQLite connection object.

        Note:
            Uses check_same_thread=False to allow connections to be used
            across threads (e.g., in Flask's threaded request handlers).
            This is safe because SQLite handles its own locking.
        """
        if self.connection is None:
            self.connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False
            )
            self.connection.row_factory = sqlite3.Row
        return self.connection

    def set_context(self, key: str, value: Any) -> None:
        """
        Set a project context value.

        Args:
            key: Context key.
            value: Context value (will be JSON-serialized).
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        serialized_value = json.dumps(value)
        cursor.execute(
            """
            INSERT INTO context (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, serialized_value, datetime.now().isoformat()),
        )
        conn.commit()

    def get_context(self, key: str) -> Optional[Any]:
        """
        Get a project context value.

        Args:
            key: Context key.

        Returns:
            Context value or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM context WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            return None
        return json.loads(row["value"])

    def add_task(
        self,
        title: str,
        description: str,
        owner: str,
        status: str = "pending",
        priority: str = "medium",
        due_date: Optional[str] = None,
    ) -> str:
        """
        Add a new task to the project.

        Args:
            title: Task title.
            description: Task description.
            owner: Task owner (agent or person).
            status: Initial status (pending, in_progress, completed, blocked).
            priority: Priority level (low, medium, high).
            due_date: Optional due date (ISO format).

        Returns:
            Generated task ID.
        """
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tasks (id, title, description, owner, status, priority, due_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, title, description, owner, status, priority, due_date, now, now),
        )
        conn.commit()
        return task_id

    def update_task(self, task_id: str, **fields: Any) -> None:
        """
        Update an existing task.

        Args:
            task_id: Task ID to update.
            **fields: Fields to update (status, owner, etc.).
        """
        if not fields:
            return

        allowed_fields = {"title", "description", "owner", "status", "priority", "due_date"}
        update_fields = {k: v for k, v in fields.items() if k in allowed_fields}

        if not update_fields:
            return

        update_fields["updated_at"] = datetime.now().isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
        values = list(update_fields.values()) + [task_id]

        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()

    def get_tasks(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Get tasks, optionally filtered by status.

        Args:
            status: Optional status filter.

        Returns:
            List of task dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if status:
            cursor.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC", (status,))
        else:
            cursor.execute("SELECT * FROM tasks ORDER BY created_at DESC")

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def add_kpi_snapshot(
        self,
        metric: str,
        value: float,
        timestamp: Optional[str] = None,
    ) -> None:
        """
        Record a KPI snapshot.

        Args:
            metric: Metric name.
            value: Metric value.
            timestamp: Optional timestamp (defaults to now).
        """
        ts = timestamp or datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO kpi_snapshots (metric, value, timestamp) VALUES (?, ?, ?)",
            (metric, value, ts),
        )
        conn.commit()

    def get_latest_kpi(self, metric: str) -> Optional[dict[str, Any]]:
        """
        Get the latest value for a KPI metric.

        Args:
            metric: Metric name.

        Returns:
            Latest KPI snapshot or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM kpi_snapshots WHERE metric = ? ORDER BY timestamp DESC LIMIT 1",
            (metric,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_kpi_history(
        self, metric: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        Get historical KPI values for a metric.

        Args:
            metric: Metric name.
            limit: Maximum number of records to return.

        Returns:
            List of KPI snapshots ordered by timestamp descending.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM kpi_snapshots WHERE metric = ? ORDER BY timestamp DESC LIMIT ?",
            (metric, limit),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def add_alert(
        self,
        level: str,
        message: str,
        source: str,
    ) -> str:
        """
        Record an alert.

        Args:
            level: Alert level (info, warning, critical).
            message: Alert message.
            source: Source agent or system.

        Returns:
            Generated alert ID.
        """
        alert_id = f"alert_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (id, level, message, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (alert_id, level, message, source, now),
        )
        conn.commit()
        return alert_id

    def get_alerts(
        self, level: Optional[str] = None, acknowledged: Optional[bool] = None
    ) -> list[dict[str, Any]]:
        """
        Get alerts, optionally filtered by level and acknowledgment status.

        Args:
            level: Optional level filter (info, warning, critical).
            acknowledged: Optional acknowledgment status filter.

        Returns:
            List of alert dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM alerts WHERE 1=1"
        params: list[Any] = []

        if level:
            query += " AND level = ?"
            params.append(level)
        if acknowledged is not None:
            query += " AND acknowledged = ?"
            params.append(1 if acknowledged else 0)

        query += " ORDER BY created_at DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert(self, alert_id: str) -> None:
        """
        Mark an alert as acknowledged.

        Args:
            alert_id: Alert ID to acknowledge.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
        conn.commit()

    def add_decision(
        self,
        title: str,
        rationale: str,
        decision_maker: str,
        impact: str,
    ) -> str:
        """
        Record a significant decision.

        Args:
            title: Decision title.
            rationale: Reasoning behind the decision.
            decision_maker: Who made the decision.
            impact: Expected impact description.

        Returns:
            Generated decision ID.
        """
        decision_id = f"decision_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO decisions (id, title, rationale, decision_maker, impact, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, title, rationale, decision_maker, impact, now),
        )
        conn.commit()
        return decision_id

    def get_decisions(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Get recent decisions.

        Args:
            limit: Maximum number of decisions to return.

        Returns:
            List of decision dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def record_meeting_prep(
        self,
        event_id: str,
        brief: str,
        created_by: str,
    ) -> str:
        """
        Record meeting preparation materials.

        Args:
            event_id: Calendar event ID.
            brief: Generated meeting brief.
            created_by: Agent that created the brief.

        Returns:
            Generated record ID.
        """
        prep_id = f"prep_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO meeting_prep (id, event_id, brief, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (prep_id, event_id, brief, created_by, now),
        )
        conn.commit()
        return prep_id

    def get_meeting_prep(self, event_id: str) -> Optional[dict[str, Any]]:
        """
        Get meeting preparation for a specific event.

        Args:
            event_id: Calendar event ID.

        Returns:
            Meeting prep dictionary or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM meeting_prep WHERE event_id = ? ORDER BY created_at DESC LIMIT 1",
            (event_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    # Telegram user methods

    def register_telegram_user(
        self,
        chat_id: str,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> None:
        """
        Register a Telegram user for notifications.

        Args:
            chat_id: Telegram chat ID.
            username: Optional Telegram username.
            first_name: Optional user's first name.
        """
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO telegram_users (chat_id, username, first_name, registered_at, is_active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                is_active = 1
            """,
            (chat_id, username, first_name, now),
        )
        conn.commit()

    def deactivate_telegram_user(self, chat_id: str) -> None:
        """
        Deactivate a Telegram user (stop notifications).

        Args:
            chat_id: Telegram chat ID.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE telegram_users SET is_active = 0 WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()

    def get_telegram_users(self, active_only: bool = True) -> list[dict[str, Any]]:
        """
        Get registered Telegram users.

        Args:
            active_only: If True, only return active users.

        Returns:
            List of user dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if active_only:
            cursor.execute("SELECT * FROM telegram_users WHERE is_active = 1")
        else:
            cursor.execute("SELECT * FROM telegram_users")

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_telegram_user(self, chat_id: str) -> Optional[dict[str, Any]]:
        """
        Get a specific Telegram user by chat ID.

        Args:
            chat_id: Telegram chat ID.

        Returns:
            User dictionary or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM telegram_users WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    # Discord session methods

    def start_discord_session(
        self,
        guild_id: str,
        channel_id: str,
        channel_name: Optional[str] = None,
    ) -> str:
        """
        Start a new Discord voice session.

        Args:
            guild_id: Discord guild (server) ID.
            channel_id: Discord voice channel ID.
            channel_name: Optional channel name.

        Returns:
            Generated session ID.
        """
        session_id = f"discord_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO discord_sessions (id, guild_id, channel_id, channel_name, started_at, status)
            VALUES (?, ?, ?, ?, ?, 'active')
            """,
            (session_id, guild_id, channel_id, channel_name, now),
        )
        conn.commit()
        return session_id

    def end_discord_session(
        self,
        session_id: str,
        transcript_path: Optional[str] = None,
    ) -> None:
        """
        End a Discord voice session.

        Args:
            session_id: Session ID to end.
            transcript_path: Optional path to transcript file.
        """
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE discord_sessions
            SET ended_at = ?, transcript_path = ?, status = 'ended'
            WHERE id = ?
            """,
            (now, transcript_path, session_id),
        )
        conn.commit()

    def get_active_discord_session(self) -> Optional[dict[str, Any]]:
        """
        Get the currently active Discord session.

        Returns:
            Session dictionary or None if no active session.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM discord_sessions WHERE status = 'active' ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_discord_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Get recent Discord sessions.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            List of session dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM discord_sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        """Close the database connection."""
        if self.connection:
            self.connection.close()
            self.connection = None
