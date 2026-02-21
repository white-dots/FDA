"""
Local Worker Agent -- operates on the local Mac Mini filesystem.

Unlike the remote WorkerAgent which SSHs into Azure VMs, this agent
reads and writes files directly on the local machine. Designed for
working on local codebases like FDA itself.

Pipeline:
1. Receives task brief (from message bus or direct call)
2. Walks the local project directory to discover files
3. Uses Claude to identify relevant files
4. Reads file contents from disk
5. Uses Claude to generate a code fix
6. Creates diffs for approval
7. Writes approved changes with backup + rollback
"""

import json
import logging
import difflib
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from fda.base_agent import BaseAgent
from fda.claude_backend import get_claude_backend
from fda.config import (
    MODEL_EXECUTOR,
    MODEL_MEETING_SUMMARY,
    LOCAL_WORKER_PROJECTS,
    LOCAL_WORKER_BACKUP_DIR,
)
from fda.comms.message_bus import MessageBus

logger = logging.getLogger(__name__)

# Directories to skip when walking the filesystem
_EXCLUDE_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", "venv", ".venv",
    "env", ".env", "dist", "build", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "htmlcov", ".eggs",
    "site-packages",
})

# File extensions to skip
_EXCLUDE_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".o", ".a",
    ".whl", ".egg", ".tar", ".gz", ".zip",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".ttf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3",
})


class LocalWorkerAgent(BaseAgent):
    """
    Local filesystem worker agent.

    Mirrors the remote WorkerAgent's interface but replaces all SSH
    operations with direct pathlib I/O. Operates only within configured
    project directories for safety.
    """

    SYSTEM_PROMPT = """You are the Local Worker agent for Datacore.
Your job is to make code changes on the local Mac Mini filesystem based on task briefs.

When you receive a task brief, you will:
1. Understand the request in context
2. Read the relevant source files from the local project
3. Identify what needs to change
4. Generate the minimal, correct code fix
5. Produce a clean diff showing exactly what changed

Important rules:
- Make minimal changes. Don't refactor unrelated code.
- Preserve the existing code style (indentation, naming conventions, etc.)
- If you're unsure about the fix, say so rather than guessing.
- Always explain what you changed and why in plain language.
- Consider edge cases and potential side effects.
- Never change database schemas without explicit approval.
- Never delete data or drop tables.
"""

    def __init__(
        self,
        projects: Optional[list[str]] = None,
        message_bus: Optional[MessageBus] = None,
        db_path: Optional[str] = None,
    ):
        """
        Initialize the Local Worker agent.

        Args:
            projects: List of local project root paths the worker can operate on.
                      Defaults to LOCAL_WORKER_PROJECTS from config.
            message_bus: Inter-agent message bus.
            db_path: Path to SQLite database.
        """
        super().__init__(
            name="worker_local",
            model=MODEL_EXECUTOR,
            system_prompt=self.SYSTEM_PROMPT,
            message_bus=message_bus,
            db_path=db_path,
        )
        self.projects = [Path(p).resolve() for p in (projects or LOCAL_WORKER_PROJECTS)]
        self._backend = get_claude_backend()

        # Cache for file listings (same pattern as remote WorkerAgent)
        # Key: str(project_path), Value: (timestamp, file_list)
        self._structure_cache: dict[str, tuple[float, list[str]]] = {}
        self._structure_cache_ttl = 300.0  # 5 minutes

        self._backup_dir = LOCAL_WORKER_BACKUP_DIR

    # ------------------------------------------------------------------
    # Path validation
    # ------------------------------------------------------------------

    def _validate_project(self, project_path: str) -> Path:
        """
        Validate that a project path is within the allowed list.

        Args:
            project_path: Path to validate.

        Returns:
            Resolved Path object.

        Raises:
            ValueError: If path is not in the allowed project list.
        """
        resolved = Path(project_path).resolve()
        for allowed in self.projects:
            if resolved == allowed or resolved.is_relative_to(allowed):
                return resolved
        raise ValueError(
            f"Project path not in allowed list: {resolved}\n"
            f"Allowed: {[str(p) for p in self.projects]}"
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def analyze_and_fix(
        self,
        project_path: str,
        task_brief: str,
        hint_files: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Analyze a task and generate a code fix on the local filesystem.

        Same return dict structure as WorkerAgent.analyze_and_fix().

        Args:
            project_path: Local project directory path.
            task_brief: Task description.
            hint_files: Optional list of file paths to examine first.
            progress_callback: Optional callback for live progress updates.

        Returns:
            Dict with: success, analysis, changes, diff, explanation, error.
        """
        def _progress(msg: str) -> None:
            logger.info(f"[LocalWorker] {msg}")
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass

        # Validate project path
        try:
            project = self._validate_project(project_path)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not project.is_dir():
            return {"success": False, "error": f"Not a directory: {project}"}

        # Step 1: Discover files
        _progress(f"Scanning {project.name}...")
        structure = self._explore_codebase(project)
        cache_key = str(project)
        was_cached = cache_key in self._structure_cache and \
            (time.monotonic() - self._structure_cache[cache_key][0]) < self._structure_cache_ttl
        if was_cached:
            _progress(f"Found {len(structure)} files (cached)")
        else:
            _progress(f"Found {len(structure)} files")

        # Step 2: Identify relevant files
        _progress("Identifying relevant files...")
        relevant_files = self._identify_relevant_files(
            task_brief, structure, hint_files, project
        )

        if not relevant_files:
            _progress("No relevant files found")
            return {
                "success": False,
                "error": "Could not identify relevant files for this task",
                "analysis": f"Explored {len(structure)} files but none matched the request",
            }

        short_names = [f.rsplit("/", 1)[-1] for f in relevant_files[:5]]
        extras = f" +{len(relevant_files) - 5} more" if len(relevant_files) > 5 else ""
        _progress(f"Found {len(relevant_files)} relevant files: {', '.join(short_names)}{extras}")

        # Step 3: Read files
        _progress(f"Reading {len(relevant_files)} files...")
        file_contents = self._read_files(project, relevant_files)

        readable_files = {p: c for p, c in file_contents.items() if c is not None}

        if not readable_files:
            _progress("Could not read any files")
            return {
                "success": False,
                "error": "Could not read any of the relevant files",
            }

        total_chars = sum(len(c) for c in readable_files.values())
        _progress(f"Read {len(readable_files)} files ({total_chars:,} chars)")

        # Step 4: Generate fix
        _progress("Analyzing with Claude...")
        fix_result = self._generate_fix(
            task_brief=task_brief,
            file_contents=readable_files,
            project_path=project,
        )

        if fix_result.get("success"):
            _progress("Analysis complete")
        else:
            _progress(f"Analysis finished with errors: {fix_result.get('error', 'unknown')[:100]}")

        return fix_result

    # ------------------------------------------------------------------
    # File discovery (replaces SSH find)
    # ------------------------------------------------------------------

    def _explore_codebase(self, project_path: Path) -> list[str]:
        """
        Walk local filesystem to discover project files.

        Replaces WorkerAgent's SSH-based `find` command.
        Uses the same 5-minute TTL cache pattern.

        Returns:
            List of relative file paths.
        """
        cache_key = str(project_path)
        now = time.monotonic()

        if cache_key in self._structure_cache:
            cached_time, cached_files = self._structure_cache[cache_key]
            if now - cached_time < self._structure_cache_ttl:
                logger.debug(
                    f"Using cached file listing for {project_path.name} "
                    f"({len(cached_files)} files, {now - cached_time:.0f}s old)"
                )
                return cached_files

        files: list[str] = []
        try:
            for path in sorted(project_path.rglob("*")):
                if not path.is_file():
                    continue

                rel = path.relative_to(project_path)
                parts = rel.parts

                # Skip excluded directories
                if any(part in _EXCLUDE_DIRS for part in parts):
                    continue

                # Skip binary/non-code extensions
                if path.suffix.lower() in _EXCLUDE_EXTENSIONS:
                    continue

                files.append(str(rel))
                if len(files) >= 2000:
                    break
        except PermissionError:
            logger.warning(f"Permission denied walking {project_path}")

        self._structure_cache[cache_key] = (now, files)
        logger.info(f"Cached file listing for {project_path.name}: {len(files)} files")
        return files

    def invalidate_structure_cache(self, project_path: Optional[str] = None) -> None:
        """Clear the structure cache."""
        if project_path:
            self._structure_cache.pop(str(Path(project_path).resolve()), None)
        else:
            self._structure_cache.clear()

    # ------------------------------------------------------------------
    # File identification (same Claude call as remote worker)
    # ------------------------------------------------------------------

    def _identify_relevant_files(
        self,
        task_brief: str,
        all_files: list[str],
        hint_files: Optional[list[str]],
        project_path: Path,
    ) -> list[str]:
        """
        Use Claude to identify which files are relevant to the task.

        Same logic as WorkerAgent._identify_relevant_files but uses
        project path info instead of ClientConfig.
        """
        if hint_files:
            return hint_files

        # Auto-detect tech stack
        tech_stack = self._detect_tech_stack(project_path)

        files_list = "\n".join(all_files[:300])

        text = self._backend.complete(
            system=(
                "You are a code analyst. Given a task description and a list of "
                "files in a repository, identify which files are most likely "
                "relevant to the task. Return ONLY a JSON array of file paths, "
                "nothing else."
            ),
            messages=[{
                "role": "user",
                "content": f"""Task: {task_brief}

Tech stack: {tech_stack}
Project: {project_path.name}

Repository files:
{files_list}

Which files should I read to understand and fix this issue?
Return a JSON array of the most relevant file paths (max 15 files).
""",
            }],
            model=MODEL_EXECUTOR,
            max_tokens=1000,
        )

        try:
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            files = json.loads(text)
            if isinstance(files, list):
                return [f for f in files if f in all_files]
        except (json.JSONDecodeError, IndexError, KeyError):
            logger.warning("Failed to parse file identification response")

        return self._heuristic_file_search(task_brief, all_files)

    def _heuristic_file_search(
        self,
        task_brief: str,
        all_files: list[str],
    ) -> list[str]:
        """Fallback file search using keyword matching."""
        keywords = task_brief.lower().split()
        scored: list[tuple[str, int]] = []
        for filepath in all_files:
            score = sum(1 for kw in keywords if kw in filepath.lower())
            if score > 0:
                scored.append((filepath, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [path for path, _ in scored[:10]]

    def _detect_tech_stack(self, project_path: Path) -> str:
        """Auto-detect tech stack from project files."""
        indicators = []
        if (project_path / "pyproject.toml").exists():
            indicators.append("Python")
        if (project_path / "requirements.txt").exists():
            indicators.append("Python")
        if (project_path / "package.json").exists():
            indicators.append("Node.js")
        if (project_path / "Cargo.toml").exists():
            indicators.append("Rust")
        if (project_path / "go.mod").exists():
            indicators.append("Go")
        if (project_path / "Dockerfile").exists():
            indicators.append("Docker")
        if any(project_path.glob("*.py")):
            if "Python" not in indicators:
                indicators.append("Python")
        return "/".join(dict.fromkeys(indicators)) or "Unknown"

    # ------------------------------------------------------------------
    # File reading (replaces SSH read_files)
    # ------------------------------------------------------------------

    def _read_files(
        self,
        project_path: Path,
        file_paths: list[str],
    ) -> dict[str, Optional[str]]:
        """
        Read files from the local filesystem.

        Replaces WorkerAgent's SSH-based read_files.
        """
        results: dict[str, Optional[str]] = {}
        for rel_path in file_paths:
            full_path = project_path / rel_path
            try:
                results[rel_path] = full_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    results[rel_path] = full_path.read_text(encoding="latin-1")
                except Exception:
                    results[rel_path] = None
            except OSError:
                results[rel_path] = None
        return results

    # ------------------------------------------------------------------
    # Fix generation (same Claude call as remote worker)
    # ------------------------------------------------------------------

    def _generate_fix(
        self,
        task_brief: str,
        file_contents: dict[str, str],
        project_path: Path,
    ) -> dict[str, Any]:
        """
        Generate a code fix using Claude.

        Same logic as WorkerAgent._generate_fix but uses project path
        context instead of ClientConfig.
        """
        source_context = ""
        for path, content in file_contents.items():
            if len(content) > 10000:
                content = content[:10000] + "\n... [truncated] ..."
            source_context += f"\n=== {path} ===\n{content}\n"

        tech_stack = self._detect_tech_stack(project_path)

        prompt = f"""You are working on a local project.

PROJECT: {project_path.name}
TECH STACK: {tech_stack}

TASK:
{task_brief}

SOURCE CODE:
{source_context}

INSTRUCTIONS:
1. Analyze the codebase in the context of the task
2. If the task requires code changes, make the MINIMAL change needed -- don't refactor unrelated code
3. If the task is an investigation or question (e.g. "check if X is configured", "does Y exist"), answer the question based on what you see in the code -- do NOT make any changes
4. Preserve the existing code style

RESPONSE FORMAT:
You MUST return a JSON object (no markdown, no extra text). Use this format:
{{
  "analysis": "Brief analysis of what you found",
  "explanation": "Plain-language explanation of your findings or what you changed and why",
  "changes": {{
    "relative/path/to/file.py": "FULL new content of the file (not just the changed part)"
  }},
  "confidence": "high|medium|low",
  "warnings": ["any concerns or things to check"]
}}

IMPORTANT:
- If no code changes are needed (investigation/query tasks), set "changes" to an empty object {{}}.
- If code changes are needed, include the COMPLETE file content in "changes", not just the diff.
- Only include files that actually changed.
- Your response must be valid JSON. Do not wrap it in markdown code blocks.
"""

        try:
            text = self._backend.complete(
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                model=MODEL_MEETING_SUMMARY,
                max_tokens=8000,
            ).strip()

            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            result = json.loads(text)

            # Generate unified diff
            diff_parts = []
            changes = result.get("changes", {})
            for filepath, new_content in changes.items():
                old_content = file_contents.get(filepath, "")
                diff = difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{filepath}",
                    tofile=f"b/{filepath}",
                )
                diff_parts.append("".join(diff))

            return {
                "success": True,
                "analysis": result.get("analysis", ""),
                "changes": changes,
                "diff": "\n".join(diff_parts),
                "explanation": result.get("explanation", ""),
                "confidence": result.get("confidence", "unknown"),
                "warnings": result.get("warnings", []),
            }

        except json.JSONDecodeError as e:
            # Claude returned natural language instead of JSON.
            # This often happens for investigation/query tasks.
            # Treat the raw text as the analysis result.
            raw = text if "text" in dir() else ""
            if raw:
                logger.info(
                    f"Claude returned non-JSON response ({len(raw)} chars) — "
                    "treating as investigation result"
                )
                return {
                    "success": True,
                    "analysis": raw,
                    "changes": {},
                    "diff": "",
                    "explanation": raw,
                    "confidence": "medium",
                    "warnings": [],
                }
            logger.error(f"Failed to parse Claude's response as JSON: {e}")
            return {
                "success": False,
                "error": f"Failed to parse code fix response: {e}",
                "raw_response": raw,
            }
        except Exception as e:
            logger.error(f"Error generating fix: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Deployment (replaces SSH upload with local write + backup)
    # ------------------------------------------------------------------

    def deploy_approved_changes(
        self,
        project_path: str,
        file_changes: dict[str, str],
    ) -> dict[str, Any]:
        """
        Write approved changes to the local filesystem with backup.

        Creates a timestamped backup before writing. Rolls back if
        any write fails mid-way.

        Args:
            project_path: Project root directory.
            file_changes: Dict of relative paths -> new file content.

        Returns:
            Dict with success, files_deployed, backup_path, error.
        """
        try:
            project = self._validate_project(project_path)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        # Create timestamped backup directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self._backup_dir / project.name / timestamp
        backup_path.mkdir(parents=True, exist_ok=True)

        deployed: list[str] = []
        try:
            for rel_path, new_content in file_changes.items():
                target = project / rel_path

                # Backup existing file
                if target.exists():
                    backup_file = backup_path / rel_path
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup_file)

                # Write new content
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_content, encoding="utf-8")
                deployed.append(rel_path)

            self.invalidate_structure_cache(project_path)

            return {
                "success": True,
                "files_deployed": deployed,
                "backup_path": str(backup_path),
            }

        except Exception as e:
            # Rollback from backup
            for rel_path in deployed:
                backup_file = backup_path / rel_path
                if backup_file.exists():
                    target = project / rel_path
                    try:
                        shutil.copy2(backup_file, target)
                    except Exception:
                        pass  # Best-effort rollback
            return {
                "success": False,
                "error": str(e),
                "rolled_back": True,
                "files_attempted": deployed,
            }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return status information for this worker."""
        return {
            "status": "running" if self._running else "idle",
            "projects": [str(p) for p in self.projects],
            "cached_listings": len(self._structure_cache),
            "timestamp": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Message bus integration
    # ------------------------------------------------------------------

    def handle_task_request(self, message: dict) -> None:
        """
        Handle a task request from FDA via the message bus.

        Expected message body:
        {
            "project_path": "/path/to/project",
            "task_brief": "Fix the ...",
            "hint_files": ["path/to/file.py"],  // optional
        }
        """
        body = json.loads(message.get("body", "{}"))
        project_path = body.get("project_path")
        task_brief = body.get("task_brief")
        hint_files = body.get("hint_files")

        if not project_path or not task_brief:
            logger.error("Invalid task request: missing project_path or task_brief")
            return

        result = self.analyze_and_fix(project_path, task_brief, hint_files)

        if self.message_bus:
            self.message_bus.send(
                from_agent="worker_local",
                to_agent="fda",
                msg_type="TASK_RESULT",
                subject=f"Local fix for {Path(project_path).name}",
                body=json.dumps(result),
                reply_to=message.get("id"),
            )

    def handle_deploy_request(self, message: dict) -> None:
        """
        Handle a deploy request from FDA (after user approval).

        Expected message body:
        {
            "project_path": "/path/to/project",
            "file_changes": {"path/to/file.py": "new content..."}
        }
        """
        body = json.loads(message.get("body", "{}"))
        project_path = body.get("project_path")
        file_changes = body.get("file_changes", {})

        if not project_path or not file_changes:
            logger.error("Invalid deploy request")
            return

        result = self.deploy_approved_changes(project_path, file_changes)

        if self.message_bus:
            self.message_bus.send(
                from_agent="worker_local",
                to_agent="fda",
                msg_type="DEPLOY_RESULT",
                subject=f"Local deploy {'OK' if result.get('success') else 'FAILED'}",
                body=json.dumps(result),
                reply_to=message.get("id"),
            )

    def _handle_status_request(self, message: dict) -> None:
        """Respond to a status request."""
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker_local",
                to_agent=message.get("from", "fda"),
                msg_type="STATUS_RESPONSE",
                subject="Local Worker status",
                body=json.dumps(self.get_status()),
                reply_to=message.get("id"),
            )

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def run_event_loop(self) -> None:
        """
        Main event loop -- listen for task and deploy requests.

        Polls the message bus every 2 seconds for messages addressed
        to "worker_local".
        """
        logger.info("Local Worker agent started, listening for requests...")

        if self.state:
            self.state.update_agent_status("worker_local", "running")

        while True:
            try:
                if self.message_bus:
                    messages = self.message_bus.get_pending("worker_local")
                    for msg in messages:
                        msg_type = msg.get("type", "")
                        self.message_bus.mark_read(msg["id"])

                        if msg_type == "TASK_REQUEST":
                            self.handle_task_request(msg)
                        elif msg_type == "DEPLOY_REQUEST":
                            self.handle_deploy_request(msg)
                        elif msg_type == "STATUS_REQUEST":
                            self._handle_status_request(msg)
                        else:
                            logger.debug(f"Ignoring message type: {msg_type}")

                    if self.state:
                        self.state.agent_heartbeat("worker_local")

                time.sleep(2)

            except KeyboardInterrupt:
                logger.info("Local Worker agent shutting down...")
                if self.state:
                    self.state.update_agent_status("worker_local", "stopped")
                break
            except Exception as e:
                logger.error(f"Error in local worker event loop: {e}", exc_info=True)
                time.sleep(5)
