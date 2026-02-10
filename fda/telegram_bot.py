"""
Telegram Bot integration for FDA system.

Provides a Telegram interface for querying the FDA agent and receiving
proactive notifications about project status, alerts, and updates.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.config import (
    TELEGRAM_BOT_TOKEN_ENV,
    MODEL_FDA,
)
from fda.state.project_state import ProjectState

logger = logging.getLogger(__name__)


TELEGRAM_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent), a personal AI assistant running on the user's computer, responding via Telegram.

You are NOT a project management tool. You are a general-purpose personal assistant that helps the user manage their daily work and life.

Your scope is the user's entire work environment:
- Their calendar and meetings
- Their tasks and to-do items
- Their notes and journal entries
- Anything they need help tracking or remembering

Keep responses concise for mobile reading:
- Short paragraphs
- Bullet points for lists
- Essential information only

Be warm, helpful, and conversational - like a skilled executive assistant who knows the user well.
"""


class TelegramBotAgent(BaseAgent):
    """
    Telegram Bot Agent for FDA system.

    Provides a Telegram interface for:
    - Answering project questions (/ask)
    - Showing project status (/status)
    - Listing tasks (/tasks)
    - Showing alerts (/alerts)
    - Receiving proactive notifications
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        project_state_path: Optional[Path] = None,
    ):
        """
        Initialize the Telegram Bot Agent.

        Args:
            bot_token: Telegram bot token. If not provided, reads from
                      TELEGRAM_BOT_TOKEN environment variable.
            project_state_path: Path to the project state database.
        """
        print("[TelegramBot] Initializing...", flush=True)
        print("[TelegramBot] Calling super().__init__...", flush=True)
        super().__init__(
            name="TelegramBot",
            model=MODEL_FDA,
            system_prompt=TELEGRAM_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )
        print("[TelegramBot] BaseAgent initialized", flush=True)

        self.bot_token = bot_token or os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
        if not self.bot_token:
            raise ValueError(
                f"Telegram bot token required. Set {TELEGRAM_BOT_TOKEN_ENV} "
                "environment variable or pass bot_token parameter."
            )
        print("[TelegramBot] Bot token configured", flush=True)

        self._application = None
        self._loop = None
        print("[TelegramBot] Initialization complete", flush=True)

    def _get_application(self) -> Any:
        """Get or create the Telegram application."""
        if self._application is not None:
            return self._application

        try:
            from telegram.ext import (
                Application,
                CommandHandler,
                MessageHandler,
                filters,
            )
            from telegram.request import HTTPXRequest
        except ImportError:
            raise ImportError(
                "python-telegram-bot is required for TelegramBotAgent. "
                "Install it with: pip install python-telegram-bot"
            )

        # Configure with longer timeouts and connection pool settings
        # to handle unstable connections
        request = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=20.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=10.0,
        )

        self._application = (
            Application.builder()
            .token(self.bot_token)
            .request(request)
            .get_updates_request(request)
            .build()
        )

        # Register command handlers
        self._application.add_handler(CommandHandler("start", self._handle_start))
        self._application.add_handler(CommandHandler("help", self._handle_help))
        self._application.add_handler(CommandHandler("ask", self._handle_ask))
        self._application.add_handler(CommandHandler("status", self._handle_status))
        self._application.add_handler(CommandHandler("tasks", self._handle_tasks))
        self._application.add_handler(CommandHandler("alerts", self._handle_alerts))
        self._application.add_handler(CommandHandler("stop", self._handle_stop))

        # Handle plain text messages as questions
        self._application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        # Add error handler
        self._application.add_error_handler(self._handle_error)

        return self._application

    async def _handle_error(self, update: Any, context: Any) -> None:
        """Handle errors in the bot."""
        logger.error(f"[TelegramBot] Error: {context.error}")

        # Check if it's a network error
        error_str = str(context.error).lower()
        if "network" in error_str or "disconnected" in error_str or "timeout" in error_str:
            logger.warning("[TelegramBot] Network error detected - will retry automatically")
        else:
            logger.exception(f"[TelegramBot] Unhandled exception: {context.error}")

    async def _handle_start(self, update: Any, context: Any) -> None:
        """Handle /start command - register user and start onboarding if needed."""
        user = update.effective_user
        chat_id = str(update.effective_chat.id)

        # Register user in database
        self.state.register_telegram_user(
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name,
        )

        # Check if user has been onboarded
        is_onboarded = self.state.get_context("onboarded")
        user_name = self.state.get_context("user_name")

        if is_onboarded and user_name:
            # Returning user
            welcome_message = f"""Welcome back, {user_name}!

I'm your personal AI assistant. Just send me a message with anything you need help with.

Quick commands:
- /status - See your tasks and schedule
- /tasks - List your current tasks
- /help - More commands

What can I help you with today?
"""
        else:
            # New user - start onboarding
            # Store that we're in onboarding mode for this chat
            context.user_data["onboarding_step"] = 1

            welcome_message = f"""Hi {user.first_name or 'there'}! I'm FDA, your personal AI assistant.

I'd love to get to know you so I can help you better. Let me ask you a few quick questions.

First: What should I call you?
"""

        await update.message.reply_text(welcome_message)
        logger.info(f"[TelegramBot] User registered: {chat_id} ({user.username}), onboarded: {is_onboarded}")

    async def _handle_help(self, update: Any, context: Any) -> None:
        """Handle /help command."""
        help_message = """I'm FDA, your personal AI assistant. Here's what I can do:

*Commands:*
/status - See your tasks and reminders
/tasks - List your current tasks
/alerts - Show pending reminders
/stop - Pause notifications

*Or just message me:*
- Ask questions about anything
- "Remind me to..." - I'll track it
- "What's on my plate?" - Quick overview
- "Help me with..." - I'll assist

What do you need?
"""
        await update.message.reply_text(help_message)

    async def _handle_ask(self, update: Any, context: Any) -> None:
        """Handle /ask command - answer project questions."""
        # Get question from command arguments
        question = " ".join(context.args) if context.args else None

        if not question:
            await update.message.reply_text(
                "What would you like to know? Example:\n/ask What should I focus on today?"
            )
            return

        await update.message.reply_text("Thinking...")

        try:
            # Get project context and answer
            response = self._answer_question(question)
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"[TelegramBot] Error answering question: {e}")
            await update.message.reply_text(
                "Sorry, I encountered an error processing your question."
            )

    async def _handle_message(self, update: Any, context: Any) -> None:
        """Handle plain text messages - either onboarding or questions."""
        message = update.message.text

        if not message:
            return

        # Check if we're in onboarding flow
        onboarding_step = context.user_data.get("onboarding_step", 0)

        if onboarding_step > 0:
            await self._handle_onboarding(update, context, message)
            return

        # Regular message - answer as a question
        await update.message.reply_text("Thinking...")

        try:
            response = self._answer_question(message)
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"[TelegramBot] Error answering message: {e}")
            await update.message.reply_text(
                "Sorry, I encountered an error processing your message."
            )

    async def _handle_onboarding(self, update: Any, context: Any, message: str) -> None:
        """Handle onboarding conversation flow."""
        step = context.user_data.get("onboarding_step", 1)
        chat_id = str(update.effective_chat.id)

        if step == 1:
            # Got the user's name
            context.user_data["user_name"] = message.strip()
            context.user_data["onboarding_step"] = 2

            await update.message.reply_text(
                f"Nice to meet you, {message.strip()}!\n\n"
                "What do you do? (e.g., 'software engineer', 'product manager', 'entrepreneur')"
            )

        elif step == 2:
            # Got the user's role
            context.user_data["user_role"] = message.strip()
            context.user_data["onboarding_step"] = 3

            await update.message.reply_text(
                "What are you working on right now? What are your main goals or priorities?"
            )

        elif step == 3:
            # Got the user's goals
            context.user_data["user_goals"] = message.strip()
            context.user_data["onboarding_step"] = 4

            await update.message.reply_text(
                "Last question: What's challenging or frustrating about your current workflow?"
            )

        elif step == 4:
            # Got challenges - now ask for timezone
            context.user_data["user_challenges"] = message.strip()
            context.user_data["onboarding_step"] = 5

            await update.message.reply_text(
                "Almost done! What's your timezone?\n\n"
                "Examples: America/New_York, Europe/London, Asia/Tokyo, UTC\n"
                "(Or just type 'skip' to use system default)"
            )

        elif step == 5:
            # Got timezone - complete onboarding
            tz_input = message.strip()
            user_timezone = None

            if tz_input.lower() != "skip" and tz_input:
                # Validate the timezone
                from fda.utils.timezone import validate_timezone
                validated_tz = validate_timezone(tz_input)
                if validated_tz:
                    user_timezone = validated_tz
                else:
                    # Invalid timezone, but continue anyway
                    user_timezone = None

            context.user_data["user_timezone"] = user_timezone

            # Save all the onboarding data
            user_name = context.user_data.get("user_name", "")
            user_role = context.user_data.get("user_role", "")
            user_goals = context.user_data.get("user_goals", "")
            user_challenges = message.strip()

            user_timezone = context.user_data.get("user_timezone")

            self.state.set_context("user_name", user_name)
            self.state.set_context("user_role", user_role)
            self.state.set_context("user_goals", user_goals)
            self.state.set_context("user_challenges", user_challenges)
            self.state.set_context("user_timezone", user_timezone)
            self.state.set_context("onboarded", True)
            self.state.set_context("onboarded_at", datetime.now().isoformat())
            self.state.set_context("onboarded_via", "telegram")

            # Log to journal
            journal_content = f"""# First Meeting with {user_name} (via Telegram)

**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

## About {user_name}
- **Role:** {user_role}
- **Goals:** {user_goals}
- **Challenges:** {user_challenges}
- **Timezone:** {user_timezone or 'System default'}
"""
            self.log_to_journal(
                summary=f"First meeting with {user_name} - Onboarding via Telegram",
                content=journal_content,
                tags=["onboarding", "first-meeting", "telegram"],
                relevance_decay="slow",
            )

            # Clear onboarding state
            context.user_data["onboarding_step"] = 0

            # Generate personalized welcome using Claude
            try:
                synthesis_prompt = f"""The user just completed onboarding via Telegram. Create a brief, warm welcome that:
1. Acknowledges what they shared
2. Suggests 1-2 specific ways you can help based on their goals/challenges
3. Asks what they'd like to tackle first

User info:
- Name: {user_name}
- Role: {user_role}
- Goals: {user_goals}
- Challenges: {user_challenges}

Keep it conversational and under 150 words. Don't use excessive formatting."""

                welcome = self.chat(synthesis_prompt, include_history=False)
                await update.message.reply_text(welcome)
            except Exception as e:
                logger.error(f"[TelegramBot] Error generating welcome: {e}")
                await update.message.reply_text(
                    f"Thanks, {user_name}! I've got everything I need to help you.\n\n"
                    "Just message me anytime with questions, tasks to track, or anything you need help with!"
                )

            logger.info(f"[TelegramBot] Onboarding completed for {chat_id}: {user_name}")

    def _answer_question(self, question: str) -> str:
        """Answer a question using FDA agent capabilities."""
        # Build context with user info
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

        # Add task/project context
        project_context = self.get_project_context()
        context.update(project_context)

        # Search journal for relevant entries
        relevant_entries = self.search_journal(question, top_n=3)
        if relevant_entries:
            context["relevant_notes"] = [
                {
                    "summary": e.get("summary"),
                    "date": e.get("created_at", "")[:10],
                }
                for e in relevant_entries
            ]

        return self.chat_with_context(question, context)

    async def _handle_status(self, update: Any, context: Any) -> None:
        """Handle /status command - show user's current status."""
        try:
            user_name = self.state.get_context("user_name") or "there"
            tasks = self.state.get_tasks()
            alerts = self.state.get_alerts(acknowledged=False)

            # Count tasks by status
            status_counts = {}
            for task in tasks:
                status = task.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            # Build status message
            status_parts = []
            status_parts.append(f"Hey {user_name}, here's your status:\n")

            # Task summary
            if tasks:
                status_parts.append(f"*Tasks:* {len(tasks)} total")
                if status_counts:
                    for status, count in sorted(status_counts.items()):
                        emoji = {
                            "pending": "⏳",
                            "in_progress": "🔄",
                            "completed": "✅",
                            "blocked": "🚫",
                        }.get(status, "•")
                        status_parts.append(f"  {emoji} {status}: {count}")
            else:
                status_parts.append("*Tasks:* None tracked yet")

            # Alerts/Reminders
            if alerts:
                critical = [a for a in alerts if a.get("level") == "critical"]
                warnings = [a for a in alerts if a.get("level") == "warning"]

                status_parts.append(f"\n*Reminders:* {len(alerts)}")
                if critical:
                    status_parts.append(f"  🔴 Urgent: {len(critical)}")
                if warnings:
                    status_parts.append(f"  🟡 Normal: {len(warnings)}")

            status_parts.append("\nAnything you need help with?")

            await update.message.reply_text("\n".join(status_parts))

        except Exception as e:
            logger.error(f"[TelegramBot] Error getting status: {e}")
            await update.message.reply_text("Sorry, I had trouble getting your status.")

    async def _handle_tasks(self, update: Any, context: Any) -> None:
        """Handle /tasks command - list tasks."""
        try:
            # Check for status filter
            status_filter = None
            if context.args:
                status_filter = context.args[0].lower()

            tasks = self.state.get_tasks(status=status_filter)

            if not tasks:
                msg = "No tasks found"
                if status_filter:
                    msg += f" with status '{status_filter}'"
                await update.message.reply_text(msg + ".")
                return

            # Build task list
            task_lines = [f"*Tasks* ({len(tasks)} total)\n"]

            for task in tasks[:10]:  # Limit to 10 tasks
                emoji = {
                    "pending": "⏳",
                    "in_progress": "🔄",
                    "completed": "✅",
                    "blocked": "🚫",
                }.get(task.get("status", ""), "•")

                priority_marker = ""
                if task.get("priority") == "high":
                    priority_marker = " ‼️"

                task_lines.append(
                    f"{emoji} {task.get('title', 'Untitled')}{priority_marker}"
                )
                if task.get("owner"):
                    task_lines.append(f"   Owner: {task.get('owner')}")

            if len(tasks) > 10:
                task_lines.append(f"\n... and {len(tasks) - 10} more")

            await update.message.reply_text("\n".join(task_lines))

        except Exception as e:
            logger.error(f"[TelegramBot] Error listing tasks: {e}")
            await update.message.reply_text("Error retrieving tasks.")

    async def _handle_alerts(self, update: Any, context: Any) -> None:
        """Handle /alerts command - show alerts."""
        try:
            alerts = self.state.get_alerts(acknowledged=False)

            if not alerts:
                await update.message.reply_text("No unacknowledged alerts.")
                return

            alert_lines = [f"*Alerts* ({len(alerts)} unacknowledged)\n"]

            for alert in alerts[:10]:
                emoji = {
                    "critical": "🔴",
                    "warning": "🟡",
                    "info": "🔵",
                }.get(alert.get("level", ""), "•")

                alert_lines.append(f"{emoji} [{alert.get('level')}] {alert.get('message')}")
                alert_lines.append(f"   Source: {alert.get('source')} | {alert.get('created_at', '')[:10]}")

            if len(alerts) > 10:
                alert_lines.append(f"\n... and {len(alerts) - 10} more")

            await update.message.reply_text("\n".join(alert_lines))

        except Exception as e:
            logger.error(f"[TelegramBot] Error listing alerts: {e}")
            await update.message.reply_text("Error retrieving alerts.")

    async def _handle_stop(self, update: Any, context: Any) -> None:
        """Handle /stop command - deactivate user."""
        chat_id = str(update.effective_chat.id)
        self.state.deactivate_telegram_user(chat_id)
        await update.message.reply_text(
            "You've been unsubscribed from notifications. "
            "Send /start to resubscribe."
        )
        logger.info(f"[TelegramBot] User deactivated: {chat_id}")

    def run_event_loop(self) -> None:
        """
        Run the Telegram bot event loop.

        This starts the bot and processes incoming messages.
        """
        import sys

        print("[TelegramBot] Building application...", flush=True)
        logger.info("[TelegramBot] Starting event loop...")

        app = self._get_application()

        print("[TelegramBot] Application built. Starting polling...", flush=True)
        print("[TelegramBot] Connecting to Telegram API...", flush=True)

        # Run the bot with robust polling configuration
        try:
            app.run_polling(
                allowed_updates=["message"],
                drop_pending_updates=True,  # Ignore old messages on startup
                poll_interval=1.0,  # Poll every second
                timeout=30,  # Long polling timeout
            )
        except KeyboardInterrupt:
            logger.info("[TelegramBot] Received shutdown signal")
            print("\n[TelegramBot] Received shutdown signal", flush=True)
        except Exception as e:
            logger.error(f"[TelegramBot] Error in event loop: {e}")
            print(f"[TelegramBot] Error: {e}", file=sys.stderr, flush=True)

        logger.info("[TelegramBot] Event loop stopped")
        print("[TelegramBot] Event loop stopped", flush=True)

    async def send_message(self, chat_id: str, text: str) -> bool:
        """
        Send a message to a specific Telegram chat.

        Args:
            chat_id: Telegram chat ID.
            text: Message text.

        Returns:
            True if message sent successfully, False otherwise.
        """
        try:
            app = self._get_application()
            await app.bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as e:
            logger.error(f"[TelegramBot] Failed to send message to {chat_id}: {e}")
            return False

    async def broadcast_message(self, text: str) -> int:
        """
        Broadcast a message to all active users.

        Args:
            text: Message text.

        Returns:
            Number of users messaged successfully.
        """
        users = self.state.get_telegram_users(active_only=True)
        success_count = 0

        for user in users:
            if await self.send_message(user["chat_id"], text):
                success_count += 1

        logger.info(f"[TelegramBot] Broadcast sent to {success_count}/{len(users)} users")
        return success_count

    async def send_alert_notification(self, alert: dict[str, Any]) -> int:
        """
        Send an alert notification to all active users.

        Args:
            alert: Alert dictionary from state.

        Returns:
            Number of users notified.
        """
        emoji = {
            "critical": "🔴",
            "warning": "🟡",
            "info": "🔵",
        }.get(alert.get("level", ""), "•")

        message = f"{emoji} *FDA Alert*\n\n"
        message += f"Level: {alert.get('level', 'unknown').upper()}\n"
        message += f"Message: {alert.get('message')}\n"
        message += f"Source: {alert.get('source')}"

        return await self.broadcast_message(message)


def get_bot_token() -> Optional[str]:
    """Get Telegram bot token from environment or stored config."""
    # First try environment variable
    token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
    if token:
        return token

    # Try stored config
    try:
        state = ProjectState()
        token = state.get_context("telegram_bot_token")
        if token:
            return token
    except Exception:
        pass

    return None


def setup_bot_token(token: str) -> None:
    """Store Telegram bot token in project state."""
    state = ProjectState()
    state.set_context("telegram_bot_token", token)
    logger.info("[TelegramBot] Bot token stored in project state")
