"""
Executor Agent implementation.

The Executor agent is responsible for task execution, blocker management,
and requesting reviews from the FDA.
"""

import logging
import time
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.config import MODEL_EXECUTOR, DEFAULT_CHECK_INTERVAL_MINUTES

logger = logging.getLogger(__name__)


EXECUTOR_SYSTEM_PROMPT = """You are the Executor Agent for a project management system.

Your responsibilities include:
1. **Task Execution**: Pick up and execute tasks from the queue
2. **Progress Tracking**: Update task status and track completion
3. **Blocker Management**: Identify and report blockers
4. **Quality Assurance**: Ensure work meets requirements before requesting review
5. **Communication**: Report status to the FDA agent

When executing tasks:
- Break down complex tasks into actionable steps
- Document your progress and decisions
- Identify blockers early and escalate appropriately
- Ensure deliverables meet the stated requirements
- Request review when work is complete

When responding:
- Be specific about what you've done and what remains
- Clearly state any blockers or dependencies
- Provide estimates when possible
- Document any assumptions made
"""


class ExecutorAgent(BaseAgent):
    """
    Executor Agent for task execution and delivery.

    The Executor agent picks up tasks, executes them, tracks progress,
    and reports blockers and completion status.
    """

    def __init__(self, project_state_path: Optional[Path] = None):
        """
        Initialize the Executor agent.

        Args:
            project_state_path: Path to the project state database.
        """
        super().__init__(
            name="Executor",
            model=MODEL_EXECUTOR,
            system_prompt=EXECUTOR_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

        self.current_task: Optional[dict[str, Any]] = None
        self.task_start_time: Optional[datetime] = None

    def run_event_loop(self) -> None:
        """
        Run the main event loop for task execution.

        Continuously picks up tasks, executes them, and handles completion.
        """
        logger.info("[Executor] Starting event loop...")

        check_interval = DEFAULT_CHECK_INTERVAL_MINUTES * 60

        while self._running:
            try:
                # Process pending messages first
                messages = self.get_pending_messages()
                for message in messages:
                    self._handle_message(message)

                # If not currently working on a task, pick one up
                if self.current_task is None:
                    task = self.pick_up_task()
                    if task:
                        self.current_task = task
                        self.task_start_time = datetime.now()
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

                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("[Executor] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[Executor] Error in event loop: {e}")
                time.sleep(60)

        logger.info("[Executor] Event loop stopped")

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message."""
        msg_type = message.get("type", "")
        subject = message.get("subject", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")

        logger.info(f"[Executor] Received message from {from_agent}: {subject}")

        self.message_bus.mark_read(message["id"])

        if msg_type == "review_response":
            # Handle review response from FDA
            self._handle_review_response(message)

        elif msg_type == "task_assignment":
            # Handle direct task assignment
            logger.info(f"[Executor] Received task assignment: {subject}")

        elif msg_type == "priority_change":
            # Handle priority change notification
            logger.info(f"[Executor] Priority change: {body}")

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
