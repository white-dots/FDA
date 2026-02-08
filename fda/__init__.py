"""
FDA Multi-Agent System for Project Coordination.

This package provides a distributed multi-agent system for managing
project delivery, including FDA (Facilitating Director Agent) oversight,
task execution, and knowledge management.
"""

__version__ = "0.1.0"
__author__ = "Jae Heuk Jung"
__description__ = "FDA Multi-Agent System for Project Coordination"

from fda.base_agent import BaseAgent
from fda.fda_agent import FDAAgent
from fda.executor_agent import ExecutorAgent
from fda.librarian_agent import LibrarianAgent

__all__ = [
    "BaseAgent",
    "FDAAgent",
    "ExecutorAgent",
    "LibrarianAgent",
]
