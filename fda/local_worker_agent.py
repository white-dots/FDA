"""
Local Worker Agent -- operates on the local Mac Mini filesystem.

Uses Claude's tool-use API to autonomously explore and modify local codebases.
Instead of walking all files upfront and sending them to Claude to pick,
Claude uses tools (list_directory, read_file, search_files, etc.) to decide
what to explore — like a developer would.

Also supports file organization tasks — scanning directories, understanding
file purposes and relationships, and sorting them into logical folders.
Git repositories are detected and left untouched.
"""

import json
import logging
import difflib
import os
import subprocess
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from fda.base_agent import BaseAgent
from fda.claude_backend import get_claude_backend, ToolLoopTimeoutError
from fda.config import (
    MODEL_EXECUTOR,
    MODEL_MEETING_SUMMARY,
    LOCAL_WORKER_PROJECTS,
    LOCAL_WORKER_BACKUP_DIR,
    ANALYZE_TIMEOUT_SECONDS,
    REPO_DISCOVERY_SKIP_DIRS,
    REPO_DISCOVERY_MAX_DEPTH,
)
from fda.comms.message_bus import MessageBus

logger = logging.getLogger(__name__)

# Directories to skip when listing
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

# ---------------------------------------------------------------------------
# Tool definitions for the agentic loop
# ---------------------------------------------------------------------------

_LOCAL_WORKER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_directory",
        "description": (
            "List files and subdirectories in a directory. "
            "Returns filenames with '/' suffix for directories. "
            "Use '.' for the project root. Good for understanding project structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from project root. Use '.' for root.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full contents of a file. Use this to examine source "
            "code, configs, or any text file in the project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path from project root.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "Search for a regex pattern across files in the project (like grep). "
            "Returns matching lines with file paths and line numbers. "
            "Great for finding function definitions, imports, error messages, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search pattern (regular expression).",
                },
                "path": {
                    "type": "string",
                    "description": "Subdirectory to search in (relative to project root). Defaults to '.'.",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "File glob to filter (e.g. '*.py', '*.sql'). Defaults to all code files.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Record a code change to a file. Provide the COMPLETE new file "
            "content, not just the changed part. The change will be applied "
            "after user approval. You MUST read the file first before writing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path from project root.",
                },
                "content": {
                    "type": "string",
                    "description": "The COMPLETE new content for the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Execute a shell command in the project directory. "
            "Use for running tests, checking git status, inspecting "
            "processes, etc. Output is capped at 10k chars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
            },
            "required": ["command"],
        },
    },
]


def _plan_result_to_dict(result) -> dict[str, Any]:
    """Translate a fda.organize.PlanResult into the back-compat dict shape.

    Back-compat keys: success, summary, moves, deletions, dirs_created,
    repos_skipped. Additive: per-move `reason`, plus `discrepancies`,
    `leftover_empty_dirs`, and `failures` (one entry per failed outcome
    so the orchestrator can render the spec's `## Couldn't Complete`
    section).
    """
    from fda.organize.models import OperationKind

    moves: list[dict[str, str]] = []
    deletions: list[dict[str, str]] = []
    dirs_created: list[str] = []
    failures: list[dict[str, str]] = []
    for outcome in result.outcomes:
        op = outcome.operation
        if outcome.status == "failed":
            failures.append({
                "kind": op.kind.value,
                "source": op.source or "",
                "destination": op.destination or "",
                "reason": op.reason,
                "error": outcome.error or "",
            })
            continue
        if outcome.status not in ("applied", "rescued", "skipped"):
            continue
        if op.kind == OperationKind.MOVE:
            moves.append({
                "from": op.source,
                "to": op.destination,
                "reason": op.reason,
            })
        elif op.kind == OperationKind.DELETE:
            deletions.append({"path": op.source, "reason": op.reason})
        elif op.kind == OperationKind.CREATE_DIR:
            dirs_created.append(op.destination)

    success = (
        all(o.status in ("applied", "rescued", "skipped") for o in result.outcomes)
        and not result.discrepancies
    )

    return {
        "success": success,
        "summary": result.summary,
        "moves": moves,
        "deletions": deletions,
        "dirs_created": dirs_created,
        "repos_skipped": list(result.repos_skipped),
        "discrepancies": list(result.discrepancies),
        "leftover_empty_dirs": list(result.leftover_empty_dirs),
        "failures": failures,
        "log_path": result.log_path,
    }


class LocalWorkerAgent(BaseAgent):
    """
    Local filesystem worker agent.

    Uses Claude's tool-use API to autonomously explore and modify local
    codebases. Claude decides which files to read and what to change,
    rather than scanning all files upfront.

    Also supports file organization — scanning directories, understanding
    file purposes, and sorting them into logical folders while respecting
    git repositories.
    """

    SYSTEM_PROMPT = """You are the Local Worker agent for Datacore.
Your job is to analyze codebases and make code changes on the local Mac Mini filesystem based on task briefs.

You have tools to explore and modify the filesystem:
- list_directory: See what files and folders exist in a directory
- read_file: Read a file's contents
- search_files: Search for patterns across files (like grep)
- write_file: Record code changes (provide COMPLETE file content)
- run_command: Execute shell commands (tests, git, etc.)

WORKFLOW:
1. Start by listing the project root to understand the structure
2. Read key files (README, config files, etc.) to understand the project
3. Use search_files to find relevant code patterns
4. Read the specific files you need to understand
5. Analyze and form your response
6. If code changes are needed, use write_file with the COMPLETE new file content

IMPORTANT RULES:
- Make minimal changes — don't refactor unrelated code
- Preserve the existing code style (indentation, naming conventions, etc.)
- If you're unsure about the fix, say so rather than guessing
- Always explain what you found/changed and why in plain language
- Consider edge cases and potential side effects
- Never change database schemas without explicit approval
- Never delete data or drop tables
- For investigation/query tasks, just report your findings — no changes needed
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
        self._backup_dir = LOCAL_WORKER_BACKUP_DIR

        # Tool-use state (reset per analyze_and_fix call)
        self._current_project: Optional[Path] = None
        self._pending_changes: dict[str, str] = {}
        self._files_read: dict[str, str] = {}

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
    # Main entry point — agentic tool-use loop
    # ------------------------------------------------------------------

    def analyze_and_fix(
        self,
        project_path: str,
        task_brief: str,
        hint_files: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Analyze a task and generate a code fix using agentic tool-use.

        Instead of walking all files upfront, Claude uses tools to
        autonomously explore the codebase and decide what to read/change.

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

        # Reset tool-use state for this call
        self._current_project = project
        self._pending_changes = {}
        self._files_read = {}

        tech_stack = self._detect_tech_stack(project)

        # Build context with hint files if provided
        hint_context = ""
        if hint_files:
            hint_context = (
                f"\n\nHINT: Start by examining these files: "
                f"{', '.join(hint_files)}"
            )

        messages = [{
            "role": "user",
            "content": (
                f"PROJECT: {project.name}\n"
                f"TECH STACK: {tech_stack}\n"
                f"ROOT: {project}\n\n"
                f"TASK:\n{task_brief}"
                f"{hint_context}\n\n"
                "Please explore the codebase and address this task. "
                "Use list_directory and read_file to understand the code, "
                "search_files to find relevant patterns, and write_file "
                "if code changes are needed."
            ),
        }]

        _progress(f"Analyzing {project.name} with tool-use...")

        try:
            response = self._backend.complete_with_tools(
                system=self.SYSTEM_PROMPT,
                messages=messages,
                tools=_LOCAL_WORKER_TOOLS,
                tool_executor=self._execute_tool,
                model=MODEL_MEETING_SUMMARY,
                max_tokens=8000,
                max_iterations=15,
                progress_callback=_progress,
                timeout=ANALYZE_TIMEOUT_SECONDS,
            )
        except ToolLoopTimeoutError as e:
            logger.warning(f"Local worker timed out: {e}")
            return {
                "success": False,
                "error": f"Analysis timed out after {e.elapsed:.0f}s. Try a simpler task or a more specific request.",
            }
        except Exception as e:
            logger.error(f"Tool-use loop failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

        # Build result from tool-use state
        changes = dict(self._pending_changes)
        diff_parts = []
        for filepath, new_content in changes.items():
            old_content = self._files_read.get(filepath, "")
            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{filepath}",
                tofile=f"b/{filepath}",
            )
            diff_parts.append("".join(diff))

        if changes:
            _progress(f"Generated changes to {len(changes)} file(s)")
        else:
            _progress("Analysis complete (no code changes)")

        return {
            "success": True,
            "analysis": response,
            "changes": changes,
            "diff": "\n".join(diff_parts),
            "explanation": response,
            "confidence": "high" if changes else "medium",
            "warnings": [],
        }

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Execute a worker tool and return the result as a string."""
        project = self._current_project
        if project is None:
            return "Error: no project context set"

        try:
            if tool_name == "list_directory":
                return self._tool_list_directory(project, tool_input)
            elif tool_name == "read_file":
                return self._tool_read_file(project, tool_input)
            elif tool_name == "search_files":
                return self._tool_search_files(project, tool_input)
            elif tool_name == "write_file":
                return self._tool_write_file(project, tool_input)
            elif tool_name == "run_command":
                return self._tool_run_command(project, tool_input)
            else:
                return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.error(f"Tool {tool_name} error: {e}", exc_info=True)
            return f"Error executing {tool_name}: {e}"

    def _tool_list_directory(
        self, project: Path, tool_input: dict[str, Any]
    ) -> str:
        """List files and directories."""
        rel_path = tool_input.get("path", ".")
        full_path = (project / rel_path).resolve()

        if not full_path.is_relative_to(project):
            return "Error: Path is outside the project directory"
        if not full_path.is_dir():
            return f"Error: Not a directory: {rel_path}"

        entries: list[str] = []
        try:
            for entry in sorted(full_path.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    if entry.name not in _EXCLUDE_DIRS:
                        entries.append(f"{entry.name}/")
                elif entry.is_file():
                    if entry.suffix.lower() not in _EXCLUDE_EXTENSIONS:
                        entries.append(entry.name)
        except PermissionError:
            return f"Error: Permission denied: {rel_path}"

        return "\n".join(entries) if entries else "(empty directory)"

    def _tool_read_file(
        self, project: Path, tool_input: dict[str, Any]
    ) -> str:
        """Read a file's contents."""
        rel_path = tool_input.get("path", "")
        if not rel_path:
            return "Error: path is required"

        full_path = (project / rel_path).resolve()
        if not full_path.is_relative_to(project):
            return "Error: Path is outside the project directory"
        if not full_path.is_file():
            return f"Error: File not found: {rel_path}"

        try:
            content = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = full_path.read_text(encoding="latin-1")
            except Exception as e:
                return f"Error reading file: {e}"
        except Exception as e:
            return f"Error reading file: {e}"

        # Store for diffing later
        self._files_read[rel_path] = content

        if len(content) > 30000:
            return (
                content[:30000]
                + f"\n... [truncated — file is {len(content):,} chars total]"
            )
        return content

    def _tool_search_files(
        self, project: Path, tool_input: dict[str, Any]
    ) -> str:
        """Search for a pattern in files (like grep)."""
        pattern = tool_input.get("pattern", "")
        if not pattern:
            return "Error: pattern is required"

        rel_path = tool_input.get("path", ".")
        file_pattern = tool_input.get("file_pattern", "")

        full_path = (project / rel_path).resolve()
        if not full_path.is_relative_to(project):
            return "Error: Path is outside the project directory"

        # Build grep command
        if file_pattern:
            include_flags = f'--include="{file_pattern}"'
        else:
            include_flags = (
                '--include="*.py" --include="*.sql" --include="*.yaml" '
                '--include="*.yml" --include="*.sh" --include="*.cfg" '
                '--include="*.conf" --include="*.json" --include="*.toml" '
                '--include="*.js" --include="*.ts" --include="*.html" '
                '--include="*.css" --include="*.md" --include="*.txt"'
            )

        # Escape double quotes in pattern for shell safety
        safe_pattern = pattern.replace('"', '\\"')

        cmd = (
            f'grep -rn -i {include_flags} '
            f'--exclude-dir=node_modules --exclude-dir=__pycache__ '
            f'--exclude-dir=.git --exclude-dir=venv --exclude-dir=.venv '
            f'--exclude-dir=env --exclude-dir=.env '
            f'-E "{safe_pattern}" . 2>/dev/null | head -50'
        )

        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=15, cwd=str(full_path),
            )
            output = result.stdout.strip()
            if output:
                return output
            return f"No matches found for pattern: {pattern}"
        except subprocess.TimeoutExpired:
            return "Search timed out (15s limit)"
        except Exception as e:
            return f"Search error: {e}"

    def _tool_write_file(
        self, project: Path, tool_input: dict[str, Any]
    ) -> str:
        """Record a file change (applied after user approval)."""
        rel_path = tool_input.get("path", "")
        content = tool_input.get("content", "")
        if not rel_path:
            return "Error: path is required"
        if not content:
            return "Error: content is required"

        full_path = (project / rel_path).resolve()
        if not full_path.is_relative_to(project):
            return "Error: Path is outside the project directory"

        # Store the pending change
        self._pending_changes[rel_path] = content

        # Read original for diffing if not already read
        if rel_path not in self._files_read and full_path.is_file():
            try:
                self._files_read[rel_path] = full_path.read_text(
                    encoding="utf-8"
                )
            except Exception:
                self._files_read[rel_path] = ""

        return (
            f"✓ Recorded change to {rel_path} ({len(content):,} chars). "
            "Change will be applied after approval."
        )

    def _tool_run_command(
        self, project: Path, tool_input: dict[str, Any]
    ) -> str:
        """Execute a shell command in the project directory."""
        command = tool_input.get("command", "")
        if not command:
            return "Error: command is required"

        # Block dangerous commands
        dangerous_patterns = [
            "rm -rf /", "rm -r /", "dd if=", "mkfs.", "> /dev/",
            "chmod -R 777", ":(){", "fork bomb",
        ]
        cmd_lower = command.lower()
        for d in dangerous_patterns:
            if d in cmd_lower:
                return f"Error: Potentially dangerous command blocked"

        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=30, cwd=str(project),
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += f"\n[stderr]\n{result.stderr}"
                else:
                    output = result.stderr
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output[:10000] if output else "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out (30s limit)"
        except Exception as e:
            return f"Command error: {e}"

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

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
    # Repository discovery
    # ------------------------------------------------------------------

    def discover_repos(self) -> list[dict[str, Any]]:
        """
        Scan allowed project roots for git repositories.

        Walks each directory in self.projects up to REPO_DISCOVERY_MAX_DEPTH
        levels deep, looking for .git directories. Registers newly found
        repos in ProjectState and logs discoveries to the journal.

        Returns:
            List of newly discovered repo dicts.
        """
        newly_discovered: list[dict[str, Any]] = []
        known_paths: set[str] = set()

        # Load already-known projects from state
        if self.state:
            try:
                for proj in self.state.get_all_projects():
                    known_paths.add(proj["path"])
            except Exception as e:
                logger.warning(f"Could not load known projects: {e}")

        for root in self.projects:
            if not root.is_dir():
                continue
            self._scan_for_repos(
                root, 0, REPO_DISCOVERY_MAX_DEPTH,
                known_paths, newly_discovered,
            )

        # Register new repos in ProjectState
        for repo_info in newly_discovered:
            if self.state:
                try:
                    self.state.add_project(
                        path=repo_info["path"],
                        name=repo_info["name"],
                        tech_stack=repo_info.get("tech_stack_list"),
                        git_remote=repo_info.get("git_remote"),
                        git_branch=repo_info.get("git_branch"),
                        git_commit_hash=repo_info.get("git_commit_hash"),
                    )
                except Exception as e:
                    logger.warning(f"Could not register repo {repo_info['name']}: {e}")

        # Journal log if any new repos found
        if newly_discovered:
            names = [r["name"] for r in newly_discovered]
            try:
                self.log_to_journal(
                    summary=f"Discovered {len(newly_discovered)} new repo(s): {', '.join(names[:5])}",
                    content=(
                        "## Newly Discovered Repositories\n\n"
                        + "\n".join(
                            f"- **{r['name']}** — `{r['path']}` ({r.get('tech_stack', 'unknown')})"
                            for r in newly_discovered
                        )
                    ),
                    tags=["discovery", "repos", "local-worker"],
                    relevance_decay="slow",
                )
            except Exception as e:
                logger.warning(f"Failed to log repo discovery to journal: {e}")

        logger.info(
            f"Repo discovery complete: {len(newly_discovered)} new, "
            f"{len(known_paths)} previously known"
        )
        return newly_discovered

    def _scan_for_repos(
        self,
        directory: Path,
        current_depth: int,
        max_depth: int,
        known_paths: set[str],
        results: list[dict[str, Any]],
    ) -> None:
        """Recursively scan a directory for git repositories."""
        if current_depth > max_depth:
            return

        try:
            entries = sorted(directory.iterdir())
        except (PermissionError, OSError):
            return

        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.name in REPO_DISCOVERY_SKIP_DIRS:
                continue
            if entry.name.startswith(".") and entry.name != ".git":
                continue

            # Found a git repo
            if (entry / ".git").exists():
                resolved_path = str(entry.resolve())
                if resolved_path not in known_paths:
                    repo_info = self._extract_repo_metadata(entry)
                    results.append(repo_info)
                    known_paths.add(resolved_path)
                # Don't recurse into git repos
                continue

            # Recurse into subdirectories
            self._scan_for_repos(
                entry, current_depth + 1, max_depth,
                known_paths, results,
            )

    def _extract_repo_metadata(self, repo_path: Path) -> dict[str, Any]:
        """Extract metadata from a git repository."""
        resolved = str(repo_path.resolve())
        name = repo_path.name
        tech_stack = self._detect_tech_stack(repo_path)

        git_remote = None
        git_branch = None
        git_commit_hash = None

        try:
            result = subprocess.run(
                ["git", "-C", resolved, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                git_remote = result.stdout.strip()
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["git", "-C", resolved, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                git_branch = result.stdout.strip()
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["git", "-C", resolved, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                git_commit_hash = result.stdout.strip()
        except Exception:
            pass

        tech_list = None
        if tech_stack and tech_stack != "Unknown":
            tech_list = [t.strip() for t in tech_stack.split("/") if t.strip()]

        return {
            "path": resolved,
            "name": name,
            "tech_stack": tech_stack,
            "tech_stack_list": tech_list,
            "git_remote": git_remote,
            "git_branch": git_branch,
            "git_commit_hash": git_commit_hash,
        }

    def get_repo_shortcuts(self) -> dict[str, str]:
        """
        Return a mapping of repo name -> repo path for all known repos.

        Allows users to reference repos by name (e.g. "FDA") instead of
        full paths.
        """
        shortcuts: dict[str, str] = {}
        if self.state:
            try:
                for proj in self.state.get_all_projects():
                    name = proj["name"].lower()
                    shortcuts[name] = proj["path"]
                    # Also add without hyphens/underscores
                    simple = name.replace("-", "").replace("_", "")
                    if simple != name:
                        shortcuts[simple] = proj["path"]
            except Exception:
                pass
        return shortcuts

    def resolve_project_path(self, name_or_path: str) -> str:
        """
        Resolve a project name shortcut or path to an absolute path.

        Checks shortcuts first, then falls back to raw path handling.
        """
        # Already an absolute path
        if name_or_path.startswith("/") or name_or_path.startswith("~"):
            return str(Path(name_or_path).expanduser().resolve())

        # Try shortcut lookup
        shortcuts = self.get_repo_shortcuts()
        lookup = name_or_path.lower().strip()
        if lookup in shortcuts:
            return shortcuts[lookup]

        # Fallback: return as-is
        return name_or_path

    # ------------------------------------------------------------------
    # File organization — reader+classifier+plan_builder+executor+verifier
    # ------------------------------------------------------------------

    def organize_files(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Organize files in `target_path` via the
        reader+classifier+plan_builder+executor+verifier pipeline. Returns
        the back-compat dict shape used by Telegram, the web UI, and the
        orchestrator."""
        from fda.organize import organize as _organize
        from fda.organize.models import PlanResult

        try:
            target_path = self.resolve_project_path(target_path)
            result = _organize(
                target_path,
                instructions,
                backend=self._backend,
                allowed_roots=self.projects,
                progress_callback=progress_callback,
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"organize_files failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

        assert isinstance(result, PlanResult)
        return _plan_result_to_dict(result)

    def organize_files_preview(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        """Build a Plan without executing it. Returns a fda.organize.Plan."""
        from fda.organize import organize as _organize

        target_path = self.resolve_project_path(target_path)
        return _organize(
            target_path,
            instructions,
            preview=True,
            backend=self._backend,
            allowed_roots=self.projects,
            progress_callback=progress_callback,
        )

    def organize_files_apply(
        self,
        plan,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Apply a previously-generated Plan. Returns the back-compat dict."""
        from fda.organize import apply_plan as _apply

        try:
            result = _apply(plan, progress_callback=progress_callback)
        except Exception as e:
            logger.error(f"organize_files_apply failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
        return _plan_result_to_dict(result)

    def _is_inside_git_repo(self, path: Path) -> bool:
        """Back-compat shim — delegates to fda.organize._fs."""
        from fda.organize import _fs
        return _fs.is_inside_git_repo(path)

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        """Convert bytes to human-readable size."""
        for unit in ("B", "KB", "MB", "GB"):
            if abs(size_bytes) < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024  # type: ignore[assignment]
        return f"{size_bytes:.1f} TB"

    # ------------------------------------------------------------------
    # Deployment (local write + backup)
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

    def _start_file_indexer_schedule(self) -> None:
        """
        Start the daily file indexer schedule.

        Runs the indexer once shortly after startup (10 min delay), then
        daily at the configured hour (default 3 AM). Uses a local embedding
        model (no API key required).
        """
        if not self.state:
            return
        try:
            from fda.config import FILE_INDEXER_DAILY_HOUR
            from fda.file_indexer import FileIndexer
            from fda.scheduler import Scheduler
        except ImportError as e:
            logger.warning(f"[worker_local] cannot start file indexer: {e}")
            return

        def run_indexer() -> None:
            try:
                logger.info("[worker_local] Running scheduled file indexer...")
                indexer = FileIndexer(self.state)
                stats = indexer.run()
                logger.info(f"[worker_local] Indexer finished: {stats.to_dict()}")
            except Exception as e:
                logger.error(f"[worker_local] Indexer run failed: {e}", exc_info=True)

        self._indexer_scheduler = Scheduler()
        daily_time = f"{FILE_INDEXER_DAILY_HOUR:02d}:00"
        self._indexer_scheduler.register_daily_task("file_indexer", daily_time, run_indexer)
        # Also trigger one run 10 minutes after startup so the index is warm
        self._indexer_scheduler.register_one_time("file_indexer_startup", run_indexer, 600)
        self._indexer_scheduler.run_in_background()
        logger.info(f"[worker_local] File indexer scheduled daily at {daily_time}")

    def run_event_loop(self) -> None:
        """
        Main event loop -- listen for task and deploy requests.

        Polls the message bus every 2 seconds for messages addressed
        to "worker_local".
        """
        logger.info("Local Worker agent started, listening for requests...")

        if self.state:
            self.state.update_agent_status("worker_local", "running")

        # Start the file indexer scheduler (daily run + one delayed startup run)
        self._start_file_indexer_schedule()

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
