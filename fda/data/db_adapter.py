"""
Database data adapter.

Pulls data from SQL databases using SQLAlchemy.
"""

import logging
from typing import Any, Optional
from datetime import datetime

from fda.data.base import DataAdapter

logger = logging.getLogger(__name__)


class DBAdapter(DataAdapter):
    """
    Adapter for pulling data from SQL databases.

    Supports SQLite, PostgreSQL, MySQL, and other SQLAlchemy-compatible databases.
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize the database adapter.

        Args:
            config: Configuration dictionary containing:
                   - connection_string: SQLAlchemy connection URL
                   - queries: Dict of metric names to SQL queries
                   - pool_size: Connection pool size (default 5)
                   - pool_timeout: Pool timeout in seconds (default 30)
        """
        self.config = config
        self.connection_string = config.get("connection_string", "")
        self.queries = config.get("queries", {})
        self.pool_size = config.get("pool_size", 5)
        self.pool_timeout = config.get("pool_timeout", 30)

        self.engine = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Initialize the database engine if not already done."""
        if self._initialized:
            return

        try:
            from sqlalchemy import create_engine

            engine_kwargs: dict[str, Any] = {}

            # SQLite doesn't support pool_size
            if not self.connection_string.startswith("sqlite"):
                engine_kwargs["pool_size"] = self.pool_size
                engine_kwargs["pool_timeout"] = self.pool_timeout

            self.engine = create_engine(self.connection_string, **engine_kwargs)
            self._initialized = True

        except ImportError:
            raise ImportError(
                "SQLAlchemy is required for DBAdapter. "
                "Install it with: pip install sqlalchemy"
            )

    def test_connection(self) -> bool:
        """
        Test connection to the database.

        Returns:
            True if database is accessible, False otherwise.
        """
        try:
            self._ensure_initialized()

            from sqlalchemy import text

            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                return True

        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False

    def pull_latest(self, metric: Optional[str] = None) -> dict[str, Any]:
        """
        Pull latest data from the database.

        Args:
            metric: Specific metric to pull, or None for all.

        Returns:
            Dictionary of metric names to values.
        """
        self._ensure_initialized()

        results: dict[str, Any] = {}

        queries_to_run = (
            {metric: self.queries[metric]}
            if metric and metric in self.queries
            else self.queries
        )

        for metric_name, query_config in queries_to_run.items():
            try:
                data = self._execute_query(query_config)
                results[metric_name] = data
            except Exception as e:
                logger.error(f"Failed to pull metric '{metric_name}': {e}")
                results[metric_name] = {"error": str(e)}

        results["_metadata"] = {
            "pulled_at": datetime.now().isoformat(),
            "metrics_pulled": len(queries_to_run),
        }

        return results

    def _execute_query(self, query_config: Any) -> Any:
        """
        Execute a query and return results.

        Args:
            query_config: Query string or config dict.

        Returns:
            Query results.
        """
        from sqlalchemy import text

        # Handle simple string query or complex config
        if isinstance(query_config, str):
            query = query_config
            params = {}
            result_type = "all"  # Return all rows
        else:
            query = query_config.get("query", "")
            params = query_config.get("params", {})
            result_type = query_config.get("result_type", "all")

        with self.engine.connect() as conn:
            result = conn.execute(text(query), params)

            if result_type == "scalar":
                # Return single value
                row = result.fetchone()
                return row[0] if row else None

            elif result_type == "one":
                # Return single row as dict
                row = result.fetchone()
                if row:
                    return dict(row._mapping)
                return None

            elif result_type == "all":
                # Return all rows as list of dicts
                rows = result.fetchall()
                return [dict(row._mapping) for row in rows]

            elif result_type == "column":
                # Return single column as list
                rows = result.fetchall()
                return [row[0] for row in rows]

            else:
                rows = result.fetchall()
                return [dict(row._mapping) for row in rows]

    def get_schema(self) -> dict[str, Any]:
        """
        Get the schema of available tables and columns.

        Returns:
            Dictionary describing available data.
        """
        self._ensure_initialized()

        from sqlalchemy import inspect

        schema: dict[str, Any] = {
            "connection_string": self._mask_connection_string(),
            "configured_queries": list(self.queries.keys()),
            "tables": {},
        }

        try:
            inspector = inspect(self.engine)

            for table_name in inspector.get_table_names():
                columns = []
                for column in inspector.get_columns(table_name):
                    columns.append({
                        "name": column["name"],
                        "type": str(column["type"]),
                        "nullable": column.get("nullable", True),
                    })

                # Get primary keys
                pk = inspector.get_pk_constraint(table_name)

                schema["tables"][table_name] = {
                    "columns": columns,
                    "primary_key": pk.get("constrained_columns", []) if pk else [],
                }

        except Exception as e:
            logger.error(f"Failed to retrieve schema: {e}")
            schema["error"] = str(e)

        return schema

    def _mask_connection_string(self) -> str:
        """Mask sensitive parts of the connection string."""
        if not self.connection_string:
            return ""

        # Simple masking - replace password if present
        if "@" in self.connection_string and "://" in self.connection_string:
            # Format: driver://user:password@host/db
            parts = self.connection_string.split("://", 1)
            if len(parts) == 2 and "@" in parts[1]:
                driver = parts[0]
                rest = parts[1]
                at_idx = rest.index("@")
                auth_part = rest[:at_idx]
                host_part = rest[at_idx:]

                if ":" in auth_part:
                    user = auth_part.split(":")[0]
                    return f"{driver}://{user}:****{host_part}"

        return self.connection_string

    def execute_raw(self, query: str, params: Optional[dict[str, Any]] = None) -> Any:
        """
        Execute a raw SQL query.

        Args:
            query: SQL query string.
            params: Optional parameters for the query.

        Returns:
            Query results as list of dicts.
        """
        self._ensure_initialized()

        from sqlalchemy import text

        with self.engine.connect() as conn:
            result = conn.execute(text(query), params or {})

            if result.returns_rows:
                rows = result.fetchall()
                return [dict(row._mapping) for row in rows]

            return {"rows_affected": result.rowcount}

    def get_table_sample(
        self, table_name: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """
        Get a sample of rows from a table.

        Args:
            table_name: Name of the table.
            limit: Maximum number of rows to return.

        Returns:
            List of row dictionaries.
        """
        # Validate table name to prevent SQL injection
        if not table_name.replace("_", "").isalnum():
            raise ValueError(f"Invalid table name: {table_name}")

        query = f"SELECT * FROM {table_name} LIMIT :limit"
        return self.execute_raw(query, {"limit": limit})

    def get_row_count(self, table_name: str) -> int:
        """
        Get the row count for a table.

        Args:
            table_name: Name of the table.

        Returns:
            Number of rows in the table.
        """
        # Validate table name
        if not table_name.replace("_", "").isalnum():
            raise ValueError(f"Invalid table name: {table_name}")

        query = f"SELECT COUNT(*) FROM {table_name}"
        result = self._execute_query({"query": query, "result_type": "scalar"})
        return int(result) if result else 0

    def close(self) -> None:
        """Close the database connection and dispose of the engine."""
        if self.engine:
            self.engine.dispose()
            self.engine = None
            self._initialized = False


class SQLiteAdapter(DBAdapter):
    """
    Convenience adapter for SQLite databases.
    """

    def __init__(self, db_path: str, queries: Optional[dict[str, Any]] = None):
        """
        Initialize SQLite adapter.

        Args:
            db_path: Path to the SQLite database file.
            queries: Optional dict of metric names to SQL queries.
        """
        super().__init__({
            "connection_string": f"sqlite:///{db_path}",
            "queries": queries or {},
        })


class PostgreSQLAdapter(DBAdapter):
    """
    Convenience adapter for PostgreSQL databases.
    """

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 5432,
        queries: Optional[dict[str, Any]] = None,
    ):
        """
        Initialize PostgreSQL adapter.

        Args:
            host: Database host.
            database: Database name.
            user: Username.
            password: Password.
            port: Port number (default 5432).
            queries: Optional dict of metric names to SQL queries.
        """
        connection_string = (
            f"postgresql://{user}:{password}@{host}:{port}/{database}"
        )
        super().__init__({
            "connection_string": connection_string,
            "queries": queries or {},
        })


class MySQLAdapter(DBAdapter):
    """
    Convenience adapter for MySQL databases.
    """

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        queries: Optional[dict[str, Any]] = None,
    ):
        """
        Initialize MySQL adapter.

        Args:
            host: Database host.
            database: Database name.
            user: Username.
            password: Password.
            port: Port number (default 3306).
            queries: Optional dict of metric names to SQL queries.
        """
        connection_string = (
            f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
        )
        super().__init__({
            "connection_string": connection_string,
            "queries": queries or {},
        })
