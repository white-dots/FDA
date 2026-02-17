"""
FDA Orchestrator — the main entry point for Datacore's client automation.

Ties together all components:
- KakaoTalk reader: monitors client chat rooms for new messages
- FDA agent: classifies messages and creates task briefs with business context
- Worker agent: analyzes codebases and generates fixes
- Telegram bot: sends approval requests and handles responses
- Deployer: pushes approved changes to Azure VMs

This is the process that runs continuously on the Mac Mini.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Any, Optional
from datetime import datetime
from pathlib import Path

import anthropic

from fda.config import (
    MODEL_FDA,
    MODEL_MEETING_SUMMARY,
    STATE_DB_PATH,
    MESSAGE_BUS_PATH,
    PROJECT_ROOT,
)
from fda.state.project_state import ProjectState
from fda.comms.message_bus import MessageBus
from fda.clients.client_config import ClientManager, ClientConfig
from fda.kakaotalk.reader import KakaoTalkReader
from fda.kakaotalk.parser import KakaoMessage
from fda.worker_agent import WorkerAgent
from fda.telegram_approval import ApprovalManager, PendingApproval

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
    Main orchestrator for Datacore's FDA system.

    Runs on the Mac Mini, continuously monitoring KakaoTalk messages
    and coordinating between agents to handle client requests.
    """

    def __init__(
        self,
        clients_dir: Optional[Path] = None,
        export_dir: Optional[Path] = None,
        auto_export: bool = False,
        poll_interval_seconds: int = 60,
    ):
        """
        Initialize the orchestrator.

        Args:
            clients_dir: Directory containing client YAML configs.
            export_dir: Directory for KakaoTalk exports.
            auto_export: Whether to auto-trigger KakaoTalk exports.
            poll_interval_seconds: How often to check for new messages.
        """
        # Core components
        self.state = ProjectState(STATE_DB_PATH)
        self.message_bus = MessageBus(MESSAGE_BUS_PATH)
        self.claude = anthropic.Anthropic()

        # Client management
        self.client_manager = ClientManager(clients_dir)

        # KakaoTalk reader
        self.kakao_reader = KakaoTalkReader(
            export_dir=export_dir,
            auto_export=auto_export,
        )

        # Worker agent
        self.worker = WorkerAgent(
            client_manager=self.client_manager,
            message_bus=self.message_bus,
            db_path=str(STATE_DB_PATH),
        )

        # Telegram approval system
        self.approval_manager = ApprovalManager()
        self.approval_manager.set_handlers(
            on_approve=self._handle_approval,
            on_reject=self._handle_rejection,
        )

        # Configuration
        self.poll_interval = poll_interval_seconds
        self._running = False
        self._paused = False

        # Restore last-checked timestamps from state
        self._restore_checkpoints()

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

    def classify_message(self, message: KakaoMessage, client: ClientConfig) -> str:
        """
        Classify an incoming KakaoTalk message.

        Args:
            message: The parsed message.
            client: Client context.

        Returns:
            Category: TASK_REQUEST, QUESTION, INFORMATION, or GREETING.
        """
        try:
            response = self.claude.messages.create(
                model=MODEL_FDA,
                max_tokens=50,
                system=MESSAGE_CLASSIFIER_PROMPT,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Client: {client.name}\n"
                        f"Sender: {message.sender}\n"
                        f"Message: {message.text}"
                    ),
                }],
            )
            category = response.content[0].text.strip().upper()

            if category in ("TASK_REQUEST", "QUESTION", "INFORMATION", "GREETING"):
                return category

            # Default to INFORMATION if classification is unclear
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

        Args:
            messages: Recent messages from the client (may be multiple
                      related messages).
            client: Client configuration with business context.

        Returns:
            Detailed task brief string.
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
            response = self.claude.messages.create(
                model=MODEL_MEETING_SUMMARY,  # Use Sonnet for quality
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.error(f"Error creating task brief: {e}")
            # Fallback: just forward the messages
            return f"Client {client.name} request:\n{messages_text}"

    def process_new_messages(self) -> None:
        """
        Check all client chat rooms for new messages and process them.

        This is the main polling loop body.
        """
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
                    # TODO: Auto-answer questions using client context
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
        # Create task brief
        task_brief = self.create_task_brief(messages, client)

        # Log the task
        task_id = self.state.add_task(
            title=f"[{client.name}] {messages[0].text[:100]}",
            description=task_brief,
            owner="worker",
            priority="medium",
        )
        logger.info(f"Task created: {task_id} for {client.name}")

        # Send to Worker for analysis
        logger.info(f"Sending task to Worker for {client.name}...")
        fix_result = self.worker.analyze_and_fix(
            client_id=client.client_id,
            task_brief=task_brief,
        )

        if fix_result.get("success"):
            # Queue for user approval via Telegram
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

            # Send to Telegram
            self._send_approval_to_telegram(approval)

            # Update task status
            self.state.update_task(task_id, status="awaiting_approval")
        else:
            error = fix_result.get("error", "Unknown error")
            logger.error(f"Worker failed for {client.name}: {error}")

            # Notify user about the failure
            self._send_telegram_notification(
                f"⚠️ Failed to generate fix for {client.name}\n\n"
                f"Request: {messages[0].text[:200]}\n"
                f"Error: {error}\n\n"
                "You may need to handle this one manually."
            )

            self.state.update_task(task_id, status="blocked")

    def _send_approval_to_telegram(self, approval: PendingApproval) -> None:
        """Send an approval request to the user via Telegram."""
        message = approval.format_telegram_message()
        self._send_telegram_notification(message)

    def _send_telegram_notification(self, text: str) -> None:
        """
        Send a notification to the user via Telegram.

        Uses the stored bot token and chat ID.
        """
        try:
            import telegram

            bot_token = self.state.get_context("telegram_bot_token")
            if not bot_token:
                import os
                from fda.config import TELEGRAM_BOT_TOKEN_ENV
                bot_token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)

            if not bot_token:
                logger.error("No Telegram bot token configured")
                return

            users = self.state.get_telegram_users(active_only=True)
            if not users:
                logger.warning("No active Telegram users to notify")
                return

            bot = telegram.Bot(token=bot_token)

            for user in users:
                try:
                    asyncio.get_event_loop().run_until_complete(
                        bot.send_message(
                            chat_id=user["chat_id"],
                            text=text,
                            parse_mode="Markdown",
                        )
                    )
                except RuntimeError:
                    # No event loop running, create one
                    loop = asyncio.new_event_loop()
                    loop.run_until_complete(
                        bot.send_message(
                            chat_id=user["chat_id"],
                            text=text,
                            parse_mode="Markdown",
                        )
                    )
                    loop.close()

        except ImportError:
            logger.error("python-telegram-bot not installed")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    async def _handle_approval(self, approval: PendingApproval) -> None:
        """Called when user approves a code change."""
        logger.info(f"Deploying approved changes for {approval.client_name}...")

        result = self.worker.deploy_approved_changes(
            client_id=approval.client_id,
            file_changes=approval.file_changes,
        )

        if result.success:
            self._send_telegram_notification(
                f"✅ Deployed to {approval.client_name}\n\n"
                f"{result.summary()}"
            )

            # Log decision
            self.state.add_decision(
                title=f"Deployed fix for {approval.client_name}",
                rationale=approval.explanation,
                decision_maker="john (approved via telegram)",
                impact=f"Files changed: {', '.join(approval.file_changes.keys())}",
            )
        else:
            self._send_telegram_notification(
                f"❌ Deployment FAILED for {approval.client_name}\n\n"
                f"{result.summary()}\n\n"
                "You may need to fix this manually."
            )

    async def _handle_rejection(self, approval: PendingApproval, reason: str) -> None:
        """Called when user rejects a code change."""
        logger.info(
            f"Change rejected for {approval.client_name}: {reason}"
        )

        # Log the rejection
        self.state.add_decision(
            title=f"Rejected fix for {approval.client_name}",
            rationale=f"Rejection reason: {reason}",
            decision_maker="john (rejected via telegram)",
            impact="No changes deployed",
        )

    def run(self) -> None:
        """
        Run the orchestrator — main entry point.

        Starts the KakaoTalk polling loop and Telegram bot in parallel.
        """
        logger.info("=" * 60)
        logger.info("FDA Orchestrator starting...")
        logger.info(f"Clients loaded: {len(self.client_manager.list_clients())}")
        logger.info(f"Poll interval: {self.poll_interval}s")
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

        # Start Worker agent in a separate thread
        worker_thread = threading.Thread(
            target=self.worker.run_event_loop,
            daemon=True,
            name="worker-agent",
        )
        worker_thread.start()

        # Main polling loop
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
        """Pause message processing."""
        self._paused = True
        logger.info("Orchestrator paused")

    def resume(self) -> None:
        """Resume message processing."""
        self._paused = False
        logger.info("Orchestrator resumed")
