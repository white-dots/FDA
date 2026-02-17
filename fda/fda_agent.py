"""
FDA (Facilitating Director Agent) implementation.

FDA is a PEER agent - the user-facing interface of the multi-agent system.
It communicates with users via Discord voice (primary) and Telegram (secondary),
and collaborates with Librarian and Executor agents to fulfill requests.

FDA does NOT boss the other agents - they are equals collaborating via message bus.
"""

import logging
import time
import json
import re
import os
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.config import MODEL_FDA, DEFAULT_CHECK_INTERVAL_MINUTES
from fda.outlook import OutlookCalendar
from fda.comms.message_bus import MessageTypes, Agents

logger = logging.getLogger(__name__)


FDA_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent), a personal AI assistant - a PERSONA living on the user's computer.

You are the user-facing interface of a multi-agent system. You work with two peer agents:
- **Librarian**: Knows about files, documents, and knowledge on the computer
- **Executor**: Can run commands, create files, and take actions

You collaborate with these peers - you don't boss them around. When the user asks for something:
- If it's about finding files or information → Ask Librarian
- If it's about running commands or making changes → Ask Executor
- If you can answer directly from your knowledge → Do so

Your scope is the user's entire work environment:
- Their calendar and meetings
- Their tasks and to-do items
- Their communications (Telegram, Discord voice)
- Their files and documents (via Librarian)
- Commands and actions (via Executor)

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
- When you need to search or run something, tell the user you're asking your peers

You are the voice and face of the system. The user talks to YOU via Discord voice or Telegram.
"""


class FDAAgent(BaseAgent):
    """
    Facilitating Director Agent - the user-facing peer agent.

    FDA is the primary interface for users via Discord voice and Telegram.
    It collaborates with Librarian and Executor agents to fulfill requests.
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

        # Track pending requests to peer agents
        self.pending_requests: dict[str, dict[str, Any]] = {}

    def run_event_loop(self) -> None:
        """
        Run the main event loop for the FDA agent.

        As a peer agent:
        1. Process messages from peer agents (Librarian, Executor)
        2. Handle user requests from Discord/Telegram
        3. Monitor project health and upcoming meetings
        """
        logger.info("[FDA] Starting event loop...")

        # Update agent status
        self.state.update_agent_status(self.name.lower(), "running", "Starting up")

        check_interval = min(DEFAULT_CHECK_INTERVAL_MINUTES * 60, 30)  # Check frequently

        while self._running:
            try:
                # Heartbeat
                self.state.agent_heartbeat(self.name.lower())
                self.state.update_agent_status(self.name.lower(), "running")

                # Process pending messages from peers
                messages = self.get_pending_messages()
                for message in messages:
                    self._handle_message(message)

                # Check for upcoming meetings
                if self.calendar:
                    self._check_upcoming_meetings()

                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("[FDA] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[FDA] Error in event loop: {e}")
                time.sleep(60)

        self.state.update_agent_status(self.name.lower(), "stopped")
        logger.info("[FDA] Event loop stopped")

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message from a peer agent."""
        msg_type = message.get("type", "")
        subject = message.get("subject", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")
        msg_id = message.get("id", "")
        reply_to = message.get("reply_to")

        logger.info(f"[FDA] Received {msg_type} from {from_agent}: {subject}")
        self.message_bus.mark_read(msg_id)

        # Handle responses from peer agents
        if msg_type in [
            MessageTypes.SEARCH_RESULT,
            MessageTypes.EXECUTE_RESULT,
            MessageTypes.FILE_COMPLETE,
            MessageTypes.KNOWLEDGE_RESULT,
            MessageTypes.INDEX_COMPLETE,
        ]:
            self._handle_peer_response(message)

        elif msg_type == MessageTypes.DISCOVERY:
            # Peer agent shared a discovery
            self._handle_discovery(message)

        elif msg_type == MessageTypes.BLOCKER:
            # Peer agent reports being blocked
            self.add_alert("warning", f"Blocker from {from_agent}: {subject} - {body}")

        elif msg_type == MessageTypes.STATUS_RESPONSE:
            # Peer agent status update
            logger.info(f"[FDA] Status from {from_agent}: {body}")

        # Legacy message types
        elif msg_type == "review_request":
            task_id = body
            result = self.review_task(task_id)
            self.send_message(
                to_agent=from_agent,
                msg_type="review_response",
                subject=f"Review complete: {task_id}",
                body=str(result),
            )

        elif msg_type == "blocker":
            self.add_alert("warning", f"Blocker reported: {subject} - {body}")

        elif msg_type == "alert":
            level = "critical" if "critical" in subject.lower() else "warning"
            self.add_alert(level, body)

    def _handle_peer_response(self, message: dict[str, Any]) -> None:
        """Handle a response from a peer agent (Librarian or Executor)."""
        msg_type = message.get("type", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")
        reply_to = message.get("reply_to")

        try:
            result_data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            result_data = {"result": body}

        success = result_data.get("success", True)
        result = result_data.get("result")
        error = result_data.get("error")

        # Store the response for retrieval
        if reply_to and reply_to in self.pending_requests:
            self.pending_requests[reply_to]["response"] = result_data
            self.pending_requests[reply_to]["completed"] = True
            logger.info(f"[FDA] Received response for request {reply_to}")

        if error:
            logger.warning(f"[FDA] Peer {from_agent} reported error: {error}")

    def _handle_discovery(self, message: dict[str, Any]) -> None:
        """Handle a discovery shared by a peer agent."""
        body = message.get("body", "")
        from_agent = message.get("from", "")

        try:
            discovery_data = json.loads(body)
            discovery_type = discovery_data.get("discovery_type", "unknown")
            description = discovery_data.get("description", "")

            logger.info(f"[FDA] Discovery from {from_agent}: {discovery_type} - {description}")

            # Store in state for future reference
            self.state.add_discovery(
                agent=from_agent,
                discovery_type=discovery_type,
                description=description,
                details=discovery_data.get("details"),
            )

        except (json.JSONDecodeError, TypeError):
            logger.info(f"[FDA] Discovery from {from_agent}: {body}")

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

    # ========== Peer Request Methods ==========

    def request_file_search(
        self,
        query: str,
        path: Optional[str] = None,
        wait_for_response: bool = True,
        timeout: float = 30.0,
    ) -> Optional[dict[str, Any]]:
        """
        Request a file search from the Librarian peer.

        Args:
            query: Search query.
            path: Optional path to search within.
            wait_for_response: Whether to wait for the response.
            timeout: How long to wait for response.

        Returns:
            Search results if wait_for_response=True, else the request ID.
        """
        msg_id = self.message_bus.request_search(
            from_agent=self.name.lower(),
            query=query,
            path=path,
        )

        self.pending_requests[msg_id] = {
            "type": "search",
            "query": query,
            "sent_at": datetime.now().isoformat(),
            "completed": False,
            "response": None,
        }

        if wait_for_response:
            response = self.message_bus.wait_for_response(
                agent_name=self.name.lower(),
                request_id=msg_id,
                timeout_seconds=timeout,
            )
            if response:
                self._handle_peer_response(response)
                return self.pending_requests[msg_id].get("response")
            # Timed out waiting for response - return a dict that indicates timeout
            return {"request_id": msg_id, "timed_out": True}

        return {"request_id": msg_id}

    def request_command_execution(
        self,
        command: str,
        cwd: Optional[str] = None,
        wait_for_response: bool = True,
        timeout: float = 60.0,
    ) -> Optional[dict[str, Any]]:
        """
        Request command execution from the Executor peer.

        Args:
            command: Command to execute.
            cwd: Working directory.
            wait_for_response: Whether to wait for the response.
            timeout: How long to wait for response.

        Returns:
            Execution results if wait_for_response=True, else the request ID.
        """
        msg_id = self.message_bus.request_execute(
            from_agent=self.name.lower(),
            command=command,
            cwd=cwd,
        )

        self.pending_requests[msg_id] = {
            "type": "execute",
            "command": command,
            "sent_at": datetime.now().isoformat(),
            "completed": False,
            "response": None,
        }

        if wait_for_response:
            response = self.message_bus.wait_for_response(
                agent_name=self.name.lower(),
                request_id=msg_id,
                timeout_seconds=timeout,
            )
            if response:
                self._handle_peer_response(response)
                return self.pending_requests[msg_id].get("response")

        return {"request_id": msg_id}

    def request_file_operation(
        self,
        operation: str,
        path: str,
        content: Optional[str] = None,
        destination: Optional[str] = None,
        wait_for_response: bool = True,
        timeout: float = 30.0,
    ) -> Optional[dict[str, Any]]:
        """
        Request a file operation from the Executor peer.

        Args:
            operation: Operation type ('create', 'edit', 'delete', 'read', 'copy', 'move').
            path: File path.
            content: Content for create/edit.
            destination: Destination for copy/move.
            wait_for_response: Whether to wait for the response.
            timeout: How long to wait for response.

        Returns:
            Operation results if wait_for_response=True, else the request ID.
        """
        msg_id = self.message_bus.request_file_operation(
            from_agent=self.name.lower(),
            operation=operation,
            path=path,
            content=content,
        )

        self.pending_requests[msg_id] = {
            "type": "file_operation",
            "operation": operation,
            "path": path,
            "sent_at": datetime.now().isoformat(),
            "completed": False,
            "response": None,
        }

        if wait_for_response:
            response = self.message_bus.wait_for_response(
                agent_name=self.name.lower(),
                request_id=msg_id,
                timeout_seconds=timeout,
            )
            if response:
                self._handle_peer_response(response)
                return self.pending_requests[msg_id].get("response")

        return {"request_id": msg_id}

    def request_knowledge(
        self,
        question: str,
        context: Optional[dict[str, Any]] = None,
        wait_for_response: bool = True,
        timeout: float = 30.0,
    ) -> Optional[dict[str, Any]]:
        """
        Request knowledge/information from the Librarian peer.

        Args:
            question: Question to ask.
            context: Additional context.
            wait_for_response: Whether to wait for the response.
            timeout: How long to wait for response.

        Returns:
            Knowledge results if wait_for_response=True, else the request ID.
        """
        msg_id = self.message_bus.request_knowledge(
            from_agent=self.name.lower(),
            question=question,
            context=context,
        )

        self.pending_requests[msg_id] = {
            "type": "knowledge",
            "question": question,
            "sent_at": datetime.now().isoformat(),
            "completed": False,
            "response": None,
        }

        if wait_for_response:
            response = self.message_bus.wait_for_response(
                agent_name=self.name.lower(),
                request_id=msg_id,
                timeout_seconds=timeout,
            )
            if response:
                self._handle_peer_response(response)
                return self.pending_requests[msg_id].get("response")

        return {"request_id": msg_id}

    def get_peer_status(self, agent_name: str) -> Optional[dict[str, Any]]:
        """
        Get the status of a peer agent.

        Args:
            agent_name: Name of the peer agent.

        Returns:
            Agent status dictionary.
        """
        return self.state.get_agent_status(agent_name.lower())

    def get_all_peer_statuses(self) -> list[dict[str, Any]]:
        """
        Get status of all peer agents.

        Returns:
            List of agent status dictionaries.
        """
        return self.state.get_all_agent_statuses()

    def onboard_interactive(self) -> dict[str, Any]:
        """
        Interactive onboarding that asks the user questions to set up FDA.

        Asks about:
        - Who the user is and what they do
        - Their goals and priorities
        - Where their data lives (calendars, task systems, files)
        - Communication preferences

        Returns:
            Dictionary containing onboarding results.
        """
        import os
        from pathlib import Path

        print("\n" + "=" * 60)
        print("Welcome to FDA - Your Personal AI Assistant")
        print("=" * 60)
        print("\nI'd like to get to know you and understand how I can help.")
        print("Let's go through a few questions to set things up.\n")

        # Gather information through questions
        responses = {}

        # Question 1: About the user
        print("-" * 40)
        print("1. ABOUT YOU")
        print("-" * 40)
        responses["name"] = input("What should I call you? > ").strip()
        responses["role"] = input("What do you do? (e.g., 'software engineer', 'product manager', 'entrepreneur') > ").strip()
        responses["context"] = input("Tell me briefly about your current work or situation: > ").strip()

        # Question 2: Goals
        print("\n" + "-" * 40)
        print("2. YOUR GOALS")
        print("-" * 40)
        responses["goals"] = input("What are you trying to accomplish right now? What are your priorities? > ").strip()
        responses["challenges"] = input("What's challenging or frustrating about your current workflow? > ").strip()

        # Question 3: Data sources
        print("\n" + "-" * 40)
        print("3. YOUR DATA & TOOLS")
        print("-" * 40)
        print("Where do you keep your important information?")
        print("(I can connect to calendars, read files, track tasks, etc.)")
        print()

        # Calendar
        cal_response = input("Do you use Outlook/Office 365 calendar? (y/n) > ").strip().lower()
        responses["uses_outlook"] = cal_response in ("y", "yes")

        # Files/Documents
        responses["important_folders"] = input("Any folders I should know about? (paths, comma-separated, or 'skip') > ").strip()

        # Communication
        print("\nFor notifications and quick questions:")
        tg_response = input("Do you want to set up Telegram notifications? (y/n) > ").strip().lower()
        responses["uses_telegram"] = tg_response in ("y", "yes")

        discord_response = input("Do you want to set up Discord integration? (y/n) > ").strip().lower()
        responses["uses_discord"] = discord_response in ("y", "yes")

        # Question 4: Preferences
        print("\n" + "-" * 40)
        print("4. PREFERENCES")
        print("-" * 40)
        responses["check_in_time"] = input("What time should I do daily check-ins? (e.g., '9:00 AM' or 'skip') > ").strip()
        responses["communication_style"] = input("How should I communicate? (brief/detailed/adaptive) > ").strip() or "adaptive"

        # Question 5: Timezone
        print("\n" + "-" * 40)
        print("5. TIMEZONE")
        print("-" * 40)
        print("What's your timezone? Examples: America/New_York, Europe/London, Asia/Tokyo")
        print("(Enter 'auto' to detect from system, or just press Enter to skip)")
        tz_response = input("Timezone > ").strip()

        if tz_response.lower() == "auto" or not tz_response:
            # Try to detect system timezone
            responses["timezone"] = self._detect_system_timezone()
            if responses["timezone"]:
                print(f"  Detected timezone: {responses['timezone']}")
            else:
                print("  Could not detect timezone, will use system default")
        else:
            # Validate the provided timezone
            from fda.utils.timezone import validate_timezone
            validated_tz = validate_timezone(tz_response)
            if validated_tz:
                responses["timezone"] = validated_tz
                print(f"  Using timezone: {validated_tz}")
            else:
                print(f"  Warning: '{tz_response}' doesn't look like a valid timezone. Using system default.")
                responses["timezone"] = None

        # Now process with Claude to create a personalized setup
        print("\n" + "=" * 60)
        print("Setting up your personalized FDA assistant...")
        print("=" * 60 + "\n")

        # Store raw responses
        self.state.set_context("user_name", responses["name"])
        self.state.set_context("user_role", responses["role"])
        self.state.set_context("user_context", responses["context"])
        self.state.set_context("user_goals", responses["goals"])
        self.state.set_context("user_challenges", responses["challenges"])
        self.state.set_context("communication_style", responses["communication_style"])
        self.state.set_context("user_timezone", responses.get("timezone"))
        self.state.set_context("onboarded", True)
        self.state.set_context("onboarded_at", datetime.now().isoformat())

        # Build the full interview transcript for the journal
        interview_transcript = f"""# First Meeting with {responses['name']}

**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

---

## About {responses['name']}

**What should I call you?**
> {responses['name']}

**What do you do?**
> {responses['role']}

**Tell me about your current work or situation:**
> {responses['context']}

---

## Goals & Priorities

**What are you trying to accomplish right now?**
> {responses['goals']}

**What's challenging or frustrating about your current workflow?**
> {responses['challenges']}

---

## Tools & Data Sources

- **Outlook/Office 365 calendar:** {'Yes' if responses['uses_outlook'] else 'No'}
- **Important folders:** {responses['important_folders'] if responses['important_folders'] and responses['important_folders'].lower() != 'skip' else 'None specified'}
- **Telegram notifications:** {'Yes' if responses['uses_telegram'] else 'No'}
- **Discord integration:** {'Yes' if responses['uses_discord'] else 'No'}

---

## Preferences

- **Daily check-in time:** {responses['check_in_time'] if responses['check_in_time'] and responses['check_in_time'].lower() != 'skip' else 'Not set'}
- **Communication style:** {responses['communication_style']}
- **Timezone:** {responses.get('timezone') or 'System default'}

---

## Key Takeaways

- {responses['name']} is a {responses['role']}
- Main focus: {responses['goals'][:200] if len(responses['goals']) > 200 else responses['goals']}
- Key challenge: {responses['challenges'][:200] if len(responses['challenges']) > 200 else responses['challenges']}
"""

        # Save the full interview as the first journal entry
        self.log_to_journal(
            summary=f"First meeting with {responses['name']} - Onboarding interview",
            content=interview_transcript,
            tags=["onboarding", "first-meeting", "user-profile"],
            relevance_decay="slow",
        )

        # Ask Claude to synthesize and create a personalized welcome
        synthesis_prompt = f"""Based on this onboarding information, create a brief personalized summary and suggest 2-3 immediate ways I can help.

User Info:
- Name: {responses['name']}
- Role: {responses['role']}
- Context: {responses['context']}
- Goals: {responses['goals']}
- Challenges: {responses['challenges']}
- Communication style preference: {responses['communication_style']}
- Uses Outlook calendar: {responses['uses_outlook']}
- Uses Telegram: {responses['uses_telegram']}
- Uses Discord: {responses['uses_discord']}

Keep it warm and conversational. Don't use excessive formatting. End by asking what they'd like to tackle first."""

        welcome_response = self.chat(synthesis_prompt, include_history=False)

        print(welcome_response)

        # Provide next steps based on their choices
        print("\n" + "-" * 40)
        print("NEXT STEPS")
        print("-" * 40)

        if responses["uses_outlook"]:
            print("- Run 'fda calendar login' to connect your Outlook calendar")

        if responses["uses_telegram"]:
            print("- Run 'fda telegram setup' to configure Telegram notifications")

        if responses["uses_discord"]:
            print("- Run 'fda discord setup' to configure Discord integration")

        print("- Run 'fda ask \"<your question>\"' to chat with me anytime")
        print("- Run 'fda task add \"<task>\"' to track something")
        print()

        return {
            "status": "completed",
            "responses": responses,
            "welcome": welcome_response,
            "timestamp": datetime.now().isoformat(),
        }

    def is_onboarded(self) -> bool:
        """Check if the user has completed onboarding."""
        return bool(self.state.get_context("onboarded"))

    def _detect_system_timezone(self) -> Optional[str]:
        """
        Try to detect the system's timezone.

        Returns:
            IANA timezone name or None if detection fails.
        """
        from fda.utils.timezone import detect_system_timezone
        return detect_system_timezone()

    def gather_daily_context(self, start_of_day: datetime, end_of_day: datetime) -> dict[str, Any]:
        """
        Gather context from today for journal generation.

        Args:
            start_of_day: Start of day in user's timezone.
            end_of_day: End of day in user's timezone.

        Returns:
            Dictionary containing today's context.
        """
        today_str = start_of_day.strftime("%Y-%m-%d")

        context = {
            "date": start_of_day.strftime("%A, %B %d, %Y"),
            "date_iso": today_str,
            "user_name": self.state.get_context("user_name"),
            "user_role": self.state.get_context("user_role"),
            "user_goals": self.state.get_context("user_goals"),
        }

        # Get all tasks and filter for today
        all_tasks = self.state.get_tasks()
        today_tasks = []
        completed_today = []
        in_progress = []

        for t in all_tasks:
            updated_at = t.get("updated_at", "")
            if isinstance(updated_at, str) and updated_at.startswith(today_str):
                today_tasks.append(t)
                if t.get("status") == "completed":
                    completed_today.append(t)
            if t.get("status") == "in_progress":
                in_progress.append(t)

        context["tasks_completed_today"] = [t.get("title") for t in completed_today]
        context["tasks_in_progress"] = [t.get("title") for t in in_progress[:5]]
        context["tasks_updated_today"] = len(today_tasks)

        # Get calendar events for today (if calendar connected)
        context["calendar_events"] = []
        if self.calendar:
            try:
                events = self.calendar.get_events_today()
                context["calendar_events"] = [
                    {"subject": e.get("subject"), "start": e.get("start")}
                    for e in events
                ]
            except Exception as e:
                logger.debug(f"Could not fetch calendar events: {e}")

        # Get journal entries from today
        recent_entries = self.search_journal("", top_n=10)
        today_entries = [
            e for e in recent_entries
            if e.get("created_at", "").startswith(today_str)
        ]
        context["journal_entries_today"] = [e.get("summary") for e in today_entries]

        # Get any alerts from today
        alerts = self.state.get_alerts()
        today_alerts = [
            a for a in alerts
            if a.get("created_at", "").startswith(today_str)
        ]
        context["alerts_today"] = [a.get("message") for a in today_alerts[:5]]

        # Get any decisions made today
        decisions = self.state.get_decisions(limit=10)
        today_decisions = [
            d for d in decisions
            if d.get("created_at", "").startswith(today_str)
        ]
        context["decisions_today"] = [d.get("title") for d in today_decisions]

        # Get today's conversations from ALL interfaces (Discord, Telegram, CLI)
        try:
            all_messages = self.state.get_messages_today(limit=200)
            if all_messages:
                convo_summary = []
                for msg in all_messages:
                    source = msg.get("source", "unknown")
                    role = msg.get("username", "User") if msg["role"] == "user" else "FDA"
                    # Truncate long messages for summary
                    content = msg["content"][:200]
                    convo_summary.append(f"[{source}] {role}: {content}")
                context["conversations_today"] = convo_summary
        except Exception as e:
            logger.debug(f"Could not fetch conversation messages: {e}")

        return context

    def generate_daily_journal(self, context: dict[str, Any], current_time: datetime) -> str:
        """
        Generate a reflective journal entry using Claude.

        Args:
            context: Daily context gathered by gather_daily_context.
            current_time: Current time in user's timezone.

        Returns:
            Generated journal entry content as markdown.
        """
        user_name = context.get("user_name", "the user")
        user_role = context.get("user_role", "professional")

        # Build a summary of activities
        tasks_completed = context.get("tasks_completed_today", [])
        tasks_in_progress = context.get("tasks_in_progress", [])
        calendar_events = context.get("calendar_events", [])
        journal_entries = context.get("journal_entries_today", [])
        alerts = context.get("alerts_today", [])
        decisions = context.get("decisions_today", [])
        discord_convos = context.get("conversations_today", [])

        prompt = f"""Based on today's activities, write a personal daily journal entry for {user_name}.

Today's Date: {context.get('date')}
Current Time: {current_time.strftime('%I:%M %p')}
Role: {user_role}

## Today's Activities

### Tasks Completed
{chr(10).join(f'- {t}' for t in tasks_completed) if tasks_completed else '- No tasks marked as completed today'}

### Tasks In Progress
{chr(10).join(f'- {t}' for t in tasks_in_progress) if tasks_in_progress else '- No tasks currently in progress'}

### Calendar Events
{chr(10).join(f'- {e.get("subject")} at {e.get("start", "")[:16]}' for e in calendar_events) if calendar_events else '- No calendar events (or calendar not connected)'}

### Notes Made Today
{chr(10).join(f'- {e}' for e in journal_entries) if journal_entries else '- No journal entries made today'}

### Alerts/Reminders
{chr(10).join(f'- {a}' for a in alerts) if alerts else '- No alerts today'}

### Decisions Made
{chr(10).join(f'- {d}' for d in decisions) if decisions else '- No major decisions recorded'}

### Conversations Today (Discord, Telegram, CLI)
{chr(10).join(f'- {c}' for c in discord_convos[:30]) if discord_convos else '- No conversations today'}

---

Write a reflective journal entry that:
1. Summarizes what was accomplished today
2. Notes any important events, meetings, or interactions
3. Reflects on progress toward goals (their goals: {context.get('user_goals', 'not specified')})
4. Mentions any challenges, blockers, or lessons learned
5. Suggests focus areas or intentions for tomorrow

Write in first person, conversational tone as if {user_name} is writing their own journal.
Keep it meaningful but concise (200-400 words).
Format as markdown with appropriate headers.
Don't be overly positive if nothing was accomplished - be honest and reflective."""

        return self.chat(prompt, include_history=False)

    def generate_daily_brief(self) -> str:
        """
        Generate a concise, spoken-friendly daily briefing.

        Gathers today's context (tasks, calendar, alerts, journal) and
        produces a natural-sounding brief suitable for text-to-speech.

        Returns:
            A spoken-friendly daily briefing string.
        """
        from datetime import timedelta

        now = datetime.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        # Gather today's context
        context = self.gather_daily_context(start_of_day, end_of_day)

        user_name = context.get("user_name", "there")
        user_role = context.get("user_role", "")
        tasks_completed = context.get("tasks_completed_today", [])
        tasks_in_progress = context.get("tasks_in_progress", [])
        calendar_events = context.get("calendar_events", [])
        journal_entries = context.get("journal_entries_today", [])
        alerts = context.get("alerts_today", [])
        decisions = context.get("decisions_today", [])
        user_goals = context.get("user_goals", "")
        discord_convos = context.get("conversations_today", [])

        prompt = f"""Generate a spoken daily briefing for {user_name}.
Today is {context.get('date')}, current time is {now.strftime('%I:%M %p')}.

Here's today's context:

Tasks completed today: {', '.join(tasks_completed) if tasks_completed else 'None yet'}
Tasks in progress: {', '.join(tasks_in_progress) if tasks_in_progress else 'None tracked'}
Calendar events: {', '.join(e.get('subject', 'Unknown') + ' at ' + str(e.get('start', ''))[:16] for e in calendar_events) if calendar_events else 'No events scheduled'}
Alerts: {', '.join(alerts) if alerts else 'No alerts'}
Recent notes: {', '.join(journal_entries[:5]) if journal_entries else 'No journal entries'}
Decisions made today: {', '.join(decisions) if decisions else 'None'}
Their goals: {user_goals if user_goals else 'Not specified'}
Today's conversations: {chr(10).join(discord_convos[:20]) if discord_convos else 'No conversations yet'}

Write a warm, natural spoken briefing as if you're a personal assistant greeting {user_name} for the day.
Keep it conversational and concise (under 200 words) — this will be read aloud via text-to-speech.
Start with a greeting, then cover:
1. Quick overview of the day ahead (calendar)
2. Current task status
3. Any alerts or items needing attention
4. A motivational or helpful closing thought

Do NOT use markdown, bullet points, or special formatting — just natural spoken language.
Do NOT use emojis."""

        return self.chat(prompt, include_history=False)

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

    def _explain_capability_limitation(self, question: str) -> str:
        """
        Explain why FDA can't fulfill a request that requires external capabilities.

        Called when Claude Code isn't available but the request needs tools
        like web search, API access, etc.

        Args:
            question: The original question.

        Returns:
            A helpful message explaining the limitation and how to resolve it.
        """
        return (
            "This request requires capabilities I don't have directly "
            "(like web search, real-time data, or external API access). "
            "I tried to delegate to Claude Code which has these tools, "
            "but it's not currently available.\n\n"
            "To enable this, please:\n"
            "1. Make sure the Executor agent is running: `fda start --all`\n"
            "2. Or ask me through Claude Code directly if you have access\n\n"
            "Is there something else I can help you with using my current capabilities?"
        )

    def _requires_external_capabilities(self, question: str) -> bool:
        """
        Check if the question requires capabilities FDA doesn't have.

        FDA is a simple Claude API wrapper without tools. Requests needing
        web search, real-time data, API access, code execution, etc.
        should be delegated to Claude Code which has these capabilities.

        Args:
            question: The question to check.

        Returns:
            True if the question requires capabilities beyond FDA's scope.
        """
        question_lower = question.lower()

        # Web search / internet lookups
        web_search_phrases = [
            "search the web", "web search", "search online", "look up online",
            "google", "search for", "find online", "internet search",
            "what's the latest", "current news", "recent news", "today's news",
            "latest update", "what happened", "breaking news",
        ]

        # Real-time / live data
        realtime_phrases = [
            "current price", "stock price", "weather", "forecast",
            "right now", "live", "real-time", "realtime", "real time",
            "up to date", "latest", "current",
        ]

        # External API / integration tasks
        api_phrases = [
            "api", "fetch data", "download", "scrape", "crawl",
            "call the", "query the", "access the", "connect to",
        ]

        # Code execution / automation
        automation_phrases = [
            "run this code", "execute", "automate", "script",
            "install", "deploy", "build", "compile",
        ]

        # Research / complex tasks
        research_phrases = [
            "research", "investigate", "analyze this", "deep dive",
            "comprehensive", "detailed analysis", "thorough review",
        ]

        # Explicit Claude Code delegation requests
        claude_code_phrases = [
            "using claude code", "use claude code", "with claude code",
            "via claude code", "through claude code", "ask claude code",
            "claude code", "delegate to claude",
        ]

        all_capability_phrases = (
            web_search_phrases +
            realtime_phrases +
            api_phrases +
            automation_phrases +
            research_phrases +
            claude_code_phrases
        )

        return any(phrase in question_lower for phrase in all_capability_phrases)

    def ask(self, question: str, use_claude_code: bool = True, conversation_history: list[dict[str, str]] = None) -> str:
        """
        Ask the FDA agent a question.

        By default, delegates to Claude Code (uses Max subscription).
        Falls back to direct API if Claude Code unavailable.

        Automatically detects requests requiring external capabilities
        (web search, real-time data, API access) and delegates those
        to Claude Code which has those tools available.

        Args:
            question: The question to ask.
            use_claude_code: If True (default), try to use Claude Code via Max subscription.
            conversation_history: Recent conversation exchanges for context continuity.

        Returns:
            The FDA agent's response.
        """
        question_lower = question.lower()

        # Classify the user's intent using LLM
        intent = self._classify_intent(question)
        logger.info(f"[FDA] Intent classified as: {intent}")

        # Check if this should be delegated to a peer agent
        peer_result = None

        # If user mentions an explicit path, remember it as the active project path
        explicit_path = self._extract_path_from_question(question)
        if explicit_path:
            self.state.set_context("active_project_path", explicit_path)
            logger.info(f"[FDA] Saved active project path: {explicit_path}")

        # File/knowledge requests → Librarian
        if intent == "librarian":
            peer_result = self._delegate_to_librarian(question)

        # Simple status/info queries - answer directly without Claude Code
        is_simple_query = intent == "greeting"

        # Check if the request requires capabilities FDA doesn't have
        requires_delegation = self._requires_external_capabilities(question)

        # Try Claude Code for:
        # 1. Requests requiring external capabilities (web search, APIs, etc.)
        # 2. Complex questions that aren't simple greetings
        if use_claude_code and not is_simple_query and not peer_result:
            claude_code_result = self._try_claude_code(question)
            if claude_code_result:
                self._journal_interaction(question, claude_code_result, source="claude_code")
                return claude_code_result

            # If Claude Code failed, always fall through to direct API.
            # FDA can still give a helpful answer even without web search/tools.
            if requires_delegation:
                logger.info("[FDA] Claude Code unavailable for capability-heavy request, answering directly")

        # Fall back to direct API call if Claude Code not available
        # Build context with user info if available
        context = {}

        # Add user context from onboarding
        user_name = self.state.get_context("user_name")
        if user_name:
            context["user"] = {
                "name": user_name,
                "role": self.state.get_context("user_role"),
                "goals": self.state.get_context("user_goals"),
                "challenges": self.state.get_context("user_challenges"),
            }

        # Add task context
        project_context = self.get_project_context()
        context.update(project_context)

        # Add project knowledge context
        project_knowledge = self._get_relevant_project_knowledge(question)
        if project_knowledge:
            context["project_knowledge"] = project_knowledge

        # Add peer agent results if we delegated
        if peer_result:
            context["peer_agent_result"] = peer_result

        # Search journal for relevant entries
        relevant_entries = self.search_journal(question, top_n=3)
        if relevant_entries:
            context["relevant_notes"] = [
                {
                    "summary": e.get("summary"),
                    "content": self.journal_retriever._read_entry_content(
                        e.get("filename", "")
                    )[:500],
                }
                for e in relevant_entries
            ]

        # Add conversation history for context continuity
        if conversation_history:
            # Format as readable conversation log
            convo_lines = []
            for msg in conversation_history:
                role = "User" if msg.get("role") == "user" else "FDA"
                convo_lines.append(f"{role}: {msg.get('content', '')[:300]}")
            context["recent_conversation_for_context"] = "\n".join(convo_lines)

        # If we got a peer result, include it in the prompt
        if peer_result:
            enhanced_question = f"""{question}

[I asked my peer agents to help with this. Here's what they found:]
{json.dumps(peer_result, indent=2)[:2000]}

Please summarize and present this information naturally to the user."""
        else:
            enhanced_question = question

        response = self.chat_with_context(enhanced_question, context)

        if not is_simple_query:
            source = "librarian+fda" if peer_result else "fda"
            self._journal_interaction(question, response, source=source)

        return response

    def _classify_intent(self, question: str) -> str:
        """
        Classify the user's question intent using the LLM.

        Returns one of:
        - 'greeting': simple greetings, thanks, small talk
        - 'librarian': finding files, searching documents, looking up info on disk
        - 'executor': running commands, executing code, making changes
        - 'general': everything else (project questions, status, advice, etc.)
        """
        try:
            prompt = (
                "Classify this user message into exactly one category. "
                "Reply with ONLY the category name, nothing else.\n\n"
                "Categories:\n"
                "- greeting: greetings, thanks, small talk (hi, hello, thanks, how are you)\n"
                "- librarian: finding/locating/searching for files, images, documents, "
                "or information stored on disk\n"
                "- executor: running commands, executing scripts, making file changes, "
                "deploying, building\n"
                "- general: project questions, status, advice, planning, or anything else\n\n"
                f"Message: {question[:300]}\n\n"
                "Category:"
            )
            result = self.chat(prompt, include_history=False, max_tokens=20, temperature=0.0)
            intent = result.strip().lower().rstrip(".")

            if intent in ("greeting", "librarian", "executor", "general"):
                return intent

            # Fuzzy match in case the model adds extra words
            for category in ("greeting", "librarian", "executor", "general"):
                if category in intent:
                    return category

            return "general"
        except Exception as e:
            logger.debug(f"[FDA] Intent classification failed: {e}")
            return "general"

    def _journal_interaction(
        self,
        question: str,
        response: str,
        source: str = "fda",
    ) -> None:
        """
        Summarize and journal a Q&A interaction.

        Uses Claude to generate a concise summary, then saves it to the
        journal so FDA retains memory of conversations.

        Args:
            question: The user's question.
            response: The response that was given.
            source: Where the response came from (fda, claude_code, librarian+fda).
        """
        try:
            summary_prompt = (
                f"Summarize this interaction in one sentence (max 20 words) "
                f"for a journal log. Focus on what was asked and the key outcome.\n\n"
                f"Question: {question[:500]}\n"
                f"Response: {response[:1000]}"
            )
            summary = self.chat(summary_prompt, include_history=False, max_tokens=100)
            summary = summary.strip().rstrip(".")

            content = (
                f"## Q&A Interaction\n\n"
                f"**Source:** {source}\n\n"
                f"**Question:** {question}\n\n"
                f"**Response:**\n{response}\n"
            )

            self.log_to_journal(
                summary=summary,
                content=content,
                tags=["interaction", source.replace("+", "-")],
                relevance_decay="medium",
            )
        except Exception as e:
            logger.debug(f"[FDA] Failed to journal interaction: {e}")

    def _try_claude_code(self, question: str) -> Optional[str]:
        """
        Try to answer a question using Claude Code (Max subscription).

        Args:
            question: The question to ask.

        Returns:
            Response from Claude Code, or None if unavailable.
        """
        # Check if Executor is running
        executor_status = self.get_peer_status(Agents.EXECUTOR)
        if not executor_status or executor_status.get("status") != "running":
            logger.debug("[FDA] Executor not running, falling back to direct API")
            return None

        # Build context for Claude Code
        user_name = self.state.get_context("user_name") or "user"
        user_role = self.state.get_context("user_role") or ""
        user_goals = self.state.get_context("user_goals") or ""

        # Prepare the prompt with context
        # Note: Claude Code has access to tools (web search, bash, file access)
        # that the FDA agent's direct API calls don't have
        prompt = f"""You are FDA, a personal AI assistant for {user_name}.
{f"They are a {user_role}." if user_role else ""}
{f"Their goals: {user_goals}" if user_goals else ""}

User's question: {question}

You have access to tools like web search, file access, and command execution.
Use them if needed to fully answer the question.
Answer helpfully and conversationally. Be concise but thorough."""

        logger.info(f"[FDA] Delegating to Claude Code: {question[:50]}...")

        result = self._delegate_to_claude_code(
            prompt=prompt,
            timeout=120,  # 2 minute timeout for questions
        )

        if result is None:
            logger.debug("[FDA] Claude Code did not respond, falling back to direct API")
            return None

        if result.get("success"):
            output = result.get("output", "").strip()
            if output:
                logger.info("[FDA] Got response from Claude Code")
                return output

        # Claude Code failed - log and fall back
        error = result.get("error", "")
        if error:
            logger.warning(f"[FDA] Claude Code error: {error}")

        return None

    @staticmethod
    def _extract_path_from_question(question: str) -> Optional[str]:
        """
        Extract an explicit filesystem path from the user's question.

        Detects absolute paths (starting with / or ~) that the user explicitly
        references so we can search there directly instead of relying on default
        exploration roots.

        Args:
            question: The user's question text.

        Returns:
            An existing directory or file path if found, else None.
        """
        # Match absolute paths like /Users/foo/bar or ~/Documents/project
        path_patterns = [
            r'(/[A-Za-z][A-Za-z0-9_.\-/]*(?:/[A-Za-z0-9_.\-]+)+)',  # /absolute/path
            r'(~/[A-Za-z0-9_.\-/]+)',  # ~/relative/path
        ]
        for pattern in path_patterns:
            matches = re.findall(pattern, question)
            for match in matches:
                # Expand ~ to home directory
                expanded = os.path.expanduser(match)
                # Strip trailing punctuation that might have been captured
                expanded = expanded.rstrip('.,;:!?)"\'')
                if os.path.exists(expanded):
                    logger.info(f"[FDA] Extracted explicit path from question: {expanded}")
                    return expanded
        return None

    def _get_relevant_project_knowledge(self, question: str) -> Optional[dict[str, Any]]:
        """
        Search the project knowledge base for relevant project context.

        Splits the question into keywords, searches the project_keywords table,
        and returns a summary of the top-scoring project.

        Args:
            question: The user's question.

        Returns:
            Dictionary with project info, domains, and keywords, or None.
        """
        # Split question into keywords
        words = re.findall(r'[a-zA-Z]{2,}', question)
        if not words:
            return None

        # Filter out common stopwords
        stopwords = {
            "the", "is", "are", "was", "were", "do", "does", "did", "have", "has",
            "had", "be", "been", "being", "will", "would", "could", "should", "may",
            "can", "what", "where", "when", "how", "why", "who", "which", "that",
            "this", "with", "for", "from", "about", "into", "through", "during",
            "before", "after", "above", "below", "between", "and", "but", "or",
            "not", "no", "yes", "all", "any", "each", "every", "some", "many",
            "much", "more", "most", "few", "less", "other", "another", "such",
            "only", "own", "same", "than", "too", "very", "just", "also",
        }
        keywords = [w.lower() for w in words if w.lower() not in stopwords]
        if not keywords:
            return None

        try:
            results = self.state.search_project_keywords(keywords, limit=3)
            if not results:
                return None

            # Get summary for the top-scoring project
            top = results[0]
            summary = self.state.get_project_summary(top["project_id"])
            if not summary:
                return None

            project = summary["project"]
            domains = summary["domains"]
            top_keywords = summary["top_keywords"]

            return {
                "project_name": project.get("name"),
                "project_path": project.get("path"),
                "project_type": project.get("project_type"),
                "description": project.get("description"),
                "tech_stack": project.get("tech_stack"),
                "domains": [
                    {"name": d["domain_name"], "description": d.get("description"), "file_count": d.get("file_count")}
                    for d in domains[:5]
                ],
                "relevance_score": top.get("total_score"),
                "matched_keywords": top.get("matched_keywords"),
            }
        except Exception as e:
            logger.debug(f"[FDA] Project knowledge lookup failed: {e}")
            return None

    def _delegate_to_librarian(self, question: str) -> Optional[dict[str, Any]]:
        """Delegate a search/knowledge request to Librarian."""
        logger.info(f"[FDA] Delegating to Librarian: {question}")

        # Extract explicit path from the user's question (e.g. /Users/.../Smartstore)
        explicit_path = self._extract_path_from_question(question)
        if not explicit_path:
            # Fall back to previously-stored active project path
            stored_path = self.state.get_context("active_project_path")
            if stored_path and os.path.exists(stored_path):
                explicit_path = stored_path
                logger.info(f"[FDA] Using stored active project path: {explicit_path}")
        if explicit_path:
            logger.info(f"[FDA] User specified explicit path: {explicit_path}")

        # Check if Librarian is running
        librarian_status = self.get_peer_status(Agents.LIBRARIAN)
        if librarian_status and librarian_status.get("status") == "running":
            # Send request and wait for response, passing explicit path if found
            result = self.request_file_search(question, path=explicit_path, wait_for_response=True, timeout=15.0)

            # Validate that Librarian actually returned a *useful* result.
            # request_file_search returns {"request_id": ...} on timeout, or
            # {"success": false, "error": ...} when Librarian fails, or
            # {"success": true, "result": {...}} with empty files/matches.
            if result and isinstance(result, dict):
                if result.get("success") is True and result.get("result"):
                    inner = result["result"]
                    # Check if Librarian actually found files or content matches
                    has_files = bool(inner.get("files"))
                    has_matches = bool(inner.get("matches"))
                    has_routes = bool(inner.get("routes"))
                    if has_files or has_matches or has_routes:
                        logger.info("[FDA] Librarian returned useful results")
                        return result
                    else:
                        logger.info("[FDA] Librarian returned no file matches, falling back")
                elif result.get("request_id"):
                    logger.warning("[FDA] Librarian timed out, falling back to filesystem search")
                else:
                    error = result.get("error", "unknown error")
                    logger.warning(f"[FDA] Librarian returned error: {error}, falling back")
        else:
            logger.info("[FDA] Librarian not running")

        # Fallback: search the filesystem directly using find command
        logger.info("[FDA] Searching filesystem directly as fallback")
        return self._search_filesystem_directly(question, explicit_path=explicit_path)

    def _search_filesystem_directly(self, question: str, explicit_path: Optional[str] = None) -> Optional[dict[str, Any]]:
        """
        Search the filesystem directly as a fallback when Librarian can't find files.

        If an explicit_path is provided (extracted from the user's question),
        search ONLY within that path. Otherwise, uses an LLM call to extract
        search terms and searches the home directory.

        Args:
            question: The user's question about files.
            explicit_path: An explicit path the user referenced in the question.

        Returns:
            Search results dict, or None if nothing found.
        """
        import subprocess

        try:
            # If the user gave us an explicit directory, list its contents directly
            if explicit_path and os.path.isdir(explicit_path):
                logger.info(f"[FDA] Exploring explicit directory: {explicit_path}")
                found_files = []
                try:
                    result = subprocess.run(
                        ["find", explicit_path, "-maxdepth", "3",
                         "-not", "-path", "*/.*",
                         "-not", "-path", "*/node_modules/*",
                         "-not", "-path", "*/__pycache__/*",
                         "-not", "-path", "*/.venv/*"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if result.stdout.strip():
                        found_files = result.stdout.strip().split("\n")
                except (subprocess.TimeoutExpired, Exception) as e:
                    logger.warning(f"[FDA] find command failed on explicit path: {e}")

                found_files = list(dict.fromkeys(found_files))
                if found_files:
                    return {
                        "success": True,
                        "explicit_path": explicit_path,
                        "files_found": found_files[:30],
                        "count": len(found_files),
                        "summary": f"Found {len(found_files)} file(s) in '{explicit_path}'",
                    }

            # If explicit path is a file, read its info directly
            if explicit_path and os.path.isfile(explicit_path):
                logger.info(f"[FDA] Explicit path is a file: {explicit_path}")
                return {
                    "success": True,
                    "explicit_path": explicit_path,
                    "files_found": [explicit_path],
                    "count": 1,
                    "summary": f"Found file: {explicit_path}",
                }

            # No explicit path - extract search term via LLM
            extract_prompt = (
                "Extract the filename, file pattern, or search keyword from this question. "
                "Reply with ONLY the search term (e.g., 'linkedin', '*.py', 'resume.pdf'). "
                "No explanation.\n\n"
                f"Question: {question[:300]}\n\n"
                "Search term:"
            )
            search_term = self.chat(
                extract_prompt, include_history=False, max_tokens=30, temperature=0.0
            ).strip().strip("'\"")

            if not search_term:
                return None

            logger.info(f"[FDA] Filesystem search for: {search_term}")

            # Search from the user's home directory with reasonable depth
            home = os.path.expanduser("~")
            found_files = []
            try:
                result = subprocess.run(
                    ["find", home, "-maxdepth", "5",
                     "-iname", f"*{search_term}*",
                     "-not", "-path", "*/.*",
                     "-not", "-path", "*/node_modules/*",
                     "-not", "-path", "*/__pycache__/*",
                     "-not", "-path", "*/.venv/*"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.stdout.strip():
                    found_files = result.stdout.strip().split("\n")
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"[FDA] find command failed: {e}")

            # Deduplicate while preserving order
            found_files = list(dict.fromkeys(found_files))

            if found_files:
                return {
                    "success": True,
                    "search_term": search_term,
                    "files_found": found_files[:20],
                    "count": len(found_files),
                    "summary": f"Found {len(found_files)} file(s) matching '{search_term}'",
                }

            # No files found - return None so the caller can try other approaches
            logger.info(f"[FDA] No files matching '{search_term}' found in filesystem")
            return None

        except Exception as e:
            logger.warning(f"[FDA] Direct filesystem search failed: {e}")
            return None

    def _delegate_to_executor(self, question: str) -> Optional[dict[str, Any]]:
        """Delegate an execution request to Executor."""
        logger.info(f"[FDA] Delegating to Executor: {question}")

        # Check if Executor is running
        executor_status = self.get_peer_status(Agents.EXECUTOR)
        if not executor_status or executor_status.get("status") != "running":
            logger.warning("[FDA] Executor is not running, cannot delegate")
            return None

        # Parse the question to extract command if possible
        # For now, just send the whole question as context
        # The Executor will interpret it

        # Don't auto-execute arbitrary commands - just return info about capability
        return {
            "info": "I can help execute commands, but I need your explicit confirmation first.",
            "question": question,
            "executor_available": True,
        }

    def _delegate_to_claude_code(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        allow_edits: bool = False,
        timeout: int = 300,
    ) -> Optional[dict[str, Any]]:
        """
        Delegate a coding task to Claude Code via Executor.

        This uses the user's Max subscription instead of API credits.

        Args:
            prompt: The coding task/question.
            cwd: Working directory for Claude Code.
            allow_edits: If True, allow Claude Code to edit files.
            timeout: Timeout in seconds (default 5 minutes).

        Returns:
            Claude Code result or None if Executor unavailable.
        """
        logger.info(f"[FDA] Delegating to Claude Code: {prompt[:80]}...")

        # Check if Executor is running
        executor_status = self.get_peer_status(Agents.EXECUTOR)
        if not executor_status or executor_status.get("status") != "running":
            logger.warning("[FDA] Executor is not running, cannot delegate to Claude Code")
            return None

        # Send request via message bus
        msg_id = self.message_bus.request_claude_code(
            from_agent=self.name.lower(),
            prompt=prompt,
            cwd=cwd,
            allow_edits=allow_edits,
            timeout=timeout,
            priority="high",
        )

        # Wait for response
        response = self.message_bus.wait_for_response(
            agent_name=self.name.lower(),
            request_id=msg_id,
            timeout_seconds=float(timeout + 30),  # Extra buffer for response
            poll_interval=1.0,
        )

        if response:
            self.message_bus.mark_read(response["id"])
            body = response.get("body", "{}")
            try:
                result = json.loads(body)
                return result.get("result")
            except (json.JSONDecodeError, TypeError):
                return {"output": body}

        return None

    def ask_claude_code(
        self,
        task: str,
        cwd: Optional[str] = None,
        allow_edits: bool = False,
    ) -> str:
        """
        Ask Claude Code to perform a coding task.

        This is a convenience method that delegates to Claude Code and
        formats the response nicely. Uses Max subscription credits.

        Args:
            task: The coding task/question.
            cwd: Working directory for Claude Code.
            allow_edits: If True, allow Claude Code to edit files.

        Returns:
            Formatted response from Claude Code.
        """
        result = self._delegate_to_claude_code(
            prompt=task,
            cwd=cwd,
            allow_edits=allow_edits,
        )

        if result is None:
            return "I couldn't reach Claude Code. Please make sure the Executor agent is running with `fda start --all`."

        if result.get("success"):
            output = result.get("output", "")
            return f"Claude Code completed the task:\n\n{output}"
        else:
            error = result.get("error", "Unknown error")
            output = result.get("output", "")
            response = f"Claude Code encountered an issue: {error}"
            if output:
                response += f"\n\nPartial output:\n{output}"
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
