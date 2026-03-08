"""
Worker Agent — merged Librarian + Executor for remote code operations.

Uses Claude's tool-use API to autonomously explore and modify remote
codebases via SSH. Instead of walking all files upfront, Claude uses
tools (list_directory, read_file, search_files, etc.) to decide what
to explore — like a developer would.

The Worker handles the technical side of client requests:
1. Receives task briefs from FDA via the message bus
2. Uses tools to explore the codebase on the Azure VM via SSH
3. Generates code fixes autonomously
4. Creates diffs for FDA to send to the user for approval
5. Deploys approved changes to the VM
"""

import json
import logging
import difflib
import re
import time
from typing import Any, Callable, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.claude_backend import get_claude_backend, ToolLoopTimeoutError
from fda.config import MODEL_EXECUTOR, MODEL_MEETING_SUMMARY, ANALYZE_TIMEOUT_SECONDS
from fda.clients.client_config import ClientConfig, ClientManager
from fda.remote.ssh_manager import SSHManager
from fda.remote.deploy import Deployer, DeployResult
from fda.comms.message_bus import MessageBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stopwords for keyword extraction (kept for backward compatibility)
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "what", "which", "who", "whom", "where", "when", "how", "why",
    "and", "or", "but", "if", "then", "so", "because", "as", "of",
    "at", "by", "for", "with", "about", "against", "between", "through",
    "to", "from", "in", "on", "up", "out", "off", "over", "under",
    "not", "no", "nor", "only", "very", "too", "also", "just",
    "check", "look", "find", "show", "tell", "explain", "describe",
    "works", "work", "working", "make", "run", "use", "used",
})

# ---------------------------------------------------------------------------
# Tool definitions for the agentic loop
# ---------------------------------------------------------------------------

_REMOTE_WORKER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_directory",
        "description": (
            "List files and subdirectories on the remote VM. "
            "Returns directory contents. Use '.' for the repo root. "
            "Good for understanding project structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path on the remote VM, relative to repo root "
                        "or absolute. Use '.' for the repo root."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full contents of a file from the remote VM. "
            "Use this to examine source code, configs, or any text file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File path on the remote VM (relative to repo root "
                        "or absolute)."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": (
            "Search for a regex pattern in files on the remote VM (like grep). "
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
                    "description": (
                        "Directory to search in (relative to repo root or "
                        "absolute). Defaults to repo root."
                    ),
                },
                "file_pattern": {
                    "type": "string",
                    "description": (
                        "File glob to filter (e.g. '*.py', '*.sql'). "
                        "Defaults to all code files."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Record a code change to a file on the remote VM. Provide the "
            "COMPLETE new file content, not just the changed part. The "
            "change will be applied after user approval. You MUST read "
            "the file first before writing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path on the remote VM.",
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
            "Execute a shell command on the remote VM via SSH. "
            "Use for checking service status, running tests, "
            "inspecting logs, git operations, etc. Output is capped at 10k chars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute on the remote VM.",
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory for the command. "
                        "Defaults to the repo root."
                    ),
                },
            },
            "required": ["command"],
        },
    },
]


class WorkerAgent(BaseAgent):
    """
    Technical execution agent for Datacore client operations.

    Uses Claude's tool-use API to autonomously explore and modify
    client codebases on remote Azure VMs via SSH. Claude decides
    which files to read and what to change, rather than scanning
    all files upfront.
    """

    SYSTEM_PROMPT = """You are the Worker agent for Datacore, a software consultancy.
Your job is to analyze codebases and make code changes on client Azure VMs based on task briefs from the FDA agent.

You have tools to explore and modify the remote filesystem via SSH:
- list_directory: See what files and folders exist on the VM
- read_file: Read a file's contents from the VM
- search_files: Search for patterns across files (like grep)
- write_file: Record code changes (provide COMPLETE file content)
- run_command: Execute shell commands on the VM

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
        client_manager: ClientManager,
        message_bus: Optional[MessageBus] = None,
        db_path: Optional[str] = None,
    ):
        """
        Initialize the Worker agent.

        Args:
            client_manager: Manager for client configs.
            message_bus: Inter-agent message bus.
            db_path: Path to SQLite database.
        """
        super().__init__(
            name="worker",
            model=MODEL_EXECUTOR,
            system_prompt=self.SYSTEM_PROMPT,
            message_bus=message_bus,
            db_path=db_path,
        )
        self.client_manager = client_manager
        self._backend = get_claude_backend()

        # Cache SSH connections per client
        self._ssh_connections: dict[str, SSHManager] = {}
        self._deployers: dict[str, Deployer] = {}

        # Tool-use state (reset per analyze_and_fix call)
        self._current_ssh: Optional[SSHManager] = None
        self._current_client: Optional[ClientConfig] = None
        self._current_repo_path: Optional[str] = None
        self._pending_changes: dict[str, str] = {}
        self._files_read: dict[str, str] = {}

        # Warm up SSH connections to all clients at init time
        self._warmup_connections()

    def _get_ssh(self, client: ClientConfig) -> SSHManager:
        """Get or create SSH connection for a client."""
        if client.client_id not in self._ssh_connections:
            self._ssh_connections[client.client_id] = SSHManager(
                host=client.vm.host,
                user=client.vm.ssh_user,
                ssh_key=client.vm.ssh_key,
                port=client.vm.port,
            )
        return self._ssh_connections[client.client_id]

    def _get_deployer(self, client: ClientConfig) -> Deployer:
        """Get or create deployer for a client."""
        if client.client_id not in self._deployers:
            self._deployers[client.client_id] = Deployer(client)
        return self._deployers[client.client_id]

    def _warmup_connections(self) -> None:
        """Proactively establish SSH ControlMaster connections to all clients.

        Called at init time so the first analyze_and_fix() call doesn't pay
        the ~1.5s TCP+SSH handshake cost. The ControlMaster persists for
        10 minutes of inactivity, and subsequent SSH commands multiplex
        over it (~50ms instead of ~1.5s).
        """
        for client in self.client_manager.list_clients():
            try:
                ssh = self._get_ssh(client)
                if ssh.warmup():
                    logger.info(
                        f"SSH connection warmed up for {client.name} "
                        f"({client.vm.host})"
                    )
                else:
                    logger.warning(
                        f"Failed to warm up SSH for {client.name} "
                        f"({client.vm.host})"
                    )
            except Exception as e:
                logger.warning(f"SSH warmup error for {client.name}: {e}")

    # ------------------------------------------------------------------
    # Main entry point — agentic tool-use loop
    # ------------------------------------------------------------------

    def analyze_and_fix(
        self,
        client_id: str,
        task_brief: str,
        hint_files: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Analyze a client request and generate a code fix using agentic tool-use.

        Instead of walking all files upfront, Claude uses tools to
        autonomously explore the remote codebase and decide what to
        read/change.

        Args:
            client_id: Client identifier.
            task_brief: Business-context task description from FDA.
            hint_files: Optional list of file paths to examine first.
            progress_callback: Optional callback for live progress updates.

        Returns:
            Dict with:
              - success: bool
              - analysis: str
              - changes: dict[str, str] (file path -> new content)
              - diff: str (unified diff)
              - explanation: str
              - error: Optional[str]
        """
        def _progress(msg: str) -> None:
            """Send a progress update (to callback + logger)."""
            logger.info(f"[Worker] {msg}")
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass

        client = self.client_manager.get_client(client_id)
        if not client:
            return {
                "success": False,
                "error": f"Unknown client: {client_id}",
            }

        ssh = self._get_ssh(client)
        repo_path = client.project.repo_path

        # Reset tool-use state for this call
        self._current_ssh = ssh
        self._current_client = client
        self._current_repo_path = repo_path
        self._pending_changes = {}
        self._files_read = {}

        # Build context with hint files if provided
        hint_context = ""
        if hint_files:
            hint_context = (
                f"\n\nHINT: Start by examining these files: "
                f"{', '.join(hint_files)}"
            )

        # Include extra repo paths in the context
        extra_paths = client.project.extra_repo_paths or []
        extra_context = ""
        if extra_paths:
            extra_context = (
                f"\n\nADDITIONAL PATHS: The project also includes these "
                f"directories: {', '.join(extra_paths)}"
            )

        messages = [{
            "role": "user",
            "content": (
                f"{client.get_context_for_prompt()}\n\n"
                f"REPO ROOT: {repo_path}\n"
                f"TASK:\n{task_brief}"
                f"{hint_context}"
                f"{extra_context}\n\n"
                "Please explore the codebase via the tools and address "
                "this task. Use list_directory and read_file to understand "
                "the code, search_files to find relevant patterns, and "
                "write_file if code changes are needed."
            ),
        }]

        _progress(f"📂 Analyzing {client.name} codebase with tool-use...")

        try:
            response = self._backend.complete_with_tools(
                system=self.SYSTEM_PROMPT,
                messages=messages,
                tools=_REMOTE_WORKER_TOOLS,
                tool_executor=self._execute_tool,
                model=MODEL_MEETING_SUMMARY,
                max_tokens=8000,
                max_iterations=15,
                progress_callback=_progress,
                timeout=ANALYZE_TIMEOUT_SECONDS,
            )
        except ToolLoopTimeoutError as e:
            logger.warning(f"Remote worker timed out: {e}")
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

        is_investigation = not changes
        if changes:
            _progress(f"✅ Generated changes to {len(changes)} file(s)")
        else:
            _progress("✅ Analysis complete (no code changes)")

        return {
            "success": True,
            "investigation": is_investigation,
            "analysis": response,
            "changes": changes,
            "diff": "\n".join(diff_parts),
            "explanation": response,
            "confidence": "high" if changes else "medium",
            "warnings": [],
        }

    # ------------------------------------------------------------------
    # Tool execution (via SSH)
    # ------------------------------------------------------------------

    def _execute_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Execute a worker tool via SSH and return the result."""
        ssh = self._current_ssh
        repo_path = self._current_repo_path

        if ssh is None or repo_path is None:
            return "Error: no SSH context set"

        try:
            if tool_name == "list_directory":
                return self._tool_list_directory(ssh, repo_path, tool_input)
            elif tool_name == "read_file":
                return self._tool_read_file(ssh, repo_path, tool_input)
            elif tool_name == "search_files":
                return self._tool_search_files(ssh, repo_path, tool_input)
            elif tool_name == "write_file":
                return self._tool_write_file(tool_input)
            elif tool_name == "run_command":
                return self._tool_run_command(ssh, repo_path, tool_input)
            else:
                return f"Unknown tool: {tool_name}"
        except Exception as e:
            logger.error(f"Tool {tool_name} error: {e}", exc_info=True)
            return f"Error executing {tool_name}: {e}"

    def _resolve_path(self, repo_path: str, rel_path: str) -> str:
        """Resolve a relative path to an absolute path on the remote VM."""
        if not rel_path or rel_path == ".":
            return repo_path
        if rel_path.startswith("/"):
            return rel_path  # already absolute
        if rel_path.startswith("~"):
            return rel_path  # let the remote shell expand it
        return f"{repo_path}/{rel_path}"

    def _tool_list_directory(
        self,
        ssh: SSHManager,
        repo_path: str,
        tool_input: dict[str, Any],
    ) -> str:
        """List files and directories on the remote VM."""
        rel_path = tool_input.get("path", ".")
        full_path = self._resolve_path(repo_path, rel_path)

        # List with file types: d for dir, f for file
        cmd = (
            f'ls -1ap "{full_path}" 2>/dev/null '
            f'| grep -v "^\\.$" | grep -v "^\\.\\.$" | head -200'
        )
        result = ssh.execute(cmd, timeout=10)

        if not result.success or not result.stdout.strip():
            # Try with find as fallback
            cmd = (
                f'find "{full_path}" -maxdepth 1 -mindepth 1 '
                f'! -name ".*" '
                f'! -name "node_modules" ! -name "__pycache__" '
                f'! -name "venv" ! -name ".venv" '
                f'-printf "%f%y\\n" 2>/dev/null '
                f'| sort | head -200'
            )
            result = ssh.execute(cmd, timeout=10)
            if not result.success or not result.stdout.strip():
                return f"Error: Could not list directory: {rel_path}"

            # Parse find output (filename + type char: d for dir, f for file)
            entries = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.endswith("d"):
                    entries.append(line[:-1] + "/")
                else:
                    entries.append(line[:-1] if line.endswith("f") else line)
            return "\n".join(entries) if entries else "(empty directory)"

        return result.stdout.strip()

    def _tool_read_file(
        self,
        ssh: SSHManager,
        repo_path: str,
        tool_input: dict[str, Any],
    ) -> str:
        """Read a file from the remote VM."""
        rel_path = tool_input.get("path", "")
        if not rel_path:
            return "Error: path is required"

        full_path = self._resolve_path(repo_path, rel_path)

        # Expand ~ on remote
        if "~" in full_path:
            home_result = ssh.execute("echo $HOME", timeout=5)
            remote_home = (
                home_result.stdout.strip() if home_result.success else "/home"
            )
            full_path = full_path.replace("~", remote_home)

        # Read via SSH
        file_contents = ssh.read_files([full_path])
        content = file_contents.get(full_path)

        if content is None:
            return f"Error: Could not read file: {rel_path}"

        # Store for diffing later
        self._files_read[rel_path] = content

        if len(content) > 30000:
            return (
                content[:30000]
                + f"\n... [truncated — file is {len(content):,} chars total]"
            )
        return content

    def _tool_search_files(
        self,
        ssh: SSHManager,
        repo_path: str,
        tool_input: dict[str, Any],
    ) -> str:
        """Search for a pattern in files on the remote VM (like grep)."""
        pattern = tool_input.get("pattern", "")
        if not pattern:
            return "Error: pattern is required"

        rel_path = tool_input.get("path", ".")
        file_pattern = tool_input.get("file_pattern", "")
        full_path = self._resolve_path(repo_path, rel_path)

        # Build grep command
        if file_pattern:
            include_flags = f'--include="{file_pattern}"'
        else:
            include_flags = (
                '--include="*.py" --include="*.sql" --include="*.yaml" '
                '--include="*.yml" --include="*.sh" --include="*.cfg" '
                '--include="*.conf" --include="*.json" --include="*.toml" '
                '--include="*.js" --include="*.ts" --include="*.html"'
            )

        # Escape double quotes in pattern for shell safety
        safe_pattern = pattern.replace('"', '\\"')

        cmd = (
            f'grep -rn -i {include_flags} '
            f'--exclude-dir=node_modules --exclude-dir=__pycache__ '
            f'--exclude-dir=.git --exclude-dir=venv --exclude-dir=.venv '
            f'--exclude-dir=env --exclude-dir=.env '
            f'-E "{safe_pattern}" "{full_path}" 2>/dev/null | head -50'
        )

        result = ssh.execute(cmd, timeout=15)

        if result.success and result.stdout.strip():
            return result.stdout.strip()
        return f"No matches found for pattern: {pattern}"

    def _tool_write_file(self, tool_input: dict[str, Any]) -> str:
        """Record a file change (applied after user approval via deploy)."""
        rel_path = tool_input.get("path", "")
        content = tool_input.get("content", "")
        if not rel_path:
            return "Error: path is required"
        if not content:
            return "Error: content is required"

        # Store the pending change
        self._pending_changes[rel_path] = content

        # If not already read, store empty for diff
        if rel_path not in self._files_read:
            self._files_read[rel_path] = ""

        return (
            f"✓ Recorded change to {rel_path} ({len(content):,} chars). "
            "Change will be applied after approval."
        )

    def _tool_run_command(
        self,
        ssh: SSHManager,
        repo_path: str,
        tool_input: dict[str, Any],
    ) -> str:
        """Execute a shell command on the remote VM via SSH."""
        command = tool_input.get("command", "")
        if not command:
            return "Error: command is required"

        cwd = tool_input.get("cwd", repo_path)
        if cwd == ".":
            cwd = repo_path

        # Block dangerous commands
        dangerous_patterns = [
            "rm -rf /", "rm -r /", "dd if=", "mkfs.", "> /dev/",
            "chmod -R 777", ":(){",
        ]
        cmd_lower = command.lower()
        for d in dangerous_patterns:
            if d in cmd_lower:
                return "Error: Potentially dangerous command blocked"

        result = ssh.execute(command, cwd=cwd, timeout=30)

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += f"\n[stderr]\n{result.stderr}"
            else:
                output = result.stderr
        if not result.success:
            output += f"\n[command failed]"

        return output[:10000] if output else "(no output)"

    # ------------------------------------------------------------------
    # Backward compatibility — no-op cache methods
    # ------------------------------------------------------------------

    def invalidate_structure_cache(self, client_id: Optional[str] = None) -> None:
        """No-op — kept for backward compatibility.

        The tool-use approach doesn't use a file listing cache;
        Claude explores the codebase dynamically via tools.
        """
        pass

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    def deploy_approved_changes(
        self,
        client_id: str,
        file_changes: dict[str, str],
    ) -> DeployResult:
        """
        Deploy approved code changes to a client's VM.

        Called after FDA receives user approval via Telegram.

        Args:
            client_id: Client identifier.
            file_changes: Dict of relative file paths -> new content.

        Returns:
            DeployResult with status and details.
        """
        client = self.client_manager.get_client(client_id)
        if not client:
            return DeployResult(
                success=False,
                client_id=client_id,
                error=f"Unknown client: {client_id}",
            )

        deployer = self._get_deployer(client)
        result = deployer.deploy_files(file_changes)
        return result

    def test_all_connections(self) -> dict[str, dict]:
        """
        Test SSH connectivity to all client VMs.

        Returns:
            Dict mapping client_id -> connection status.
        """
        results = {}
        for client in self.client_manager.list_clients():
            deployer = self._get_deployer(client)
            results[client.client_id] = deployer.test_connectivity()
        return results

    # ------------------------------------------------------------------
    # Message bus integration
    # ------------------------------------------------------------------

    def handle_task_request(self, message: dict) -> None:
        """
        Handle a task request from FDA via the message bus.

        Expected message body:
        {
            "client_id": "client_a",
            "task_brief": "Client A wants...",
            "hint_files": ["path/to/file.py"],  // optional
        }
        """
        body = json.loads(message.get("body", "{}"))
        client_id = body.get("client_id")
        task_brief = body.get("task_brief")
        hint_files = body.get("hint_files")

        if not client_id or not task_brief:
            logger.error("Invalid task request: missing client_id or task_brief")
            return

        # Analyze and generate fix
        result = self.analyze_and_fix(client_id, task_brief, hint_files)

        # Send result back to FDA
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker",
                to_agent="fda",
                msg_type="TASK_RESULT",
                subject=f"Fix for {client_id}",
                body=json.dumps(result),
                reply_to=message.get("id"),
            )

    def handle_deploy_request(self, message: dict) -> None:
        """
        Handle a deploy request from FDA (after user approval).

        Expected message body:
        {
            "client_id": "client_a",
            "file_changes": {"path/to/file.py": "new content..."}
        }
        """
        body = json.loads(message.get("body", "{}"))
        client_id = body.get("client_id")
        file_changes = body.get("file_changes", {})

        if not client_id or not file_changes:
            logger.error("Invalid deploy request")
            return

        result = self.deploy_approved_changes(client_id, file_changes)

        # Send result back to FDA
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker",
                to_agent="fda",
                msg_type="DEPLOY_RESULT",
                subject=f"Deploy {'OK' if result.success else 'FAILED'} for {client_id}",
                body=json.dumps({
                    "success": result.success,
                    "summary": result.summary(),
                    "error": result.error,
                    "rolled_back": result.rolled_back,
                }),
                reply_to=message.get("id"),
            )

    def run_event_loop(self) -> None:
        """
        Main event loop — listen for task and deploy requests from FDA.
        """
        logger.info("Worker agent started, listening for requests...")

        if self.state:
            self.state.update_agent_status("worker", "running")

        while True:
            try:
                if self.message_bus:
                    messages = self.message_bus.get_pending("worker")
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
                        self.state.agent_heartbeat("worker")

                time.sleep(2)  # Check every 2 seconds

            except KeyboardInterrupt:
                logger.info("Worker agent shutting down...")
                if self.state:
                    self.state.update_agent_status("worker", "stopped")
                break
            except Exception as e:
                logger.error(f"Error in worker event loop: {e}", exc_info=True)
                time.sleep(5)

    def _handle_status_request(self, message: dict) -> None:
        """Respond to a status request."""
        connections = self.test_all_connections()
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker",
                to_agent=message.get("from", "fda"),
                msg_type="STATUS_RESPONSE",
                subject="Worker status",
                body=json.dumps({
                    "status": "running",
                    "connections": connections,
                    "timestamp": datetime.now().isoformat(),
                }),
                reply_to=message.get("id"),
            )
