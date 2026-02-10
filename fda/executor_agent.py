"""
Executor Agent implementation.

The Executor agent is a PEER agent responsible for actions:
- Running shell commands
- Creating, editing, and deleting files
- Executing scripts and programs
- Performing tasks ordered by the user via FDA

As a peer to FDA and Librarian, it collaborates via the message bus
without hierarchy - responding to requests and reporting results.
"""

import logging
import time
import subprocess
import os
import shutil
import json
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.config import MODEL_EXECUTOR, DEFAULT_CHECK_INTERVAL_MINUTES
from fda.comms.message_bus import MessageTypes, Agents

logger = logging.getLogger(__name__)


EXECUTOR_SYSTEM_PROMPT = """You are the Executor Agent - a PEER in a multi-agent system.

You work alongside FDA (user interface) and Librarian (knowledge) as equals.
No one bosses anyone - you collaborate through requests and shared results.

Your domain is ACTIONS:
1. **Shell Commands**: Run any shell command the user requests
2. **File Operations**: Create, edit, delete, move, copy files
3. **Script Execution**: Run Python, bash, and other scripts
4. **Task Execution**: Perform tasks ordered via Discord/Telegram
5. **Progress Reporting**: Report results and blockers to peers

You have access to the user's file system and can execute commands.
Always report results back - success or failure.

Safety guidelines:
- Don't execute obviously destructive commands (rm -rf /) without confirmation
- Be careful with commands that affect system state
- Log all commands executed for audit purposes

When responding to execute requests:
- Run the command and return the output
- Include exit codes and any errors
- Suggest alternatives if a command fails

Remember: You're a helpful peer, not a subordinate. You can suggest
better approaches and share discoveries with FDA.
"""


class ExecutorAgent(BaseAgent):
    """
    Executor Agent for actions and command execution.

    As a PEER agent, the Executor:
    - Runs shell commands on request
    - Creates, edits, and deletes files
    - Executes scripts and programs
    - Reports results back to peers
    - Handles tasks from the task queue
    """

    # Commands that are considered dangerous and need extra care
    DANGEROUS_PATTERNS = [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        ":(){:|:&};:",  # Fork bomb
        "dd if=/dev/zero",
        "> /dev/sda",
    ]

    def __init__(
        self,
        project_state_path: Optional[Path] = None,
        working_directory: Optional[str] = None,
    ):
        """
        Initialize the Executor agent.

        Args:
            project_state_path: Path to the project state database.
            working_directory: Default working directory for commands.
        """
        super().__init__(
            name="Executor",
            model=MODEL_EXECUTOR,
            system_prompt=EXECUTOR_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

        self.working_directory = working_directory or os.path.expanduser("~")
        self.current_task: Optional[dict[str, Any]] = None
        self.task_start_time: Optional[datetime] = None
        self.command_history: list[dict[str, Any]] = []

    def run_event_loop(self) -> None:
        """
        Run the main event loop for the Executor.

        As a peer agent:
        1. Process execution requests from peers
        2. Handle file operation requests
        3. Pick up tasks from the queue when idle
        """
        logger.info("[Executor] Starting event loop...")

        # Update agent status
        self.state.update_agent_status(self.name.lower(), "running", "Starting up")

        # Check for messages frequently (every 1 second) for responsive inter-agent comms
        message_check_interval = 1  # Check messages every 1 second
        task_check_counter = 0
        task_check_frequency = 5  # Check for new tasks every 5 message checks

        print("[Executor] Ready and listening for requests...")

        while self._running:
            try:
                # Heartbeat
                self.state.agent_heartbeat(self.name.lower())
                self.state.update_agent_status(self.name.lower(), "running")

                # Process pending messages from peers (highest priority, check frequently!)
                messages = self.get_pending_messages()
                for message in messages:
                    self._handle_message(message)

                # Check for tasks less frequently than messages
                task_check_counter += 1
                if task_check_counter >= task_check_frequency and self.current_task is None:
                    task_check_counter = 0
                    task = self.pick_up_task()
                    if task:
                        self.current_task = task
                        self.task_start_time = datetime.now()
                        self.state.update_agent_status(
                            self.name.lower(), "busy",
                            f"Executing task: {task.get('title', '')[:30]}"
                        )
                        logger.info(f"[Executor] Picked up task: {task.get('title')}")

                        # Execute the task
                        result = self.execute_task(task)

                        # Handle the result
                        if result.get("status") == "completed":
                            self.request_review(task["id"])
                        elif result.get("status") == "blocked":
                            self.report_blocker(task["id"], result.get("reason", "Unknown blocker"))

                        self.current_task = None
                        self.task_start_time = None
                        self.state.update_agent_status(self.name.lower(), "running")

                time.sleep(message_check_interval)

            except KeyboardInterrupt:
                logger.info("[Executor] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[Executor] Error in event loop: {e}")
                time.sleep(60)

        self.state.update_agent_status(self.name.lower(), "stopped")
        logger.info("[Executor] Event loop stopped")

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message from a peer agent."""
        msg_type = message.get("type", "")
        subject = message.get("subject", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")

        logger.info(f"[Executor] Received {msg_type} from {from_agent}: {subject}")
        self.message_bus.mark_read(msg_id)

        # Update status while processing
        self.state.update_agent_status(self.name.lower(), "busy", f"Processing {msg_type}")

        try:
            if msg_type == MessageTypes.EXECUTE_REQUEST:
                self._handle_execute_request(message)

            elif msg_type == MessageTypes.FILE_REQUEST:
                self._handle_file_request(message)

            elif msg_type == MessageTypes.STATUS_REQUEST:
                self._handle_status_request(message)

            elif msg_type == MessageTypes.CLAUDE_CODE_REQUEST:
                self._handle_claude_code_request(message)

            # Legacy message types for backward compatibility
            elif msg_type == "review_response":
                self._handle_review_response(message)

            elif msg_type == "task_assignment":
                logger.info(f"[Executor] Received task assignment: {subject}")

            elif msg_type == "priority_change":
                logger.info(f"[Executor] Priority change: {body}")

        except Exception as e:
            logger.error(f"[Executor] Error handling message: {e}")
            self.message_bus.send_result(
                from_agent=self.name.lower(),
                to_agent=from_agent.lower(),
                msg_type=MessageTypes.EXECUTE_RESULT,
                result=None,
                success=False,
                error=str(e),
                reply_to=msg_id,
            )

        # Reset status
        self.state.update_agent_status(self.name.lower(), "running")

    def _handle_execute_request(self, message: dict[str, Any]) -> None:
        """Handle a command execution request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        body = message.get("body", "")

        # Parse the request
        try:
            request = json.loads(body)
            command = request.get("command", "")
            cwd = request.get("cwd", self.working_directory)
        except (json.JSONDecodeError, TypeError):
            command = body
            cwd = self.working_directory

        logger.info(f"[Executor] Executing command: {command}")

        # Execute the command
        result = self.run_command(command, cwd)

        # Send result back
        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.EXECUTE_RESULT,
            result=result,
            success=result.get("success", False),
            error=result.get("error"),
            reply_to=msg_id,
        )

    def _handle_file_request(self, message: dict[str, Any]) -> None:
        """Handle a file operation request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        body = message.get("body", "")

        try:
            request = json.loads(body)
            operation = request.get("operation", "")
            path = request.get("path", "")
            content = request.get("content")
        except (json.JSONDecodeError, TypeError):
            self.message_bus.send_result(
                from_agent=self.name.lower(),
                to_agent=from_agent.lower(),
                msg_type=MessageTypes.FILE_COMPLETE,
                result=None,
                success=False,
                error="Invalid file request format",
                reply_to=msg_id,
            )
            return

        logger.info(f"[Executor] File operation: {operation} on {path}")

        # Perform the operation
        if operation == "create":
            result = self.create_file(path, content or "")
        elif operation == "edit":
            result = self.edit_file(path, content or "")
        elif operation == "delete":
            result = self.delete_file(path)
        elif operation == "read":
            result = self.read_file(path)
        elif operation == "copy":
            dest = request.get("destination", "")
            result = self.copy_file(path, dest)
        elif operation == "move":
            dest = request.get("destination", "")
            result = self.move_file(path, dest)
        else:
            result = {"success": False, "error": f"Unknown operation: {operation}"}

        # Send result back
        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.FILE_COMPLETE,
            result=result,
            success=result.get("success", False),
            error=result.get("error"),
            reply_to=msg_id,
        )

    def _handle_status_request(self, message: dict[str, Any]) -> None:
        """Handle a status request from a peer."""
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")

        status = {
            "agent": self.name,
            "status": "running",
            "working_directory": self.working_directory,
            "current_task": self.current_task.get("title") if self.current_task else None,
            "commands_executed": len(self.command_history),
            "recent_commands": [
                {"command": c.get("command", "")[:50], "success": c.get("success")}
                for c in self.command_history[-5:]
            ],
        }

        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.STATUS_RESPONSE,
            result=status,
            success=True,
            reply_to=msg_id,
        )

    def _handle_claude_code_request(self, message: dict[str, Any]) -> None:
        """
        Handle a Claude Code request from a peer.

        This delegates coding tasks to Claude Code CLI, which uses the user's
        Max subscription instead of API credits.
        """
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        body = message.get("body", "")

        # Parse the request
        try:
            request = json.loads(body)
            prompt = request.get("prompt", "")
            cwd = request.get("cwd", self.working_directory)
            allow_edits = request.get("allow_edits", False)
            timeout = request.get("timeout", 300)  # 5 minute default
        except (json.JSONDecodeError, TypeError):
            prompt = body
            cwd = self.working_directory
            allow_edits = False
            timeout = 300

        logger.info(f"[Executor] Claude Code request: {prompt[:100]}...")
        print(f"[Executor] Running Claude Code: {prompt[:80]}...")

        # Run Claude Code
        result = self.run_claude_code(
            prompt=prompt,
            cwd=cwd,
            allow_edits=allow_edits,
            timeout=timeout,
        )

        # Send result back
        self.message_bus.send_result(
            from_agent=self.name.lower(),
            to_agent=from_agent.lower(),
            msg_type=MessageTypes.CLAUDE_CODE_RESULT,
            result=result,
            success=result.get("success", False),
            error=result.get("error"),
            reply_to=msg_id,
        )

    def run_claude_code(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        allow_edits: bool = False,
        timeout: int = 300,
    ) -> dict[str, Any]:
        """
        Run Claude Code CLI with a prompt.

        This uses the user's Max subscription instead of API credits.

        Args:
            prompt: The task/question to send to Claude Code.
            cwd: Working directory for Claude Code.
            allow_edits: If True, use --dangerously-skip-permissions to allow edits.
            timeout: Timeout in seconds (default 5 minutes).

        Returns:
            Dictionary with success status, output, and any errors.
        """
        cwd = cwd or self.working_directory

        # Build the command
        cmd = ["claude", "--print"]

        if allow_edits:
            # This allows Claude Code to make file changes without prompts
            cmd.append("--dangerously-skip-permissions")

        cmd.append(prompt)

        logger.info(f"[Executor] Running Claude Code in {cwd}")

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            success = result.returncode == 0
            output = result.stdout

            # Log to command history
            self.command_history.append({
                "type": "claude_code",
                "prompt": prompt[:200],
                "success": success,
                "timestamp": datetime.now().isoformat(),
                "cwd": cwd,
            })

            if success:
                logger.info(f"[Executor] Claude Code completed successfully")
                return {
                    "success": True,
                    "output": output,
                    "prompt": prompt,
                }
            else:
                error_msg = result.stderr or "Claude Code returned non-zero exit code"
                logger.warning(f"[Executor] Claude Code failed: {error_msg}")
                return {
                    "success": False,
                    "output": output,
                    "error": error_msg,
                    "prompt": prompt,
                }

        except subprocess.TimeoutExpired:
            error_msg = f"Claude Code timed out after {timeout} seconds"
            logger.error(f"[Executor] {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "prompt": prompt,
            }
        except FileNotFoundError:
            error_msg = "Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            logger.error(f"[Executor] {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "prompt": prompt,
            }
        except Exception as e:
            logger.error(f"[Executor] Claude Code error: {e}")
            return {
                "success": False,
                "error": str(e),
                "prompt": prompt,
            }

    def _handle_review_response(self, message: dict[str, Any]) -> None:
        """Handle a review response from FDA."""
        body = message.get("body", "")

        # Parse the review response
        if "approved" in body.lower():
            logger.info("[Executor] Task approved by FDA")
            # Task is complete, no further action needed
        else:
            logger.info("[Executor] Task needs revision based on FDA feedback")
            # Could trigger re-work of the task

    def pick_up_task(self) -> Optional[dict[str, Any]]:
        """
        Pick up a task from the queue.

        Returns:
            Task dictionary if available, None otherwise.
        """
        # Get pending tasks
        pending_tasks = self.state.get_tasks(status="pending")

        if not pending_tasks:
            return None

        # Sort by priority (high first) then by creation date (oldest first)
        priority_order = {"high": 0, "medium": 1, "low": 2}

        sorted_tasks = sorted(
            pending_tasks,
            key=lambda t: (
                priority_order.get(t.get("priority", "medium"), 1),
                t.get("created_at", ""),
            ),
        )

        if not sorted_tasks:
            return None

        # Pick up the highest priority task
        task = sorted_tasks[0]

        # Update task status to in_progress
        self.state.update_task(task["id"], status="in_progress", owner=self.name)

        # Log the pickup
        self.log_to_journal(
            summary=f"Task picked up: {task.get('title')}",
            content=f"Started working on task {task.get('id')}: {task.get('title')}\n\nDescription: {task.get('description')}",
            tags=["task-pickup", "executor"],
            relevance_decay="fast",
        )

        return task

    def execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """
        Execute a given task.

        Args:
            task: The task to execute.

        Returns:
            Execution results including output and status.
        """
        task_id = task.get("id")
        title = task.get("title", "")
        description = task.get("description", "")

        logger.info(f"[Executor] Executing task: {title}")

        # Build context for the task
        context = self.get_project_context()
        context["current_task"] = task

        # Search for relevant journal entries
        relevant = self.search_journal(f"{title} {description}", top_n=3)
        if relevant:
            context["relevant_history"] = [
                {"summary": e.get("summary"), "author": e.get("author")}
                for e in relevant
            ]

        # Ask Claude to help execute the task
        prompt = f"""Execute this task and provide results:

Task ID: {task_id}
Title: {title}
Description: {description}
Priority: {task.get('priority', 'medium')}

Please:
1. Analyze what needs to be done
2. Break down into specific steps if complex
3. Execute each step (or describe what would be done)
4. Document any decisions or assumptions made
5. Identify any blockers or dependencies
6. Provide the final output or deliverable description

If you encounter any blockers that prevent completion, clearly state them."""

        response = self.chat_with_context(prompt, context)

        # Analyze the response to determine status
        is_blocked = any(
            phrase in response.lower()
            for phrase in ["blocker", "blocked by", "cannot proceed", "waiting for", "dependency"]
        )

        if is_blocked:
            # Extract the blocker reason
            blocker_reason = self._extract_blocker_reason(response)

            # Update task status
            self.state.update_task(task_id, status="blocked")

            return {
                "status": "blocked",
                "task_id": task_id,
                "reason": blocker_reason,
                "response": response,
                "timestamp": datetime.now().isoformat(),
            }

        # Task completed successfully
        self.state.update_task(task_id, status="completed")

        # Log completion
        self.log_to_journal(
            summary=f"Task completed: {title}",
            content=f"## Task Execution: {title}\n\n{response}",
            tags=["task-complete", "executor"],
            relevance_decay="medium",
        )

        return {
            "status": "completed",
            "task_id": task_id,
            "response": response,
            "timestamp": datetime.now().isoformat(),
        }

    def _extract_blocker_reason(self, response: str) -> str:
        """Extract the blocker reason from a response."""
        # Simple extraction - look for common patterns
        response_lower = response.lower()

        for keyword in ["blocked by", "waiting for", "blocker:", "cannot proceed because"]:
            idx = response_lower.find(keyword)
            if idx != -1:
                # Extract the next sentence or line
                start = idx
                end = response.find(".", start)
                if end == -1:
                    end = response.find("\n", start)
                if end == -1:
                    end = min(start + 200, len(response))
                return response[start:end].strip()

        return "Blocker identified in task execution"

    def request_review(self, task_id: str) -> dict[str, Any]:
        """
        Request a review from the FDA agent for a completed task.

        Args:
            task_id: The ID of the task to review.

        Returns:
            Review response from the FDA agent.
        """
        logger.info(f"[Executor] Requesting review for task: {task_id}")

        # Get task details
        tasks = self.state.get_tasks()
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if not task:
            return {
                "status": "error",
                "message": f"Task {task_id} not found",
            }

        # Send review request to FDA
        msg_id = self.send_message(
            to_agent="FDA",
            msg_type="review_request",
            subject=f"Review request: {task.get('title')}",
            body=task_id,
            priority="medium",
        )

        return {
            "status": "pending",
            "task_id": task_id,
            "message_id": msg_id,
            "timestamp": datetime.now().isoformat(),
        }

    def report_blocker(self, task_id: str, reason: str) -> dict[str, Any]:
        """
        Report a blocker preventing task execution.

        Args:
            task_id: The ID of the blocked task.
            reason: Description of the blocker.

        Returns:
            Acknowledgment from the system.
        """
        logger.info(f"[Executor] Reporting blocker for task: {task_id}")

        # Get task details
        tasks = self.state.get_tasks()
        task = next((t for t in tasks if t.get("id") == task_id), None)

        task_title = task.get("title", task_id) if task else task_id

        # Add alert
        alert_id = self.add_alert(
            level="warning",
            message=f"Task blocked: {task_title} - {reason}",
        )

        # Send message to FDA
        msg_id = self.send_message(
            to_agent="FDA",
            msg_type="blocker",
            subject=f"Blocker: {task_title}",
            body=f"Task ID: {task_id}\nReason: {reason}",
            priority="high",
        )

        # Log to journal
        self.log_to_journal(
            summary=f"Blocker reported: {task_title}",
            content=f"## Blocker Report\n\nTask: {task_title}\nID: {task_id}\n\nReason:\n{reason}",
            tags=["blocker", "escalation"],
            relevance_decay="fast",
        )

        return {
            "status": "reported",
            "task_id": task_id,
            "alert_id": alert_id,
            "message_id": msg_id,
            "timestamp": datetime.now().isoformat(),
        }

    def get_current_task_status(self) -> Optional[dict[str, Any]]:
        """
        Get the status of the current task being worked on.

        Returns:
            Current task status or None if no task is active.
        """
        if self.current_task is None:
            return None

        elapsed = None
        if self.task_start_time:
            elapsed = (datetime.now() - self.task_start_time).total_seconds()

        return {
            "task_id": self.current_task.get("id"),
            "title": self.current_task.get("title"),
            "status": "in_progress",
            "elapsed_seconds": elapsed,
            "started_at": self.task_start_time.isoformat() if self.task_start_time else None,
        }

    def estimate_task_completion(self, task: dict[str, Any]) -> dict[str, Any]:
        """
        Estimate completion time for a task.

        Args:
            task: The task to estimate.

        Returns:
            Estimation details.
        """
        context = {"task": task}

        prompt = f"""Estimate the effort required for this task:

Task: {task.get('title')}
Description: {task.get('description')}
Priority: {task.get('priority')}

Please provide:
1. Complexity assessment (Simple/Medium/Complex)
2. Estimated effort (in hours)
3. Key factors affecting the estimate
4. Any risks that might impact the timeline"""

        response = self.chat_with_context(prompt, context)

        return {
            "task_id": task.get("id"),
            "title": task.get("title"),
            "estimation": response,
            "timestamp": datetime.now().isoformat(),
        }

    def handoff_task(self, task_id: str, to_agent: str, reason: str) -> dict[str, Any]:
        """
        Hand off a task to another agent.

        Args:
            task_id: The task to hand off.
            to_agent: The agent to hand off to.
            reason: Reason for the handoff.

        Returns:
            Handoff confirmation.
        """
        # Get task details
        tasks = self.state.get_tasks()
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if not task:
            return {"status": "error", "message": f"Task {task_id} not found"}

        # Update task owner
        self.state.update_task(task_id, owner=to_agent, status="pending")

        # Send handoff message
        msg_id = self.send_message(
            to_agent=to_agent,
            msg_type="task_assignment",
            subject=f"Task handoff: {task.get('title')}",
            body=f"Task ID: {task_id}\nReason: {reason}",
            priority=task.get("priority", "medium"),
        )

        # Log the handoff
        self.log_to_journal(
            summary=f"Task handoff: {task.get('title')} to {to_agent}",
            content=f"Handed off task {task_id} to {to_agent}.\nReason: {reason}",
            tags=["handoff", "task-transfer"],
            relevance_decay="fast",
        )

        # Clear current task if this was it
        if self.current_task and self.current_task.get("id") == task_id:
            self.current_task = None
            self.task_start_time = None

        return {
            "status": "handed_off",
            "task_id": task_id,
            "to_agent": to_agent,
            "message_id": msg_id,
            "timestamp": datetime.now().isoformat(),
        }

    # ========== Shell Execution Methods ==========

    def run_command(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: int = 60,
        shell: bool = True,
    ) -> dict[str, Any]:
        """
        Execute a shell command.

        Args:
            command: The command to execute.
            cwd: Working directory (defaults to self.working_directory).
            timeout: Command timeout in seconds.
            shell: Whether to run through shell (default True).

        Returns:
            Dictionary with stdout, stderr, return_code, and success.
        """
        # Safety check for dangerous commands
        if self._is_dangerous_command(command):
            logger.warning(f"[Executor] Blocked dangerous command: {command}")
            result = {
                "command": command,
                "stdout": "",
                "stderr": "Command blocked: potentially dangerous operation",
                "return_code": -1,
                "success": False,
                "error": "Blocked for safety",
                "timestamp": datetime.now().isoformat(),
            }
            self.command_history.append(result)
            return result

        working_dir = cwd or self.working_directory

        logger.info(f"[Executor] Running: {command} in {working_dir}")

        try:
            result = subprocess.run(
                command,
                shell=shell,
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            output = {
                "command": command,
                "cwd": working_dir,
                "stdout": result.stdout[:10000] if result.stdout else "",  # Limit output
                "stderr": result.stderr[:5000] if result.stderr else "",
                "return_code": result.returncode,
                "success": result.returncode == 0,
                "timestamp": datetime.now().isoformat(),
            }

        except subprocess.TimeoutExpired:
            output = {
                "command": command,
                "cwd": working_dir,
                "stdout": "",
                "stderr": f"Command timed out after {timeout} seconds",
                "return_code": -1,
                "success": False,
                "error": "timeout",
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            output = {
                "command": command,
                "cwd": working_dir,
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
                "success": False,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }

        # Add to history
        self.command_history.append(output)

        # Keep history manageable
        if len(self.command_history) > 100:
            self.command_history = self.command_history[-100:]

        # Log to journal if it's a significant command
        if output["success"] and len(command) > 10:
            self.log_to_journal(
                summary=f"Command executed: {command[:50]}",
                content=f"## Command Execution\n\nCommand: `{command}`\nWorking Directory: {working_dir}\n\nOutput:\n```\n{output['stdout'][:1000]}\n```",
                tags=["command", "execution"],
                relevance_decay="fast",
            )

        return output

    def run_script(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """
        Execute a script file.

        Args:
            script_path: Path to the script.
            args: Optional list of arguments.
            timeout: Execution timeout in seconds.

        Returns:
            Execution result dictionary.
        """
        path = Path(script_path)

        if not path.exists():
            return {
                "success": False,
                "error": f"Script not found: {script_path}",
            }

        # Determine how to run the script based on extension
        ext = path.suffix.lower()
        args = args or []

        if ext == ".py":
            cmd = f"python {script_path} {' '.join(args)}"
        elif ext in [".sh", ".bash"]:
            cmd = f"bash {script_path} {' '.join(args)}"
        elif ext == ".js":
            cmd = f"node {script_path} {' '.join(args)}"
        else:
            # Try to run it directly if executable
            cmd = f"{script_path} {' '.join(args)}"

        return self.run_command(cmd, cwd=str(path.parent), timeout=timeout)

    def _is_dangerous_command(self, command: str) -> bool:
        """Check if a command is potentially dangerous."""
        command_lower = command.lower().strip()

        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in command_lower:
                return True

        # Check for rm -rf with root paths
        if "rm " in command_lower and "-rf" in command_lower:
            if " /" in command_lower or command_lower.endswith(" /"):
                return True

        return False

    # ========== File Operation Methods ==========

    def create_file(self, path: str, content: str) -> dict[str, Any]:
        """
        Create a new file with content.

        Args:
            path: File path to create.
            content: File content.

        Returns:
            Result dictionary with success status.
        """
        try:
            file_path = Path(path)

            # Create parent directories if needed
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # Write the file
            file_path.write_text(content, encoding="utf-8")

            logger.info(f"[Executor] Created file: {path}")

            # Log to journal
            self.log_to_journal(
                summary=f"Created file: {file_path.name}",
                content=f"Created file at {path}\nSize: {len(content)} bytes",
                tags=["file-create"],
                relevance_decay="fast",
            )

            return {
                "success": True,
                "path": str(file_path.absolute()),
                "size": len(content),
            }

        except Exception as e:
            logger.error(f"[Executor] Failed to create file {path}: {e}")
            return {
                "success": False,
                "path": path,
                "error": str(e),
            }

    def edit_file(self, path: str, content: str) -> dict[str, Any]:
        """
        Edit (overwrite) an existing file.

        Args:
            path: File path to edit.
            content: New file content.

        Returns:
            Result dictionary with success status.
        """
        file_path = Path(path)

        if not file_path.exists():
            return {
                "success": False,
                "path": path,
                "error": "File does not exist",
            }

        try:
            # Read original for logging
            original_size = file_path.stat().st_size

            # Write new content
            file_path.write_text(content, encoding="utf-8")

            logger.info(f"[Executor] Edited file: {path}")

            return {
                "success": True,
                "path": str(file_path.absolute()),
                "original_size": original_size,
                "new_size": len(content),
            }

        except Exception as e:
            logger.error(f"[Executor] Failed to edit file {path}: {e}")
            return {
                "success": False,
                "path": path,
                "error": str(e),
            }

    def delete_file(self, path: str) -> dict[str, Any]:
        """
        Delete a file.

        Args:
            path: File path to delete.

        Returns:
            Result dictionary with success status.
        """
        file_path = Path(path)

        if not file_path.exists():
            return {
                "success": False,
                "path": path,
                "error": "File does not exist",
            }

        try:
            if file_path.is_dir():
                shutil.rmtree(file_path)
            else:
                file_path.unlink()

            logger.info(f"[Executor] Deleted: {path}")

            return {
                "success": True,
                "path": path,
            }

        except Exception as e:
            logger.error(f"[Executor] Failed to delete {path}: {e}")
            return {
                "success": False,
                "path": path,
                "error": str(e),
            }

    def read_file(self, path: str, max_size: int = 100000) -> dict[str, Any]:
        """
        Read a file's contents.

        Args:
            path: File path to read.
            max_size: Maximum bytes to read.

        Returns:
            Result dictionary with content.
        """
        file_path = Path(path)

        if not file_path.exists():
            return {
                "success": False,
                "path": path,
                "error": "File does not exist",
            }

        if not file_path.is_file():
            return {
                "success": False,
                "path": path,
                "error": "Path is not a file",
            }

        try:
            size = file_path.stat().st_size

            if size > max_size:
                # Read only first part
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(max_size)
                truncated = True
            else:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                truncated = False

            return {
                "success": True,
                "path": str(file_path.absolute()),
                "content": content,
                "size": size,
                "truncated": truncated,
            }

        except Exception as e:
            logger.error(f"[Executor] Failed to read {path}: {e}")
            return {
                "success": False,
                "path": path,
                "error": str(e),
            }

    def copy_file(self, source: str, destination: str) -> dict[str, Any]:
        """
        Copy a file or directory.

        Args:
            source: Source path.
            destination: Destination path.

        Returns:
            Result dictionary with success status.
        """
        src_path = Path(source)

        if not src_path.exists():
            return {
                "success": False,
                "source": source,
                "destination": destination,
                "error": "Source does not exist",
            }

        try:
            if src_path.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)

            logger.info(f"[Executor] Copied {source} to {destination}")

            return {
                "success": True,
                "source": source,
                "destination": destination,
            }

        except Exception as e:
            logger.error(f"[Executor] Failed to copy {source}: {e}")
            return {
                "success": False,
                "source": source,
                "destination": destination,
                "error": str(e),
            }

    def move_file(self, source: str, destination: str) -> dict[str, Any]:
        """
        Move a file or directory.

        Args:
            source: Source path.
            destination: Destination path.

        Returns:
            Result dictionary with success status.
        """
        src_path = Path(source)

        if not src_path.exists():
            return {
                "success": False,
                "source": source,
                "destination": destination,
                "error": "Source does not exist",
            }

        try:
            shutil.move(source, destination)

            logger.info(f"[Executor] Moved {source} to {destination}")

            return {
                "success": True,
                "source": source,
                "destination": destination,
            }

        except Exception as e:
            logger.error(f"[Executor] Failed to move {source}: {e}")
            return {
                "success": False,
                "source": source,
                "destination": destination,
                "error": str(e),
            }
