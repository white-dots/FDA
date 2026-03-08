"""
FDA Orchestrator — unified single-process entry point.

Runs all components in one process on the Mac Mini:
- KakaoTalk reader: monitors client chat rooms for new messages
- FDA agent: classifies messages, creates task briefs, monitors calendar
- Worker agent: analyzes codebases and generates fixes
- Telegram bot: user Q&A + approval requests for code changes
- Discord bot: joins meetings, takes notes, answers questions via voice
- Outlook calendar: monitors schedule, prepares meeting briefs

This is the process that `fda start` launches.
"""

import asyncio
import json
import logging
import os
import signal
import threading
import time
from typing import Any, Optional
from datetime import datetime
from pathlib import Path

from fda.claude_backend import get_claude_backend
from fda.config import (
    MODEL_FDA,
    MODEL_MEETING_SUMMARY,
    STATE_DB_PATH,
    MESSAGE_BUS_PATH,
    PROJECT_ROOT,
    DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES,
    TELEGRAM_BOT_TOKEN_ENV,
    DISCORD_BOT_TOKEN_ENV,
    SLACK_BOT_TOKEN_ENV,
)
from fda.state.project_state import ProjectState
from fda.comms.message_bus import MessageBus
from fda.clients.client_config import ClientManager, ClientConfig
from fda.kakaotalk.reader import KakaoTalkReader
from fda.kakaotalk.parser import KakaoMessage
from fda.worker_agent import WorkerAgent
from fda.local_worker_agent import LocalWorkerAgent
from fda.telegram_approval import (
    ApprovalManager,
    PendingApproval,
    register_approval_handlers,
    register_local_task_handler,
)
from fda.fda_agent import FDAAgent
from fda.outlook import OutlookCalendar

logger = logging.getLogger(__name__)


# How FDA classifies incoming KakaoTalk messages
MESSAGE_CLASSIFIER_PROMPT = """You are a message classifier for Datacore, a software consultancy.

Classify the following KakaoTalk message from a client into one of these categories:

1. TASK_REQUEST — The client is asking for a code change, bug fix, feature addition, or any modification to their system. Examples: "이 필드 좀 수정해주세요", "보고서가 안 나와요", "새로운 카테고리 추가해주세요"

2. QUESTION — The client is asking a question that doesn't require code changes. Examples: "이 기능은 어떻게 쓰나요?", "데이터 언제 업데이트 돼요?"

3. INFORMATION — The client is sharing information, providing context, or making a general statement. Examples: "다음주에 회의 있어요", "참고로 알려드립니다"

4. GREETING — Simple greeting or acknowledgment. Examples: "감사합니다", "네 알겠습니다", "안녕하세요"

Respond with ONLY the category name (TASK_REQUEST, QUESTION, INFORMATION, or GREETING), nothing else.
"""


class FDAOrchestrator:
    """
    Unified orchestrator for the FDA system.

    Runs on the Mac Mini as a single process, managing all subsystems:
    - KakaoTalk polling → message classification → Worker → Telegram approval
    - Telegram bot (user Q&A + /approve /reject commands)
    - Discord bot (voice meetings, note-taking)
    - Outlook calendar monitoring (meeting prep with SharePoint file search)
    """

    def __init__(
        self,
        clients_dir: Optional[Path] = None,
        export_dir: Optional[Path] = None,
        auto_export: bool = False,
        poll_interval_seconds: int = 60,
        enable_telegram: bool = True,
        enable_discord: bool = True,
        enable_slack: bool = True,
        enable_calendar: bool = True,
    ):
        """
        Initialize the orchestrator.

        Args:
            clients_dir: Directory containing client YAML configs.
            export_dir: Directory for KakaoTalk exports.
            auto_export: Whether to auto-trigger KakaoTalk exports.
            poll_interval_seconds: How often to check for new messages.
            enable_telegram: Start Telegram bot.
            enable_discord: Start Discord bot.
            enable_slack: Start Slack bot.
            enable_calendar: Start Outlook calendar monitoring.
        """
        # Core components
        self.state = ProjectState(STATE_DB_PATH)
        self.message_bus = MessageBus(MESSAGE_BUS_PATH)
        self._backend = get_claude_backend()

        # Client management
        self.client_manager = ClientManager(clients_dir)

        # KakaoTalk reader
        self.kakao_reader = KakaoTalkReader(
            export_dir=export_dir,
            auto_export=auto_export,
        )

        # Worker agent (merged Librarian + Executor) — remote VMs via SSH
        self.worker = WorkerAgent(
            client_manager=self.client_manager,
            message_bus=self.message_bus,
            db_path=str(STATE_DB_PATH),
        )

        # Local worker agent — operates on local Mac Mini filesystem
        self.worker_local = LocalWorkerAgent(
            message_bus=self.message_bus,
            db_path=str(STATE_DB_PATH),
        )

        # FDA agent (for calendar monitoring and meeting prep)
        self.fda_agent: Optional[FDAAgent] = None

        # Journal writer for recording worker results
        from fda.journal.writer import JournalWriter
        self._journal = JournalWriter()

        # Telegram approval system
        self.approval_manager = ApprovalManager()
        self.approval_manager.set_handlers(
            on_approve=self._handle_approval,
            on_reject=self._handle_rejection,
        )

        # Feature flags
        self._enable_telegram = enable_telegram
        self._enable_discord = enable_discord
        self._enable_slack = enable_slack
        self._enable_calendar = enable_calendar

        # Configuration
        self.poll_interval = poll_interval_seconds
        self._running = False
        self._paused = False
        self._threads: list[threading.Thread] = []

        # Bot instances — stored so orchestrator can broadcast notifications
        self._slack_bot: Optional[Any] = None
        self._discord_bot: Optional[Any] = None

        # Bot threads — keyed by name for health monitoring & targeted restart
        self._bot_threads: dict[str, threading.Thread] = {}
        self._last_health_check: float = 0.0
        self._health_check_interval: int = 3600  # 1 hour

        # Daily notetaking — tracks last run date to avoid duplicates
        self._notetaking_last_run: Optional[str] = self.state.get_context(
            "notetaking_last_run"
        )

        # Daily journal review — morning briefing to Discord/Slack
        self._journal_review_last_run: Optional[str] = self.state.get_context(
            "journal_review_last_run"
        )

        # Restore last-checked timestamps from state
        self._restore_checkpoints()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _init_calendar(self) -> Optional[OutlookCalendar]:
        """Initialize Outlook calendar if user is logged in."""
        try:
            calendar = OutlookCalendar()
            if calendar.is_logged_in():
                if calendar.authenticate():
                    logger.info("✓ Outlook calendar connected")
                    return calendar
                else:
                    logger.warning("Outlook token expired — run: fda calendar login")
            else:
                logger.info("Outlook calendar not configured — run: fda calendar login")
        except Exception as e:
            logger.warning(f"Outlook calendar init failed: {e}")
        return None

    def _init_fda_agent(self, calendar: Optional[OutlookCalendar]) -> FDAAgent:
        """Initialize the FDA agent with optional calendar."""
        outlook_config = None
        if calendar:
            # Pass the already-authenticated calendar instance
            agent = FDAAgent()
            agent.calendar = calendar
            return agent
        return FDAAgent()

    # ------------------------------------------------------------------
    # Thread launchers
    # ------------------------------------------------------------------

    def _start_telegram_bot(self) -> Optional[threading.Thread]:
        """Start the Telegram bot in a daemon thread."""
        try:
            from fda.telegram_bot import TelegramBotAgent, get_bot_token

            bot_token = get_bot_token()
            if not bot_token:
                logger.info("Telegram bot not configured — skipping")
                return None

            bot = TelegramBotAgent(
                bot_token=bot_token,
                local_task_dispatch=self._handle_local_task_request,
                remote_task_dispatch=self._handle_remote_task_request,
                local_command_dispatch=self._handle_local_command,
                remote_command_dispatch=self._handle_remote_command,
                local_organize_dispatch=self._handle_local_organize_request,
            )

            # Register approval command handlers (/approve, /reject, etc.)
            app = bot._get_application()
            register_approval_handlers(app, self.approval_manager)

            # Register /local command for local worker dispatch
            register_local_task_handler(app, self._handle_local_task_request)

            # Register /restart command
            from fda.telegram_approval import register_restart_handler
            register_restart_handler(app, self.restart)

            thread = threading.Thread(
                target=bot.run_event_loop,
                daemon=True,
                name="telegram-bot",
            )
            thread.start()
            logger.info("✓ Telegram bot started")
            return thread

        except ImportError:
            logger.warning("python-telegram-bot not installed — skipping Telegram")
        except Exception as e:
            logger.error(f"Failed to start Telegram bot: {e}")
        return None

    def _start_discord_bot(self) -> Optional[threading.Thread]:
        """Start the Discord bot in a daemon thread."""
        try:
            from fda.discord_bot import DiscordVoiceAgent, get_bot_token

            bot_token = get_bot_token()
            if not bot_token:
                logger.info("Discord bot not configured — skipping")
                return None

            discord_bot = DiscordVoiceAgent(
                bot_token=bot_token,
                fda_agent=self.fda_agent,
                worker=self.worker,
                local_task_dispatch=self._handle_local_task_request,
                remote_task_dispatch=self._handle_remote_task_request,
                local_command_dispatch=self._handle_local_command,
                remote_command_dispatch=self._handle_remote_command,
                local_organize_dispatch=self._handle_local_organize_request,
                approval_manager=self.approval_manager,
                restart_callback=self.restart,
            )
            self._discord_bot = discord_bot

            thread = threading.Thread(
                target=discord_bot.run_event_loop,
                daemon=True,
                name="discord-bot",
            )
            thread.start()
            logger.info("✓ Discord bot started")
            return thread

        except ImportError:
            logger.warning("py-cord not installed — skipping Discord")
        except Exception as e:
            logger.error(f"Failed to start Discord bot: {e}")
        return None

    def _start_slack_bot(self) -> Optional[threading.Thread]:
        """Start the Slack bot in a daemon thread (Socket Mode)."""
        try:
            from fda.slack_bot import SlackBotAgent, get_bot_tokens

            tokens = get_bot_tokens()
            if not tokens:
                logger.info("Slack bot not configured — skipping")
                return None

            bot = SlackBotAgent(
                bot_token=tokens[0],
                app_token=tokens[1],
                local_task_dispatch=self._handle_local_task_request,
                remote_task_dispatch=self._handle_remote_task_request,
                remote_command_dispatch=self._handle_remote_command,
                local_command_dispatch=self._handle_local_command,
                local_organize_dispatch=self._handle_local_organize_request,
                approval_manager=self.approval_manager,
                restart_callback=self.restart,
            )
            self._slack_bot = bot

            thread = threading.Thread(
                target=bot.run_event_loop,
                daemon=True,
                name="slack-bot",
            )
            thread.start()
            logger.info("✓ Slack bot started")
            return thread

        except ImportError:
            logger.warning("slack-bolt not installed — skipping Slack")
        except Exception as e:
            logger.error(f"Failed to start Slack bot: {e}")
        return None

    def _start_calendar_monitor(self) -> Optional[threading.Thread]:
        """Start Outlook calendar monitoring in a daemon thread."""
        if not self.fda_agent or not self.fda_agent.calendar:
            logger.info("No calendar connection — skipping calendar monitor")
            return None

        def _calendar_loop():
            interval = DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES * 60
            logger.info(
                f"Calendar monitor running (every {DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES} min)"
            )
            while self._running:
                try:
                    self.fda_agent._check_upcoming_meetings()
                except Exception as e:
                    logger.error(f"Calendar check error: {e}")
                time.sleep(interval)

        thread = threading.Thread(
            target=_calendar_loop,
            daemon=True,
            name="calendar-monitor",
        )
        thread.start()
        logger.info("✓ Calendar monitor started")
        return thread

    def _start_worker(self) -> threading.Thread:
        """Start the Worker agent in a daemon thread."""
        thread = threading.Thread(
            target=self.worker.run_event_loop,
            daemon=True,
            name="worker-agent",
        )
        thread.start()
        logger.info("✓ Worker agent started")
        return thread

    def _start_worker_local(self) -> threading.Thread:
        """Start the Local Worker agent in a daemon thread."""
        thread = threading.Thread(
            target=self.worker_local.run_event_loop,
            daemon=True,
            name="worker-local-agent",
        )
        thread.start()
        logger.info("✓ Local Worker agent started")
        return thread

    # ------------------------------------------------------------------
    # Bot health monitoring
    # ------------------------------------------------------------------

    def _check_bot_health(self) -> None:
        """Check if bot threads are alive and restart dead ones.

        Called periodically from the main polling loop (default: every hour).
        If a bot thread has died (e.g. unhandled exception, network failure),
        restarts just that bot without restarting the entire FDA process.
        """
        logger.info("[HealthCheck] Checking bot thread health...")
        dead_bots: list[str] = []

        for name, thread in list(self._bot_threads.items()):
            if not thread.is_alive():
                dead_bots.append(name)
                logger.warning(f"[HealthCheck] {name} bot thread is DEAD")
            else:
                logger.debug(f"[HealthCheck] {name} bot thread is alive")

        if not dead_bots:
            logger.info("[HealthCheck] All bot threads healthy")
            return

        # Restart dead bots
        for name in dead_bots:
            logger.info(f"[HealthCheck] Restarting {name} bot...")
            try:
                new_thread = self._restart_bot(name)
                if new_thread:
                    self._bot_threads[name] = new_thread
                    self._threads.append(new_thread)
                    logger.info(f"[HealthCheck] {name} bot restarted successfully")
                else:
                    logger.error(f"[HealthCheck] {name} bot failed to restart (returned None)")
            except Exception as e:
                logger.error(f"[HealthCheck] Failed to restart {name} bot: {e}", exc_info=True)

        # Log journal entry for visibility
        try:
            self._journal.write_entry(
                author="orchestrator",
                tags=["health-check", "bot-restart"],
                summary=f"Restarted dead bot(s): {', '.join(dead_bots)}",
                content=(
                    f"## Bot Health Check\n\n"
                    f"Dead bots detected and restarted: **{', '.join(dead_bots)}**\n\n"
                    f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                ),
                relevance_decay="fast",
            )
        except Exception as e:
            logger.error(f"[HealthCheck] Failed to write journal entry: {e}")

    def _restart_bot(self, name: str) -> Optional[threading.Thread]:
        """Restart a specific bot by name.

        Args:
            name: One of 'telegram', 'slack', 'discord'.

        Returns:
            The new thread, or None if the bot couldn't be started.
        """
        if name == "telegram":
            return self._start_telegram_bot()
        elif name == "slack":
            # Clear stale reference before restart
            self._slack_bot = None
            return self._start_slack_bot()
        elif name == "discord":
            self._discord_bot = None
            return self._start_discord_bot()
        else:
            logger.warning(f"[HealthCheck] Unknown bot name: {name}")
            return None

    # ------------------------------------------------------------------
    # Daily notetaking
    # ------------------------------------------------------------------

    def _should_run_notetaking(self) -> bool:
        """
        Check if daily notetaking should run.

        Runs once per day, after the configured notetaking time (default 21:00).

        Returns:
            True if notetaking should run now.
        """
        try:
            from fda.utils.timezone import get_user_timezone, get_current_time_for_user

            user_tz = get_user_timezone(self.state)
            now = get_current_time_for_user(user_tz)
            today = now.strftime("%Y-%m-%d")

            # Already ran today
            if self._notetaking_last_run == today:
                return False

            # Check if it's past the configured time
            nt_time = self.state.get_context("notetaking_time") or "21:00"
            try:
                hour, minute = map(int, nt_time.split(":"))
            except (ValueError, AttributeError):
                hour, minute = 21, 0

            if now.hour < hour or (now.hour == hour and now.minute < minute):
                return False

            # Check if there are any notetaking channels configured
            channels = self.state.get_notetaking_channels()
            return len(channels) > 0

        except Exception as e:
            logger.debug(f"Notetaking check failed: {e}")
            return False

    def _run_daily_notetaking(self) -> None:
        """
        Summarize conversations from notetaking channels into journal entries.

        For each configured notetaking channel, fetches today's messages,
        asks Claude to summarize them, and writes a journal entry.
        """
        from fda.journal.index import JournalIndex

        channels = self.state.get_notetaking_channels()
        if not channels:
            return

        try:
            from fda.utils.timezone import get_user_timezone, get_local_today

            user_tz = get_user_timezone(self.state)
            start_of_day, end_of_day = get_local_today(user_tz)
        except Exception:
            start_of_day = datetime.now().replace(hour=0, minute=0, second=0)
            end_of_day = datetime.now()

        today = start_of_day.strftime("%Y-%m-%d")
        index = JournalIndex()

        for ch in channels:
            try:
                # Fetch today's messages for this channel
                channel_key = f"{ch['platform']}_{ch['channel_id']}"
                messages = self.state.get_messages_today(
                    channel_id=channel_key,
                    limit=200,
                )

                if not messages:
                    logger.debug(
                        f"No messages today for notetaking channel: {channel_key}"
                    )
                    continue

                # Build transcript
                transcript_lines = []
                for m in messages:
                    username = m.get("username", "unknown")
                    content = m.get("content", "")[:500]
                    transcript_lines.append(f"[{username}] {content}")
                transcript = "\n".join(transcript_lines)

                # Summarize with Claude
                label = ch.get("label") or ch["channel_id"]
                summary_prompt = (
                    f'Summarize today\'s conversation from the "{label}" channel '
                    f"into concise daily notes.\n\n"
                    f"Focus on:\n"
                    f"- Key topics discussed\n"
                    f"- Decisions made\n"
                    f"- Action items mentioned\n"
                    f"- Important information shared\n\n"
                    f"Conversation transcript:\n{transcript[:6000]}\n\n"
                    f"Write clear, organized notes in markdown. "
                    f"Be concise but capture everything important."
                )

                summary = self._backend.complete(
                    system=(
                        "You are a note-taking assistant. "
                        "Write clear, organized daily notes from chat transcripts."
                    ),
                    messages=[{"role": "user", "content": summary_prompt}],
                    max_tokens=2000,
                )

                # Write to journal
                entry = self._journal.write_entry(
                    author="FDA",
                    summary=f"Daily notes: {label} ({today})",
                    content=(
                        f"# Daily Notes — {label}\n"
                        f"**Date:** {today}\n\n"
                        f"{summary}"
                    ),
                    tags=[
                        "notetaking",
                        "daily-summary",
                        ch["platform"],
                        ch["channel_id"],
                    ],
                    relevance_decay="medium",
                )
                index.add_entry(entry)

                logger.info(
                    f"Daily notetaking complete for {label}: "
                    f"{len(messages)} messages summarized"
                )

            except Exception as e:
                logger.error(
                    f"Failed notetaking for {ch.get('label', ch)}: {e}",
                    exc_info=True,
                )

        # Mark as done for today
        self._notetaking_last_run = today
        self.state.set_context("notetaking_last_run", today)
        logger.info(f"Daily notetaking finished for {today}")

    # ------------------------------------------------------------------
    # Daily journal review — morning briefing to Discord/Slack
    # ------------------------------------------------------------------

    def _should_run_journal_review(self) -> bool:
        """
        Check if the daily journal review should run.

        Runs once per day after 9 AM (configurable via
        ``journal_review_time`` in state). Posts a summary of
        yesterday's journal entries to Discord and Slack.

        Returns:
            True if the journal review should run now.
        """
        try:
            from fda.utils.timezone import (
                get_user_timezone,
                get_current_time_for_user,
            )

            user_tz = get_user_timezone(self.state)
            now = get_current_time_for_user(user_tz)
            today = now.strftime("%Y-%m-%d")

            # Already ran today
            if self._journal_review_last_run == today:
                return False

            # Check if it's past the configured time (default 09:00)
            from fda.config import DEFAULT_JOURNAL_REVIEW_TIME

            review_time = (
                self.state.get_context("journal_review_time")
                or DEFAULT_JOURNAL_REVIEW_TIME
            )
            try:
                hour, minute = map(int, review_time.split(":"))
            except (ValueError, AttributeError):
                hour, minute = 9, 0

            if now.hour < hour or (now.hour == hour and now.minute < minute):
                return False

            # Need at least one bot to post to
            return bool(self._slack_bot or self._discord_bot)

        except Exception as e:
            logger.debug(f"Journal review check failed: {e}")
            return False

    def _run_daily_journal_review(self) -> None:
        """
        Summarize yesterday's journal entries and post to Discord/Slack.

        Fetches all journal entries from the previous day, asks Claude
        to produce a concise morning briefing, and broadcasts it.
        """
        import asyncio
        from fda.journal.index import JournalIndex

        try:
            from fda.utils.timezone import (
                get_user_timezone,
                get_current_time_for_user,
            )

            user_tz = get_user_timezone(self.state)
            now = get_current_time_for_user(user_tz)
        except Exception:
            now = datetime.now()

        today = now.strftime("%Y-%m-%d")

        # Calculate yesterday's date range
        from datetime import timedelta

        yesterday_start = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        yesterday_end = yesterday_start.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        yesterday_str = yesterday_start.strftime("%Y-%m-%d")

        # Fetch yesterday's journal entries
        index = JournalIndex()
        entries = index.get_by_date_range(yesterday_start, yesterday_end)

        if not entries:
            # Still post — let the team know it was a quiet day
            no_update_msg_slack = (
                f":sunrise: *Morning Briefing — {yesterday_str}*\n\n"
                f"No updates yesterday."
            )
            no_update_msg_discord = (
                f"🌅 **Morning Briefing — {yesterday_str}**\n\n"
                f"No updates yesterday."
            )
            if self._slack_bot:
                try:
                    self._slack_bot.broadcast_to_channel(no_update_msg_slack)
                except Exception as e:
                    logger.error(f"Failed to post empty journal review to Slack: {e}")
            if self._discord_bot and self._discord_bot._response_channel:
                try:
                    import asyncio
                    channel = self._discord_bot._response_channel
                    loop = self._discord_bot._bot.loop if self._discord_bot._bot else None
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            channel.send(no_update_msg_discord), loop
                        )
                except Exception as e:
                    logger.error(f"Failed to post empty journal review to Discord: {e}")

            self._journal_review_last_run = today
            self.state.set_context("journal_review_last_run", today)
            logger.info(f"Daily journal review: no entries from {yesterday_str}")
            return

        # Read full content of each entry
        from fda.journal.retriever import JournalRetriever

        retriever = JournalRetriever()
        entries_with_content: list[str] = []
        for entry in entries:
            filename = entry.get("filename", "")
            summary = entry.get("summary", "")
            tags = entry.get("tags", [])
            content = retriever._read_entry_content(filename) if filename else ""

            entry_block = (
                f"### {summary}\n"
                f"**Tags:** {', '.join(tags)}\n"
            )
            if content:
                # Truncate very long entries
                entry_block += content[:2000] + "\n"
            entries_with_content.append(entry_block)

        # Build transcript of all entries
        transcript = "\n---\n".join(entries_with_content)

        # Ask Claude for a morning briefing
        briefing_prompt = (
            f"Here are all the journal entries from yesterday "
            f"({yesterday_str}):\n\n"
            f"{transcript[:8000]}\n\n"
            f"Write a concise morning briefing summarizing what happened "
            f"yesterday. Include:\n"
            f"- Key activities and accomplishments\n"
            f"- Investigations completed and their findings\n"
            f"- Code changes made or deployed\n"
            f"- Issues or errors encountered\n"
            f"- Open items or follow-ups needed today\n\n"
            f"Be concise but comprehensive. Use bullet points. "
            f"Skip trivial entries."
        )

        try:
            briefing = self._backend.complete(
                system=(
                    "You are FDA's morning briefing assistant. "
                    "Summarize yesterday's journal entries into a clear, "
                    "actionable morning briefing for the team. "
                    "Use concise bullet points."
                ),
                messages=[{"role": "user", "content": briefing_prompt}],
                max_tokens=2000,
            )
        except Exception as e:
            logger.error(f"Failed to generate journal briefing: {e}")
            self._journal_review_last_run = today
            self.state.set_context("journal_review_last_run", today)
            return

        # Format the message for each platform
        entry_count = len(entries)

        # --- Post to Slack ---
        if self._slack_bot:
            try:
                slack_msg = (
                    f":sunrise: *Morning Briefing — {yesterday_str}*\n"
                    f"_{entry_count} journal "
                    f"{'entry' if entry_count == 1 else 'entries'} "
                    f"from yesterday_\n\n"
                    f"{briefing}"
                )
                self._slack_bot.broadcast_to_channel(slack_msg)
                logger.info("Journal review posted to Slack")
            except Exception as e:
                logger.error(f"Failed to post journal review to Slack: {e}")

        # --- Post to Discord ---
        if self._discord_bot and self._discord_bot._response_channel:
            try:
                discord_msg = (
                    f"🌅 **Morning Briefing — {yesterday_str}**\n"
                    f"*{entry_count} journal "
                    f"{'entry' if entry_count == 1 else 'entries'} "
                    f"from yesterday*\n\n"
                    f"{briefing}"
                )

                # Discord bot runs in its own asyncio loop
                channel = self._discord_bot._response_channel
                loop = (
                    self._discord_bot._bot.loop
                    if self._discord_bot._bot
                    else None
                )

                if loop and loop.is_running():
                    # Split into 2000-char chunks for Discord's limit
                    remaining = discord_msg
                    while remaining:
                        chunk = remaining[:2000]
                        remaining = remaining[2000:]
                        asyncio.run_coroutine_threadsafe(
                            channel.send(chunk), loop
                        )
                else:
                    logger.debug(
                        "Discord bot event loop not running — "
                        "skipping journal review"
                    )

                logger.info("Journal review posted to Discord")
            except Exception as e:
                logger.error(
                    f"Failed to post journal review to Discord: {e}"
                )

        # Mark as done for today
        self._journal_review_last_run = today
        self.state.set_context("journal_review_last_run", today)
        logger.info(
            f"Daily journal review complete: {entry_count} entries "
            f"from {yesterday_str}"
        )

    # ------------------------------------------------------------------
    # Checkpoint management
    # ------------------------------------------------------------------

    def _restore_checkpoints(self) -> None:
        """Restore last-checked timestamps from persistent state."""
        for client in self.client_manager.list_clients():
            checkpoint = self.state.get_context(
                f"kakao_last_checked_{client.client_id}"
            )
            if checkpoint:
                try:
                    ts = datetime.fromisoformat(checkpoint)
                    self.kakao_reader.set_last_checked(
                        client.kakaotalk_room, ts
                    )
                    logger.info(
                        f"Restored checkpoint for {client.name}: {checkpoint}"
                    )
                except ValueError:
                    pass

    def _save_checkpoint(self, client_id: str, room_name: str) -> None:
        """Save last-checked timestamp to persistent state."""
        last_checked = self.kakao_reader.get_last_checked(room_name)
        if last_checked:
            self.state.set_context(
                f"kakao_last_checked_{client_id}",
                last_checked.isoformat(),
            )

    # ------------------------------------------------------------------
    # KakaoTalk message processing
    # ------------------------------------------------------------------

    def classify_message(self, message: KakaoMessage, client: ClientConfig) -> str:
        """
        Classify an incoming KakaoTalk message.

        Returns:
            Category: TASK_REQUEST, QUESTION, INFORMATION, or GREETING.
        """
        try:
            raw = self._backend.complete(
                system=MESSAGE_CLASSIFIER_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Client: {client.name}\n"
                        f"Sender: {message.sender}\n"
                        f"Message: {message.text}"
                    ),
                }],
                model=MODEL_FDA,
                max_tokens=50,
            )
            category = raw.strip().upper()

            if category in ("TASK_REQUEST", "QUESTION", "INFORMATION", "GREETING"):
                return category

            return "INFORMATION"

        except Exception as e:
            logger.error(f"Error classifying message: {e}")
            return "INFORMATION"

    def create_task_brief(
        self,
        messages: list[KakaoMessage],
        client: ClientConfig,
    ) -> str:
        """
        Create a task brief from client messages with full business context.

        This is what gets sent to the Worker agent.
        """
        messages_text = "\n".join(
            f"[{msg.sender} {msg.timestamp.strftime('%H:%M')}] {msg.text}"
            for msg in messages
        )

        prompt = f"""Based on these KakaoTalk messages from a client, create a clear, actionable task brief for a developer.

{client.get_context_for_prompt()}

RECENT MESSAGES:
{messages_text}

Create a task brief that includes:
1. What the client is asking for (in clear technical terms)
2. Which part of their system is likely affected
3. Any constraints or preferences from the business context
4. Priority assessment (is this urgent or routine?)

Be specific and actionable. The developer needs to know exactly what to change.
"""

        try:
            return self._backend.complete(
                system="",
                messages=[{"role": "user", "content": prompt}],
                model=MODEL_MEETING_SUMMARY,
                max_tokens=1000,
            ).strip()
        except Exception as e:
            logger.error(f"Error creating task brief: {e}")
            return f"Client {client.name} request:\n{messages_text}"

    def process_new_messages(self) -> None:
        """Check all client chat rooms for new messages and process them."""
        if self._paused:
            return

        room_names = self.client_manager.get_all_room_names()
        all_new_messages = self.kakao_reader.poll_all_rooms(room_names)

        for room_name, messages in all_new_messages.items():
            client = self.client_manager.get_client_by_room(room_name)
            if not client:
                logger.warning(f"No client config for room: {room_name}")
                continue

            # Store messages in state DB
            for msg in messages:
                self.state.add_conversation_message(
                    channel_id=f"kakao_{client.client_id}",
                    role="user",
                    content=msg.text,
                    source="kakaotalk",
                    username=msg.sender,
                )

            # Classify each message
            task_messages: list[KakaoMessage] = []

            for msg in messages:
                category = self.classify_message(msg, client)
                logger.info(
                    f"[{client.name}] {msg.sender}: {msg.text[:50]}... "
                    f"→ {category}"
                )

                if category == "TASK_REQUEST":
                    task_messages.append(msg)
                elif category == "QUESTION":
                    logger.info(f"Question from {client.name} — queued for review")

            # Process task requests
            if task_messages:
                self._handle_task_request(task_messages, client)

            # Save checkpoint
            self._save_checkpoint(client.client_id, room_name)

    def _handle_task_request(
        self,
        messages: list[KakaoMessage],
        client: ClientConfig,
    ) -> None:
        """
        Handle task request messages from a client.

        1. Create a task brief
        2. Send to Worker agent
        3. Worker analyzes and generates fix
        4. Queue approval for user via Telegram
        """
        task_brief = self.create_task_brief(messages, client)

        task_id = self.state.add_task(
            title=f"[{client.name}] {messages[0].text[:100]}",
            description=task_brief,
            owner="worker",
            priority="medium",
        )
        logger.info(f"Task created: {task_id} for {client.name}")

        logger.info(f"Sending task to Worker for {client.name}...")
        fix_result = self.worker.analyze_and_fix(
            client_id=client.client_id,
            task_brief=task_brief,
        )

        if fix_result.get("success"):
            approval = self.approval_manager.add_approval(
                client_id=client.client_id,
                client_name=client.name,
                task_brief=messages[0].text,
                explanation=fix_result.get("explanation", ""),
                diff=fix_result.get("diff", ""),
                file_changes=fix_result.get("changes", {}),
                confidence=fix_result.get("confidence", "unknown"),
                warnings=fix_result.get("warnings", []),
            )

            self._send_approval_to_telegram(approval)
            self.state.update_task(task_id, status="awaiting_approval")
        else:
            error = fix_result.get("error", "Unknown error")
            logger.error(f"Worker failed for {client.name}: {error}")

            self._send_telegram_notification(
                f"⚠️ Failed to generate fix for {client.name}\n\n"
                f"Request: {messages[0].text[:200]}\n"
                f"Error: {error}\n\n"
                "You may need to handle this one manually."
            )

            self.state.update_task(task_id, status="blocked")

    def _handle_local_task_request(
        self,
        task_brief: str,
        project_path: Optional[str] = None,
        progress_callback: Optional[Any] = None,
    ) -> dict[str, Any]:
        """
        Handle a local task request (from Telegram /local or Discord !local).

        1. Resolve project path (default to first configured project)
        2. Call LocalWorkerAgent.analyze_and_fix()
        3. On success, create PendingApproval with is_local=True
        4. Send approval to Telegram
        5. Return result dict for the calling bot to display

        Args:
            task_brief: Natural language task description.
            project_path: Local project directory. Defaults to first project.
            progress_callback: Optional callback(msg: str) for live progress updates.

        Returns:
            Dict with: success, approval_id, files, explanation, diff, error.
        """
        # Resolve project path (supports name shortcuts like "FDA")
        if project_path:
            project_path = self.worker_local.resolve_project_path(project_path)
        else:
            project_path = str(self.worker_local.projects[0])

        project_name = Path(project_path).name
        logger.info(f"Local task request: {task_brief[:80]}... (project: {project_name})")

        # Create task in state DB
        task_id = self.state.add_task(
            title=f"[LOCAL] {task_brief[:100]}",
            description=task_brief,
            owner="worker_local",
            priority="medium",
        )

        # Call local worker
        try:
            fix_result = self.worker_local.analyze_and_fix(
                project_path=project_path,
                task_brief=task_brief,
                progress_callback=progress_callback,
            )
        except Exception as e:
            logger.error(f"Local worker error: {e}")
            self.state.update_task(task_id, status="blocked")
            return {"success": False, "error": str(e)}

        target = f"LOCAL ({project_name})"

        if fix_result.get("success"):
            changes = fix_result.get("changes", {})

            # Investigation/query task — no code changes, just return the analysis
            if not changes:
                self.state.update_task(task_id, status="completed")
                self._log_worker_journal(
                    task_brief=task_brief,
                    target=target,
                    result_type="investigation",
                    explanation=fix_result.get("explanation", ""),
                    analysis=fix_result.get("analysis", ""),
                )
                return {
                    "success": True,
                    "investigation": True,
                    "explanation": fix_result.get("explanation", ""),
                    "analysis": fix_result.get("analysis", ""),
                    "diff": "",
                    "files": [],
                }

            # Code change task — queue for approval
            approval = self.approval_manager.add_approval(
                client_id="local",
                client_name=target,
                task_brief=task_brief,
                explanation=fix_result.get("explanation", ""),
                diff=fix_result.get("diff", ""),
                file_changes=changes,
                confidence=fix_result.get("confidence", "unknown"),
                warnings=fix_result.get("warnings", []),
                project_path=project_path,
                is_local=True,
            )

            self._send_approval_to_telegram(approval)
            self.state.update_task(task_id, status="awaiting_approval")

            self._log_worker_journal(
                task_brief=task_brief,
                target=target,
                result_type="code_change",
                explanation=fix_result.get("explanation", ""),
                files=list(changes.keys()),
                diff=fix_result.get("diff", ""),
            )

            return {
                "success": True,
                "approval_id": approval.short_id,
                "explanation": fix_result.get("explanation", ""),
                "diff": fix_result.get("diff", ""),
                "files": list(changes.keys()),
            }
        else:
            error = fix_result.get("error", "Unknown error")
            logger.error(f"Local worker failed: {error}")

            self._send_telegram_notification(
                f"⚠️ Failed to generate local fix\n\n"
                f"Request: {task_brief[:200]}\n"
                f"Error: {error}\n\n"
                "You may need to handle this one manually."
            )

            self.state.update_task(task_id, status="blocked")
            self._log_worker_journal(
                task_brief=task_brief,
                target=target,
                result_type="error",
                analysis=fix_result.get("analysis", ""),
                error=error,
            )
            return {
                "success": False,
                "error": error,
                "analysis": fix_result.get("analysis", ""),
            }

    # ------------------------------------------------------------------
    # Local file organization
    # ------------------------------------------------------------------

    def _handle_local_organize_request(
        self,
        target_path: str,
        instructions: str = "",
        progress_callback: Optional[Any] = None,
    ) -> dict[str, Any]:
        """
        Handle a file organization request — scan a directory and sort files.

        1. Call LocalWorkerAgent.organize_files()
        2. Log results to journal
        3. Return summary for the calling bot

        Args:
            target_path: Directory to organize.
            instructions: Optional user instructions.
            progress_callback: Optional callback(msg: str) for live progress.

        Returns:
            Dict with: success, summary, moves, deletions, repos_skipped, error.
        """
        # Resolve name shortcuts
        target_path = self.worker_local.resolve_project_path(target_path)
        dir_name = Path(target_path).name
        logger.info(
            f"File organize request: {dir_name} — {instructions[:80]}"
        )

        # Create task in state DB
        task_id = self.state.add_task(
            title=f"[ORGANIZE] {dir_name}",
            description=instructions or f"Organize files in {target_path}",
            owner="worker_local",
            priority="low",
        )

        try:
            result = self.worker_local.organize_files(
                target_path=target_path,
                instructions=instructions,
                progress_callback=progress_callback,
            )
        except Exception as e:
            logger.error(f"File organization error: {e}")
            self.state.update_task(task_id, status="blocked")
            return {"success": False, "error": str(e)}

        if result.get("success"):
            self.state.update_task(task_id, status="completed")

            # Log to journal
            moves = result.get("moves", [])
            deletions = result.get("deletions", [])
            repos_skipped = result.get("repos_skipped", [])
            dirs_created = result.get("dirs_created", [])
            summary = result.get("summary", "")

            # Build journal content
            parts = [f"## Target\n`{target_path}`"]
            if instructions:
                parts.append(f"## Instructions\n{instructions}")

            if moves:
                move_lines = "\n".join(
                    f"- `{m['from']}` → `{m['to']}`" for m in moves[:50]
                )
                parts.append(f"## Files Moved ({len(moves)})\n{move_lines}")

            if dirs_created:
                dir_lines = "\n".join(f"- `{d}`" for d in dirs_created)
                parts.append(f"## Directories Created\n{dir_lines}")

            if deletions:
                del_lines = "\n".join(f"- `{d}`" for d in deletions)
                parts.append(f"## Junk Deleted\n{del_lines}")

            if repos_skipped:
                repo_lines = "\n".join(f"- `{r}`" for r in repos_skipped)
                parts.append(f"## Git Repos Skipped\n{repo_lines}")

            if summary:
                parts.append(f"## Summary\n{summary[:2000]}")

            content = "\n\n".join(parts)

            journal_tags = ["worker", "local", "file-organization"]
            brief = instructions[:60] if instructions else f"Organize {dir_name}"
            journal_summary = (
                f"[LOCAL] File organization: {brief} "
                f"({len(moves)} moves, {len(deletions)} deletions)"
            )

            try:
                self._journal.write_entry(
                    author="orchestrator",
                    tags=journal_tags,
                    summary=journal_summary,
                    content=content,
                    relevance_decay="medium",
                )
            except Exception as e:
                logger.warning(f"Failed to write organize journal entry: {e}")

            return result
        else:
            error = result.get("error", "Unknown error")
            self.state.update_task(task_id, status="blocked")

            self._log_worker_journal(
                task_brief=instructions or f"Organize {dir_name}",
                target=f"LOCAL ({dir_name})",
                result_type="error",
                error=error,
            )

            return result

    # ------------------------------------------------------------------
    # Journal logging for worker results
    # ------------------------------------------------------------------

    def _log_worker_journal(
        self,
        *,
        task_brief: str,
        target: str,
        result_type: str,
        explanation: str = "",
        analysis: str = "",
        files: list[str] | None = None,
        diff: str = "",
        error: str = "",
    ) -> None:
        """Write a journal entry summarising a worker task result.

        Args:
            task_brief: The original user request.
            target: Where the work happened (e.g. "AoneBnH VM", "LOCAL (FDA)").
            result_type: One of "investigation", "code_change", "deployment", "error".
            explanation: Human-readable explanation from the worker.
            analysis: Detailed analysis text.
            files: List of files touched / examined.
            diff: Unified diff (truncated for journal).
            error: Error message if the task failed.
        """
        try:
            # Build tags
            tags = ["worker", result_type]
            if "local" in target.lower():
                tags.append("local")
            else:
                tags.append("remote")

            # Build summary line
            brief_short = task_brief[:80].rstrip(".")
            if result_type == "error":
                summary = f"[{target}] FAILED: {brief_short}"
            elif result_type == "deployment":
                summary = f"[{target}] Deployed: {brief_short}"
            else:
                summary = f"[{target}] {brief_short}"

            # Build content body
            parts = [f"## Task\n{task_brief}"]

            if explanation:
                parts.append(f"## Explanation\n{explanation[:2000]}")

            if analysis and analysis != explanation:
                parts.append(f"## Analysis\n{analysis[:2000]}")

            if files:
                file_list = "\n".join(f"- `{f}`" for f in files[:30])
                parts.append(f"## Files\n{file_list}")

            if diff:
                diff_preview = diff[:3000]
                parts.append(f"## Diff\n```diff\n{diff_preview}\n```")

            if error:
                parts.append(f"## Error\n{error[:1000]}")

            content = "\n\n".join(parts)

            self._journal.write_entry(
                author="orchestrator",
                tags=tags,
                summary=summary,
                content=content,
                relevance_decay="medium",
            )
            logger.debug(f"Journal entry written: {summary[:60]}")
        except Exception as e:
            logger.warning(f"Failed to write journal entry: {e}")

    # ------------------------------------------------------------------
    # Remote task dispatch
    # ------------------------------------------------------------------

    def _handle_remote_task_request(
        self,
        task_brief: str,
        client_id: Optional[str] = None,
        progress_callback: Optional[Any] = None,
    ) -> dict[str, Any]:
        """
        Handle a remote task request — dispatches to the remote WorkerAgent.

        The worker SSHes into the client's VM, scans the codebase, and
        either answers an investigation question or generates a code fix.

        Args:
            task_brief: Natural language task description.
            client_id: Client identifier. Defaults to first configured client.
            progress_callback: Optional callback(msg: str) for live progress.

        Returns:
            Dict with: success, investigation, explanation, analysis,
                        approval_id, files, diff, error.
        """
        # Resolve client
        if not client_id:
            clients = self.client_manager.list_clients()
            if not clients:
                return {"success": False, "error": "No clients configured"}
            client_id = clients[0].client_id

        client = self.client_manager.get_client(client_id)
        if not client:
            return {"success": False, "error": f"Unknown client: {client_id}"}

        logger.info(
            f"Remote task request for {client.name}: {task_brief[:80]}..."
        )

        # Create task in state DB
        task_id = self.state.add_task(
            title=f"[{client.name}] {task_brief[:100]}",
            description=task_brief,
            owner="worker",
            priority="medium",
        )

        # Call remote worker
        try:
            fix_result = self.worker.analyze_and_fix(
                client_id=client_id,
                task_brief=task_brief,
                progress_callback=progress_callback,
            )
        except Exception as e:
            logger.error(f"Remote worker error: {e}")
            self.state.update_task(task_id, status="blocked")
            return {"success": False, "error": str(e)}

        if fix_result.get("success"):
            changes = fix_result.get("changes", {})

            # Investigation/query task — no code changes
            if not changes:
                self.state.update_task(task_id, status="completed")
                self._log_worker_journal(
                    task_brief=task_brief,
                    target=f"{client.name} VM",
                    result_type="investigation",
                    explanation=fix_result.get("explanation", ""),
                    analysis=fix_result.get("analysis", ""),
                )
                return {
                    "success": True,
                    "investigation": True,
                    "explanation": fix_result.get("explanation", ""),
                    "analysis": fix_result.get("analysis", ""),
                    "diff": "",
                    "files": [],
                }

            # Code change task — queue for approval
            approval = self.approval_manager.add_approval(
                client_id=client_id,
                client_name=client.name,
                task_brief=task_brief,
                explanation=fix_result.get("explanation", ""),
                diff=fix_result.get("diff", ""),
                file_changes=changes,
                confidence=fix_result.get("confidence", "unknown"),
                warnings=fix_result.get("warnings", []),
            )

            self._send_approval_to_telegram(approval)
            self.state.update_task(task_id, status="awaiting_approval")

            self._log_worker_journal(
                task_brief=task_brief,
                target=f"{client.name} VM",
                result_type="code_change",
                explanation=fix_result.get("explanation", ""),
                files=list(changes.keys()),
                diff=fix_result.get("diff", ""),
            )

            return {
                "success": True,
                "approval_id": approval.short_id,
                "explanation": fix_result.get("explanation", ""),
                "diff": fix_result.get("diff", ""),
                "files": list(changes.keys()),
            }
        else:
            error = fix_result.get("error", "Unknown error")
            logger.error(f"Remote worker failed for {client.name}: {error}")
            self.state.update_task(task_id, status="blocked")
            self._log_worker_journal(
                task_brief=task_brief,
                target=f"{client.name} VM",
                result_type="error",
                analysis=fix_result.get("analysis", ""),
                error=error,
            )
            return {
                "success": False,
                "error": error,
                "analysis": fix_result.get("analysis", ""),
            }

    def _handle_remote_command(
        self,
        command: str,
        client_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Execute a shell command on a client's remote VM via SSH.

        This gives the Slack/Discord bot Claude direct shell access,
        similar to how Claude Code can run commands locally.

        Args:
            command: Shell command to execute.
            client_id: Client identifier. Defaults to first configured client.
            cwd: Working directory. Defaults to client's repo_path.

        Returns:
            Dict with: success, stdout, stderr, error.
        """
        if not client_id:
            clients = self.client_manager.list_clients()
            if not clients:
                return {"success": False, "error": "No clients configured"}
            client_id = clients[0].client_id

        client = self.client_manager.get_client(client_id)
        if not client:
            return {"success": False, "error": f"Unknown client: {client_id}"}

        # Safety: block destructive commands
        cmd_lower = command.strip().lower()
        blocked = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", "shutdown", "reboot"]
        if any(b in cmd_lower for b in blocked):
            return {"success": False, "error": "Blocked: potentially destructive command"}

        logger.info(f"Remote command on {client.name}: {command[:100]}")

        try:
            ssh = self.worker._get_ssh(client)
            work_dir = cwd or client.project.repo_path
            result = ssh.execute(command, cwd=work_dir, timeout=30)
            return {
                "success": result.success,
                "stdout": result.stdout[:5000] if result.stdout else "",
                "stderr": result.stderr[:2000] if result.stderr else "",
                "error": "" if result.success else (result.stderr or "Command failed")[:500],
            }
        except Exception as e:
            logger.error(f"Remote command error: {e}")
            return {"success": False, "error": str(e)}

    def _handle_local_command(
        self,
        command: str,
        cwd: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Execute a shell command on the local Mac Mini.

        Gives the Slack/Discord bot Claude direct local shell access
        for quick operations (ls, grep, cat, find, etc.) without
        triggering the full analyze_and_fix pipeline.

        Args:
            command: Shell command to execute.
            cwd: Working directory. Defaults to ~/Documents.

        Returns:
            Dict with: success, stdout, stderr, error.
        """
        import subprocess

        # Safety: block destructive commands
        cmd_lower = command.strip().lower()
        blocked = [
            "rm -rf /", "rm -rf ~", "mkfs", "dd if=",
            "> /dev/", "shutdown", "reboot", "sudo rm",
            "format ", "diskutil erase",
        ]
        if any(b in cmd_lower for b in blocked):
            return {"success": False, "error": "Blocked: potentially destructive command"}

        # Restrict working directory to allowed project roots
        work_dir = cwd or str(Path.home() / "Documents")
        resolved_cwd = Path(work_dir).resolve()

        allowed_roots = [Path(p).resolve() for p in [
            str(Path.home() / "Documents"),
            str(Path.home() / "Downloads"),
            str(Path.home() / "Desktop"),
        ]]
        if not any(
            resolved_cwd == root or resolved_cwd.is_relative_to(root)
            for root in allowed_roots
        ):
            return {
                "success": False,
                "error": f"Working directory not in allowed roots: {resolved_cwd}",
            }

        logger.info(f"Local command: {command[:100]} (cwd: {work_dir})")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout[:5000] if result.stdout else "",
                "stderr": result.stderr[:2000] if result.stderr else "",
                "error": "" if result.returncode == 0 else (
                    result.stderr or f"Exit code {result.returncode}"
                )[:500],
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command timed out (30s)"}
        except Exception as e:
            logger.error(f"Local command error: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Telegram notification helpers
    # ------------------------------------------------------------------

    def _send_approval_to_telegram(self, approval: PendingApproval) -> None:
        """Send an approval request to all active channels."""
        # Telegram
        telegram_msg = approval.format_telegram_message()
        self._send_telegram_notification(telegram_msg)

        # Slack
        self._send_slack_notification(approval)

        # Discord
        self._send_discord_notification(approval)

    def _send_slack_notification(self, approval: PendingApproval) -> None:
        """Send an approval notification to the Slack channel."""
        if not self._slack_bot:
            return

        try:
            if approval.is_local:
                from pathlib import Path
                project_name = Path(approval.project_path).name if approval.project_path else "local"
                target_label = f":house: LOCAL ({project_name})"
            else:
                target_label = approval.client_name

            files_str = ", ".join(approval.file_changes.keys())
            diff_preview = approval.diff[:1500] if approval.diff else ""

            text = (
                f":wrench: *Code change for {target_label}*\n\n"
                f"*Request:* {approval.task_brief[:200]}\n\n"
                f"*What changed:* {approval.explanation[:300]}\n\n"
                f"*Files:* {files_str}\n"
                f"*Confidence:* {approval.confidence}\n"
            )

            if approval.warnings:
                text += "\n:warning: *Warnings:*\n" + "\n".join(f"  - {w}" for w in approval.warnings) + "\n"

            if diff_preview:
                text += f"\n```\n{diff_preview}\n```\n"

            text += (
                f"\nReply with:\n"
                f"`!approve {approval.short_id}`\n"
                f"`!reject {approval.short_id} [reason]`\n"
                f"`!details {approval.short_id}`"
            )

            self._slack_bot.broadcast_to_channel(text)
        except Exception as e:
            logger.error(f"Failed to send Slack approval notification: {e}")

    def _send_discord_notification(self, approval: PendingApproval) -> None:
        """Send an approval notification to Discord."""
        if not self._discord_bot or not self._discord_bot._response_channel:
            return

        try:
            if approval.is_local:
                from pathlib import Path
                project_name = Path(approval.project_path).name if approval.project_path else "local"
                target_label = f"🏠 LOCAL ({project_name})"
            else:
                target_label = approval.client_name

            files_str = ", ".join(approval.file_changes.keys())
            diff_preview = approval.diff[:1200] if approval.diff else ""

            text = (
                f"🔧 **Code change for {target_label}**\n\n"
                f"**Request:** {approval.task_brief[:200]}\n\n"
                f"**What changed:** {approval.explanation[:300]}\n\n"
                f"**Files:** {files_str}\n"
                f"**Confidence:** {approval.confidence}\n"
            )

            if approval.warnings:
                text += "\n⚠️ **Warnings:**\n" + "\n".join(f"  - {w}" for w in approval.warnings) + "\n"

            if diff_preview:
                text += f"\n```diff\n{diff_preview}\n```\n"

            text += (
                f"\nReply with:\n"
                f"`!approve {approval.short_id}`\n"
                f"`!reject {approval.short_id} [reason]`\n"
                f"`!details {approval.short_id}`"
            )

            # Discord bot runs in its own asyncio loop
            import asyncio
            channel = self._discord_bot._response_channel
            loop = self._discord_bot._bot.loop if self._discord_bot._bot else None
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(channel.send(text[:2000]), loop)
            else:
                logger.debug("Discord bot event loop not running — skipping notification")
        except Exception as e:
            logger.error(f"Failed to send Discord approval notification: {e}")

    def _send_telegram_notification(self, text: str) -> None:
        """Send a notification to the user via Telegram.

        Uses a fresh asyncio event loop to avoid conflicts with the
        Telegram bot's own event loop running in its daemon thread.
        """
        try:
            import telegram

            bot_token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
            if not bot_token:
                bot_token = self.state.get_context("telegram_bot_token")

            if not bot_token:
                logger.error("No Telegram bot token configured")
                return

            users = self.state.get_telegram_users(active_only=True)
            if not users:
                logger.warning(
                    "No active Telegram users to notify. "
                    "Send /start to the Telegram bot to register."
                )
                return

            async def _send() -> None:
                bot = telegram.Bot(token=bot_token)
                for user in users:
                    try:
                        await bot.send_message(
                            chat_id=user["chat_id"],
                            text=text,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to send Telegram message to "
                            f"{user.get('chat_id')}: {e}"
                        )

            # Always create a dedicated loop — avoids conflicts with
            # the Telegram bot's event loop in another thread.
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_send())
            finally:
                loop.close()

        except ImportError:
            logger.error("python-telegram-bot not installed")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    # ------------------------------------------------------------------
    # Approval callbacks
    # ------------------------------------------------------------------

    async def _handle_approval(self, approval: PendingApproval) -> None:
        """Called when user approves a code change.

        Runs the blocking deployment in a thread executor so the
        Telegram bot's event loop stays responsive.
        """
        target_name = approval.client_name or "local"
        logger.info(f"Deploying approved changes for {target_name}...")

        loop = asyncio.get_event_loop()

        def _deploy() -> tuple[bool, str]:
            if approval.is_local and approval.project_path:
                result_dict = self.worker_local.deploy_approved_changes(
                    project_path=approval.project_path,
                    file_changes=approval.file_changes,
                )
                success = result_dict.get("success", False)
                summary = (
                    f"Files: {', '.join(result_dict.get('files_deployed', []))}"
                    if success
                    else result_dict.get("error", "Unknown error")
                )
            else:
                result = self.worker.deploy_approved_changes(
                    client_id=approval.client_id,
                    file_changes=approval.file_changes,
                )
                success = result.success
                summary = result.summary()
            return success, summary

        try:
            success, summary = await loop.run_in_executor(None, _deploy)
        except Exception as e:
            logger.error(f"Deployment error for {target_name}: {e}")
            success, summary = False, str(e)

        if success:
            self._send_telegram_notification(
                f"✅ Deployed to {target_name}\n\n{summary}"
            )

            self.state.add_decision(
                title=f"Deployed fix for {target_name}",
                rationale=approval.explanation,
                decision_maker="user (approved via telegram)",
                impact=f"Files changed: {', '.join(approval.file_changes.keys())}",
            )

            self._log_worker_journal(
                task_brief=approval.task_brief,
                target=target_name,
                result_type="deployment",
                explanation=approval.explanation,
                files=list(approval.file_changes.keys()),
                diff=approval.diff,
            )
        else:
            self._send_telegram_notification(
                f"❌ Deployment FAILED for {target_name}\n\n"
                f"{summary}\n\n"
                "You may need to fix this manually."
            )
            self._log_worker_journal(
                task_brief=approval.task_brief,
                target=target_name,
                result_type="error",
                error=f"Deployment failed: {summary}",
            )

    async def _handle_rejection(self, approval: PendingApproval, reason: str) -> None:
        """Called when user rejects a code change."""
        logger.info(
            f"Change rejected for {approval.client_name}: {reason}"
        )

        self.state.add_decision(
            title=f"Rejected fix for {approval.client_name}",
            rationale=f"Rejection reason: {reason}",
            decision_maker="user (rejected via telegram)",
            impact="No changes deployed",
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Run the orchestrator — unified single-process entry point.

        Starts all subsystems in parallel threads:
        1. Worker agent
        2. Telegram bot (with approval handlers)
        3. Discord bot (voice meetings)
        4. Outlook calendar monitor
        5. KakaoTalk polling loop (main thread)
        """
        logger.info("=" * 60)
        logger.info("FDA Orchestrator starting (unified mode)")
        logger.info(f"Clients loaded: {len(self.client_manager.list_clients())}")
        logger.info(f"KakaoTalk poll interval: {self.poll_interval}s")
        logger.info("=" * 60)

        # List loaded clients
        for client in self.client_manager.list_clients():
            logger.info(f"  Client: {client.name} ({client.client_id})")
            logger.info(f"    KakaoTalk room: {client.kakaotalk_room}")
            logger.info(f"    VM: {client.vm.host}")

        # Test VM connections
        logger.info("Testing VM connections...")
        connections = self.worker.test_all_connections()
        for client_id, status in connections.items():
            connected = "✓" if status.get("connected") else "✗"
            logger.info(f"  {client_id}: {connected} ({status.get('host')})")

        self._running = True

        # --- Initialize optional components ---

        # Outlook calendar
        calendar = None
        if self._enable_calendar:
            calendar = self._init_calendar()

        # FDA agent (needs calendar for meeting prep)
        self.fda_agent = self._init_fda_agent(calendar)

        # --- Start subsystem threads ---

        # 1. Worker agent (remote VMs)
        self._threads.append(self._start_worker())

        # 1b. Local Worker agent (local filesystem)
        self._threads.append(self._start_worker_local())

        # 1c. Discover local git repos at startup
        try:
            new_repos = self.worker_local.discover_repos()
            if new_repos:
                logger.info(f"Discovered {len(new_repos)} new local repo(s)")
                for r in new_repos:
                    logger.info(f"  New repo: {r['name']} ({r['path']})")
            else:
                try:
                    known = self.state.get_all_projects()
                    logger.info(f"Repo discovery: {len(known)} known repos, no new ones")
                except Exception:
                    logger.info("Repo discovery: no new repos found")
        except Exception as e:
            logger.warning(f"Repo discovery failed: {e}")
        self._last_repo_discovery = time.time()

        # 2. Telegram bot (with /approve, /reject, /pending commands)
        if self._enable_telegram:
            t = self._start_telegram_bot()
            if t:
                self._threads.append(t)
                self._bot_threads["telegram"] = t

        # 3. Slack bot (Socket Mode)
        if self._enable_slack:
            t = self._start_slack_bot()
            if t:
                self._threads.append(t)
                self._bot_threads["slack"] = t

        # 4. Discord bot (voice meetings)
        if self._enable_discord:
            t = self._start_discord_bot()
            if t:
                self._threads.append(t)
                self._bot_threads["discord"] = t

        # 5. Calendar monitor
        if self._enable_calendar:
            t = self._start_calendar_monitor()
            if t:
                self._threads.append(t)

        # --- Summary ---
        logger.info("-" * 40)
        logger.info(f"Running threads: {len(self._threads)}")
        for t in self._threads:
            logger.info(f"  • {t.name}")
        logger.info("-" * 40)

        # Initialize health check timer
        self._last_health_check = time.time()

        # 5. KakaoTalk polling (main thread) + bot health monitoring
        logger.info(f"Starting KakaoTalk polling (every {self.poll_interval}s)...")
        try:
            while self._running:
                try:
                    self.process_new_messages()
                except Exception as e:
                    logger.error(f"Error in polling loop: {e}", exc_info=True)

                # Periodic bot health check
                now = time.time()
                if now - self._last_health_check >= self._health_check_interval:
                    self._check_bot_health()
                    self._last_health_check = now

                # Periodic repo discovery
                from fda.config import REPO_DISCOVERY_INTERVAL_MINUTES
                if now - self._last_repo_discovery >= REPO_DISCOVERY_INTERVAL_MINUTES * 60:
                    try:
                        new_repos = self.worker_local.discover_repos()
                        if new_repos:
                            logger.info(f"Periodic discovery: {len(new_repos)} new repo(s)")
                    except Exception as e:
                        logger.warning(f"Periodic repo discovery failed: {e}")
                    self._last_repo_discovery = now

                # Daily notetaking check (9 PM)
                if self._should_run_notetaking():
                    try:
                        self._run_daily_notetaking()
                    except Exception as e:
                        logger.error(f"Daily notetaking failed: {e}", exc_info=True)

                # Daily journal review — morning briefing (9 AM)
                if self._should_run_journal_review():
                    try:
                        self._run_daily_journal_review()
                    except Exception as e:
                        logger.error(
                            f"Daily journal review failed: {e}",
                            exc_info=True,
                        )

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self._running = False

        logger.info("FDA Orchestrator stopped.")

    def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False

    def restart(self) -> None:
        """Request a full process restart.

        Writes a marker file and stops the orchestrator. The CLI
        wrapper detects the marker and re-execs the process so all
        code changes take effect.
        """
        from fda.config import RESTART_MARKER_PATH
        RESTART_MARKER_PATH.write_text(str(datetime.now()))
        logger.info("Restart requested — shutting down for restart...")
        self.stop()

    def pause(self) -> None:
        """Pause KakaoTalk message processing."""
        self._paused = True
        logger.info("Orchestrator paused")

    def resume(self) -> None:
        """Resume KakaoTalk message processing."""
        self._paused = False
        logger.info("Orchestrator resumed")
