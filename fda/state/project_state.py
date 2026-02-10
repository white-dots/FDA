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

        # File index table - Librarian's file tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS file_index (
                id TEXT PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                extension TEXT,
                size INTEGER,
                modified_at TEXT,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                summary TEXT,
                tags TEXT
            )
        """)

        # Discoveries table - agent exploration findings
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS discoveries (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                discovery_type TEXT NOT NULL,
                description TEXT NOT NULL,
                details TEXT,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Agent status table - peer agent health tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_status (
                agent_name TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'stopped',
                last_heartbeat TIMESTAMP,
                current_task TEXT
            )
        """)

        # Code routes table - Librarian's routing system for code structure
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS code_routes (
                id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                route_type TEXT NOT NULL,
                name TEXT NOT NULL,
                line_number INTEGER,
                signature TEXT,
                docstring TEXT,
                keywords TEXT,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_path) REFERENCES file_index(path)
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_index_ext ON file_index(extension)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_discoveries_agent ON discoveries(agent)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_discoveries_type ON discoveries(discovery_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_routes_type ON code_routes(route_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_routes_name ON code_routes(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code_routes_file ON code_routes(file_path)")

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

    # File index methods (for Librarian)

    def add_file_to_index(
        self,
        path: str,
        extension: Optional[str] = None,
        size: Optional[int] = None,
        modified_at: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> str:
        """
        Add or update a file in the index.

        Args:
            path: Absolute file path.
            extension: File extension (e.g., 'py', 'md').
            size: File size in bytes.
            modified_at: File modification timestamp.
            summary: AI-generated file summary.
            tags: List of tags for categorization.

        Returns:
            Generated file index ID.
        """
        file_id = f"file_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        tags_json = json.dumps(tags) if tags else None
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO file_index (id, path, extension, size, modified_at, indexed_at, summary, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                extension = excluded.extension,
                size = excluded.size,
                modified_at = excluded.modified_at,
                indexed_at = excluded.indexed_at,
                summary = excluded.summary,
                tags = excluded.tags
            """,
            (file_id, path, extension, size, modified_at, now, summary, tags_json),
        )
        conn.commit()
        return file_id

    def get_file_from_index(self, path: str) -> Optional[dict[str, Any]]:
        """
        Get a file from the index by path.

        Args:
            path: File path to look up.

        Returns:
            File index entry or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM file_index WHERE path = ?", (path,))
        row = cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        if result.get("tags"):
            result["tags"] = json.loads(result["tags"])
        return result

    def search_file_index(
        self,
        extension: Optional[str] = None,
        tags: Optional[list[str]] = None,
        path_pattern: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Search the file index.

        Args:
            extension: Filter by file extension.
            tags: Filter by tags (any match).
            path_pattern: Filter by path pattern (SQL LIKE).
            limit: Maximum results to return.

        Returns:
            List of matching file index entries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM file_index WHERE 1=1"
        params: list[Any] = []

        if extension:
            query += " AND extension = ?"
            params.append(extension)
        if path_pattern:
            query += " AND path LIKE ?"
            params.append(path_pattern)

        query += " ORDER BY indexed_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        results = []
        for row in rows:
            entry = dict(row)
            if entry.get("tags"):
                entry["tags"] = json.loads(entry["tags"])
            # Filter by tags if specified (needs to be done in Python for JSON)
            if tags:
                entry_tags = entry.get("tags") or []
                if not any(t in entry_tags for t in tags):
                    continue
            results.append(entry)

        return results

    def remove_file_from_index(self, path: str) -> None:
        """
        Remove a file from the index.

        Args:
            path: File path to remove.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM file_index WHERE path = ?", (path,))
        conn.commit()

    def get_file_index_stats(self) -> dict[str, Any]:
        """
        Get statistics about the file index.

        Returns:
            Dictionary with counts by extension and total files.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Total count
        cursor.execute("SELECT COUNT(*) as total FROM file_index")
        total = cursor.fetchone()["total"]

        # Count by extension
        cursor.execute(
            "SELECT extension, COUNT(*) as count FROM file_index GROUP BY extension ORDER BY count DESC"
        )
        by_extension = {row["extension"] or "none": row["count"] for row in cursor.fetchall()}

        return {"total": total, "by_extension": by_extension}

    # Code routes methods (for routing system)

    def add_code_route(
        self,
        file_path: str,
        route_type: str,
        name: str,
        line_number: Optional[int] = None,
        signature: Optional[str] = None,
        docstring: Optional[str] = None,
        keywords: Optional[list[str]] = None,
    ) -> str:
        """
        Add a code route (function, class, endpoint, etc.) to the routing index.

        Args:
            file_path: Path to the file containing the route.
            route_type: Type of route (function, class, method, endpoint, handler).
            name: Name of the function/class/endpoint.
            line_number: Line number in the file.
            signature: Function/method signature.
            docstring: Documentation string.
            keywords: Keywords for search indexing.

        Returns:
            Generated route ID.
        """
        route_id = f"route_{uuid.uuid4().hex[:8]}"
        keywords_json = json.dumps(keywords) if keywords else None
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO code_routes
            (id, file_path, route_type, name, line_number, signature, docstring, keywords, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (route_id, file_path, route_type, name, line_number, signature,
             docstring[:500] if docstring else None, keywords_json, datetime.now().isoformat()),
        )
        conn.commit()
        return route_id

    def search_code_routes(
        self,
        query: str,
        route_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Search code routes by name, keywords, or docstring.

        Args:
            query: Search query (searches name, keywords, docstring).
            route_type: Filter by route type.
            limit: Maximum results.

        Returns:
            List of matching code routes.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        sql = """
            SELECT * FROM code_routes
            WHERE (name LIKE ? OR keywords LIKE ? OR docstring LIKE ?)
        """
        params: list[Any] = [f"%{query}%", f"%{query}%", f"%{query}%"]

        if route_type:
            sql += " AND route_type = ?"
            params.append(route_type)

        sql += " ORDER BY name LIMIT ?"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()

        results = []
        for row in rows:
            entry = dict(row)
            if entry.get("keywords"):
                entry["keywords"] = json.loads(entry["keywords"])
            results.append(entry)

        return results

    def get_routes_for_file(self, file_path: str) -> list[dict[str, Any]]:
        """
        Get all code routes in a specific file.

        Args:
            file_path: Path to the file.

        Returns:
            List of code routes in the file.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM code_routes WHERE file_path = ? ORDER BY line_number",
            (file_path,)
        )
        rows = cursor.fetchall()

        results = []
        for row in rows:
            entry = dict(row)
            if entry.get("keywords"):
                entry["keywords"] = json.loads(entry["keywords"])
            results.append(entry)

        return results

    def get_code_routes_stats(self) -> dict[str, Any]:
        """
        Get statistics about the code routes index.

        Returns:
            Dictionary with counts by type and total routes.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM code_routes")
        total = cursor.fetchone()["total"]

        cursor.execute(
            "SELECT route_type, COUNT(*) as count FROM code_routes GROUP BY route_type ORDER BY count DESC"
        )
        by_type = {row["route_type"]: row["count"] for row in cursor.fetchall()}

        return {"total": total, "by_type": by_type}

    def clear_routes_for_file(self, file_path: str) -> int:
        """
        Clear all routes for a file (used before re-indexing).

        Args:
            file_path: Path to the file.

        Returns:
            Number of routes deleted.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM code_routes WHERE file_path = ?", (file_path,))
        deleted = cursor.rowcount
        conn.commit()
        return deleted

    # Discovery methods (for agent exploration findings)

    def add_discovery(
        self,
        agent: str,
        discovery_type: str,
        description: str,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Record a discovery made by an agent.

        Args:
            agent: Agent name that made the discovery.
            discovery_type: Type of discovery (file, pattern, tool, capability).
            description: Human-readable description.
            details: Additional details as dictionary.

        Returns:
            Generated discovery ID.
        """
        discovery_id = f"disc_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat()
        details_json = json.dumps(details) if details else None
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO discoveries (id, agent, discovery_type, description, details, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (discovery_id, agent, discovery_type, description, details_json, now),
        )
        conn.commit()
        return discovery_id

    def get_discoveries(
        self,
        agent: Optional[str] = None,
        discovery_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get discoveries, optionally filtered.

        Args:
            agent: Filter by agent name.
            discovery_type: Filter by discovery type.
            limit: Maximum results to return.

        Returns:
            List of discovery entries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM discoveries WHERE 1=1"
        params: list[Any] = []

        if agent:
            query += " AND agent = ?"
            params.append(agent)
        if discovery_type:
            query += " AND discovery_type = ?"
            params.append(discovery_type)

        query += " ORDER BY discovered_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        results = []
        for row in rows:
            entry = dict(row)
            if entry.get("details"):
                entry["details"] = json.loads(entry["details"])
            results.append(entry)

        return results

    # Agent status methods (for peer coordination)

    def update_agent_status(
        self,
        agent_name: str,
        status: str,
        current_task: Optional[str] = None,
    ) -> None:
        """
        Update an agent's status.

        Args:
            agent_name: Name of the agent.
            status: Current status (running, stopped, exploring, busy).
            current_task: Optional description of current task.
        """
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_status (agent_name, status, last_heartbeat, current_task)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent_name) DO UPDATE SET
                status = excluded.status,
                last_heartbeat = excluded.last_heartbeat,
                current_task = excluded.current_task
            """,
            (agent_name, status, now, current_task),
        )
        conn.commit()

    def get_agent_status(self, agent_name: str) -> Optional[dict[str, Any]]:
        """
        Get an agent's current status.

        Args:
            agent_name: Name of the agent.

        Returns:
            Agent status dictionary or None if not found.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_status WHERE agent_name = ?", (agent_name,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_all_agent_statuses(self) -> list[dict[str, Any]]:
        """
        Get status of all agents.

        Returns:
            List of agent status dictionaries.
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_status ORDER BY agent_name")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def agent_heartbeat(self, agent_name: str) -> None:
        """
        Update an agent's heartbeat timestamp.

        Args:
            agent_name: Name of the agent.
        """
        now = datetime.now().isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE agent_status SET last_heartbeat = ? WHERE agent_name = ?
            """,
            (now, agent_name),
        )
        conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self.connection:
            self.connection.close()
            self.connection = None
