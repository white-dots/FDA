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


TELEGRAM_SYSTEM_PROMPT = """You are the FDA (Facilitating Director Agent) responding via Telegram.

Keep responses concise and well-formatted for mobile reading:
- Use short paragraphs
- Use bullet points for lists
- Limit responses to essential information
- Use emojis sparingly for visual clarity

You have access to project tasks, alerts, decisions, and historical journal entries.
Answer questions helpfully and proactively flag important issues.
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
        super().__init__(
            name="TelegramBot",
            model=MODEL_FDA,
            system_prompt=TELEGRAM_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

        self.bot_token = bot_token or os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
        if not self.bot_token:
            raise ValueError(
                f"Telegram bot token required. Set {TELEGRAM_BOT_TOKEN_ENV} "
                "environment variable or pass bot_token parameter."
            )

        self._application = None
        self._loop = None

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
        except ImportError:
            raise ImportError(
                "python-telegram-bot is required for TelegramBotAgent. "
                "Install it with: pip install python-telegram-bot"
            )

        self._application = (
            Application.builder()
            .token(self.bot_token)
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

        return self._application

    async def _handle_start(self, update: Any, context: Any) -> None:
        """Handle /start command - register user."""
        user = update.effective_user
        chat_id = str(update.effective_chat.id)

        # Register user in database
        self.state.register_telegram_user(
            chat_id=chat_id,
            username=user.username,
            first_name=user.first_name,
        )

        welcome_message = f"""Welcome to FDA Project Assistant, {user.first_name or 'there'}!

I can help you with:
- /ask <question> - Ask about the project
- /status - Get project status summary
- /tasks - List current tasks
- /alerts - Show unacknowledged alerts
- /stop - Stop receiving notifications

You can also just send me a message and I'll try to help!
"""
        await update.message.reply_text(welcome_message)
        logger.info(f"[TelegramBot] User registered: {chat_id} ({user.username})")

    async def _handle_help(self, update: Any, context: Any) -> None:
        """Handle /help command."""
        help_message = """FDA Project Assistant Commands:

/ask <question> - Ask any question about the project
/status - Get current project status
/tasks - List tasks (add 'blocked' or 'pending' to filter)
/alerts - Show unacknowledged alerts
/stop - Stop receiving notifications

Just send a message to ask a question directly!
"""
        await update.message.reply_text(help_message)

    async def _handle_ask(self, update: Any, context: Any) -> None:
        """Handle /ask command - answer project questions."""
        # Get question from command arguments
        question = " ".join(context.args) if context.args else None

        if not question:
            await update.message.reply_text(
                "Please provide a question. Example:\n/ask What are our current blockers?"
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
        """Handle plain text messages as questions."""
        question = update.message.text

        if not question:
            return

        await update.message.reply_text("Thinking...")

        try:
            response = self._answer_question(question)
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"[TelegramBot] Error answering message: {e}")
            await update.message.reply_text(
                "Sorry, I encountered an error processing your message."
            )

    def _answer_question(self, question: str) -> str:
        """Answer a question using FDA agent capabilities."""
        # Get project context
        context = self.get_project_context()

        # Search journal for relevant entries
        relevant_entries = self.search_journal(question, top_n=3)
        if relevant_entries:
            context["relevant_history"] = [
                {
                    "summary": e.get("summary"),
                    "author": e.get("author"),
                    "date": e.get("created_at", "")[:10],
                }
                for e in relevant_entries
            ]

        # Get recent decisions
        decisions = self.state.get_decisions(limit=3)
        if decisions:
            context["recent_decisions"] = [
                {"title": d.get("title"), "date": d.get("created_at", "")[:10]}
                for d in decisions
            ]

        return self.chat_with_context(question, context)

    async def _handle_status(self, update: Any, context: Any) -> None:
        """Handle /status command - show project status."""
        try:
            tasks = self.state.get_tasks()
            alerts = self.state.get_alerts(acknowledged=False)

            # Count tasks by status
            status_counts = {}
            for task in tasks:
                status = task.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            # Build status message
            status_parts = []

            # Task summary
            status_parts.append("*Project Status*\n")
            status_parts.append(f"Total tasks: {len(tasks)}")

            if status_counts:
                for status, count in sorted(status_counts.items()):
                    emoji = {
                        "pending": "⏳",
                        "in_progress": "🔄",
                        "completed": "✅",
                        "blocked": "🚫",
                    }.get(status, "•")
                    status_parts.append(f"  {emoji} {status}: {count}")

            # Alerts
            if alerts:
                critical = [a for a in alerts if a.get("level") == "critical"]
                warnings = [a for a in alerts if a.get("level") == "warning"]

                status_parts.append(f"\n*Alerts*: {len(alerts)} unacknowledged")
                if critical:
                    status_parts.append(f"  🔴 Critical: {len(critical)}")
                if warnings:
                    status_parts.append(f"  🟡 Warnings: {len(warnings)}")

            await update.message.reply_text("\n".join(status_parts))

        except Exception as e:
            logger.error(f"[TelegramBot] Error getting status: {e}")
            await update.message.reply_text("Error retrieving project status.")

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
        logger.info("[TelegramBot] Starting event loop...")

        app = self._get_application()

        # Run the bot
        try:
            app.run_polling(allowed_updates=["message"])
        except KeyboardInterrupt:
            logger.info("[TelegramBot] Received shutdown signal")
        except Exception as e:
            logger.error(f"[TelegramBot] Error in event loop: {e}")

        logger.info("[TelegramBot] Event loop stopped")

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
