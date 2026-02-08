"""
Abstract base class for data adapters.

All data sources (APIs, databases, files) should implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class DataAdapter(ABC):
    """
    Abstract base class for data adapters.
    
    Adapters provide a unified interface for pulling metrics and data
    from various sources (APIs, databases, files).
    """

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Test that the data source is accessible.
        
        Returns:
            True if connection successful, False otherwise.
        """
        pass

    @abstractmethod
    def pull_latest(self, metric: Optional[str] = None) -> dict[str, Any]:
        """
        Pull the latest data from the source.
        
        Args:
            metric: Specific metric to pull, or None for all available.
            
        Returns:
            Dictionary of metric names to values.
        """
        pass

    @abstractmethod
    def get_schema(self) -> dict[str, Any]:
        """
        Get the schema of available metrics.
        
        Returns:
            Dictionary describing available metrics and their types.
        """
        pass
