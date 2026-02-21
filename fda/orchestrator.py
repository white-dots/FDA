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
            )

            # Register approval command handlers (/approve, /reject, etc.)
            app = bot._get_application()
            register_approval_handlers(app, self.approval_manager)

            # Register /local command for local worker dispatch
            register_local_task_handler(app, self._handle_local_task_request)

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
                approval_manager=self.approval_manager,
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
                approval_manager=self.approval_manager,
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
        # Resolve project path
        if not project_path:
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

        if fix_result.get("success"):
            changes = fix_result.get("changes", {})

            # Investigation/query task — no code changes, just return the analysis
            if not changes:
                self.state.update_task(task_id, status="completed")
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
                client_name=f"LOCAL ({project_name})",
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
            return {
                "success": False,
                "error": error,
                "analysis": fix_result.get("analysis", ""),
            }

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
            return {
                "success": False,
                "error": error,
                "analysis": fix_result.get("analysis", ""),
            }

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
        else:
            self._send_telegram_notification(
                f"❌ Deployment FAILED for {target_name}\n\n"
                f"{summary}\n\n"
                "You may need to fix this manually."
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

        # 2. Telegram bot (with /approve, /reject, /pending commands)
        if self._enable_telegram:
            t = self._start_telegram_bot()
            if t:
                self._threads.append(t)

        # 3. Slack bot (Socket Mode)
        if self._enable_slack:
            t = self._start_slack_bot()
            if t:
                self._threads.append(t)

        # 4. Discord bot (voice meetings)
        if self._enable_discord:
            t = self._start_discord_bot()
            if t:
                self._threads.append(t)

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

        # 5. KakaoTalk polling (main thread)
        logger.info(f"Starting KakaoTalk polling (every {self.poll_interval}s)...")
        try:
            while self._running:
                try:
                    self.process_new_messages()
                except Exception as e:
                    logger.error(f"Error in polling loop: {e}", exc_info=True)

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self._running = False

        logger.info("FDA Orchestrator stopped.")

    def stop(self) -> None:
        """Stop the orchestrator."""
        self._running = False

    def pause(self) -> None:
        """Pause KakaoTalk message processing."""
        self._paused = True
        logger.info("Orchestrator paused")

    def resume(self) -> None:
        """Resume KakaoTalk message processing."""
        self._paused = False
        logger.info("Orchestrator resumed")
