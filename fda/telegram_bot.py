"""
Telegram Bot integration for FDA system.

Provides a Telegram interface for querying the FDA agent and receiving
proactive notifications about project status, alerts, and updates.
"""

import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timedelta

from fda.base_agent import BaseAgent
from fda.config import (
    TELEGRAM_BOT_TOKEN_ENV,
    MODEL_FDA,
    ENABLE_EXTENDED_THINKING,
    EXTENDED_THINKING_BUDGET,
    MAX_IMAGE_UPLOAD_MB,
    MAX_DOCUMENT_UPLOAD_MB,
    SUPPORTED_IMAGE_TYPES,
    SUPPORTED_DOC_TYPES,
    SUPPORTED_TEXT_EXTENSIONS,
    HISTORY_MESSAGE_LIMIT,
    HISTORY_CHAR_LIMIT,
    HISTORY_HOURS_CUTOFF,
)
from fda.state.project_state import ProjectState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent detection — keyword-based routing for selective context loading
# ---------------------------------------------------------------------------

INTENT_PATTERNS: dict[str, list[str]] = {
    "kakao": ["kakao", "카카오", "chat room", "client said", "client message", "채팅"],
    "tasks": ["task", "to-do", "todo", "work on", "backlog", "blocked", "할일", "작업"],
    "calendar": ["calendar", "meeting", "schedule", "agenda", "일정", "미팅", "회의"],
    "journal": ["journal", "note", "wrote", "logged", "recorded", "일지", "노트"],
    "alerts": ["alert", "critical", "warning", "urgent", "알림"],
}


def _detect_intents(question: str) -> list[str]:
    """Detect which data sources a question needs, using keyword matching.

    Returns a list of intent keys (e.g. ["kakao", "tasks"]).
    Falls back to ["general"] when nothing specific matches.
    """
    q_lower = question.lower()
    matched = [
        intent
        for intent, keywords in INTENT_PATTERNS.items()
        if any(kw in q_lower for kw in keywords)
    ]
    return matched or ["general"]


TELEGRAM_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent), a personal AI assistant running on the user's computer, responding via Telegram.

You are NOT a project management tool. You are a general-purpose personal assistant that helps the user manage their daily work and life.

Your scope is the user's entire work environment:
- Their calendar and meetings
- Their tasks and to-do items
- Their notes and journal entries
- Client VMs and deployed services (via SSH)
- Local projects on the Mac Mini filesystem
- Anything they need help tracking or remembering

TOOL STRATEGY — choose the right tool for the job:
- *run_local_command*: For quick shell commands on this Mac Mini (ls, cat, grep, find, ps, etc.). Fast and direct. Use this FIRST for most local queries — listing directories, reading files, searching code, checking processes. ALWAYS prefer this over run_local_task for simple questions.
- *run_local_task*: For complex local code changes requiring full analysis pipeline. ONLY use when you need to modify code, not just read or query.
- *run_remote_command*: For quick shell commands on client VMs (ls, cat, grep, airflow CLI, systemctl, etc.). Fast and direct. Use this FIRST for most VM queries.
- *run_remote_task*: For complex code analysis on remote VMs requiring reading + understanding multiple source files. Only use when you need to analyze/modify code, not just check status.
- *search_journal*: For recalling past work. All remote/local worker results are journaled — investigations, code changes, deployments, errors. Ask the journal before re-running expensive tasks.

When investigating a problem, prefer multiple small command calls (local or remote) over the heavy task pipeline. You can run ls, cat, grep, tail logs, check service status — just like working in a terminal. Only escalate to run_*_task when you actually need code analysis or modifications.

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
        local_task_dispatch: Optional[Any] = None,
        remote_task_dispatch: Optional[Any] = None,
        local_command_dispatch: Optional[Any] = None,
        remote_command_dispatch: Optional[Any] = None,
        local_organize_dispatch: Optional[Any] = None,
    ):
        """
        Initialize the Telegram Bot Agent.

        Args:
            bot_token: Telegram bot token. If not provided, reads from
                      TELEGRAM_BOT_TOKEN environment variable.
            project_state_path: Path to the project state database.
            local_task_dispatch: Optional callback(task_brief, project_path, progress_callback) -> result dict.
            remote_task_dispatch: Optional callback(task_brief, client_id, progress_callback) -> result dict.
            local_command_dispatch: Optional callback(command, cwd) -> result dict.
            remote_command_dispatch: Optional callback(command, client_id, cwd) -> result dict.
            local_organize_dispatch: Optional callback(target_path, instructions, progress_callback) -> result dict.
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
        self._local_task_dispatch = local_task_dispatch
        self._remote_task_dispatch = remote_task_dispatch
        self._local_command_dispatch = local_command_dispatch
        self._remote_command_dispatch = remote_command_dispatch
        self._local_organize_dispatch = local_organize_dispatch
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
        self._application.add_handler(CommandHandler("organize", self._handle_organize))

        # Handle plain text messages, photos, and documents as questions
        self._application.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
                self._handle_message,
            )
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

*Code Changes:*
/local <task> - Analyze & fix local codebase (FDA, etc.)
/pending - See pending approvals
/approve <id> - Approve a code change
/reject <id> - Reject a code change
/details <id> - View full diff

*Or just message me:*
- Ask questions about anything
- "Remind me to..." - I'll track it
- "What's on my plate?" - Quick overview
- "Help me with..." - I'll assist

What do you need?
"""
        await update.message.reply_text(help_message)

    async def _extract_telegram_attachments(self, update: Any, context: Any) -> list[dict]:
        """Download Telegram photo/document attachments and build Anthropic content blocks."""
        content_blocks: list[dict] = []

        # Photos
        if update.message.photo:
            try:
                photo = update.message.photo[-1]  # highest resolution
                if not photo.file_size or photo.file_size < MAX_IMAGE_UPLOAD_MB * 1024 * 1024:
                    file = await context.bot.get_file(photo.file_id)
                    data = await file.download_as_bytearray()
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(bytes(data)).decode(),
                        },
                    })
            except Exception as e:
                logger.warning(f"[TelegramBot] Failed to download photo: {e}")

        # Documents (PDFs, images sent as files, text files)
        if update.message.document:
            doc = update.message.document
            try:
                mime = doc.mime_type or ""
                filename = doc.file_name or ""
                ext = Path(filename).suffix.lower()
                file_size = doc.file_size or 0

                if mime in SUPPORTED_IMAGE_TYPES:
                    if file_size < MAX_IMAGE_UPLOAD_MB * 1024 * 1024:
                        file = await context.bot.get_file(doc.file_id)
                        data = await file.download_as_bytearray()
                        content_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": base64.b64encode(bytes(data)).decode(),
                            },
                        })
                elif mime in SUPPORTED_DOC_TYPES:
                    if file_size < MAX_DOCUMENT_UPLOAD_MB * 1024 * 1024:
                        file = await context.bot.get_file(doc.file_id)
                        data = await file.download_as_bytearray()
                        content_blocks.append({
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": base64.b64encode(bytes(data)).decode(),
                            },
                        })
                elif ext in SUPPORTED_TEXT_EXTENSIONS:
                    if file_size < 1 * 1024 * 1024:
                        file = await context.bot.get_file(doc.file_id)
                        data = await file.download_as_bytearray()
                        text = bytes(data).decode("utf-8", errors="replace")[:8000]
                        content_blocks.append({
                            "type": "text",
                            "text": f"[File: {filename}]\n```\n{text}\n```",
                        })
            except Exception as e:
                logger.warning(f"[TelegramBot] Failed to download document: {e}")

        return content_blocks

    async def _handle_ask(self, update: Any, context: Any) -> None:
        """Handle /ask command - answer project questions."""
        # Get question from command arguments
        question = " ".join(context.args) if context.args else None

        if not question:
            await update.message.reply_text(
                "What would you like to know? Example:\n/ask What should I focus on today?"
            )
            return

        chat_id = str(update.effective_chat.id)
        username = update.effective_user.first_name if update.effective_user else None
        await update.message.reply_text("Thinking...")

        try:
            # Save user message to state DB
            self.state.add_conversation_message(chat_id, "user", question, source="telegram", username=username)

            response = self._answer_question(question, chat_id=chat_id)

            # Save assistant response to state DB
            self.state.add_conversation_message(chat_id, "assistant", response, source="telegram")

            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"[TelegramBot] Error answering question: {e}")
            await update.message.reply_text(
                "Sorry, I encountered an error processing your question."
            )

    async def _handle_message(self, update: Any, context: Any) -> None:
        """Handle text messages, photos, and documents — either onboarding or questions."""
        # Get text from message body or caption (for photos with captions)
        message = update.message.text or update.message.caption or ""

        # Check if we're in onboarding flow (text-only)
        onboarding_step = context.user_data.get("onboarding_step", 0)
        if onboarding_step > 0 and message:
            await self._handle_onboarding(update, context, message)
            return

        # Extract image/file attachments
        attachment_blocks = await self._extract_telegram_attachments(update, context)

        # Must have either text or attachments
        if not message and not attachment_blocks:
            return

        # Default question if only an image was sent
        if not message and attachment_blocks:
            message = "What do you see in this image?"

        # Regular message - answer as a question
        chat_id = str(update.effective_chat.id)
        username = update.effective_user.first_name if update.effective_user else None
        await update.message.reply_text("Thinking...")

        try:
            # Save user message to state DB
            self.state.add_conversation_message(chat_id, "user", message, source="telegram", username=username)

            response = self._answer_question(
                message, chat_id=chat_id, attachment_blocks=attachment_blocks,
            )

            # Save assistant response to state DB
            self.state.add_conversation_message(chat_id, "assistant", response, source="telegram")

            # Telegram limit: 4096 chars per message
            if len(response) > 4000:
                for i in range(0, len(response), 4000):
                    await update.message.reply_text(response[i:i + 4000])
            else:
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

    # ------------------------------------------------------------------
    # KakaoTalk export helpers
    # ------------------------------------------------------------------

    _KAKAO_EXPORT_DIR = Path.home() / "Documents" / "fda-exports" / "kakaotalk"

    def _get_kakao_messages(self, question: str) -> str:
        """Read KakaoTalk export and return messages filtered by date.

        Parses date hints from the question (e.g. "yesterday", "last week",
        a specific date) and returns matching messages formatted as text.
        """
        from fda.kakaotalk.parser import KakaoTalkParser

        # Find the latest export file
        export_dir = self._KAKAO_EXPORT_DIR
        if not export_dir.exists():
            return "(No KakaoTalk exports found)"

        candidates = list(export_dir.glob("*.csv")) + list(export_dir.glob("*.txt"))
        if not candidates:
            return "(No KakaoTalk export files found)"

        latest = max(candidates, key=lambda p: p.stat().st_mtime)

        # Determine date range from the question
        since = self._parse_date_hint(question)

        parser = KakaoTalkParser()
        if since:
            messages = parser.parse_and_diff(latest, since)
        else:
            # Default: last 24 hours
            messages = parser.parse_and_diff(latest, datetime.now() - timedelta(days=1))

        if not messages:
            date_str = since.strftime("%Y-%m-%d") if since else "last 24 hours"
            return f"(No KakaoTalk messages found since {date_str})"

        # Format messages for context (cap at 50 to keep prompt reasonable)
        lines = []
        for msg in messages[-50:]:
            ts = msg.timestamp.strftime("%m/%d %H:%M")
            lines.append(f"[{ts}] {msg.sender}: {msg.text}")

        header = f"KakaoTalk messages ({len(messages)} total, showing last {len(lines)}):"
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def _parse_date_hint(question: str) -> Optional[datetime]:
        """Extract a date reference from a question.

        Handles: "yesterday", "today", "last N days", "this week",
        "YYYY-MM-DD", "MM/DD", Korean date references.
        """
        q = question.lower()
        now = datetime.now()

        if "yesterday" in q or "어제" in q:
            return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
        if "today" in q or "오늘" in q:
            return now.replace(hour=0, minute=0, second=0)
        if "this week" in q or "이번 주" in q or "이번주" in q:
            return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
        if "last week" in q or "지난 주" in q or "지난주" in q:
            return (now - timedelta(days=now.weekday() + 7)).replace(hour=0, minute=0, second=0)

        # "last N days"
        m = re.search(r"last\s+(\d+)\s+days?", q)
        if m:
            return (now - timedelta(days=int(m.group(1)))).replace(hour=0, minute=0, second=0)

        # Explicit date: YYYY-MM-DD
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", q)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

        return None

    # ------------------------------------------------------------------
    # Agentic tool-use (API backend)
    # ------------------------------------------------------------------

    # Tool schemas for the Anthropic API tool-use loop
    _FDA_TOOLS: list[dict[str, Any]] = [
        {
            "name": "search_journal",
            "description": "Search the user's journal and notes for relevant entries. Use when the user asks about past notes, decisions, meetings, or anything previously recorded.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of results to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_kakao_chat",
            "description": "Read KakaoTalk chat messages. Use when the user asks about client chats, KakaoTalk messages, or what was discussed in a chat room.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_hint": {
                        "type": "string",
                        "description": "Date reference like 'yesterday', 'today', 'last 3 days', '2026-02-20', or Korean equivalents like '어제', '오늘'. Defaults to last 24 hours.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_tasks",
            "description": "Get the user's task list. Use when the user asks about their tasks, to-dos, or what they need to work on.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: 'pending', 'in_progress', 'completed', 'blocked'. Omit for all tasks.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_alerts",
            "description": "Get unacknowledged alerts and reminders. Use when the user asks about alerts, warnings, or urgent items.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "get_calendar_events",
            "description": "Get calendar events for a date. Use when the user asks about their schedule, meetings, or calendar.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format or 'today'/'tomorrow'. Defaults to today.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "run_local_command",
            "description": (
                "Execute a shell command on the local Mac Mini. "
                "Use this for quick queries like listing files/directories, "
                "searching code, checking processes, reading files, etc. "
                "(e.g. 'ls ~/Documents', 'grep -r pattern dir/', "
                "'cat file.py', 'find . -name \"*.py\"', 'ps aux | grep fda'). "
                "PREFER THIS over run_local_task when you just need command "
                "output — not full code analysis/changes. Returns stdout/stderr directly."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute locally.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory. Defaults to ~/Documents.",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": "run_local_task",
            "description": (
                "Dispatch a complex task to the local worker agent for full "
                "code analysis and changes on the Mac Mini filesystem. "
                "Use ONLY when you need code modifications, not for simple "
                "queries — use run_local_command for those instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Natural language description of the task to perform locally.",
                    },
                    "project_path": {
                        "type": "string",
                        "description": "Optional local project directory path. If omitted, uses the default project.",
                    },
                },
                "required": ["task"],
            },
        },
        {
            "name": "run_remote_command",
            "description": (
                "Execute a shell command on a client's remote VM via SSH. "
                "Use this for quick queries like listing files, checking service "
                "status, running CLI commands (e.g. 'ls ~/airflow/dags', "
                "'airflow dags list', 'systemctl status nginx', 'cat /etc/cron.d/airflow'). "
                "Prefer this over run_remote_task when you just need command output, "
                "not full code analysis. Returns stdout/stderr directly."
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
                        "description": "Working directory for the command. Defaults to the client's repo path.",
                    },
                    "client_id": {
                        "type": "string",
                        "description": "Optional client identifier (e.g., 'aonebnh'). If omitted, uses the default client.",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": "run_remote_task",
            "description": (
                "Dispatch a complex task to the remote worker agent that SSHes into "
                "client VMs for full code analysis and changes. Use ONLY when you "
                "need code modifications or deep multi-file analysis, not for simple "
                "queries — use run_remote_command for those instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Natural language description of the task to perform on the remote VM.",
                    },
                    "client_id": {
                        "type": "string",
                        "description": "Optional client identifier (e.g., 'aonebnh'). If omitted, uses the default client.",
                    },
                },
                "required": ["task"],
            },
        },
    ]

    def _execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute an FDA tool and return the result as a string."""
        try:
            if tool_name == "search_journal":
                query = tool_input.get("query", "")
                top_n = tool_input.get("top_n", 5)
                entries = self.journal_retriever.retrieve_with_content(
                    query_text=query, top_n=top_n
                )
                if not entries:
                    return "No journal entries found."
                results = []
                for e in entries:
                    results.append({
                        "summary": e.get("summary", ""),
                        "date": e.get("created_at", "")[:10],
                        "tags": e.get("tags", []),
                        "content": (e.get("content") or "")[:500],
                    })
                return json.dumps(results, ensure_ascii=False, default=str)

            elif tool_name == "read_kakao_chat":
                date_hint = tool_input.get("date_hint", "")
                # Reuse existing helper — it parses date hints and reads exports
                return self._get_kakao_messages(date_hint or "last 24 hours")

            elif tool_name == "get_tasks":
                status = tool_input.get("status")
                tasks = self.state.get_tasks(status=status)
                if not tasks:
                    return "No tasks found." if not status else f"No tasks with status '{status}'."
                results = [
                    {
                        "id": t.get("id"),
                        "title": t.get("title"),
                        "status": t.get("status"),
                        "priority": t.get("priority"),
                        "owner": t.get("owner"),
                        "description": (t.get("description") or "")[:200],
                    }
                    for t in tasks[:15]
                ]
                return json.dumps(results, ensure_ascii=False, default=str)

            elif tool_name == "get_alerts":
                alerts = self.state.get_alerts(acknowledged=False)
                if not alerts:
                    return "No unacknowledged alerts."
                results = [
                    {
                        "level": a.get("level"),
                        "message": a.get("message"),
                        "source": a.get("source"),
                        "created_at": a.get("created_at", "")[:16],
                    }
                    for a in alerts
                ]
                return json.dumps(results, ensure_ascii=False, default=str)

            elif tool_name == "get_calendar_events":
                date_str = tool_input.get("date", "today")
                try:
                    from fda.outlook import OutlookCalendar
                    cal = OutlookCalendar()
                    if not cal.access_token:
                        return "Calendar not connected. User needs to set up Outlook Calendar first."
                    # Parse date
                    if date_str == "today":
                        target = datetime.now().date()
                    elif date_str == "tomorrow":
                        target = (datetime.now() + timedelta(days=1)).date()
                    else:
                        target = datetime.strptime(date_str, "%Y-%m-%d").date()
                    start = datetime.combine(target, datetime.min.time())
                    end = datetime.combine(target, datetime.max.time())
                    events = cal.get_events_range(start=start, end=end)
                    if not events:
                        return f"No calendar events on {target.isoformat()}."
                    results = [
                        {
                            "subject": ev.get("subject"),
                            "start": ev.get("start", {}).get("dateTime", "")[:16],
                            "end": ev.get("end", {}).get("dateTime", "")[:16],
                            "location": ev.get("location", {}).get("displayName", ""),
                        }
                        for ev in events
                    ]
                    return json.dumps(results, ensure_ascii=False, default=str)
                except ImportError:
                    return "Calendar module not available."
                except Exception as e:
                    return f"Calendar error: {e}"

            elif tool_name == "run_local_command":
                command = tool_input.get("command", "")
                cwd = tool_input.get("cwd")

                if not self._local_command_dispatch:
                    return "Local command dispatch not available."
                try:
                    result = self._local_command_dispatch(command, cwd=cwd)
                    if result.get("success"):
                        output = result.get("stdout", "")
                        stderr = result.get("stderr", "")
                        parts = []
                        if output:
                            parts.append(output[:3000])
                        if stderr:
                            parts.append(f"STDERR:\n{stderr[:1000]}")
                        return "\n".join(parts) if parts else "(no output)"
                    else:
                        return f"Command failed: {result.get('error', 'unknown')}"
                except Exception as e:
                    return f"Error running local command: {e}"

            elif tool_name == "run_remote_command":
                command = tool_input.get("command", "")
                client_id = tool_input.get("client_id")
                cwd = tool_input.get("cwd")

                if not self._remote_command_dispatch:
                    return "Remote command dispatch not available."
                try:
                    result = self._remote_command_dispatch(
                        command, client_id=client_id, cwd=cwd,
                    )
                    if result.get("success"):
                        output = result.get("stdout", "")
                        stderr = result.get("stderr", "")
                        parts = []
                        if output:
                            parts.append(output[:3000])
                        if stderr:
                            parts.append(f"STDERR:\n{stderr[:1000]}")
                        return "\n".join(parts) if parts else "(no output)"
                    else:
                        return f"Command failed: {result.get('error', 'unknown')}"
                except Exception as e:
                    return f"Error running remote command: {e}"

            elif tool_name in ("run_remote_task", "run_local_task"):
                task = tool_input.get("task", "")
                is_remote = tool_name == "run_remote_task"

                if is_remote:
                    dispatch = self._remote_task_dispatch
                    dispatch_label = "Remote worker"
                else:
                    dispatch = self._local_task_dispatch
                    dispatch_label = "Local worker"

                if not dispatch:
                    return f"{dispatch_label} dispatch not available. The FDA system may not be fully started."
                try:
                    if is_remote:
                        client_id = tool_input.get("client_id")
                        result = dispatch(task, client_id)
                    else:
                        project_path = tool_input.get("project_path")
                        result = dispatch(task, project_path)

                    if result.get("success"):
                        if result.get("investigation"):
                            analysis = result.get("analysis") or result.get("explanation", "")
                            return analysis[:3000] if analysis else "Investigation complete — no issues found."

                        parts = []
                        if result.get("explanation"):
                            parts.append(f"Explanation: {result['explanation']}")
                        if result.get("files"):
                            parts.append(f"Files affected: {', '.join(result['files'])}")
                        if result.get("diff"):
                            parts.append(f"Diff:\n{result['diff'][:1500]}")
                        if result.get("approval_id"):
                            parts.append(f"Approval ID: {result['approval_id']} (use /approve or /reject)")
                        return "\n\n".join(parts) if parts else "Task completed successfully."
                    else:
                        error = result.get("error", "Unknown error")
                        analysis = result.get("analysis", "")
                        msg = f"{dispatch_label} task failed: {error}"
                        if analysis:
                            msg += f"\n\nAnalysis:\n{analysis[:1000]}"
                        return msg
                except Exception as e:
                    return f"Error dispatching {dispatch_label.lower()} task: {e}"

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as e:
            logger.error(f"[TelegramBot] Tool {tool_name} error: {e}")
            return f"Error executing {tool_name}: {e}"

    def _answer_with_tools(
        self,
        question: str,
        chat_id: str,
        attachment_blocks: list[dict] = None,
    ) -> str:
        """Answer using the API backend's agentic tool-use loop.

        Sends the question with tool definitions and lets Claude decide
        which data sources to consult.
        """
        # Build minimal context for the system prompt
        user_name = self.state.get_context("user_name") or "the user"
        today = datetime.now().strftime("%Y-%m-%d (%A)")

        system = TELEGRAM_SYSTEM_PROMPT + f"""
Today is {today}. The user's name is {user_name}.

You have tools to look up the user's data. Use them when you need information to answer the question — don't guess. If the user asks about chats, notes, tasks, calendar, or alerts, call the appropriate tool first.

If the user sends images or files, analyze them directly. You have full vision capabilities.
"""

        # Include recent conversation for continuity
        messages: list[dict[str, Any]] = []
        try:
            recent = self.state.get_messages_recent(
                channel_id=str(chat_id), limit=HISTORY_MESSAGE_LIMIT,
            )
            cutoff = datetime.now() - timedelta(hours=HISTORY_HOURS_CUTOFF)
            for msg in recent:
                created = msg.get("created_at", "")
                if created:
                    try:
                        msg_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if msg_time.tzinfo:
                            msg_time = msg_time.replace(tzinfo=None)
                        if msg_time < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"][:HISTORY_CHAR_LIMIT],
                })
        except Exception:
            pass

        # Build user message — include attachment content blocks if present
        if attachment_blocks:
            user_content = attachment_blocks + [{"type": "text", "text": question}]
        else:
            user_content = question
        messages.append({"role": "user", "content": user_content})

        # Extended thinking configuration
        thinking_config = (
            {"type": "enabled", "budget_tokens": EXTENDED_THINKING_BUDGET}
            if ENABLE_EXTENDED_THINKING else None
        )

        # Combine user-defined tools with server-side web search
        all_tools = self._FDA_TOOLS + [{"type": "web_search_20250305"}]

        return self.backend.complete_with_tools(
            system=system,
            messages=messages,
            tools=all_tools,
            tool_executor=self._execute_tool,
            model=self.model,
            max_tokens=4096,
            max_iterations=10,
            temperature=0.7,
            thinking=thinking_config,
        )

    # ------------------------------------------------------------------
    # Answer question — dispatches to agentic or intent-based routing
    # ------------------------------------------------------------------

    def _answer_question(
        self,
        question: str,
        chat_id: str = "telegram",
        attachment_blocks: list[dict] = None,
    ) -> str:
        """Answer a question using the best available backend.

        If the API backend is available (supports tool use), uses the
        agentic approach where Claude calls tools as needed.
        Falls back to keyword-based intent routing for the CLI backend.
        """
        from fda.claude_backend import AnthropicAPIBackend

        if isinstance(self.backend, AnthropicAPIBackend):
            return self._answer_with_tools(
                question, chat_id, attachment_blocks=attachment_blocks,
            )

        return self._answer_with_intent_router(question, chat_id)

    def _answer_with_intent_router(self, question: str, chat_id: str) -> str:
        """Fallback: answer using keyword-based intent routing (CLI backend)."""
        intents = _detect_intents(question)
        context: dict[str, Any] = {"today": datetime.now().strftime("%Y-%m-%d %H:%M")}

        user_name = self.state.get_context("user_name")
        if user_name:
            context["user_name"] = user_name

        try:
            recent_msgs = self.state.get_messages_recent(channel_id=str(chat_id), limit=5)
            if recent_msgs:
                convo = []
                for msg in recent_msgs:
                    role = "User" if msg["role"] == "user" else "FDA"
                    convo.append(f"{role}: {msg['content'][:200]}")
                context["recent_conversation"] = "\n".join(convo)
        except Exception:
            pass

        if "kakao" in intents:
            context["kakaotalk_messages"] = self._get_kakao_messages(question)

        if "tasks" in intents:
            tasks = self.state.get_tasks()
            if tasks:
                context["tasks"] = [
                    {"title": t.get("title"), "status": t.get("status"), "priority": t.get("priority")}
                    for t in tasks[:10]
                ]

        if "journal" in intents:
            entries = self.search_journal(question, top_n=3)
            if entries:
                context["journal_entries"] = [
                    {"summary": e.get("summary"), "date": e.get("created_at", "")[:10]}
                    for e in entries
                ]

        if "calendar" in intents:
            context["calendar_note"] = "Calendar integration not yet active"

        if "alerts" in intents:
            alerts = self.state.get_alerts(acknowledged=False)
            if alerts:
                context["alerts"] = [
                    {"level": a.get("level"), "message": a.get("message"), "source": a.get("source")}
                    for a in alerts
                ]

        if "general" in intents:
            entries = self.search_journal(question, top_n=2)
            if entries:
                context["relevant_notes"] = [
                    {"summary": e.get("summary"), "date": e.get("created_at", "")[:10]}
                    for e in entries
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

    async def _handle_organize(self, update: Any, context: Any) -> None:
        """Handle /organize <path> [instructions] — organize local files."""
        args = " ".join(context.args) if context.args else ""
        if not args:
            await update.message.reply_text(
                "Usage: /organize <path> [instructions]\n\n"
                "Examples:\n"
                "- /organize ~/Downloads\n"
                "- /organize ~/Desktop sort by file type\n"
                "- /organize ~/Documents/projects group related files"
            )
            return

        if not self._local_organize_dispatch:
            await update.message.reply_text(
                "⚠️ File organization not available. Make sure FDA is fully started."
            )
            return

        # Parse path and instructions
        parts = args.split(None, 1)
        target_path = os.path.expanduser(parts[0])
        instructions = parts[1] if len(parts) > 1 else ""

        await update.message.reply_text(
            f"🧹 Scanning and organizing `{target_path}`...\n\n"
            f"{('Instructions: ' + instructions + chr(10)) if instructions else ''}"
            "Git repositories will be left untouched."
        )

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self._local_organize_dispatch,
                target_path, instructions,
            )

            if result.get("success"):
                moves = result.get("moves", [])
                deletions = result.get("deletions", [])
                repos_skipped = result.get("repos_skipped", [])
                summary = result.get("summary", "")

                response = "✅ Organization complete\n\n"
                if moves:
                    response += f"Moved: {len(moves)} files\n"
                if deletions:
                    response += f"Deleted junk: {len(deletions)} files\n"
                if repos_skipped:
                    response += f"Git repos skipped: {len(repos_skipped)}\n"
                if summary:
                    response += f"\n{summary[:3000]}"

                # Telegram message limit is 4096
                if len(response) > 4000:
                    response = response[:4000] + "..."

                await update.message.reply_text(response)
            else:
                error = result.get("error", "Unknown error")
                await update.message.reply_text(
                    f"❌ Organization failed: {error[:500]}"
                )

        except Exception as e:
            logger.error(f"[TelegramBot] Organize command failed: {e}")
            await update.message.reply_text(f"❌ Error: {e}")

    def run_event_loop(self) -> None:
        """
        Run the Telegram bot event loop.

        Uses the low-level async API so it can run in a daemon thread
        (run_polling() calls signal.set_wakeup_fd which only works in
        the main thread).
        """
        import sys

        print("[TelegramBot] Building application...", flush=True)
        logger.info("[TelegramBot] Starting event loop...")

        app = self._get_application()

        print("[TelegramBot] Application built. Starting polling...", flush=True)
        print("[TelegramBot] Connecting to Telegram API...", flush=True)

        # Use a dedicated event loop so we can run in a non-main thread.
        # app.run_polling() is a convenience wrapper that installs signal
        # handlers — those only work from the main thread.  Instead we
        # drive initialize → start → updater.start_polling → idle manually.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            await app.initialize()
            await app.start()
            await app.updater.start_polling(
                allowed_updates=["message"],
                drop_pending_updates=True,
                poll_interval=1.0,
                timeout=30,
            )
            logger.info("[TelegramBot] Polling started ✓")
            print("[TelegramBot] Polling started ✓", flush=True)

            # Block until the thread is interrupted
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()

        try:
            loop.run_until_complete(_run())
        except KeyboardInterrupt:
            logger.info("[TelegramBot] Received shutdown signal")
            print("\n[TelegramBot] Received shutdown signal", flush=True)
        except Exception as e:
            logger.error(f"[TelegramBot] Error in event loop: {e}")
            print(f"[TelegramBot] Error: {e}", file=sys.stderr, flush=True)
        finally:
            loop.close()

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
