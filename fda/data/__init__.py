"""
Data adapter module for connecting to various data sources.

Provides a pluggable interface for pulling metrics and data from
APIs, databases, and files.
"""

from fda.data.base import DataAdapter
from fda.data.api_adapter import APIAdapter
from fda.data.excel_adapter import ExcelAdapter
from fda.data.db_adapter import DBAdapter, SQLiteAdapter, PostgreSQLAdapter, MySQLAdapter

__all__ = [
    "DataAdapter",
    "APIAdapter",
    "ExcelAdapter",
    "DBAdapter",
    "SQLiteAdapter",
    "PostgreSQLAdapter",
    "MySQLAdapter",
]
