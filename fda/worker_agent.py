"""
Worker Agent — merged Librarian + Executor for remote code operations.

The Worker handles the technical side of client requests:
1. Receives task briefs from FDA via the message bus
2. SSHs into the correct Azure VM to read the codebase
3. Generates code fixes using Claude with full context
4. Creates diffs for FDA to send to the user for approval
5. Deploys approved changes to the VM

This replaces the separate Librarian and Executor agents from the
original FDA architecture, since for Datacore's use case, the agent
that reads the code is the same one that needs to change it.
"""

import json
import logging
import difflib
from typing import Any, Optional
from datetime import datetime

import anthropic

from fda.base_agent import BaseAgent
from fda.config import MODEL_EXECUTOR, MODEL_MEETING_SUMMARY
from fda.clients.client_config import ClientConfig, ClientManager
from fda.remote.ssh_manager import SSHManager
from fda.remote.deploy import Deployer, DeployResult
from fda.comms.message_bus import MessageBus

logger = logging.getLogger(__name__)


class WorkerAgent(BaseAgent):
    """
    Technical execution agent for Datacore client operations.

    Combines code reading (Librarian) and code execution (Executor)
    into a single agent that operates on remote Azure VMs via SSH.
    """

    SYSTEM_PROMPT = """You are the Worker agent for Datacore, a software consultancy.
Your job is to make code changes on client Azure VMs based on task briefs from the FDA agent.

When you receive a task brief, you will:
1. Understand the client's request in business context
2. Read the relevant source files from their codebase
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
        self.claude = anthropic.Anthropic()

        # Cache SSH connections per client
        self._ssh_connections: dict[str, SSHManager] = {}
        self._deployers: dict[str, Deployer] = {}

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

    def analyze_and_fix(
        self,
        client_id: str,
        task_brief: str,
        hint_files: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Analyze a client request and generate a code fix.

        This is the main entry point for the Worker.

        Args:
            client_id: Client identifier.
            task_brief: Business-context task description from FDA.
            hint_files: Optional list of file paths to examine first.

        Returns:
            Dict with:
              - success: bool
              - analysis: str (what the agent found)
              - changes: dict[str, str] (file path -> new content)
              - diff: str (unified diff of all changes)
              - explanation: str (human-readable explanation)
              - error: Optional[str]
        """
        client = self.client_manager.get_client(client_id)
        if not client:
            return {
                "success": False,
                "error": f"Unknown client: {client_id}",
            }

        ssh = self._get_ssh(client)
        repo_path = client.project.repo_path

        # Step 1: Understand the codebase structure
        logger.info(f"Analyzing codebase for {client.name}...")
        structure = self._explore_codebase(ssh, repo_path, client)

        # Step 2: Identify relevant files
        relevant_files = self._identify_relevant_files(
            task_brief, structure, hint_files, client
        )

        if not relevant_files:
            return {
                "success": False,
                "error": "Could not identify relevant files for this task",
                "analysis": f"Explored {len(structure)} files but none matched the request",
            }

        # Step 3: Read the relevant source files
        logger.info(f"Reading {len(relevant_files)} relevant files...")
        file_contents = ssh.read_files(relevant_files)

        # Filter out files that couldn't be read
        readable_files = {
            path: content
            for path, content in file_contents.items()
            if content is not None
        }

        if not readable_files:
            return {
                "success": False,
                "error": "Could not read any of the relevant files",
            }

        # Step 4: Generate the fix using Claude
        logger.info("Generating code fix...")
        fix_result = self._generate_fix(
            task_brief=task_brief,
            client=client,
            file_contents=readable_files,
            repo_path=repo_path,
        )

        return fix_result

    def _explore_codebase(
        self,
        ssh: SSHManager,
        repo_path: str,
        client: ClientConfig,
    ) -> list[str]:
        """
        Get an overview of the codebase structure.

        Returns a list of file paths in the repository.
        """
        # Get file list, excluding common non-code directories
        result = ssh.execute(
            "find . -type f "
            "! -path './.git/*' "
            "! -path './node_modules/*' "
            "! -path './__pycache__/*' "
            "! -path './venv/*' "
            "! -path './.venv/*' "
            "! -path './env/*' "
            "! -path './.env/*' "
            "! -path './dist/*' "
            "! -path './build/*' "
            "! -path './*.pyc' "
            "| head -500",
            cwd=repo_path,
            timeout=15,
        )

        if result.success:
            return [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
        return []

    def _identify_relevant_files(
        self,
        task_brief: str,
        all_files: list[str],
        hint_files: Optional[list[str]],
        client: ClientConfig,
    ) -> list[str]:
        """
        Use Claude to identify which files are relevant to the task.

        Args:
            task_brief: The task description.
            all_files: All files in the repo.
            hint_files: Pre-identified files to include.
            client: Client config for context.

        Returns:
            List of file paths to read.
        """
        if hint_files:
            return hint_files

        # Use Claude to pick relevant files
        files_list = "\n".join(all_files[:300])  # Limit to avoid token overflow

        response = self.claude.messages.create(
            model=MODEL_EXECUTOR,
            max_tokens=1000,
            system="You are a code analyst. Given a task description and a list of files in a repository, identify which files are most likely relevant to the task. Return ONLY a JSON array of file paths, nothing else.",
            messages=[{
                "role": "user",
                "content": f"""Task: {task_brief}

Tech stack: {client.project.tech_stack}

Repository files:
{files_list}

Which files should I read to understand and fix this issue?
Return a JSON array of the most relevant file paths (max 15 files).
""",
            }],
        )

        try:
            # Extract JSON from response
            text = response.content[0].text.strip()
            # Handle markdown code blocks
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            files = json.loads(text)
            if isinstance(files, list):
                return [f for f in files if f in all_files]
        except (json.JSONDecodeError, IndexError, KeyError):
            logger.warning("Failed to parse file identification response")

        # Fallback: look for common patterns based on task keywords
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

    def _generate_fix(
        self,
        task_brief: str,
        client: ClientConfig,
        file_contents: dict[str, str],
        repo_path: str,
    ) -> dict[str, Any]:
        """
        Generate a code fix using Claude.

        Args:
            task_brief: Business-context task description.
            client: Client configuration.
            file_contents: Dict of file path -> file content.
            repo_path: Repository root path on the VM.

        Returns:
            Dict with success, changes, diff, and explanation.
        """
        # Build the source code context
        source_context = ""
        for path, content in file_contents.items():
            # Truncate very large files
            if len(content) > 10000:
                content = content[:10000] + "\n... [truncated] ..."
            source_context += f"\n=== {path} ===\n{content}\n"

        prompt = f"""You are making a code change for a client.

{client.get_context_for_prompt()}

TASK:
{task_brief}

SOURCE CODE:
{source_context}

INSTRUCTIONS:
1. Identify what needs to change to fulfill the task
2. Make the MINIMAL change needed — don't refactor unrelated code
3. Preserve the existing code style

RESPONSE FORMAT:
Return a JSON object with:
{{
  "analysis": "Brief analysis of what you found",
  "explanation": "Plain-language explanation of what you changed and why",
  "changes": {{
    "relative/path/to/file.py": "FULL new content of the file (not just the changed part)"
  }},
  "confidence": "high|medium|low",
  "warnings": ["any concerns or things to check"]
}}

IMPORTANT: In "changes", include the COMPLETE file content, not just the diff.
Only include files that actually changed.
"""

        try:
            response = self.claude.messages.create(
                model=MODEL_MEETING_SUMMARY,  # Use Sonnet for quality code generation
                max_tokens=8000,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()

            # Extract JSON from response
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
            logger.error(f"Failed to parse Claude's response as JSON: {e}")
            return {
                "success": False,
                "error": f"Failed to parse code fix response: {e}",
                "raw_response": text if 'text' in dir() else "",
            }
        except Exception as e:
            logger.error(f"Error generating fix: {e}")
            return {
                "success": False,
                "error": str(e),
            }

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
        return deployer.deploy_files(file_changes)

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

    # Message bus integration

    def handle_task_request(self, message: dict) -> None:
        """
        Handle a task request from FDA via the message bus.

        Expected message body:
        {
            "client_id": "client_a",
            "task_brief": "Client A wants...",
            "hint_files": ["path/to/file.py"],  # optional
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
        import time

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
