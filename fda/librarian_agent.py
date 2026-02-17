"""
Librarian Agent implementation.

The Librarian agent is a PEER agent that manages knowledge and discovery.
It explores the file system, indexes documents, generates reports,
and maintains the project journal.

As a peer to FDA and Executor, it collaborates via the message bus
without hierarchy - responding to requests and sharing discoveries.
"""

import logging
import time
import subprocess
import os
import re
import json
import ast
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timedelta

from fda.base_agent import BaseAgent
import math
from collections import Counter

from fda.config import MODEL_LIBRARIAN, DEFAULT_CHECK_INTERVAL_MINUTES, PROJECT_ROOT
from fda.comms.message_bus import MessageTypes, Agents

logger = logging.getLogger(__name__)


LIBRARIAN_SYSTEM_PROMPT = """You are the Librarian Agent - a PEER in a multi-agent system.

You work alongside FDA (user interface) and Executor (actions) as equals.
No one bosses anyone - you collaborate through requests and shared knowledge.

Your domain is KNOWLEDGE & DISCOVERY:
1. **File System Exploration**: Use grep, find, ls to explore the computer
2. **Indexing**: Track interesting files (docs, configs, code) in the file index
3. **Knowledge Management**: Maintain and organize project documentation
4. **Information Retrieval**: Help find files, patterns, and historical information
5. **Report Generation**: Create reports when requested

You have access to the user's file system and can run shell commands to explore.
When you discover something interesting, share it with your peers.

When responding to search/knowledge requests:
- Be thorough but concise
- Include file paths and line numbers where relevant
- Organize results logically
- Suggest related searches if appropriate

Remember: You're a helpful peer, not a subordinate. You can suggest actions
and share discoveries proactively with FDA.
"""


class LibrarianAgent(BaseAgent):
    """
    Librarian Agent for knowledge management and file exploration.

    As a PEER agent, the Librarian:
    - Explores the file system using grep, find, ls
    - Indexes documents, configs, and code files
    - Manages the project journal
    - Responds to knowledge requests from peers
    - Shares discoveries proactively
    """

    # File extensions of interest for indexing
    INTERESTING_EXTENSIONS = [
        "py", "js", "ts", "go", "rs", "java", "c", "cpp", "h",  # Code
        "md", "txt", "rst", "adoc",                              # Docs
        "json", "yaml", "yml", "toml", "ini", "cfg",            # Config
        "sql", "sh", "bash", "zsh",                              # Scripts/DB
        "html", "css", "scss",                                   # Web
    ]

    # Directories to skip during exploration
    SKIP_DIRS = [
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        ".idea", ".vscode", "dist", "build", ".cache", ".pytest_cache",
        ".eggs", "*.egg-info", "site-packages", ".tox", ".mypy_cache",
    ]

    # Default exploration depth (deep enough to index most projects)
    DEFAULT_EXPLORATION_DEPTH = 6

    # Max files to index per extension (increase for thorough indexing)
    MAX_FILES_PER_EXTENSION = 500

    # Default folders to explore (relative to home directory)
    DEFAULT_EXPLORATION_FOLDERS = ["Desktop", "Downloads", "Documents"]

    def __init__(
        self,
        project_state_path: Optional[Path] = None,
        exploration_roots: Optional[list[str]] = None,
        exploration_depth: int = DEFAULT_EXPLORATION_DEPTH,
    ):
        """
        Initialize the Librarian agent.

        Args:
            project_state_path: Path to the project state database.
            exploration_roots: List of root paths for file system exploration.
                              Defaults to ~/Desktop, ~/Downloads, ~/Documents.
            exploration_depth: How deep to explore directories (default 6).
        """
        super().__init__(
            name="Librarian",
            model=MODEL_LIBRARIAN,
            system_prompt=LIBRARIAN_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )
        # Use default folders if not specified
        if exploration_roots:
            self.exploration_roots = exploration_roots
        else:
            home = Path.home()
            self.exploration_roots = [
                str(home / folder)
                for folder in self.DEFAULT_EXPLORATION_FOLDERS
                if (home / folder).exists()
            ]
        self.exploration_depth = exploration_depth
        self._exploration_complete = False
        self._routing_complete = False
        self._project_knowledge_complete = False

    def run_event_loop(self) -> None:
        """
        Run the main event loop for the Librarian.

        As a peer agent:
        1. On startup, explore the file system and index interesting files
        2. Build a routing system (index functions, classes, endpoints)
        3. Continuously process requests from peer agents
        4. Share discoveries proactively
        """
        logger.info("[Librarian] Starting event loop...")
        logger.info(f"[Librarian] Exploration roots: {self.exploration_roots}")
        logger.info(f"[Librarian] Exploration depth: {self.exploration_depth}")

        # Update agent status
        self.state.update_agent_status(self.name.lower(), "running", "Starting up")

        # Run initial exploration if not done
        if not self._exploration_complete:
            self.state.update_agent_status(self.name.lower(), "exploring", "Initial file exploration")
            try:
                print(f"[Librarian] Starting file exploration...")
                print(f"[Librarian] Folders to explore: {', '.join(self.exploration_roots)}")
                print(f"[Librarian] This may take a while for depth {self.exploration_depth}...")

                # Explore each root folder
                total_stats = {"files_found": 0, "files_indexed": 0, "errors": 0}
                for root in self.exploration_roots:
                    if os.path.exists(root):
                        print(f"[Librarian] Exploring: {root}")
                        stats = self.explore_filesystem(root, max_depth=self.exploration_depth)
                        total_stats["files_found"] += stats.get("files_found", 0)
                        total_stats["files_indexed"] += stats.get("files_indexed", 0)
                        total_stats["errors"] += stats.get("errors", 0)
                    else:
                        print(f"[Librarian] Skipping (not found): {root}")

                self._exploration_complete = True
                print(f"[Librarian] File exploration complete! Total: {total_stats['files_indexed']} files indexed")
            except Exception as e:
                logger.error(f"[Librarian] Exploration error: {e}")
                print(f"[Librarian] Exploration error: {e}")

        # Build routing system after file exploration
        if not self._routing_complete and self._exploration_complete:
            self.state.update_agent_status(self.name.lower(), "routing", "Building code routing system")
            try:
                print("[Librarian] Building code routing system...")
                self.build_routing_system()
                self._routing_complete = True
                print("[Librarian] Routing system complete!")
            except Exception as e:
                logger.error(f"[Librarian] Routing system error: {e}")
                print(f"[Librarian] Routing system error: {e}")

        # Build project knowledge base after routing system
        if self._routing_complete and not self._project_knowledge_complete:
            self.state.update_agent_status(self.name.lower(), "analyzing", "Building project knowledge base")
            try:
                print("[Librarian] Discovering and analyzing projects...")
                projects = self.discover_projects()
                for project in projects:
                    self.build_project_knowledge(project["path"])
                self._project_knowledge_complete = True
                print(f"[Librarian] Project knowledge base complete! {len(projects)} projects analyzed")
            except Exception as e:
                logger.error(f"[Librarian] Project knowledge error: {e}")
                print(f"[Librarian] Project knowledge error: {e}")
                self._project_knowledge_complete = True  # Don't retry on error

        # Check for messages frequently (every 1 second) for responsive inter-agent comms
        # Maintenance tasks run less frequently
        message_check_interval = 1  # Check messages every 1 second
        maintenance_interval = 300  # Run maintenance every 5 minutes
        last_maintenance = time.time()

        print("[Librarian] Ready and listening for requests...")

        while self._running:
            try:
                # Heartbeat
                self.state.agent_heartbeat(self.name.lower())
                self.state.update_agent_status(self.name.lower(), "running")

                # Process pending messages from peers (check frequently!)
                messages = self.get_pending_messages()
                for message in messages:
                    self._handle_message(message)

                # Periodic maintenance tasks (run less frequently)
                if time.time() - last_maintenance > maintenance_interval:
                    self._run_maintenance()
                    last_maintenance = time.time()

                time.sleep(message_check_interval)

            except KeyboardInterrupt:
                logger.info("[Librarian] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[Librarian] Error in event loop: {e}")
                time.sleep(60)

        self.state.update_agent_status(self.name.lower(), "stopped")
        logger.info("[Librarian] Event loop stopped")

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message from a peer agent."""
        msg_type = message.get("type", "")
        subject = message.get("subject", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")

        logger.info(f"[Librarian] Received {msg_type} from {from_agent}: {subject}")
        self.message_bus.mark_read(msg_id)

        # Update status while processing
        self.state.update_agent_status(self.name.lower(), "busy", f"Processing {msg_type}")

        try:
            if msg_type == MessageTypes.SEARCH_REQUEST:
                self._handle_search_request(message)

            elif msg_type == MessageTypes.INDEX_REQUEST:
                self._handle_index_request(message)

            elif msg_type == MessageTypes.KNOWLEDGE_REQUEST:
                self._handle_knowledge_request(message)

            elif msg_type == MessageTypes.STATUS_REQUEST:
                self._handle_status_request(message)

            # Legacy message types for backward compatibility
            elif msg_type == "report_request":
                report_type = body or "daily"
                report = self.generate_report(report_type)
                self.send_message(
                    to_agent=from_agent,
                    msg_type="report",
                    subject=f"{report_type.title()} Report",
                    body=report,
                )

            elif msg_type == "meeting_brief_request":
                event = {"id": body, "subject": subject}
                brief = self.generate_meeting_brief(event)
                self.send_message(
                    to_agent=from_agent,
                    msg_type="meeting_brief",
                    subject=f"Brief: {subject}",
                    body=brief,
                )

            elif msg_type == "search_request":
                # Legacy search (journal only)
                results = self.search_journal(body, top_n=5)
                response = self._format_search_results(results)
                self.send_message(
                    to_agent=from_agent,
                    msg_type="search_results",
                    subject=f"Search: {body}",
                    body=response,
                )

        except Exception as e:
            logger.error(f"[Librarian] Error handling message: {e}")
            # Send error response
            self.message_bus.send_result(
                from_agent=self.name.lower(),
                to_agent=from_agent.lower(),
                msg_type=MessageTypes.SEARCH_RESULT,
                result=None,
                success=False,
                error=str(e),
                reply_to=msg_id,
            )

        # Reset status
        self.state.update_agent_status(self.name.lower(), "running")

    def _handle_search_request(self, message: dict[str, Any]) -> None:
        """Handle a search request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        body = message.get("body", "")

        # Parse the request
        default_path = self.exploration_roots[0] if self.exploration_roots else str(Path.home())
        explicit_path_provided = False
        try:
            request = json.loads(body)
            query = request.get("query", "")
            # Use first exploration root as default search path
            # Note: request.get("path") returns None when path is JSON null,
            # so we need an explicit fallback with `or`
            raw_path = request.get("path")
            if raw_path and os.path.exists(raw_path):
                path = raw_path
                explicit_path_provided = True
                logger.info(f"[Librarian] Using explicit path from FDA: {path}")
            else:
                path = default_path
            search_type = request.get("search_type", "smart")  # smart, routes, files, journal
        except (json.JSONDecodeError, TypeError):
            # Body is just the query string
            query = body
            path = default_path
            search_type = "smart"

        logger.info(f"[Librarian] Searching for: {query} (type: {search_type}, path: {path})")

        # Execute appropriate search
        if search_type == "routes":
            # Search code routes only
            route_results = self.search_routes(query)
            results = {
                "query": query,
                "routes": route_results,
                "summary": f"Found {len(route_results)} code routes matching '{query}'",
            }
        elif search_type == "files":
            # Search file index only
            file_results = self.state.search_file_index(query=query, limit=20)
            results = {
                "query": query,
                "files": file_results,
                "summary": f"Found {len(file_results)} files matching '{query}'",
            }
        elif search_type == "journal":
            # Search journal only
            journal_results = self.search_journal(query, top_n=10)
            results = {
                "query": query,
                "journal": journal_results,
                "summary": f"Found {len(journal_results)} journal entries matching '{query}'",
            }
        else:
            # Smart search: search everything
            results = self._smart_search(query, path, explicit_path=explicit_path_provided)

            # Also search code routes
            route_results = self.search_routes(query)
            if route_results:
                results["routes"] = route_results
                results["summary"] += f" + {len(route_results)} code routes"

        # Send results back
        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.SEARCH_RESULT,
            result=results,
            success=True,
            reply_to=msg_id,
        )

    def _handle_index_request(self, message: dict[str, Any]) -> None:
        """Handle an index request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        body = message.get("body", "")

        try:
            request = json.loads(body)
            file_path = request.get("path", "")
        except (json.JSONDecodeError, TypeError):
            file_path = body

        if file_path and os.path.exists(file_path):
            self.index_file(file_path)
            result = f"Indexed: {file_path}"
            success = True
        else:
            result = f"File not found: {file_path}"
            success = False

        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.INDEX_COMPLETE,
            result=result,
            success=success,
            reply_to=msg_id,
        )

    def _handle_knowledge_request(self, message: dict[str, Any]) -> None:
        """Handle a knowledge/question request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        body = message.get("body", "")

        try:
            request = json.loads(body)
            question = request.get("question", "")
            context = request.get("context", {})
        except (json.JSONDecodeError, TypeError):
            question = body
            context = {}

        # Search journal and file index for relevant info
        journal_results = self.search_journal(question, top_n=5)
        file_results = self.state.search_file_index(path_pattern=f"%{question.split()[0] if question else ''}%", limit=5)

        # Use AI to formulate an answer
        answer_context = {
            "question": question,
            "journal_entries": journal_results[:3],
            "relevant_files": file_results[:5],
            "additional_context": context,
        }

        prompt = f"""Based on the knowledge available, answer this question:

Question: {question}

Use the journal entries and file information provided in the context.
If you don't have enough information, say so clearly.
Be concise but helpful."""

        answer = self.chat_with_context(prompt, answer_context)

        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.KNOWLEDGE_RESULT,
            result={"answer": answer, "sources": journal_results[:3]},
            success=True,
            reply_to=msg_id,
        )

    def _handle_status_request(self, message: dict[str, Any]) -> None:
        """Handle a status request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")

        stats = self.state.get_file_index_stats()
        discoveries = self.state.get_discoveries(agent=self.name.lower(), limit=5)

        status = {
            "agent": self.name,
            "status": "running",
            "exploration_complete": self._exploration_complete,
            "file_index": stats,
            "recent_discoveries": len(discoveries),
            "exploration_roots": self.exploration_roots,
        }

        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.STATUS_RESPONSE,
            result=status,
            success=True,
            reply_to=msg_id,
        )

    def _run_maintenance(self) -> None:
        """Run periodic maintenance tasks."""
        # Update the journal index
        try:
            self.update_index()
        except Exception as e:
            logger.error(f"[Librarian] Error updating index: {e}")

    # ========== File Exploration Methods ==========

    def explore_filesystem(self, root_path: str, max_depth: int = DEFAULT_EXPLORATION_DEPTH) -> dict[str, Any]:
        """
        Explore the file system and index interesting files.

        Args:
            root_path: Root directory to start exploration.
            max_depth: Maximum depth to traverse (default 6 for thorough exploration).

        Returns:
            Summary of exploration results.
        """
        logger.info(f"[Librarian] Starting exploration from: {root_path} (depth: {max_depth})")
        print(f"[Librarian] Exploring with depth {max_depth}, max {self.MAX_FILES_PER_EXTENSION} files per extension")

        stats = {
            "files_found": 0,
            "files_indexed": 0,
            "errors": 0,
            "by_extension": {},
        }

        for ext in self.INTERESTING_EXTENSIONS:
            try:
                files = self.find_files_by_extension(root_path, [ext], max_depth)
                ext_count = len(files)
                stats["files_found"] += ext_count
                stats["by_extension"][ext] = {"found": ext_count, "indexed": 0}

                if ext_count > 0:
                    print(f"[Librarian] Found {ext_count} .{ext} files")

                for file_path in files[:self.MAX_FILES_PER_EXTENSION]:
                    try:
                        self.index_file(file_path)
                        stats["files_indexed"] += 1
                        stats["by_extension"][ext]["indexed"] = stats["by_extension"][ext].get("indexed", 0) + 1
                    except Exception as e:
                        logger.debug(f"Failed to index {file_path}: {e}")
                        stats["errors"] += 1

            except Exception as e:
                logger.error(f"[Librarian] Error finding .{ext} files: {e}")
                stats["errors"] += 1

        # Log the exploration results
        discovery_msg = f"Explored {root_path} (depth {max_depth}): found {stats['files_found']} files, indexed {stats['files_indexed']}"
        self.state.add_discovery(
            agent=self.name.lower(),
            discovery_type="exploration",
            description=discovery_msg,
            details=stats,
        )

        # Share discovery with FDA
        self.message_bus.share_discovery(
            from_agent=self.name.lower(),
            discovery_type="exploration_complete",
            description=discovery_msg,
            details=stats,
        )

        logger.info(f"[Librarian] Exploration complete: {stats}")
        print(f"[Librarian] Exploration complete: {stats['files_indexed']} files indexed")
        return stats

    def find_files_by_extension(
        self,
        path: str,
        extensions: list[str],
        max_depth: int = 5,
    ) -> list[str]:
        """
        Find files with specific extensions using the find command.

        Args:
            path: Directory to search.
            extensions: List of file extensions (without dot).
            max_depth: Maximum directory depth.

        Returns:
            List of file paths.
        """
        if not os.path.exists(path):
            return []

        # Build exclude patterns
        excludes = []
        for skip_dir in self.SKIP_DIRS:
            excludes.extend(["-not", "-path", f"*/{skip_dir}/*"])

        # Build name patterns
        name_patterns = []
        for ext in extensions:
            if name_patterns:
                name_patterns.append("-o")
            name_patterns.extend(["-name", f"*.{ext}"])

        cmd = [
            "find", path,
            "-maxdepth", str(max_depth),
            "-type", "f",
            *excludes,
            "(", *name_patterns, ")",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes per extension search
            )
            files = [f for f in result.stdout.strip().split("\n") if f]
            return files
        except subprocess.TimeoutExpired:
            logger.warning(f"[Librarian] find command timed out for {path} (5 min limit)")
            return []
        except Exception as e:
            logger.error(f"[Librarian] find error: {e}")
            return []

    def grep_for_pattern(
        self,
        pattern: str,
        path: str,
        file_pattern: Optional[str] = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Search for a pattern in files using grep.

        Args:
            pattern: Regex pattern to search for.
            path: Directory or file to search.
            file_pattern: Optional glob pattern to filter files (e.g., "*.py").
            max_results: Maximum number of results to return.

        Returns:
            List of match dictionaries with file, line_number, and content.
        """
        if not os.path.exists(path):
            return []

        cmd = ["grep", "-r", "-n", "-I"]  # -I skips binary files

        # Add file pattern if specified
        if file_pattern:
            cmd.extend(["--include", file_pattern])

        # Exclude directories
        for skip_dir in self.SKIP_DIRS:
            cmd.extend(["--exclude-dir", skip_dir])

        cmd.extend([pattern, path])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            matches = []
            for line in result.stdout.strip().split("\n")[:max_results]:
                if ":" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        matches.append({
                            "file": parts[0],
                            "line_number": int(parts[1]) if parts[1].isdigit() else 0,
                            "content": parts[2].strip()[:200],  # Truncate long lines
                        })

            return matches

        except subprocess.TimeoutExpired:
            logger.warning(f"[Librarian] grep timed out for pattern: {pattern}")
            return []
        except Exception as e:
            logger.error(f"[Librarian] grep error: {e}")
            return []

    def _smart_search(self, query: str, path: str, explicit_path: bool = False) -> dict[str, Any]:
        """
        Perform a smart search based on the query.

        Determines the best search strategy based on the query:
        - File extension search: "*.py files" or "python files"
        - Filename search: find files by name across exploration roots
        - Pattern search: "TODO", "FIXME", function names
        - Content search: general text search

        When explicit_path is True, the user provided the path directly,
        so we search ONLY within that path instead of all exploration roots.

        Args:
            query: The search query.
            path: Path to search in.
            explicit_path: If True, restrict search to the given path only.

        Returns:
            Search results dictionary.
        """
        results = {
            "query": query,
            "path": path,
            "files": [],
            "matches": [],
            "summary": "",
        }

        query_lower = query.lower()

        # When the user gave an explicit directory path, list its contents first
        if explicit_path and os.path.isdir(path):
            logger.info(f"[Librarian] Exploring user-specified directory: {path}")
            import subprocess
            try:
                result = subprocess.run(
                    ["find", path, "-maxdepth", "3",
                     "-not", "-path", "*/.*",
                     "-not", "-path", "*/node_modules/*",
                     "-not", "-path", "*/__pycache__/*",
                     "-not", "-path", "*/.venv/*"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.stdout.strip():
                    all_files = result.stdout.strip().split("\n")
                    results["files"] = all_files[:30]
                    results["summary"] = f"Found {len(all_files)} items in '{path}'"
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"[Librarian] Error exploring explicit path: {e}")

            # Also try content search within the explicit path
            keywords = self._extract_search_keywords(query)
            if keywords:
                content_matches = self.grep_for_pattern(keywords, path)
                if content_matches:
                    results["matches"] = content_matches
                    results["summary"] += f" + {len(content_matches)} content matches"

            return results

        # Check for file type searches
        if "python" in query_lower or ".py" in query_lower:
            results["files"] = self.find_files_by_extension(path, ["py"])
            results["summary"] = f"Found {len(results['files'])} Python files"

        elif "javascript" in query_lower or ".js" in query_lower:
            results["files"] = self.find_files_by_extension(path, ["js", "ts"])
            results["summary"] = f"Found {len(results['files'])} JavaScript/TypeScript files"

        elif "config" in query_lower:
            results["files"] = self.find_files_by_extension(
                path, ["json", "yaml", "yml", "toml", "ini", "cfg"]
            )
            results["summary"] = f"Found {len(results['files'])} config files"

        elif "markdown" in query_lower or ".md" in query_lower or "docs" in query_lower:
            results["files"] = self.find_files_by_extension(path, ["md", "txt", "rst"])
            results["summary"] = f"Found {len(results['files'])} documentation files"

        # General search: try both filename search and content search
        else:
            # Extract keywords for filename search (strip common filler words)
            keywords = self._extract_search_keywords(query)

            # 1. Search for files by name across all exploration roots
            if keywords:
                filename_results = self._find_files_by_name(keywords)
                if filename_results:
                    results["files"] = filename_results
                    results["summary"] = f"Found {len(filename_results)} file(s) matching '{keywords}'"

            # 2. Also try content search (grep) in the specified path
            content_matches = self.grep_for_pattern(query, path)
            if content_matches:
                results["matches"] = content_matches
                if results["summary"]:
                    results["summary"] += f" + {len(content_matches)} content matches"
                else:
                    results["summary"] = f"Found {len(content_matches)} content matches for '{query}'"

            if not results["summary"]:
                results["summary"] = f"Found 0 matches for '{query}'"

        # Also search the journal
        journal_results = self.search_journal(query, top_n=3)
        if journal_results:
            results["journal_matches"] = journal_results
            results["summary"] += f" + {len(journal_results)} journal entries"

        return results

    def _extract_search_keywords(self, query: str) -> str:
        """
        Extract meaningful search keywords from a natural language query.

        Strips common filler words to get the core search term(s).

        Args:
            query: Natural language query.

        Returns:
            Extracted keyword string, or empty string if nothing useful.
        """
        filler_words = {
            "do", "you", "see", "a", "an", "the", "is", "are", "there",
            "find", "search", "for", "look", "looking", "where", "what",
            "can", "could", "please", "help", "me", "my", "i", "have",
            "any", "some", "that", "that's", "which", "about", "related", "to",
            "file", "files", "picture", "image", "document", "named",
            "called", "with", "of", "in", "on", "at", "it", "its", "it's",
            "how", "show", "get", "locate", "does", "did", "has",
            "don't", "doesn't", "isn't", "aren't", "wasn't", "weren't",
            "i'm", "i've", "i'll", "you're", "you've", "there's",
        }
        words = query.lower().split()
        keywords = [w.strip("'\".,!?") for w in words if w.strip("'\".,!?") not in filler_words]
        return " ".join(keywords) if keywords else ""

    def _find_files_by_name(self, keywords: str) -> list[str]:
        """
        Search for files by name across all exploration roots.

        Uses the `find` command to locate files whose names contain
        any of the given keywords.

        Args:
            keywords: Space-separated keywords to search for in filenames.

        Returns:
            List of matching file paths.
        """
        import subprocess

        found_files = []
        # Search each keyword individually for better coverage
        search_terms = keywords.split()

        for search_dir in self.exploration_roots:
            if not os.path.isdir(search_dir):
                continue
            for term in search_terms:
                if len(term) < 2:  # Skip very short terms
                    continue
                try:
                    result = subprocess.run(
                        ["find", search_dir, "-maxdepth", str(self.exploration_depth),
                         "-iname", f"*{term}*",
                         "-not", "-path", "*/.*"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.stdout.strip():
                        found_files.extend(result.stdout.strip().split("\n"))
                except (subprocess.TimeoutExpired, Exception):
                    continue

        # Deduplicate while preserving order
        return list(dict.fromkeys(found_files))[:20]

    def index_file(self, file_path: str) -> Optional[str]:
        """
        Index a single file into the database.

        Args:
            file_path: Path to the file to index.

        Returns:
            File ID if indexed successfully, None otherwise.
        """
        try:
            path = Path(file_path)
            if not path.exists() or not path.is_file():
                return None

            stat = path.stat()

            # Generate a simple summary (first few lines for code files)
            summary = None
            if path.suffix in [".py", ".js", ".ts", ".go", ".rs"]:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()[:10]
                        # Look for docstrings or comments
                        for line in lines:
                            line = line.strip()
                            if line.startswith('"""') or line.startswith("'''"):
                                summary = line.strip('"\' ')
                                break
                            elif line.startswith("//") or line.startswith("#"):
                                summary = line.lstrip("/#").strip()
                                break
                except Exception:
                    pass

            # Determine tags based on path and extension
            tags = [path.suffix.lstrip(".")] if path.suffix else []
            if "test" in str(path).lower():
                tags.append("test")
            if "config" in str(path).lower():
                tags.append("config")

            return self.state.add_file_to_index(
                path=str(path.absolute()),
                extension=path.suffix.lstrip("."),
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                summary=summary,
                tags=tags,
            )

        except Exception as e:
            logger.debug(f"[Librarian] Failed to index {file_path}: {e}")
            return None

    def generate_report(self, report_type: str) -> str:
        """
        Generate a report of the specified type.

        Args:
            report_type: Type of report (daily, weekly, monthly, project).

        Returns:
            The generated report as a string.
        """
        logger.info(f"[Librarian] Generating {report_type} report")

        # Gather data based on report type
        context = self._gather_report_data(report_type)

        prompt = self._get_report_prompt(report_type)

        response = self.chat_with_context(prompt, context)

        # Log the report to journal
        self.log_to_journal(
            summary=f"{report_type.title()} Report - {datetime.now().strftime('%Y-%m-%d')}",
            content=f"## {report_type.title()} Report\n\n{response}",
            tags=["report", report_type],
            relevance_decay="medium" if report_type == "daily" else "slow",
        )

        return response

    def _gather_report_data(self, report_type: str) -> dict[str, Any]:
        """Gather data for a specific report type."""
        context: dict[str, Any] = {}

        # Get tasks
        tasks = self.state.get_tasks()
        context["total_tasks"] = len(tasks)
        context["tasks_by_status"] = {}
        for task in tasks:
            status = task.get("status", "unknown")
            if status not in context["tasks_by_status"]:
                context["tasks_by_status"][status] = []
            context["tasks_by_status"][status].append(task)

        # Get alerts
        context["alerts"] = self.state.get_alerts(acknowledged=False)

        # Get decisions
        if report_type in ["weekly", "monthly", "project"]:
            context["decisions"] = self.state.get_decisions(limit=10)
        else:
            context["decisions"] = self.state.get_decisions(limit=5)

        # Get journal entries based on timeframe
        if report_type == "daily":
            context["journal_entries"] = self.journal_retriever.index.get_recent(limit=10)
        elif report_type == "weekly":
            week_ago = datetime.now() - timedelta(days=7)
            context["journal_entries"] = self.journal_retriever.index.get_by_date_range(
                week_ago, datetime.now()
            )
        elif report_type == "monthly":
            month_ago = datetime.now() - timedelta(days=30)
            context["journal_entries"] = self.journal_retriever.index.get_by_date_range(
                month_ago, datetime.now()
            )
        else:  # project
            context["journal_entries"] = self.journal_retriever.index.entries

        # Get KPI data
        context["kpis"] = {
            "completion_rate": self.state.get_latest_kpi("completion_rate"),
            "block_rate": self.state.get_latest_kpi("block_rate"),
            "total_tasks": self.state.get_latest_kpi("total_tasks"),
        }

        return context

    def _get_report_prompt(self, report_type: str) -> str:
        """Get the appropriate prompt for a report type."""
        base_prompt = """Generate a comprehensive {type} report for this project.

Include the following sections:
1. **Executive Summary**: Brief overview of the period
2. **Accomplishments**: What was completed
3. **Current Status**: Where things stand now
4. **Challenges & Blockers**: Issues encountered
5. **Upcoming Work**: What's planned next
6. **Metrics & KPIs**: Key numbers and trends
7. **Recommendations**: Suggested actions

Be specific, cite data where available, and make recommendations actionable."""

        if report_type == "daily":
            return base_prompt.format(type="daily") + """

Focus on:
- Tasks completed today
- Tasks in progress
- Immediate blockers
- Tomorrow's priorities"""

        elif report_type == "weekly":
            return base_prompt.format(type="weekly") + """

Focus on:
- Week's key accomplishments
- Progress against weekly goals
- Trends over the week
- Next week's priorities"""

        elif report_type == "monthly":
            return base_prompt.format(type="monthly") + """

Focus on:
- Month's major milestones
- Progress against monthly goals
- Trend analysis
- Strategic observations
- Next month's objectives"""

        else:  # project
            return base_prompt.format(type="project") + """

Focus on:
- Overall project health
- Progress against project goals
- Major decisions made
- Lessons learned
- Critical path items
- Risk assessment"""

    def generate_meeting_brief(self, event: dict[str, Any]) -> str:
        """
        Generate a brief for an upcoming meeting.

        Args:
            event: Calendar event details.

        Returns:
            The meeting brief as a string.
        """
        event_id = event.get("id", "unknown")
        subject = event.get("subject", "Unknown Meeting")

        logger.info(f"[Librarian] Generating brief for: {subject}")

        # Gather context
        context = self.get_project_context()

        # Search for relevant journal entries
        relevant = self.search_journal(subject, top_n=5)
        context["relevant_entries"] = [
            {
                "summary": e.get("summary"),
                "author": e.get("author"),
                "date": e.get("created_at", "")[:10],
            }
            for e in relevant
        ]

        # Add event details
        context["event"] = event

        prompt = f"""Prepare a comprehensive meeting brief for:

Meeting: {subject}
Event ID: {event_id}
Time: {event.get('start', 'Unknown')}
Location: {event.get('location', 'Unknown')}
Attendees: {', '.join(a.get('name', a.get('email', '')) for a in event.get('attendees', []))}

Generate a brief that includes:
1. **Meeting Purpose**: What this meeting is about
2. **Background**: Relevant context and history
3. **Key Discussion Points**: Topics to cover
4. **Current Status**: Relevant project status
5. **Open Questions**: Items needing decisions
6. **Action Items to Discuss**: Outstanding tasks
7. **Preparation Notes**: What attendees should review beforehand

Base the brief on the project context and relevant historical entries provided."""

        response = self.chat_with_context(prompt, context)

        # Store the brief
        self.state.record_meeting_prep(
            event_id=event_id,
            brief=response,
            created_by=self.name,
        )

        return response

    def write_journal_entry(self, entry: dict[str, Any]) -> Path:
        """
        Write an entry to the project journal.

        Args:
            entry: Dictionary containing entry metadata and content.
                  Required keys: summary, content
                  Optional keys: tags, relevance_decay

        Returns:
            Path to the written journal file.
        """
        summary = entry.get("summary", "Journal Entry")
        content = entry.get("content", "")
        tags = entry.get("tags", ["librarian"])
        relevance_decay = entry.get("relevance_decay", "medium")

        # Optionally enhance the content with AI
        if entry.get("enhance", False):
            enhanced_content = self._enhance_journal_entry(summary, content)
            content = enhanced_content

        return self.journal_writer.write_entry(
            author=self.name,
            tags=tags,
            summary=summary,
            content=content,
            relevance_decay=relevance_decay,
        )

    def _enhance_journal_entry(self, summary: str, content: str) -> str:
        """Enhance a journal entry with AI assistance."""
        prompt = f"""Enhance this journal entry for better clarity and organization:

Summary: {summary}

Content:
{content}

Please:
1. Improve the structure and formatting
2. Add appropriate headers if needed
3. Ensure key points are clear
4. Add any helpful context
5. Keep the factual content unchanged

Return the enhanced entry."""

        return self.chat(prompt, include_history=False)

    def update_index(self) -> None:
        """
        Update the journal index with recent entries.

        Scans the journal directory for entries not in the index.
        """
        logger.debug("[Librarian] Updating journal index")

        journal_dir = self.journal_writer.journal_dir
        index = self.journal_retriever.index

        # Get current indexed filenames
        indexed_files = {e.get("filename") for e in index.entries}

        # Scan journal directory
        new_entries = 0
        for filepath in journal_dir.glob("*.md"):
            if filepath.name not in indexed_files:
                # Read and index this entry
                try:
                    entry_data = self.journal_writer.read_entry(filepath.name)
                    metadata = entry_data.get("metadata", {})

                    index.add_entry({
                        "filename": filepath.name,
                        "author": metadata.get("author", "unknown"),
                        "tags": metadata.get("tags", []),
                        "summary": metadata.get("title", filepath.stem),
                        "created_at": metadata.get("created_at", datetime.now().isoformat()),
                        "relevance_decay": metadata.get("relevance_decay", "medium"),
                    })
                    new_entries += 1
                except Exception as e:
                    logger.error(f"Failed to index {filepath}: {e}")

        if new_entries > 0:
            logger.info(f"[Librarian] Added {new_entries} new entries to index")

    def alert_fda(self, message: str, level: str = "warning") -> None:
        """
        Send an alert message to the FDA agent.

        Args:
            message: The alert message.
            level: Alert level (info, warning, critical).
        """
        # Add to alerts
        self.add_alert(level, message)

        # Send message to FDA
        self.send_message(
            to_agent="FDA",
            msg_type="alert",
            subject=f"{level.title()} Alert from Librarian",
            body=message,
            priority="high" if level == "critical" else "medium",
        )

    def _format_search_results(self, results: list[dict[str, Any]]) -> str:
        """Format search results for display."""
        if not results:
            return "No matching entries found."

        lines = [f"Found {len(results)} matching entries:\n"]

        for i, entry in enumerate(results, 1):
            lines.append(f"{i}. **{entry.get('summary', 'Untitled')}**")
            lines.append(f"   Author: {entry.get('author')} | Date: {entry.get('created_at', '')[:10]}")
            lines.append(f"   Tags: {', '.join(entry.get('tags', []))}")
            lines.append(f"   Score: {entry.get('combined_score', 0):.3f}")
            lines.append("")

        return "\n".join(lines)

    def summarize_entries(
        self,
        query: Optional[str] = None,
        tags: Optional[list[str]] = None,
        days: int = 7,
    ) -> str:
        """
        Summarize journal entries matching criteria.

        Args:
            query: Optional search query.
            tags: Optional tag filter.
            days: Number of days to look back.

        Returns:
            Summary of matching entries.
        """
        # Get relevant entries
        if query or tags:
            entries = self.search_journal(query or "", tags, top_n=20)
        else:
            start_date = datetime.now() - timedelta(days=days)
            entries = self.journal_retriever.index.get_by_date_range(
                start_date, datetime.now()
            )

        if not entries:
            return "No entries found matching the criteria."

        # Build context for summarization
        entry_summaries = []
        for entry in entries[:20]:
            content = self.journal_retriever._read_entry_content(
                entry.get("filename", "")
            )
            entry_summaries.append({
                "summary": entry.get("summary"),
                "author": entry.get("author"),
                "date": entry.get("created_at", "")[:10],
                "content_preview": content[:500] if content else "",
            })

        context = {"entries": entry_summaries}

        prompt = """Summarize these journal entries:

Provide:
1. **Overview**: What these entries cover
2. **Key Themes**: Common topics or patterns
3. **Important Findings**: Notable insights
4. **Decisions Made**: Any decisions documented
5. **Open Items**: Things that may need follow-up

Be concise but comprehensive."""

        return self.chat_with_context(prompt, context)

    def create_knowledge_digest(self) -> str:
        """
        Create a digest of important project knowledge.

        Returns:
            Knowledge digest as a string.
        """
        # Gather different types of entries
        context: dict[str, Any] = {}

        # Get slow-decay entries (important long-term knowledge)
        all_entries = self.journal_retriever.index.entries
        slow_decay = [
            e for e in all_entries
            if e.get("relevance_decay") == "slow"
        ]
        context["slow_decay_entries"] = slow_decay[:10]

        # Get entries by key tags
        key_tags = ["decision", "strategic", "architecture", "onboarding"]
        for tag in key_tags:
            tagged = [e for e in all_entries if tag in e.get("tags", [])]
            context[f"{tag}_entries"] = tagged[:5]

        # Get decisions
        context["decisions"] = self.state.get_decisions(limit=10)

        prompt = """Create a knowledge digest for this project.

This digest should capture the most important, enduring information that
new team members or stakeholders should know.

Include:
1. **Project Overview**: What this project is about
2. **Key Decisions**: Important decisions and their rationale
3. **Architecture & Design**: Technical or structural decisions
4. **Lessons Learned**: Important insights from the journey
5. **Important Contacts**: Key people and their roles (if known)
6. **Critical Information**: Anything else essential to know

Focus on information that remains relevant over time."""

        digest = self.chat_with_context(prompt, context)

        # Log the digest
        self.log_to_journal(
            summary="Knowledge Digest",
            content=f"## Project Knowledge Digest\n\n{digest}",
            tags=["digest", "knowledge", "reference"],
            relevance_decay="slow",
        )

        return digest

    # ========== Code Routing System ==========

    def build_routing_system(self) -> dict[str, Any]:
        """
        Build a routing system by analyzing indexed code files.

        Extracts functions, classes, methods, and API endpoints from code files
        to create a searchable routing index.

        Returns:
            Statistics about the routing system.
        """
        logger.info("[Librarian] Building code routing system...")

        stats = {
            "files_analyzed": 0,
            "routes_added": 0,
            "errors": 0,
            "by_type": {},
        }

        # Get all indexed code files
        file_stats = self.state.get_file_index_stats()
        code_extensions = ["py", "js", "ts", "go", "java"]

        for ext in code_extensions:
            if ext not in file_stats.get("by_extension", {}):
                continue

            # Get files of this extension
            files = self.state.search_file_index(extension=ext, limit=500)

            for file_entry in files:
                file_path = file_entry.get("path")
                if not file_path:
                    continue

                try:
                    routes = self._analyze_code_file(file_path, ext)
                    stats["files_analyzed"] += 1

                    for route in routes:
                        self.state.add_code_route(
                            file_path=file_path,
                            route_type=route["type"],
                            name=route["name"],
                            line_number=route.get("line"),
                            signature=route.get("signature"),
                            docstring=route.get("docstring"),
                            keywords=route.get("keywords"),
                        )
                        stats["routes_added"] += 1

                        # Track by type
                        route_type = route["type"]
                        stats["by_type"][route_type] = stats["by_type"].get(route_type, 0) + 1

                except Exception as e:
                    logger.debug(f"[Librarian] Error analyzing {file_path}: {e}")
                    stats["errors"] += 1

        # Share discovery with FDA
        discovery_msg = f"Built routing system: {stats['routes_added']} routes from {stats['files_analyzed']} files"
        self.message_bus.share_discovery(
            from_agent=self.name.lower(),
            discovery_type="routing_complete",
            description=discovery_msg,
            details=stats,
        )

        logger.info(f"[Librarian] Routing system complete: {stats}")
        print(f"[Librarian] Routing complete: {stats['routes_added']} routes indexed")
        return stats

    def _analyze_code_file(self, file_path: str, extension: str) -> list[dict[str, Any]]:
        """
        Analyze a code file and extract routes (functions, classes, endpoints).

        Args:
            file_path: Path to the code file.
            extension: File extension (py, js, ts, etc.).

        Returns:
            List of route dictionaries.
        """
        routes = []

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                lines = content.split("\n")
        except Exception:
            return routes

        if extension == "py":
            routes.extend(self._analyze_python_file(file_path, content, lines))
        elif extension in ["js", "ts"]:
            routes.extend(self._analyze_javascript_file(file_path, content, lines))
        elif extension == "go":
            routes.extend(self._analyze_go_file(file_path, content, lines))

        return routes

    def _analyze_python_file(self, file_path: str, content: str, lines: list[str]) -> list[dict[str, Any]]:
        """Analyze a Python file for functions, classes, and endpoints."""
        routes = []

        try:
            tree = ast.parse(content)
        except SyntaxError:
            # Fall back to regex-based parsing
            return self._analyze_python_regex(file_path, lines)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                # Get function signature
                args = []
                for arg in node.args.args:
                    args.append(arg.arg)
                signature = f"{node.name}({', '.join(args)})"

                # Get docstring
                docstring = ast.get_docstring(node)

                # Determine route type
                route_type = "function"
                keywords = [node.name.lower()]

                # Check for decorators that indicate endpoints/handlers
                for decorator in node.decorator_list:
                    dec_name = ""
                    if isinstance(decorator, ast.Name):
                        dec_name = decorator.id
                    elif isinstance(decorator, ast.Attribute):
                        dec_name = decorator.attr
                    elif isinstance(decorator, ast.Call):
                        if isinstance(decorator.func, ast.Attribute):
                            dec_name = decorator.func.attr
                        elif isinstance(decorator.func, ast.Name):
                            dec_name = decorator.func.id

                    dec_lower = dec_name.lower()
                    if dec_lower in ["route", "get", "post", "put", "delete", "patch"]:
                        route_type = "endpoint"
                        keywords.append("api")
                        keywords.append(dec_lower)
                    elif dec_lower in ["command", "event", "handler"]:
                        route_type = "handler"
                        keywords.append(dec_lower)
                    elif dec_lower == "property":
                        route_type = "property"

                # Add keywords from function name
                name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', node.name)
                keywords.extend([p.lower() for p in name_parts])

                routes.append({
                    "type": route_type,
                    "name": node.name,
                    "line": node.lineno,
                    "signature": signature,
                    "docstring": docstring,
                    "keywords": list(set(keywords)),
                })

            elif isinstance(node, ast.ClassDef):
                docstring = ast.get_docstring(node)
                keywords = [node.name.lower()]

                # Add keywords from class name
                name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', node.name)
                keywords.extend([p.lower() for p in name_parts])

                # Check for base classes
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        keywords.append(base.id.lower())

                routes.append({
                    "type": "class",
                    "name": node.name,
                    "line": node.lineno,
                    "signature": f"class {node.name}",
                    "docstring": docstring,
                    "keywords": list(set(keywords)),
                })

        return routes

    def _analyze_python_regex(self, file_path: str, lines: list[str]) -> list[dict[str, Any]]:
        """Fallback regex-based Python analysis."""
        routes = []

        func_pattern = re.compile(r'^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)')
        class_pattern = re.compile(r'^class\s+(\w+)')

        for i, line in enumerate(lines, 1):
            # Functions
            match = func_pattern.match(line.strip())
            if match:
                name = match.group(1)
                args = match.group(2)
                routes.append({
                    "type": "function",
                    "name": name,
                    "line": i,
                    "signature": f"{name}({args})",
                    "keywords": [name.lower()],
                })

            # Classes
            match = class_pattern.match(line.strip())
            if match:
                name = match.group(1)
                routes.append({
                    "type": "class",
                    "name": name,
                    "line": i,
                    "signature": f"class {name}",
                    "keywords": [name.lower()],
                })

        return routes

    def _analyze_javascript_file(self, file_path: str, content: str, lines: list[str]) -> list[dict[str, Any]]:
        """Analyze a JavaScript/TypeScript file for functions, classes, and exports."""
        routes = []

        # Function patterns
        func_patterns = [
            re.compile(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)'),
            re.compile(r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>'),
            re.compile(r'(?:export\s+)?const\s+(\w+)\s*=\s*function'),
        ]

        class_pattern = re.compile(r'(?:export\s+)?class\s+(\w+)')

        for i, line in enumerate(lines, 1):
            # Functions
            for pattern in func_patterns:
                match = pattern.search(line)
                if match:
                    name = match.group(1)
                    keywords = [name.lower()]
                    name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', name)
                    keywords.extend([p.lower() for p in name_parts])

                    routes.append({
                        "type": "function",
                        "name": name,
                        "line": i,
                        "signature": name,
                        "keywords": list(set(keywords)),
                    })
                    break

            # Classes
            match = class_pattern.search(line)
            if match:
                name = match.group(1)
                keywords = [name.lower()]
                name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', name)
                keywords.extend([p.lower() for p in name_parts])

                routes.append({
                    "type": "class",
                    "name": name,
                    "line": i,
                    "signature": f"class {name}",
                    "keywords": list(set(keywords)),
                })

        return routes

    def _analyze_go_file(self, file_path: str, content: str, lines: list[str]) -> list[dict[str, Any]]:
        """Analyze a Go file for functions and types."""
        routes = []

        func_pattern = re.compile(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(([^)]*)\)')
        type_pattern = re.compile(r'^type\s+(\w+)\s+(struct|interface)')

        for i, line in enumerate(lines, 1):
            # Functions
            match = func_pattern.match(line)
            if match:
                name = match.group(1)
                args = match.group(2)
                keywords = [name.lower()]

                # Check for handler patterns
                route_type = "function"
                if "Handler" in name or "handler" in line.lower():
                    route_type = "handler"
                    keywords.append("handler")

                routes.append({
                    "type": route_type,
                    "name": name,
                    "line": i,
                    "signature": f"func {name}({args})",
                    "keywords": keywords,
                })

            # Types (struct/interface)
            match = type_pattern.match(line)
            if match:
                name = match.group(1)
                kind = match.group(2)
                routes.append({
                    "type": kind,
                    "name": name,
                    "line": i,
                    "signature": f"type {name} {kind}",
                    "keywords": [name.lower(), kind],
                })

        return routes

    def search_routes(self, query: str, route_type: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Search the code routing system.

        Args:
            query: Search query (searches name, keywords, docstring).
            route_type: Optional filter by route type (function, class, endpoint, handler).

        Returns:
            List of matching routes with file info.
        """
        return self.state.search_code_routes(query, route_type=route_type, limit=20)

    def get_file_routes(self, file_path: str) -> list[dict[str, Any]]:
        """
        Get all routes in a specific file.

        Args:
            file_path: Path to the file.

        Returns:
            List of routes in the file, ordered by line number.
        """
        return self.state.get_routes_for_file(file_path)

    def get_routing_stats(self) -> dict[str, Any]:
        """
        Get statistics about the routing system.

        Returns:
            Dictionary with total routes and breakdown by type.
        """
        return self.state.get_code_routes_stats()

    # ========== Project Discovery & Knowledge Base ==========

    # Marker files that identify project roots
    PROJECT_MARKERS = {
        "python": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"],
        "javascript": ["package.json"],
        "typescript": ["package.json", "tsconfig.json"],
        "go": ["go.mod"],
        "rust": ["Cargo.toml"],
        "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "ruby": ["Gemfile"],
        "csharp": ["*.csproj", "*.sln"],
    }

    # Stopwords to filter from keyword indexing
    KEYWORD_STOPWORDS = {
        "self", "none", "true", "false", "return", "import", "from", "class",
        "def", "if", "else", "for", "while", "try", "except", "with", "as",
        "in", "not", "and", "or", "is", "the", "a", "an", "of", "to", "it",
        "this", "that", "get", "set", "init", "main", "new", "str", "int",
        "list", "dict", "bool", "float", "type", "any", "all", "has", "can",
        "do", "be", "was", "are", "test", "args", "kwargs", "var", "val",
        "let", "const", "function", "export", "default", "module", "require",
    }

    def discover_projects(self, max_depth: int = 4) -> list[dict[str, Any]]:
        """
        Discover projects (git repos, packages) across exploration roots.

        Walks each exploration root looking for .git/ directories and
        marker files (pyproject.toml, package.json, etc.).

        Args:
            max_depth: Maximum directory depth to search.

        Returns:
            List of discovered project dictionaries.
        """
        logger.info("[Librarian] Starting project discovery...")
        print("[Librarian] Discovering projects...")

        discovered = []
        seen_paths = set()

        for root in self.exploration_roots:
            if not os.path.isdir(root):
                continue

            print(f"[Librarian] Scanning: {root}")

            # Find .git directories as primary project indicators
            try:
                result = subprocess.run(
                    ["find", root, "-maxdepth", str(max_depth),
                     "-type", "d", "-name", ".git",
                     "-not", "-path", "*/node_modules/*",
                     "-not", "-path", "*/.venv/*",
                     "-not", "-path", "*/venv/*"],
                    capture_output=True, text=True, timeout=60,
                )
                for git_dir in result.stdout.strip().split("\n"):
                    if not git_dir:
                        continue
                    project_path = os.path.dirname(git_dir)
                    if project_path in seen_paths:
                        continue
                    seen_paths.add(project_path)

                    project = self._analyze_project_root(project_path)
                    if project:
                        discovered.append(project)
                        print(f"[Librarian] Found project: {project['name']} ({project['project_type']}) at {project_path}")

            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"[Librarian] Error scanning {root}: {e}")

            # Also look for standalone marker files (projects without .git)
            for lang, markers in self.PROJECT_MARKERS.items():
                for marker in markers:
                    if "*" in marker:
                        continue  # Skip glob patterns for find
                    try:
                        result = subprocess.run(
                            ["find", root, "-maxdepth", str(max_depth),
                             "-name", marker, "-type", "f",
                             "-not", "-path", "*/node_modules/*",
                             "-not", "-path", "*/.venv/*"],
                            capture_output=True, text=True, timeout=30,
                        )
                        for marker_path in result.stdout.strip().split("\n"):
                            if not marker_path:
                                continue
                            project_path = os.path.dirname(marker_path)
                            if project_path in seen_paths:
                                continue
                            seen_paths.add(project_path)

                            project = self._analyze_project_root(project_path)
                            if project:
                                discovered.append(project)
                                print(f"[Librarian] Found project: {project['name']} ({project['project_type']}) at {project_path}")

                    except (subprocess.TimeoutExpired, Exception):
                        continue

        # Store all discovered projects
        for project in discovered:
            self.state.add_project(
                path=project["path"],
                name=project["name"],
                project_type=project["project_type"],
                tech_stack=project.get("tech_stack"),
                git_remote=project.get("git_remote"),
                git_branch=project.get("git_branch"),
                git_commit_hash=project.get("git_commit_hash"),
            )

        print(f"[Librarian] Discovery complete: found {len(discovered)} projects")
        logger.info(f"[Librarian] Discovered {len(discovered)} projects")
        return discovered

    def _analyze_project_root(self, path: str) -> Optional[dict[str, Any]]:
        """
        Analyze a project root directory to extract metadata.

        Args:
            path: Path to the project root.

        Returns:
            Project metadata dictionary, or None if not a valid project.
        """
        project: dict[str, Any] = {"path": path}

        # Get git info
        git_info = self._get_git_info(path)
        project.update(git_info)

        # Detect tech stack and project type
        tech_stack, project_type = self._detect_tech_stack(path)
        project["tech_stack"] = tech_stack
        project["project_type"] = project_type

        # Extract project name
        project["name"] = self._extract_project_name(path, project_type)

        return project

    def _get_git_info(self, path: str) -> dict[str, Any]:
        """
        Get git information for a project.

        Args:
            path: Project root path.

        Returns:
            Dictionary with git_commit_hash, git_remote, git_branch.
        """
        info: dict[str, Any] = {}

        git_commands = {
            "git_commit_hash": ["git", "rev-parse", "HEAD"],
            "git_remote": ["git", "remote", "get-url", "origin"],
            "git_branch": ["git", "branch", "--show-current"],
        }

        for key, cmd in git_commands.items():
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=5, cwd=path,
                )
                if result.returncode == 0:
                    info[key] = result.stdout.strip()
            except (subprocess.TimeoutExpired, Exception):
                pass

        return info

    def _detect_tech_stack(self, path: str) -> tuple[list[str], str]:
        """
        Detect the tech stack and project type from marker files.

        Args:
            path: Project root path.

        Returns:
            Tuple of (tech_stack list, project_type string).
        """
        tech_stack = []
        project_type = "unknown"

        # Check Python markers
        pyproject = os.path.join(path, "pyproject.toml")
        setup_py = os.path.join(path, "setup.py")
        requirements = os.path.join(path, "requirements.txt")

        if os.path.exists(pyproject) or os.path.exists(setup_py):
            tech_stack.append("python")
            project_type = "python"
            # Try to read dependencies from pyproject.toml
            if os.path.exists(pyproject):
                try:
                    with open(pyproject, "r", encoding="utf-8") as f:
                        content = f.read()
                    deps = self._extract_python_deps(content)
                    tech_stack.extend(deps)
                except Exception:
                    pass
        elif os.path.exists(requirements):
            tech_stack.append("python")
            project_type = "python"
            try:
                with open(requirements, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            pkg = re.split(r'[>=<\[!]', line)[0].strip()
                            if pkg:
                                tech_stack.append(pkg.lower())
            except Exception:
                pass

        # Check JS/TS markers
        package_json = os.path.join(path, "package.json")
        if os.path.exists(package_json):
            if "python" not in tech_stack:
                project_type = "javascript"
            tech_stack.append("javascript")
            try:
                with open(package_json, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                deps = list((pkg.get("dependencies", {}) or {}).keys())[:10]
                tech_stack.extend(deps)
                if "typescript" in pkg.get("devDependencies", {}) or os.path.exists(os.path.join(path, "tsconfig.json")):
                    tech_stack.append("typescript")
                    project_type = "typescript"
            except Exception:
                pass

        # Check other markers
        if os.path.exists(os.path.join(path, "go.mod")):
            tech_stack.append("go")
            project_type = "go"
        if os.path.exists(os.path.join(path, "Cargo.toml")):
            tech_stack.append("rust")
            project_type = "rust"
        if os.path.exists(os.path.join(path, "pom.xml")) or os.path.exists(os.path.join(path, "build.gradle")):
            tech_stack.append("java")
            project_type = "java"

        # Deduplicate
        tech_stack = list(dict.fromkeys(tech_stack))[:20]
        return tech_stack, project_type

    def _extract_python_deps(self, pyproject_content: str) -> list[str]:
        """Extract dependency names from pyproject.toml content."""
        deps = []
        in_deps = False
        for line in pyproject_content.split("\n"):
            line = line.strip()
            if line.startswith("dependencies") and "=" in line:
                in_deps = True
                continue
            if in_deps:
                if line.startswith("]"):
                    break
                # Extract package name from "package>=version" or "package"
                match = re.match(r'"([a-zA-Z0-9_-]+)', line)
                if match:
                    deps.append(match.group(1).lower())
        return deps[:15]

    def _extract_project_name(self, path: str, project_type: str) -> str:
        """
        Extract project name from config files or directory name.

        Args:
            path: Project root path.
            project_type: Detected project type.

        Returns:
            Project name.
        """
        # Try pyproject.toml
        pyproject = os.path.join(path, "pyproject.toml")
        if os.path.exists(pyproject):
            try:
                with open(pyproject, "r", encoding="utf-8") as f:
                    for line in f:
                        match = re.match(r'^name\s*=\s*"([^"]+)"', line.strip())
                        if match:
                            return match.group(1)
            except Exception:
                pass

        # Try package.json
        package_json = os.path.join(path, "package.json")
        if os.path.exists(package_json):
            try:
                with open(package_json, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                    if "name" in pkg:
                        return pkg["name"]
            except Exception:
                pass

        # Fall back to directory name
        return os.path.basename(path)

    def build_project_knowledge(self, project_path: str, force: bool = False) -> dict[str, Any]:
        """
        Build knowledge base for a project: domains, keywords, description.

        Args:
            project_path: Path to the project root.
            force: If True, re-analyze even if git commit hasn't changed.

        Returns:
            Statistics about the knowledge built.
        """
        logger.info(f"[Librarian] Building knowledge for: {project_path}")
        print(f"[Librarian] Analyzing project: {project_path}")

        stats: dict[str, Any] = {
            "path": project_path,
            "domains": 0,
            "keywords": 0,
            "files_analyzed": 0,
            "cached": False,
        }

        # Check cache
        git_info = self._get_git_info(project_path)
        current_hash = git_info.get("git_commit_hash", "")

        if not force and current_hash:
            if not self.state.project_needs_reanalysis(project_path, current_hash):
                print(f"[Librarian] Skipping (unchanged): {project_path}")
                stats["cached"] = True
                return stats

        # Get or create project record
        project = self.state.get_project_by_path(project_path)
        if not project:
            # Discover it first
            proj_info = self._analyze_project_root(project_path)
            if not proj_info:
                return stats
            project_id = self.state.add_project(
                path=project_path,
                name=proj_info["name"],
                project_type=proj_info["project_type"],
                tech_stack=proj_info.get("tech_stack"),
                git_remote=proj_info.get("git_remote"),
                git_branch=proj_info.get("git_branch"),
                git_commit_hash=proj_info.get("git_commit_hash"),
            )
        else:
            project_id = project["id"]

        # Clear old data
        self.state.clear_project_domains(project_id)
        self.state.clear_project_keywords(project_id)

        # Scan project files
        code_extensions = ["py", "js", "ts", "go", "rs", "java"]
        all_files: list[str] = []
        for ext in code_extensions:
            files = self.find_files_by_extension(project_path, [ext], max_depth=6)
            all_files.extend(files)

        stats["files_analyzed"] = len(all_files)

        # Analyze code files and collect routes
        all_routes: list[dict[str, Any]] = []
        file_routes_map: dict[str, list[dict[str, Any]]] = {}

        for file_path in all_files:
            ext = os.path.splitext(file_path)[1].lstrip(".")
            try:
                routes = self._analyze_code_file(file_path, ext)
                all_routes.extend(routes)
                file_routes_map[file_path] = routes
            except Exception:
                continue

        # Domain clustering
        domains = self._cluster_domains(project_path, all_files, file_routes_map)
        for domain in domains:
            domain_id = self.state.add_project_domain(
                project_id=project_id,
                domain_name=domain["name"],
                description=domain.get("description"),
                file_paths=domain["files"],
                entry_points=domain.get("entry_points"),
                keywords=domain.get("keywords"),
                file_count=len(domain["files"]),
            )
            domain["id"] = domain_id

        stats["domains"] = len(domains)

        # Keyword indexing with TF-IDF
        keywords_data = self._build_keyword_index(
            project_id, project_path, all_files, all_routes, file_routes_map, domains
        )
        if keywords_data:
            self.state.add_project_keywords_batch(keywords_data)
        stats["keywords"] = len(keywords_data)

        # Generate AI description
        description = self._generate_project_description(
            project_path, stats, domains, all_routes[:50]
        )

        # Update project record
        self.state.update_project(
            project_id,
            description=description,
            file_count=len(all_files),
            code_route_count=len(all_routes),
            git_commit_hash=current_hash,
            last_analyzed_at=datetime.now().isoformat(),
        )

        print(f"[Librarian] Analyzed: {len(all_files)} files, {len(domains)} domains, {len(keywords_data)} keywords")
        return stats

    def _cluster_domains(
        self,
        project_path: str,
        all_files: list[str],
        file_routes_map: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """
        Cluster files into functional domains by top-level subdirectory.

        Args:
            project_path: Project root path.
            all_files: All code file paths.
            file_routes_map: Map of file path to extracted routes.

        Returns:
            List of domain dictionaries.
        """
        # Group files by top-level subdirectory relative to project root
        dir_groups: dict[str, list[str]] = {}
        for f in all_files:
            try:
                rel = os.path.relpath(f, project_path)
            except ValueError:
                continue
            parts = rel.split(os.sep)
            if len(parts) > 1:
                group_key = parts[0]
            else:
                group_key = "(root)"
            if group_key not in dir_groups:
                dir_groups[group_key] = []
            dir_groups[group_key].append(f)

        domains = []
        small_groups: list[tuple[str, list[str]]] = []

        for dir_name, files in dir_groups.items():
            if len(files) < 3:
                small_groups.append((dir_name, files))
                continue

            # Infer domain purpose from function/class names
            route_names = []
            for f in files:
                for route in file_routes_map.get(f, []):
                    route_names.append(route["name"].lower())

            domain_name = self._infer_domain_name(dir_name, route_names)
            keywords = self._extract_domain_keywords(route_names)

            # Find entry points (files with most routes)
            file_route_counts = [
                (f, len(file_routes_map.get(f, [])))
                for f in files
            ]
            file_route_counts.sort(key=lambda x: x[1], reverse=True)
            entry_points = [f for f, _ in file_route_counts[:3]]

            domains.append({
                "name": domain_name,
                "files": files,
                "entry_points": entry_points,
                "keywords": keywords,
                "description": f"{domain_name} ({len(files)} files)",
            })

        # Merge small groups
        if small_groups:
            merged_files = []
            merged_names = []
            for name, files in small_groups:
                merged_files.extend(files)
                merged_names.append(name)

            if merged_files:
                route_names = []
                for f in merged_files:
                    for route in file_routes_map.get(f, []):
                        route_names.append(route["name"].lower())

                domains.append({
                    "name": "Utilities & Config",
                    "files": merged_files,
                    "entry_points": merged_files[:3],
                    "keywords": self._extract_domain_keywords(route_names),
                    "description": f"Utilities & Config ({len(merged_files)} files from {', '.join(merged_names[:5])})",
                })

        return domains

    def _infer_domain_name(self, dir_name: str, route_names: list[str]) -> str:
        """Infer a human-readable domain name from directory and route names."""
        # Check for common patterns
        dir_lower = dir_name.lower()
        patterns = {
            "api": "API Layer",
            "routes": "API Routes",
            "handlers": "Request Handlers",
            "models": "Data Models",
            "schemas": "Data Schemas",
            "services": "Business Logic",
            "controllers": "Controllers",
            "middleware": "Middleware",
            "utils": "Utilities",
            "helpers": "Helpers",
            "tests": "Tests",
            "test": "Tests",
            "config": "Configuration",
            "db": "Database",
            "database": "Database",
            "auth": "Authentication",
            "components": "UI Components",
            "pages": "Pages",
            "views": "Views",
            "templates": "Templates",
            "static": "Static Assets",
            "scripts": "Scripts",
            "lib": "Library",
            "core": "Core",
            "cmd": "CLI Commands",
            "internal": "Internal",
            "pkg": "Packages",
            "src": "Source",
        }

        for pattern, name in patterns.items():
            if pattern in dir_lower:
                return name

        # Check route names for clues
        name_counter = Counter()
        for name in route_names:
            parts = re.findall(r'[A-Z][a-z]*|[a-z]+', name)
            for p in parts:
                if len(p) > 2 and p.lower() not in self.KEYWORD_STOPWORDS:
                    name_counter[p.lower()] += 1

        if name_counter:
            top_word = name_counter.most_common(1)[0][0]
            return f"{top_word.title()} Module"

        return dir_name.replace("_", " ").replace("-", " ").title()

    def _extract_domain_keywords(self, route_names: list[str]) -> list[str]:
        """Extract keywords from a list of route/function names."""
        word_counter: Counter = Counter()
        for name in route_names:
            parts = re.findall(r'[A-Z][a-z]*|[a-z]+', name)
            for p in parts:
                p_lower = p.lower()
                if len(p_lower) > 2 and p_lower not in self.KEYWORD_STOPWORDS:
                    word_counter[p_lower] += 1

        return [word for word, _ in word_counter.most_common(20)]

    def _build_keyword_index(
        self,
        project_id: str,
        project_path: str,
        all_files: list[str],
        all_routes: list[dict[str, Any]],
        file_routes_map: dict[str, list[dict[str, Any]]],
        domains: list[dict[str, Any]],
    ) -> list[tuple[str, str, float, Optional[str], Optional[str], Optional[str]]]:
        """
        Build a TF-IDF weighted keyword index for a project.

        Args:
            project_id: Project ID.
            project_path: Project root path.
            all_files: All code files.
            all_routes: All extracted routes.
            file_routes_map: Map of file to routes.
            domains: Clustered domains.

        Returns:
            List of (project_id, keyword, weight, source_type, source_path, domain_id) tuples.
        """
        # Build domain lookup
        domain_lookup: dict[str, Optional[str]] = {}
        for domain in domains:
            domain_id = domain.get("id")
            for f in domain.get("files", []):
                domain_lookup[f] = domain_id

        # Collect keyword occurrences: keyword -> list of (source_type, source_path)
        keyword_sources: dict[str, list[tuple[str, str]]] = {}
        # Track document frequency: keyword -> set of files
        keyword_df: dict[str, set[str]] = {}

        total_files = max(len(all_files), 1)

        for file_path, routes in file_routes_map.items():
            file_keywords: set[str] = set()

            # Keywords from file name
            basename = os.path.splitext(os.path.basename(file_path))[0]
            name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', basename)
            for p in name_parts:
                kw = p.lower()
                if len(kw) > 1 and kw not in self.KEYWORD_STOPWORDS:
                    file_keywords.add(kw)
                    if kw not in keyword_sources:
                        keyword_sources[kw] = []
                    keyword_sources[kw].append(("filename", file_path))

            for route in routes:
                # Keywords from function/class name
                name_parts = re.findall(r'[A-Z][a-z]*|[a-z]+', route["name"])
                source_type = route["type"]  # function, class, etc.
                for p in name_parts:
                    kw = p.lower()
                    if len(kw) > 1 and kw not in self.KEYWORD_STOPWORDS:
                        file_keywords.add(kw)
                        if kw not in keyword_sources:
                            keyword_sources[kw] = []
                        keyword_sources[kw].append((source_type, file_path))

                # Keywords from docstring
                if route.get("docstring"):
                    doc_words = re.findall(r'[a-zA-Z]{3,}', route["docstring"][:200])
                    for w in doc_words:
                        kw = w.lower()
                        if kw not in self.KEYWORD_STOPWORDS:
                            file_keywords.add(kw)
                            if kw not in keyword_sources:
                                keyword_sources[kw] = []
                            keyword_sources[kw].append(("docstring", file_path))

                # Keywords from existing route keywords
                for kw in route.get("keywords", []):
                    kw_lower = kw.lower()
                    if len(kw_lower) > 1 and kw_lower not in self.KEYWORD_STOPWORDS:
                        file_keywords.add(kw_lower)
                        if kw_lower not in keyword_sources:
                            keyword_sources[kw_lower] = []
                        keyword_sources[kw_lower].append((source_type, file_path))

            # Update document frequency
            for kw in file_keywords:
                if kw not in keyword_df:
                    keyword_df[kw] = set()
                keyword_df[kw].add(file_path)

        # Compute TF-IDF weights and build output tuples
        results = []
        for keyword, sources in keyword_sources.items():
            tf = len(sources)  # Term frequency (number of occurrences)
            df = len(keyword_df.get(keyword, set()))  # Document frequency
            idf = math.log(total_files / max(df, 1)) + 1.0
            weight = tf * idf

            # Use the first source as representative
            source_type = sources[0][0]
            source_path = sources[0][1]
            domain_id = domain_lookup.get(source_path)

            results.append((project_id, keyword, weight, source_type, source_path, domain_id))

        # Sort by weight descending and cap at 10,000
        results.sort(key=lambda x: x[2], reverse=True)
        return results[:10000]

    def _generate_project_description(
        self,
        project_path: str,
        stats: dict[str, Any],
        domains: list[dict[str, Any]],
        sample_routes: list[dict[str, Any]],
    ) -> str:
        """
        Generate an AI description for a project.

        Args:
            project_path: Project root path.
            stats: Analysis statistics.
            domains: Discovered domains.
            sample_routes: Sample code routes for context.

        Returns:
            2-3 sentence project description.
        """
        domain_summary = ", ".join(d["name"] for d in domains[:5])
        route_names = [r["name"] for r in sample_routes[:20]]

        prompt = f"""Write exactly 2-3 sentences describing this software project. Start directly with the description, no preamble.

Project: {os.path.basename(project_path)}
Path: {project_path}
Files: {stats.get('files_analyzed', 0)}
Domains: {domain_summary}
Key symbols: {', '.join(route_names[:15])}

Focus on what the project does and its purpose."""

        try:
            return self.chat(prompt, include_history=False, max_tokens=200)
        except Exception as e:
            logger.warning(f"[Librarian] Could not generate description: {e}")
            return f"Software project at {project_path} with {stats.get('files_analyzed', 0)} code files."
