"""
FDA (Facilitating Director Agent) implementation.

The FDA agent is your personal AI assistant for managing your daily work,
tasks, calendar, and communications across your entire computer environment.
"""

import logging
import time
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.config import MODEL_FDA, DEFAULT_CHECK_INTERVAL_MINUTES
from fda.outlook import OutlookCalendar

logger = logging.getLogger(__name__)


FDA_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent), a personal AI assistant running on the user's computer.

You are NOT a project management tool for software development. You are a general-purpose personal assistant that helps the user manage their daily work and life.

Your scope is the user's entire work environment:
- Their calendar and meetings
- Their tasks and to-do items
- Their communications (Telegram, Discord, email)
- Their notes and journal entries
- Anything they need help tracking or remembering

Your personality:
- Helpful and proactive, like a skilled executive assistant
- Conversational and natural - not robotic or overly formal
- You remember context from past conversations
- You anticipate needs and offer suggestions
- You're direct and concise, but warm

When responding:
- Talk naturally, like a helpful colleague
- Don't use excessive formatting unless it helps clarity
- Be brief for simple questions, detailed when needed
- If you don't have information, just say so plainly
- Offer to help track or remember things for the user

You have access to:
- Tasks the user wants to track
- Journal entries and notes
- Calendar events (if connected)
- Alerts and reminders
- Historical context from past interactions
"""


class FDAAgent(BaseAgent):
    """
    Facilitating Director Agent - your personal AI assistant.

    FDA helps you manage your daily work, tasks, calendar, and communications
    across your entire computer environment.
    """

    def __init__(
        self,
        state_path: Optional[Path] = None,
        outlook_config: Optional[dict[str, str]] = None,
    ):
        """
        Initialize the FDA agent.

        Args:
            state_path: Path to the state database.
            outlook_config: Optional Outlook calendar configuration.
        """
        super().__init__(
            name="FDA",
            model=MODEL_FDA,
            system_prompt=FDA_SYSTEM_PROMPT,
            project_state_path=state_path,
        )

        # Initialize Outlook calendar if config provided
        self.calendar: Optional[OutlookCalendar] = None
        if outlook_config:
            self.calendar = OutlookCalendar(
                client_id=outlook_config["client_id"],
                tenant_id=outlook_config["tenant_id"],
                client_secret=outlook_config.get("client_secret"),
            )

    def run_event_loop(self) -> None:
        """
        Run the main event loop for the FDA agent.

        Periodically checks for messages, monitors project health,
        and prepares for upcoming meetings.
        """
        logger.info("[FDA] Starting event loop...")

        check_interval = DEFAULT_CHECK_INTERVAL_MINUTES * 60  # Convert to seconds

        while self._running:
            try:
                # Process pending messages
                messages = self.get_pending_messages()
                for message in messages:
                    self._handle_message(message)

                # Check for upcoming meetings
                if self.calendar:
                    self._check_upcoming_meetings()

                # Periodic health check (less frequent)
                # This could be expanded to run at specific intervals

                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("[FDA] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[FDA] Error in event loop: {e}")
                time.sleep(60)  # Wait before retrying

        logger.info("[FDA] Event loop stopped")

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message."""
        msg_type = message.get("type", "")
        subject = message.get("subject", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")

        logger.info(f"[FDA] Processing message from {from_agent}: {subject}")

        self.message_bus.mark_read(message["id"])

        if msg_type == "review_request":
            # Handle review request from Executor
            task_id = body  # Assuming body contains task_id
            result = self.review_task(task_id)
            self.send_message(
                to_agent=from_agent,
                msg_type="review_response",
                subject=f"Review complete: {task_id}",
                body=str(result),
            )

        elif msg_type == "blocker":
            # Handle blocker report
            self.add_alert("warning", f"Blocker reported: {subject} - {body}")

        elif msg_type == "alert":
            # Handle alert from other agents
            level = "critical" if "critical" in subject.lower() else "warning"
            self.add_alert(level, body)

    def _check_upcoming_meetings(self) -> None:
        """Check for upcoming meetings and prepare if needed."""
        if not self.calendar:
            return

        try:
            # Get meetings in the next 45 minutes
            upcoming = self.calendar.get_upcoming_events(within_minutes=45)

            for event in upcoming:
                event_id = event.get("id")
                if not event_id:
                    continue

                # Check if we already have prep for this meeting
                existing_prep = self.state.get_meeting_prep(event_id)
                if existing_prep:
                    continue

                # Prepare for the meeting
                logger.info(f"[FDA] Preparing for meeting: {event.get('subject')}")
                self.prepare_meeting(event_id)

        except Exception as e:
            logger.error(f"[FDA] Error checking meetings: {e}")

    def onboard(self) -> dict[str, Any]:
        """
        Onboard a new project with the FDA agent.

        Gathers project context, goals, and initial team information.

        Returns:
            Dictionary containing onboarding results and project context.
        """
        # Get any existing context
        existing_context = self.state.get_context("project_info")

        prompt = """Please help me onboard this project. I need to gather the following information:

1. Project name and brief description
2. Key objectives and success criteria
3. Main stakeholders and their roles
4. Current status and any existing work
5. Key risks or concerns

Based on the information provided, create a structured project context that I can use for ongoing management.

If there's existing context, summarize what we know and identify any gaps."""

        if existing_context:
            prompt += f"\n\nExisting project context:\n{existing_context}"

        response = self.chat(prompt, include_history=True)

        # Log the onboarding to journal
        self.log_to_journal(
            summary="Project onboarding completed",
            content=f"## Onboarding Session\n\n{response}",
            tags=["onboarding", "project-setup"],
            relevance_decay="slow",
        )

        return {
            "status": "completed",
            "response": response,
            "timestamp": datetime.now().isoformat(),
        }

    def daily_checkin(self) -> dict[str, Any]:
        """
        Perform daily health check of the project.

        Reviews current tasks, blockers, and KPI trends.

        Returns:
            Dictionary containing checkin results and any alerts.
        """
        # Gather project context
        context = self.get_project_context()

        # Get recent journal entries
        recent_entries = self.search_journal("", top_n=5)
        context["recent_journal_entries"] = [
            {"summary": e.get("summary"), "author": e.get("author")}
            for e in recent_entries
        ]

        # Get recent decisions
        decisions = self.state.get_decisions(limit=5)
        context["recent_decisions"] = decisions

        prompt = """Perform a daily project health check based on the current context.

Please provide:
1. **Overall Health Assessment**: Rate the project health (Good/Needs Attention/At Risk)
2. **Key Highlights**: What's going well?
3. **Concerns**: What needs attention?
4. **Blocked Items**: Any blockers that need immediate action?
5. **Recommendations**: Specific actions for today
6. **KPI Summary**: Any notable trends or changes

Be specific and actionable in your recommendations."""

        response = self.chat_with_context(prompt, context)

        # Parse response to determine if there are critical issues
        is_critical = any(
            word in response.lower()
            for word in ["at risk", "critical", "urgent", "blocked"]
        )

        if is_critical:
            self.add_alert("warning", "Daily checkin identified issues requiring attention")

        # Log to journal
        self.log_to_journal(
            summary=f"Daily checkin - {datetime.now().strftime('%Y-%m-%d')}",
            content=f"## Daily Health Check\n\n{response}",
            tags=["daily-checkin", "health-check"],
            relevance_decay="fast",
        )

        return {
            "status": "completed",
            "response": response,
            "is_critical": is_critical,
            "timestamp": datetime.now().isoformat(),
        }

    def ask(self, question: str) -> str:
        """
        Ask the FDA agent a question about the project.

        Args:
            question: The question to ask.

        Returns:
            The FDA agent's response.
        """
        # Get relevant context
        context = self.get_project_context()

        # Search journal for relevant entries
        relevant_entries = self.search_journal(question, top_n=3)
        if relevant_entries:
            context["relevant_journal_entries"] = [
                {
                    "summary": e.get("summary"),
                    "content": self.journal_retriever._read_entry_content(
                        e.get("filename", "")
                    )[:500],
                }
                for e in relevant_entries
            ]

        response = self.chat_with_context(question, context)

        return response

    def review_task(self, task_id: str) -> dict[str, Any]:
        """
        Review a specific task and provide feedback.

        Args:
            task_id: The ID of the task to review.

        Returns:
            Review results and recommendations.
        """
        # Get task details
        tasks = self.state.get_tasks()
        task = next((t for t in tasks if t.get("id") == task_id), None)

        if not task:
            return {
                "status": "error",
                "message": f"Task {task_id} not found",
            }

        context = {
            "task": task,
            "project_context": self.get_project_context(),
        }

        prompt = f"""Review this task and provide feedback:

Task ID: {task.get('id')}
Title: {task.get('title')}
Description: {task.get('description')}
Status: {task.get('status')}
Owner: {task.get('owner')}
Priority: {task.get('priority')}

Please provide:
1. Assessment of the task completion/progress
2. Any concerns or issues
3. Recommendations for next steps
4. Whether this task can be marked as complete (if applicable)"""

        response = self.chat_with_context(prompt, context)

        # Determine approval status
        approved = any(
            phrase in response.lower()
            for phrase in ["approved", "can be marked as complete", "looks good"]
        )

        return {
            "status": "completed",
            "task_id": task_id,
            "response": response,
            "approved": approved,
            "timestamp": datetime.now().isoformat(),
        }

    def check_kpis(self) -> dict[str, Any]:
        """
        Check key performance indicators for the project.

        Returns:
            Dictionary containing KPI values, trends, and health status.
        """
        # Get task-based KPIs
        tasks = self.state.get_tasks()

        total_tasks = len(tasks)
        completed_tasks = len([t for t in tasks if t.get("status") == "completed"])
        blocked_tasks = len([t for t in tasks if t.get("status") == "blocked"])
        in_progress = len([t for t in tasks if t.get("status") == "in_progress"])

        completion_rate = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
        block_rate = (blocked_tasks / total_tasks * 100) if total_tasks > 0 else 0

        # Record KPI snapshots
        self.state.add_kpi_snapshot("completion_rate", completion_rate)
        self.state.add_kpi_snapshot("block_rate", block_rate)
        self.state.add_kpi_snapshot("total_tasks", total_tasks)

        # Get historical trends
        completion_history = self.state.get_kpi_history("completion_rate", limit=7)
        block_history = self.state.get_kpi_history("block_rate", limit=7)

        kpi_data = {
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "in_progress_tasks": in_progress,
            "blocked_tasks": blocked_tasks,
            "completion_rate": round(completion_rate, 1),
            "block_rate": round(block_rate, 1),
            "completion_trend": [h.get("value") for h in completion_history],
            "block_trend": [h.get("value") for h in block_history],
        }

        # Get AI analysis
        context = {"kpi_data": kpi_data}
        prompt = """Analyze these project KPIs and provide insights:

Please assess:
1. Overall project velocity
2. Concerning trends
3. Areas performing well
4. Recommendations for improvement"""

        analysis = self.chat_with_context(prompt, context)

        return {
            "status": "completed",
            "kpis": kpi_data,
            "analysis": analysis,
            "timestamp": datetime.now().isoformat(),
        }

    def prepare_meeting(self, event_id: str) -> dict[str, Any]:
        """
        Prepare briefing materials for an upcoming meeting.

        Args:
            event_id: The ID of the calendar event.

        Returns:
            Dictionary containing meeting brief, agenda, and discussion points.
        """
        # Get event details if calendar is available
        event_details = {}
        if self.calendar:
            try:
                event_details = self.calendar.get_event_details(event_id)
            except Exception as e:
                logger.warning(f"Could not fetch event details: {e}")

        # Get project context
        context = self.get_project_context()
        context["event"] = event_details

        # Get recent relevant journal entries
        meeting_subject = event_details.get("subject", "")
        if meeting_subject:
            relevant = self.search_journal(meeting_subject, top_n=3)
            context["relevant_history"] = [
                e.get("summary") for e in relevant
            ]

        prompt = f"""Prepare a briefing for this upcoming meeting:

Meeting: {event_details.get('subject', 'Unknown')}
Time: {event_details.get('start', 'Unknown')}
Attendees: {', '.join(a.get('name', a.get('email', '')) for a in event_details.get('attendees', []))}
Location: {event_details.get('location', 'Unknown')}

Please provide:
1. **Meeting Brief**: Key context and background
2. **Suggested Agenda**: Discussion topics in priority order
3. **Key Points to Address**: Important items that must be covered
4. **Potential Questions**: Questions that might come up
5. **Recommended Actions**: Outcomes to aim for
6. **Supporting Data**: Relevant metrics or status updates"""

        response = self.chat_with_context(prompt, context)

        # Store the preparation
        prep_id = self.state.record_meeting_prep(
            event_id=event_id,
            brief=response,
            created_by=self.name,
        )

        # Log to journal
        self.log_to_journal(
            summary=f"Meeting prep: {event_details.get('subject', event_id)}",
            content=f"## Meeting Preparation\n\n{response}",
            tags=["meeting-prep", "briefing"],
            relevance_decay="fast",
        )

        return {
            "status": "completed",
            "prep_id": prep_id,
            "event_id": event_id,
            "brief": response,
            "timestamp": datetime.now().isoformat(),
        }

    def make_decision(
        self,
        title: str,
        options: list[str],
        context_info: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Make a strategic decision with recorded rationale.

        Args:
            title: Title of the decision to make.
            options: List of options to choose from.
            context_info: Additional context for the decision.

        Returns:
            Dictionary with the decision and rationale.
        """
        context = self.get_project_context()
        if context_info:
            context["additional_context"] = context_info

        options_str = "\n".join(f"{i+1}. {opt}" for i, opt in enumerate(options))

        prompt = f"""I need to make a decision about: {title}

Options:
{options_str}

Based on the current project context, please:
1. Analyze each option's pros and cons
2. Recommend the best option
3. Provide clear rationale for the recommendation
4. Identify any risks or considerations
5. Suggest implementation steps"""

        response = self.chat_with_context(prompt, context)

        # Record the decision
        decision_id = self.state.add_decision(
            title=title,
            rationale=response,
            decision_maker=self.name,
            impact="To be determined based on implementation",
        )

        # Log to journal
        self.log_to_journal(
            summary=f"Decision: {title}",
            content=f"## Decision Record\n\n**Options considered:**\n{options_str}\n\n**Analysis and Decision:**\n{response}",
            tags=["decision", "strategic"],
            relevance_decay="slow",
        )

        return {
            "status": "completed",
            "decision_id": decision_id,
            "title": title,
            "response": response,
            "timestamp": datetime.now().isoformat(),
        }
