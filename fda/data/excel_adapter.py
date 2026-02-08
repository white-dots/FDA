"""
Excel/CSV data adapter.

Watches a directory for Excel and CSV files and reads data using pandas.
"""

import logging
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

import pandas as pd

from fda.data.base import DataAdapter

logger = logging.getLogger(__name__)


class ExcelAdapter(DataAdapter):
    """
    Adapter for reading data from Excel and CSV files.

    Watches a directory for .xlsx, .xls, and .csv files and reads
    them using pandas for processing.
    """

    def __init__(self, watch_dir: Path, config: Optional[dict[str, Any]] = None):
        """
        Initialize the Excel adapter.

        Args:
            watch_dir: Directory to watch for Excel/CSV files.
            config: Optional configuration dictionary with:
                   - file_pattern: Glob pattern for files to watch (default "*.xlsx")
                   - sheet_name: Sheet name for Excel files (default 0, first sheet)
                   - metric_columns: Dict mapping metric names to column names
                   - date_column: Column to use for date filtering
                   - aggregate: Aggregation method ("latest", "sum", "mean", "count")
        """
        self.watch_dir = Path(watch_dir)
        self.config = config or {}
        self.file_pattern = self.config.get("file_pattern", "*.xlsx")
        self.sheet_name = self.config.get("sheet_name", 0)
        self.metric_columns = self.config.get("metric_columns", {})
        self.date_column = self.config.get("date_column")
        self.aggregate = self.config.get("aggregate", "latest")

    def test_connection(self) -> bool:
        """
        Test that the watch directory is accessible.

        Returns:
            True if directory exists and is readable.
        """
        try:
            if not self.watch_dir.exists():
                logger.error(f"Watch directory does not exist: {self.watch_dir}")
                return False

            if not self.watch_dir.is_dir():
                logger.error(f"Watch path is not a directory: {self.watch_dir}")
                return False

            # Try to list files to verify read access
            list(self.watch_dir.iterdir())
            return True

        except PermissionError as e:
            logger.error(f"Permission denied accessing {self.watch_dir}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error accessing watch directory: {e}")
            return False

    def pull_latest(self, metric: Optional[str] = None) -> dict[str, Any]:
        """
        Pull latest data from Excel/CSV files in the watch directory.

        Args:
            metric: Specific metric to pull, or None for all.

        Returns:
            Dictionary of metric names to values from the files.
        """
        results: dict[str, Any] = {}

        # Get the latest files
        files = self._get_latest_files()
        if not files:
            logger.warning(f"No files matching '{self.file_pattern}' in {self.watch_dir}")
            return results

        # Read and combine data from files
        all_data: list[pd.DataFrame] = []
        for filepath in files:
            try:
                df = self._read_file(filepath)
                df["_source_file"] = filepath.name
                all_data.append(df)
            except Exception as e:
                logger.error(f"Failed to read {filepath}: {e}")

        if not all_data:
            return results

        # Combine all dataframes
        combined_df = pd.concat(all_data, ignore_index=True)

        # Extract metrics
        if metric and metric in self.metric_columns:
            column = self.metric_columns[metric]
            results[metric] = self._extract_metric(combined_df, column)
        elif self.metric_columns:
            for metric_name, column in self.metric_columns.items():
                results[metric_name] = self._extract_metric(combined_df, column)
        else:
            # If no metric columns configured, return summary stats for numeric columns
            for column in combined_df.select_dtypes(include=["number"]).columns:
                if not column.startswith("_"):
                    results[column] = self._extract_metric(combined_df, column)

        # Add metadata
        results["_metadata"] = {
            "files_processed": len(files),
            "total_rows": len(combined_df),
            "latest_file": files[0].name if files else None,
            "pulled_at": datetime.now().isoformat(),
        }

        return results

    def _extract_metric(self, df: pd.DataFrame, column: str) -> Any:
        """
        Extract a metric value from a dataframe column.

        Args:
            df: The dataframe.
            column: Column name to extract.

        Returns:
            The extracted metric value based on aggregation method.
        """
        if column not in df.columns:
            return {"error": f"Column '{column}' not found"}

        series = df[column].dropna()

        if series.empty:
            return None

        if self.aggregate == "latest":
            # If date column exists, sort by it and get last value
            if self.date_column and self.date_column in df.columns:
                sorted_df = df.dropna(subset=[column]).sort_values(
                    self.date_column, ascending=False
                )
                if not sorted_df.empty:
                    return sorted_df.iloc[0][column]
            return series.iloc[-1]

        elif self.aggregate == "sum":
            return float(series.sum())

        elif self.aggregate == "mean":
            return float(series.mean())

        elif self.aggregate == "count":
            return int(series.count())

        elif self.aggregate == "min":
            return float(series.min())

        elif self.aggregate == "max":
            return float(series.max())

        elif self.aggregate == "all":
            return series.tolist()

        else:
            return series.iloc[-1]

    def get_schema(self) -> dict[str, Any]:
        """
        Get the schema of available data from files.

        Returns:
            Dictionary describing available columns and types.
        """
        schema: dict[str, Any] = {
            "watch_dir": str(self.watch_dir),
            "file_pattern": self.file_pattern,
            "configured_metrics": self.metric_columns,
            "columns": {},
        }

        # Try to read the latest file to get column info
        files = self._get_latest_files()
        if files:
            try:
                df = self._read_file(files[0])
                for column in df.columns:
                    schema["columns"][column] = {
                        "dtype": str(df[column].dtype),
                        "non_null_count": int(df[column].count()),
                        "sample_values": df[column].dropna().head(3).tolist(),
                    }
            except Exception as e:
                logger.error(f"Failed to read schema from {files[0]}: {e}")

        return schema

    def _get_latest_files(self, limit: int = 10) -> list[Path]:
        """
        Get the latest files matching the pattern.

        Args:
            limit: Maximum number of files to return.

        Returns:
            List of file paths sorted by modification time (newest first).
        """
        if not self.watch_dir.exists():
            return []

        # Handle multiple patterns
        patterns = (
            self.file_pattern.split(",")
            if "," in self.file_pattern
            else [self.file_pattern]
        )

        all_files: list[Path] = []
        for pattern in patterns:
            pattern = pattern.strip()
            all_files.extend(self.watch_dir.glob(pattern))

        # Also check for CSV files if xlsx is specified
        if "*.xlsx" in patterns and "*.csv" not in patterns:
            all_files.extend(self.watch_dir.glob("*.csv"))

        # Sort by modification time (newest first)
        all_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        return all_files[:limit]

    def _read_file(self, filepath: Path) -> pd.DataFrame:
        """
        Read a single Excel or CSV file.

        Args:
            filepath: Path to the file to read.

        Returns:
            Pandas DataFrame with the file contents.
        """
        suffix = filepath.suffix.lower()

        if suffix == ".csv":
            return pd.read_csv(filepath)

        elif suffix in (".xlsx", ".xls"):
            return pd.read_excel(
                filepath,
                sheet_name=self.sheet_name,
                engine="openpyxl" if suffix == ".xlsx" else None,
            )

        else:
            raise ValueError(f"Unsupported file type: {suffix}")

    def read_all_sheets(self, filepath: Path) -> dict[str, pd.DataFrame]:
        """
        Read all sheets from an Excel file.

        Args:
            filepath: Path to the Excel file.

        Returns:
            Dictionary mapping sheet names to DataFrames.
        """
        if filepath.suffix.lower() not in (".xlsx", ".xls"):
            raise ValueError("read_all_sheets only works with Excel files")

        return pd.read_excel(filepath, sheet_name=None)

    def get_file_summary(self) -> dict[str, Any]:
        """
        Get a summary of files in the watch directory.

        Returns:
            Dictionary with file statistics.
        """
        files = self._get_latest_files(limit=100)

        if not files:
            return {
                "total_files": 0,
                "watch_dir": str(self.watch_dir),
                "pattern": self.file_pattern,
            }

        file_info = []
        for f in files[:10]:
            stat = f.stat()
            file_info.append({
                "name": f.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

        return {
            "total_files": len(files),
            "watch_dir": str(self.watch_dir),
            "pattern": self.file_pattern,
            "latest_files": file_info,
        }

    def watch_for_changes(
        self,
        callback: Optional[callable] = None,
        poll_interval: float = 5.0,
    ) -> None:
        """
        Watch the directory for file changes.

        Note: This is a simple polling-based implementation.
        For production use, consider using watchdog library.

        Args:
            callback: Function to call when changes detected.
            poll_interval: Seconds between checks.
        """
        import time

        last_files: dict[str, float] = {}

        # Get initial state
        for f in self._get_latest_files(limit=100):
            last_files[str(f)] = f.stat().st_mtime

        logger.info(f"Watching {self.watch_dir} for changes...")

        try:
            while True:
                time.sleep(poll_interval)

                current_files: dict[str, float] = {}
                for f in self._get_latest_files(limit=100):
                    current_files[str(f)] = f.stat().st_mtime

                # Check for new or modified files
                for path, mtime in current_files.items():
                    if path not in last_files:
                        logger.info(f"New file detected: {path}")
                        if callback:
                            callback("new", Path(path))
                    elif mtime > last_files[path]:
                        logger.info(f"Modified file detected: {path}")
                        if callback:
                            callback("modified", Path(path))

                # Check for deleted files
                for path in last_files:
                    if path not in current_files:
                        logger.info(f"Deleted file detected: {path}")
                        if callback:
                            callback("deleted", Path(path))

                last_files = current_files

        except KeyboardInterrupt:
            logger.info("Stopped watching for changes")
