"""
Slack Bot integration for FDA system.

Provides a Slack interface for querying the FDA agent and receiving
proactive notifications. Uses Socket Mode (no public URL required).

Requires:
    pip install slack-bolt
"""

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timedelta

from fda.base_agent import BaseAgent
from fda.config import (
    SLACK_BOT_TOKEN_ENV,
    SLACK_APP_TOKEN_ENV,
    SLACK_CHANNEL_ID_ENV,
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
# Intent detection — keyword-based routing for CLI fallback
# ---------------------------------------------------------------------------

INTENT_PATTERNS: dict[str, list[str]] = {
    "kakao": ["kakao", "카카오", "chat room", "client said", "client message", "채팅"],
    "tasks": ["task", "to-do", "todo", "work on", "backlog", "blocked", "할일", "작업"],
    "calendar": ["calendar", "meeting", "schedule", "agenda", "일정", "미팅", "회의"],
    "journal": ["journal", "note", "wrote", "logged", "recorded", "일지", "노트"],
    "alerts": ["alert", "critical", "warning", "urgent", "알림"],
}


def _detect_intents(question: str) -> list[str]:
    """Detect which data sources a question needs."""
    q_lower = question.lower()
    matched = [
        intent
        for intent, keywords in INTENT_PATTERNS.items()
        if any(kw in q_lower for kw in keywords)
    ]
    return matched or ["general"]


SLACK_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent), a personal AI assistant running on the user's computer, responding via Slack.

You are NOT a project management tool. You are a general-purpose personal assistant that helps the user manage their daily work and life.

Your scope is the user's entire work environment:
- Their calendar and meetings
- Their tasks and to-do items
- Their notes and journal entries
- Client VMs and deployed services (via SSH)
- Anything they need help tracking or remembering

TOOL STRATEGY — choose the right tool for the job:
- *run_local_command*: For quick shell commands on this Mac Mini (ls, cat, grep, find, ps, etc.). Fast and direct. Use this FIRST for most local queries — listing directories, reading files, searching code, checking processes. ALWAYS prefer this over run_local_task for simple questions.
- *run_local_task*: For complex local code changes requiring full analysis pipeline. ONLY use when you need to modify code, not just read or query.
- *run_remote_command*: For quick shell commands on client VMs (ls, cat, grep, airflow CLI, systemctl, etc.). Fast and direct. Use this FIRST for most VM queries.
- *run_remote_task*: For complex code analysis on remote VMs requiring reading + understanding multiple source files. Only use when you need to analyze/modify code, not just check status.
- *search_journal*: For recalling past work. All remote/local worker results are journaled — investigations, code changes, deployments, errors. Ask the journal before re-running expensive tasks.

When investigating a problem, prefer multiple small command calls (local or remote) over the heavy task pipeline. You can run ls, cat, grep, tail logs, check service status — just like working in a terminal. Only escalate to run_*_task when you actually need code analysis or modifications.

Keep responses concise:
- Short paragraphs
- Bullet points for lists
- Essential information only

Use Slack-compatible formatting:
- *bold* for emphasis (not **bold**)
- _italic_ for secondary info
- `code` for technical terms
- > for quotes

Be warm, helpful, and conversational - like a skilled executive assistant who knows the user well.
"""


class SlackBotAgent(BaseAgent):
    """
    Slack Bot Agent for FDA system.

    Uses Socket Mode for real-time messaging without a public URL.
    Supports the same agentic tool-use as the Telegram bot.
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        app_token: Optional[str] = None,
        channel_id: Optional[str] = None,
        project_state_path: Optional[Path] = None,
        local_task_dispatch: Optional[Any] = None,
        remote_task_dispatch: Optional[Any] = None,
        remote_command_dispatch: Optional[Any] = None,
        local_command_dispatch: Optional[Any] = None,
        local_organize_dispatch: Optional[Any] = None,
        approval_manager: Optional[Any] = None,
        restart_callback: Optional[Any] = None,
    ):
        """
        Initialize the Slack Bot Agent.

        Args:
            bot_token: Slack Bot User OAuth Token (xoxb-...).
            app_token: Slack App-Level Token for Socket Mode (xapp-...).
            channel_id: Optional channel ID to restrict the bot to.
            project_state_path: Path to the project state database.
            local_task_dispatch: Optional callback(task_brief, project_path) -> result dict.
                                 This is orchestrator._handle_local_task_request.
            remote_task_dispatch: Optional callback(task_brief, client_id) -> result dict.
                                  This is orchestrator._handle_remote_task_request.
            remote_command_dispatch: Optional callback(command, client_id, cwd) -> result dict.
                                     This is orchestrator._handle_remote_command.
            local_command_dispatch: Optional callback(command, cwd) -> result dict.
                                    This is orchestrator._handle_local_command.
            local_organize_dispatch: Optional callback(target_path, instructions) -> result dict.
                                      This is orchestrator._handle_local_organize_request.
            approval_manager: Optional ApprovalManager for approve/reject commands.
            restart_callback: Optional callback() to restart FDA.
        """
        super().__init__(
            name="SlackBot",
            model=MODEL_FDA,
            system_prompt=SLACK_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

        self.bot_token = bot_token or os.environ.get(SLACK_BOT_TOKEN_ENV)
        self.app_token = app_token or os.environ.get(SLACK_APP_TOKEN_ENV)
        self.channel_id = channel_id or os.environ.get(SLACK_CHANNEL_ID_ENV)
        self._local_task_dispatch = local_task_dispatch
        self._remote_task_dispatch = remote_task_dispatch
        self._remote_command_dispatch = remote_command_dispatch
        self._local_command_dispatch = local_command_dispatch
        self._local_organize_dispatch = local_organize_dispatch
        self._approval_manager = approval_manager
        self._restart_callback = restart_callback

        if not self.bot_token:
            raise ValueError(
                f"Slack bot token required. Set {SLACK_BOT_TOKEN_ENV} "
                "environment variable or pass bot_token parameter."
            )
        if not self.app_token:
            raise ValueError(
                f"Slack app token required for Socket Mode. Set {SLACK_APP_TOKEN_ENV} "
                "environment variable or pass app_token parameter."
            )

        self._app = None
        self._handler = None
        self._bot_user_id: Optional[str] = None

        # Current message context — set during _process_message so tools
        # can post intermediate status updates to the Slack thread.
        self._current_say: Optional[Any] = None
        self._current_client: Optional[Any] = None
        self._current_channel: Optional[str] = None
        self._current_thread_ts: Optional[str] = None

    # ------------------------------------------------------------------
    # Slack app setup
    # ------------------------------------------------------------------

    def _get_application(self) -> Any:
        """Get or create the Slack Bolt application."""
        if self._app is not None:
            return self._app

        try:
            from slack_bolt import App
        except ImportError:
            raise ImportError(
                "slack-bolt is required for SlackBotAgent. "
                "Install it with: pip install slack-bolt"
            )

        self._app = App(token=self.bot_token)

        # Register event handlers
        self._app.event("message")(self._on_message)
        self._app.event("app_mention")(self._on_app_mention)

        return self._app

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_message(self, event: dict, say: Any, client: Any) -> None:
        """Handle plain messages in channels/DMs."""
        logger.info(
            f"[SlackBot] _on_message: channel={event.get('channel')}, "
            f"user={event.get('user')}, bot_id={event.get('bot_id')}, "
            f"subtype={event.get('subtype')}, text={event.get('text', '')[:50]!r}"
        )
        # Ignore bot messages and message edits
        if event.get("bot_id") or event.get("subtype"):
            return

        # Channel filter — only respond in the configured channel
        channel = event.get("channel", "")
        if self.channel_id and channel != self.channel_id:
            return

        text = event.get("text", "").strip()
        if not text:
            return

        # Strip bot mention if present (e.g., "<@U123> hello" → "hello")
        if self._bot_user_id:
            text = re.sub(rf"<@{self._bot_user_id}>\s*", "", text).strip()

        if not text:
            return

        user_id = event.get("user", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        # Build a durable `say` wrapper that uses the raw API client
        # instead of bolt's request-scoped `say` callback, which may
        # become invalid after the event handler returns.
        def _say(text: str = "", thread_ts: str = thread_ts, **kwargs):
            client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, **kwargs)

        # Dispatch to a thread so the event handler returns immediately.
        # This prevents long-running tool calls from blocking bolt's
        # listener and causing subsequent messages to time out.
        threading.Thread(
            target=self._process_message_safe,
            args=(text, channel, user_id, thread_ts, _say, client, event),
            daemon=True,
        ).start()

    def _on_app_mention(self, event: dict, say: Any, client: Any) -> None:
        """Handle @mentions of the bot."""
        text = event.get("text", "").strip()
        if not text:
            return

        # Strip the mention
        if self._bot_user_id:
            text = re.sub(rf"<@{self._bot_user_id}>\s*", "", text).strip()

        if not text:
            return

        channel = event.get("channel", "")
        user_id = event.get("user", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        def _say(text: str = "", thread_ts: str = thread_ts, **kwargs):
            client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts, **kwargs)

        threading.Thread(
            target=self._process_message_safe,
            args=(text, channel, user_id, thread_ts, _say, client, event),
            daemon=True,
        ).start()

    def _process_message_safe(self, *args) -> None:
        """Wrapper around _process_message that catches all exceptions.

        Daemon threads die silently on unhandled exceptions — this
        ensures errors are always logged.
        """
        try:
            self._process_message(*args)
        except Exception as e:
            logger.error(f"[SlackBot] Unhandled error in message thread: {e}", exc_info=True)

    def _process_message(
        self,
        text: str,
        channel: str,
        user_id: str,
        thread_ts: str,
        say: Any,
        client: Any,
        event: dict = None,
    ) -> None:
        """Process an incoming message — commands or questions."""
        # Store context so tools can post intermediate updates
        self._current_say = say
        self._current_client = client
        self._current_channel = channel
        self._current_thread_ts = thread_ts

        # Resolve username
        username = self._get_username(client, user_id)

        # Handle command prefixes
        if text == "!help":
            self._handle_help(say, thread_ts)
            return
        if text == "!status":
            self._handle_status(say, thread_ts)
            return
        if text.startswith("!tasks"):
            self._handle_tasks(text, say, thread_ts)
            return
        if text == "!alerts":
            self._handle_alerts(say, thread_ts)
            return
        if text.startswith("!organize"):
            self._handle_organize(text, channel, say, thread_ts, client)
            return
        if text.startswith("!local"):
            self._handle_local(text, channel, say, thread_ts, client)
            return
        if text.startswith("!approve"):
            self._handle_approve(text, say, thread_ts)
            return
        if text.startswith("!reject"):
            self._handle_reject(text, say, thread_ts)
            return
        if text == "!pending":
            self._handle_pending(say, thread_ts)
            return
        if text.startswith("!details"):
            self._handle_details(text, say, thread_ts)
            return
        if text == "!restart":
            self._handle_restart(say, thread_ts)
            return

        # Regular question — add thinking reaction
        try:
            client.reactions_add(
                channel=channel,
                name="hourglass_flowing_sand",
                timestamp=thread_ts,
            )
        except Exception:
            pass

        try:
            # Extract file attachments (images, PDFs, text files)
            attachment_blocks = self._extract_slack_attachments(event or {}, client)

            # Save user message
            self.state.add_conversation_message(
                channel_id=channel,
                role="user",
                content=text,
                source="slack",
                username=username,
            )

            # Check if we'll use the streaming path (API backend)
            from fda.claude_backend import AnthropicAPIBackend
            is_streaming = isinstance(self.backend, AnthropicAPIBackend)

            # Get answer — streaming path handles its own message posting
            response = self._answer_question(
                text, chat_id=channel, attachment_blocks=attachment_blocks,
            )

            # Save assistant response
            self.state.add_conversation_message(
                channel_id=channel,
                role="assistant",
                content=response,
                source="slack",
            )

            # Non-streaming path: post the response the old way
            if not is_streaming:
                formatted = self._to_slack_markdown(response)
                for chunk in self._split_message(formatted):
                    say(text=chunk, thread_ts=thread_ts)

            # Update reaction
            try:
                client.reactions_remove(
                    channel=channel,
                    name="hourglass_flowing_sand",
                    timestamp=thread_ts,
                )
                client.reactions_add(
                    channel=channel,
                    name="white_check_mark",
                    timestamp=thread_ts,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[SlackBot] Error answering message: {e}")
            say(
                text="Sorry, I encountered an error processing your message.",
                thread_ts=thread_ts,
            )
            try:
                client.reactions_remove(
                    channel=channel,
                    name="hourglass_flowing_sand",
                    timestamp=thread_ts,
                )
                client.reactions_add(
                    channel=channel, name="x", timestamp=thread_ts,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _handle_help(self, say: Any, thread_ts: str) -> None:
        """Show help message."""
        say(
            text=(
                "*FDA — Your Personal AI Assistant*\n\n"
                "Just send a message and I'll help. Examples:\n"
                "- _What was in kakao chat yesterday?_\n"
                "- _What tasks are blocked?_\n"
                "- _What's on my calendar today?_\n\n"
                "*Commands:*\n"
                "`!status` — Task and alert summary\n"
                "`!tasks` — List your tasks\n"
                "`!alerts` — Show pending alerts\n"
                "`!local <task>` — Dispatch task to worker agent (VM/codebase)\n"
                "`!organize <path> [instructions]` — Sort files in a directory\n"
                "`!pending` — List pending approvals\n"
                "`!approve <id>` — Approve a code change\n"
                "`!reject <id> [reason]` — Reject a code change\n"
                "`!details <id>` — Show full diff for an approval\n"
                "`!restart` — Restart the FDA system\n"
                "`!help` — This message"
            ),
            thread_ts=thread_ts,
        )

    def _handle_status(self, say: Any, thread_ts: str) -> None:
        """Show status summary."""
        try:
            user_name = self.state.get_context("user_name") or "there"
            tasks = self.state.get_tasks()
            alerts = self.state.get_alerts(acknowledged=False)

            status_counts: dict[str, int] = {}
            for task in tasks:
                s = task.get("status", "unknown")
                status_counts[s] = status_counts.get(s, 0) + 1

            parts = [f"Hey {user_name}, here's your status:\n"]

            if tasks:
                parts.append(f"*Tasks:* {len(tasks)} total")
                for status, count in sorted(status_counts.items()):
                    parts.append(f"  {status}: {count}")
            else:
                parts.append("*Tasks:* None tracked yet")

            if alerts:
                parts.append(f"\n*Alerts:* {len(alerts)} unacknowledged")

            say(text="\n".join(parts), thread_ts=thread_ts)

        except Exception as e:
            logger.error(f"[SlackBot] Error getting status: {e}")
            say(text="Sorry, I had trouble getting your status.", thread_ts=thread_ts)

    def _handle_tasks(self, text: str, say: Any, thread_ts: str) -> None:
        """List tasks, optionally filtered by status."""
        try:
            # Parse optional status filter: "!tasks blocked"
            parts = text.split(maxsplit=1)
            status_filter = parts[1].strip() if len(parts) > 1 else None

            tasks = self.state.get_tasks(status=status_filter)

            if not tasks:
                msg = "No tasks found"
                if status_filter:
                    msg += f" with status `{status_filter}`"
                say(text=msg + ".", thread_ts=thread_ts)
                return

            lines = [f"*Tasks* ({len(tasks)} total)\n"]
            for task in tasks[:10]:
                priority_marker = " :bangbang:" if task.get("priority") == "high" else ""
                lines.append(
                    f"- `{task.get('status', '?')}` {task.get('title', 'Untitled')}{priority_marker}"
                )
            if len(tasks) > 10:
                lines.append(f"\n... and {len(tasks) - 10} more")

            say(text="\n".join(lines), thread_ts=thread_ts)

        except Exception as e:
            logger.error(f"[SlackBot] Error listing tasks: {e}")
            say(text="Error retrieving tasks.", thread_ts=thread_ts)

    def _handle_alerts(self, say: Any, thread_ts: str) -> None:
        """Show unacknowledged alerts."""
        try:
            alerts = self.state.get_alerts(acknowledged=False)

            if not alerts:
                say(text="No unacknowledged alerts.", thread_ts=thread_ts)
                return

            lines = [f"*Alerts* ({len(alerts)} unacknowledged)\n"]
            for alert in alerts[:10]:
                level = alert.get("level", "info")
                lines.append(f"- `{level}` {alert.get('message')}")

            say(text="\n".join(lines), thread_ts=thread_ts)

        except Exception as e:
            logger.error(f"[SlackBot] Error listing alerts: {e}")
            say(text="Error retrieving alerts.", thread_ts=thread_ts)

    def _handle_local(self, text: str, channel: str, say: Any, thread_ts: str, client: Any) -> None:
        """Handle !local <task> — dispatch to local worker agent."""
        task = text[len("!local"):].strip()
        if not task:
            say(
                text=(
                    "Usage: `!local <task description>`\n\n"
                    "Examples:\n"
                    "- `!local check if Amazon SES is configured`\n"
                    "- `!local list running services on the VM`\n"
                    "- `!local fix the health endpoint`"
                ),
                thread_ts=thread_ts,
            )
            return

        if not self._local_task_dispatch:
            say(
                text="Worker agent dispatch not available. Make sure FDA is fully started.",
                thread_ts=thread_ts,
            )
            return

        say(
            text=f":house: Analyzing local codebase...\n\n*Task:* {task}\n\nThis may take a moment (file scan + Claude analysis).",
            thread_ts=thread_ts,
        )

        try:
            # Add thinking reaction
            try:
                client.reactions_add(
                    channel=channel,
                    name="gear",
                    timestamp=thread_ts,
                )
            except Exception:
                pass

            progress_cb = self._make_progress_callback(say, thread_ts)
            result = self._local_task_dispatch(task, None, progress_callback=progress_cb)

            if result.get("success"):
                # Investigation task — just show the findings
                if result.get("investigation"):
                    analysis = result.get("analysis") or result.get("explanation", "")
                    response = f":white_check_mark: *Investigation complete*\n\n{analysis}"
                    for chunk in self._split_message(response):
                        say(text=chunk, thread_ts=thread_ts)
                else:
                    # Code change task — show approval info
                    files = ", ".join(result.get("files", []))
                    explanation = result.get("explanation", "")[:500]
                    response = (
                        f":white_check_mark: Analysis complete! Approval queued.\n\n"
                        f"*What changed:* {explanation}\n"
                        f"*Files:* {files}\n"
                        f"*Approval ID:* `{result.get('approval_id', '?')}`\n\n"
                        "Check Telegram for the approval request, or use /pending."
                    )
                    diff = result.get("diff", "")
                    if diff:
                        diff_preview = diff[:800]
                        response += f"\n```\n{diff_preview}\n```"

                    for chunk in self._split_message(response):
                        say(text=chunk, thread_ts=thread_ts)
            else:
                error = result.get("error", "Unknown error")
                say(
                    text=f":x: Failed to generate fix.\n\nError: {error[:500]}",
                    thread_ts=thread_ts,
                )

            # Update reaction
            try:
                client.reactions_remove(channel=channel, name="gear", timestamp=thread_ts)
                client.reactions_add(
                    channel=channel,
                    name="white_check_mark" if result.get("success") else "x",
                    timestamp=thread_ts,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[SlackBot] Error in !local command: {e}")
            say(text=f":x: Error: {e}", thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # File organization command
    # ------------------------------------------------------------------

    def _handle_organize(
        self,
        text: str,
        channel: str,
        say: Any,
        thread_ts: str,
        client: Any,
    ) -> None:
        """Handle !organize [path] [instructions] — organize local files."""
        args = text[len("!organize"):].strip()
        if not args:
            say(
                text=(
                    "Usage: `!organize <path> [instructions]`\n\n"
                    "Examples:\n"
                    "- `!organize ~/Downloads`\n"
                    "- `!organize ~/Desktop sort by file type`\n"
                    "- `!organize ~/Documents/projects group related files`"
                ),
                thread_ts=thread_ts,
            )
            return

        if not self._local_organize_dispatch:
            say(
                text="File organization not available. Make sure FDA is fully started.",
                thread_ts=thread_ts,
            )
            return

        # Parse path and instructions — first token is the path
        parts = args.split(None, 1)
        target_path = os.path.expanduser(parts[0])
        instructions = parts[1] if len(parts) > 1 else ""

        say(
            text=(
                f":broom: Scanning and organizing `{target_path}`...\n\n"
                f"{('*Instructions:* ' + instructions) if instructions else ''}\n\n"
                "Git repositories will be left untouched."
            ),
            thread_ts=thread_ts,
        )

        try:
            try:
                client.reactions_add(
                    channel=channel, name="gear", timestamp=thread_ts,
                )
            except Exception:
                pass

            progress_cb = self._make_progress_callback(say, thread_ts)
            result = self._local_organize_dispatch(
                target_path, instructions, progress_callback=progress_cb,
            )

            if result.get("success"):
                moves = result.get("moves", [])
                deletions = result.get("deletions", [])
                repos_skipped = result.get("repos_skipped", [])
                summary = result.get("summary", "")

                response = f":white_check_mark: *Organization complete*\n\n"
                if moves:
                    response += f"*Moved:* {len(moves)} files\n"
                if deletions:
                    response += f"*Deleted junk:* {len(deletions)} files\n"
                if repos_skipped:
                    response += f"*Git repos skipped:* {len(repos_skipped)}\n"
                if summary:
                    response += f"\n{summary[:1500]}"

                for chunk in self._split_message(response):
                    say(text=chunk, thread_ts=thread_ts)
            else:
                error = result.get("error", "Unknown error")
                say(
                    text=f":x: Organization failed.\n\nError: {error[:500]}",
                    thread_ts=thread_ts,
                )

            try:
                client.reactions_remove(
                    channel=channel, name="gear", timestamp=thread_ts,
                )
                client.reactions_add(
                    channel=channel,
                    name="white_check_mark" if result.get("success") else "x",
                    timestamp=thread_ts,
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[SlackBot] Error in !organize command: {e}")
            say(text=f":x: Error: {e}", thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # Approval commands
    # ------------------------------------------------------------------

    def _handle_approve(self, text: str, say: Any, thread_ts: str) -> None:
        """Handle !approve <short_id>."""
        if not self._approval_manager:
            say(text="Approval system not available.", thread_ts=thread_ts)
            return

        parts = text.split(None, 1)
        if len(parts) < 2:
            say(text="Usage: `!approve <id>`\nUse `!pending` to see pending approvals.", thread_ts=thread_ts)
            return

        short_id = parts[1].strip()

        # ApprovalManager.approve() is async — run in a new event loop
        loop = asyncio.new_event_loop()
        try:
            approval = loop.run_until_complete(self._approval_manager.approve(short_id))
        finally:
            loop.close()

        if approval:
            files = ", ".join(approval.file_changes.keys())
            say(
                text=(
                    f":white_check_mark: *Approved!* Deploying changes to {approval.client_name}...\n"
                    f"*Files:* {files}\n\n"
                    "I'll notify you when deployment is complete."
                ),
                thread_ts=thread_ts,
            )
        else:
            say(text=f":x: No pending approval found with ID: `{short_id}`\nUse `!pending` to see current approvals.", thread_ts=thread_ts)

    def _handle_reject(self, text: str, say: Any, thread_ts: str) -> None:
        """Handle !reject <short_id> [reason]."""
        if not self._approval_manager:
            say(text="Approval system not available.", thread_ts=thread_ts)
            return

        parts = text.split(None, 2)
        if len(parts) < 2:
            say(text="Usage: `!reject <id> [reason]`", thread_ts=thread_ts)
            return

        short_id = parts[1].strip()
        reason = parts[2].strip() if len(parts) > 2 else "No reason given"

        loop = asyncio.new_event_loop()
        try:
            approval = loop.run_until_complete(self._approval_manager.reject(short_id, reason))
        finally:
            loop.close()

        if approval:
            say(
                text=f":no_entry_sign: *Rejected* change for {approval.client_name}.\n*Reason:* {reason}",
                thread_ts=thread_ts,
            )
        else:
            say(text=f":x: No pending approval found with ID: `{short_id}`", thread_ts=thread_ts)

    def _handle_pending(self, say: Any, thread_ts: str) -> None:
        """Handle !pending — list all pending approvals."""
        if not self._approval_manager:
            say(text="Approval system not available.", thread_ts=thread_ts)
            return

        pending = self._approval_manager.list_pending()

        if not pending:
            say(text="No pending approvals.", thread_ts=thread_ts)
            return

        lines = [f"*Pending approvals ({len(pending)}):*\n"]
        for approval in pending:
            lines.append(
                f"  `{approval.short_id}` — {approval.client_name}: "
                f"{approval.task_brief[:80]}..."
            )
        lines.append("\nUse `!approve <id>` or `!reject <id> [reason]`")

        say(text="\n".join(lines), thread_ts=thread_ts)

    def _handle_details(self, text: str, say: Any, thread_ts: str) -> None:
        """Handle !details <short_id> — show full diff."""
        if not self._approval_manager:
            say(text="Approval system not available.", thread_ts=thread_ts)
            return

        parts = text.split(None, 1)
        if len(parts) < 2:
            say(text="Usage: `!details <id>`", thread_ts=thread_ts)
            return

        short_id = parts[1].strip()
        approval = self._approval_manager.get_pending(short_id)

        if not approval:
            say(text=f":x: No pending approval found with ID: `{short_id}`", thread_ts=thread_ts)
            return

        files_str = "\n".join(f"  - {f}" for f in approval.file_changes.keys())
        response = (
            f"*Details for {approval.client_name}* (`{approval.short_id}`)\n\n"
            f"*Task:* {approval.task_brief}\n\n"
            f"*What changed:* {approval.explanation}\n\n"
            f"*Files:*\n{files_str}\n"
            f"*Confidence:* {approval.confidence}\n"
        )

        if approval.warnings:
            response += "\n*Warnings:*\n" + "\n".join(f"  - {w}" for w in approval.warnings) + "\n"

        if approval.diff:
            diff_preview = approval.diff[:2000]
            response += f"\n```\n{diff_preview}\n```"

        for chunk in self._split_message(response):
            say(text=chunk, thread_ts=thread_ts)

    def _handle_restart(self, say: Any, thread_ts: str) -> None:
        """Handle !restart — restart the FDA system."""
        if not self._restart_callback:
            say(text="Restart not available.", thread_ts=thread_ts)
            return

        say(
            text=":arrows_counterclockwise: Restarting FDA... Back in a few seconds.",
            thread_ts=thread_ts,
        )
        self._restart_callback()

    # ------------------------------------------------------------------
    # Progress callback for worker tasks
    # ------------------------------------------------------------------

    def _make_progress_callback(
        self, say: Any, thread_ts: str
    ) -> "Callable[[str], None]":
        """Create a callback that posts progress updates to a Slack thread."""
        from typing import Callable

        def _callback(msg: str) -> None:
            try:
                say(text=f":gear: {msg}", thread_ts=thread_ts)
            except Exception as e:
                logger.debug(f"[SlackBot] Progress update failed: {e}")

        return _callback

    # ------------------------------------------------------------------
    # Slack formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_slack_markdown(text: str) -> str:
        """Convert standard markdown to Slack mrkdwn."""
        # **bold** → *bold*
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        # ~~strike~~ → ~strike~
        text = re.sub(r"~~(.+?)~~", r"~\1~", text)
        return text

    @staticmethod
    def _split_message(text: str, max_len: int = 3900) -> list[str]:
        """Split a long message to fit Slack's 4000-char limit."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            # Try to split at newline
            split_at = remaining.rfind("\n", 0, max_len)
            if split_at < max_len * 0.5:
                # Fall back to space
                split_at = remaining.rfind(" ", 0, max_len)
            if split_at < max_len * 0.5:
                split_at = max_len

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()

        return chunks

    def _get_username(self, client: Any, user_id: str) -> str:
        """Resolve a Slack user ID to a display name."""
        try:
            result = client.users_info(user=user_id)
            if result["ok"]:
                profile = result["user"].get("profile", {})
                return (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or result["user"].get("name", user_id)
                )
        except Exception:
            pass
        return user_id

    # ------------------------------------------------------------------
    # KakaoTalk export helpers (shared with Telegram bot)
    # ------------------------------------------------------------------

    _KAKAO_EXPORT_DIR = Path.home() / "Documents" / "fda-exports" / "kakaotalk"

    def _get_kakao_messages(self, question: str) -> str:
        """Read KakaoTalk export and return messages filtered by date."""
        from fda.kakaotalk.parser import KakaoTalkParser

        export_dir = self._KAKAO_EXPORT_DIR
        if not export_dir.exists():
            return "(No KakaoTalk exports found)"

        candidates = list(export_dir.glob("*.csv")) + list(export_dir.glob("*.txt"))
        if not candidates:
            return "(No KakaoTalk export files found)"

        latest = max(candidates, key=lambda p: p.stat().st_mtime)

        since = self._parse_date_hint(question)

        parser = KakaoTalkParser()
        if since:
            messages = parser.parse_and_diff(latest, since)
        else:
            messages = parser.parse_and_diff(latest, datetime.now() - timedelta(days=1))

        if not messages:
            date_str = since.strftime("%Y-%m-%d") if since else "last 24 hours"
            return f"(No KakaoTalk messages found since {date_str})"

        lines = []
        for msg in messages[-50:]:
            ts = msg.timestamp.strftime("%m/%d %H:%M")
            lines.append(f"[{ts}] {msg.sender}: {msg.text}")

        header = f"KakaoTalk messages ({len(messages)} total, showing last {len(lines)}):"
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def _parse_date_hint(question: str) -> Optional[datetime]:
        """Extract a date reference from a question."""
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

        m = re.search(r"last\s+(\d+)\s+days?", q)
        if m:
            return (now - timedelta(days=int(m.group(1)))).replace(hour=0, minute=0, second=0)

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

    _FDA_TOOLS: list[dict[str, Any]] = [
        {
            "name": "search_journal",
            "description": "Search the journal for past entries including: remote/local worker investigation results, code changes, deployments, errors, meetings, decisions, and notes. Use when the user asks about past work, previous findings, or anything previously recorded. Also check here before re-running expensive remote tasks — the answer may already be journaled.",
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
                        "description": "Date reference like 'yesterday', 'today', 'last 3 days', '2026-02-20', or Korean equivalents.",
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
            "description": "Get unacknowledged alerts and reminders.",
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
            "name": "run_remote_task",
            "description": "Dispatch a complex code analysis task to the remote worker agent. Use ONLY for tasks that require reading and understanding multiple source files (e.g., 'why is the email failing', 'refactor the DAG'). For simple queries like listing files, checking status, or running commands, use run_remote_command instead — it's much faster.",
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
                    # Build progress callback from current Slack context
                    progress_cb = None
                    if self._current_say and self._current_thread_ts:
                        progress_cb = self._make_progress_callback(
                            self._current_say, self._current_thread_ts
                        )
                    if is_remote:
                        client_id = tool_input.get("client_id")
                        result = dispatch(
                            task, client_id, progress_callback=progress_cb
                        )
                    else:
                        project_path = tool_input.get("project_path")
                        result = dispatch(
                            task, project_path, progress_callback=progress_cb
                        )

                    if result.get("success"):
                        # Investigation task — return the analysis directly
                        if result.get("investigation"):
                            analysis = result.get("analysis") or result.get("explanation", "")
                            return analysis[:3000] if analysis else "Investigation complete — no issues found."

                        # Code change task — return change summary
                        parts = []
                        if result.get("explanation"):
                            parts.append(f"Explanation: {result['explanation']}")
                        if result.get("files"):
                            parts.append(f"Files affected: {', '.join(result['files'])}")
                        if result.get("diff"):
                            diff_preview = result["diff"][:1500]
                            parts.append(f"Diff:\n{diff_preview}")
                        if result.get("approval_id"):
                            parts.append(f"Approval ID: {result['approval_id']} (check Telegram to approve/reject)")
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
                        command, client_id=client_id, cwd=cwd
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

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as e:
            logger.error(f"[SlackBot] Tool {tool_name} error: {e}")
            return f"Error executing {tool_name}: {e}"

    def _extract_slack_attachments(self, event: dict, client: Any) -> list[dict]:
        """Download Slack file attachments and build Anthropic content blocks.

        Supports images (JPEG/PNG/GIF/WebP), PDFs, and text-based source files.
        """
        content_blocks: list[dict] = []
        files = event.get("files", [])
        if not files:
            return content_blocks

        bot_token = getattr(client, "token", None) or self.bot_token
        for f in files:
            try:
                mime = f.get("mimetype", "")
                filename = f.get("name", "")
                size = f.get("size", 0)
                ext = Path(filename).suffix.lower()
                url = f.get("url_private", "")
                if not url:
                    continue

                if mime in SUPPORTED_IMAGE_TYPES:
                    if size > MAX_IMAGE_UPLOAD_MB * 1024 * 1024:
                        continue
                    req = urllib.request.Request(url)
                    req.add_header("Authorization", f"Bearer {bot_token}")
                    data = urllib.request.urlopen(req, timeout=30).read()
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(data).decode(),
                        },
                    })
                elif mime in SUPPORTED_DOC_TYPES:
                    if size > MAX_DOCUMENT_UPLOAD_MB * 1024 * 1024:
                        continue
                    req = urllib.request.Request(url)
                    req.add_header("Authorization", f"Bearer {bot_token}")
                    data = urllib.request.urlopen(req, timeout=30).read()
                    content_blocks.append({
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(data).decode(),
                        },
                    })
                elif ext in SUPPORTED_TEXT_EXTENSIONS:
                    if size > 1 * 1024 * 1024:
                        continue
                    req = urllib.request.Request(url)
                    req.add_header("Authorization", f"Bearer {bot_token}")
                    data = urllib.request.urlopen(req, timeout=30).read()
                    text_content = data.decode("utf-8", errors="replace")[:8000]
                    content_blocks.append({
                        "type": "text",
                        "text": f"[File: {filename}]\n```\n{text_content}\n```",
                    })
            except Exception as e:
                logger.warning(f"[SlackBot] Failed to process file {f.get('name')}: {e}")

        return content_blocks

    def _build_system_prompt(self) -> str:
        """Build the full system prompt with dynamic context."""
        user_name = self.state.get_context("user_name") or "the user"
        today = datetime.now().strftime("%Y-%m-%d (%A)")
        return SLACK_SYSTEM_PROMPT + f"""
Today is {today}. The user's name is {user_name}.

You have tools to look up the user's data. Use them when you need information to answer the question — don't guess. If the user asks about chats, notes, tasks, calendar, or alerts, call the appropriate tool first.

If the user sends images or files, analyze them directly. You have full vision capabilities.
"""

    def _build_conversation_messages(
        self,
        chat_id: str,
        question: str,
        attachment_blocks: list[dict] = None,
    ) -> list[dict[str, Any]]:
        """Build the messages array with recent history + current question."""
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
        return messages

    def _answer_with_tools(
        self,
        question: str,
        chat_id: str,
        attachment_blocks: list[dict] = None,
    ) -> str:
        """Answer using the API backend's streaming agentic tool-use loop.

        Streams tokens into a Slack message via chat.update for a
        real-time typing experience. Falls back to non-streaming
        if streaming is unavailable.
        """
        system = self._build_system_prompt()
        messages = self._build_conversation_messages(
            chat_id, question, attachment_blocks=attachment_blocks,
        )

        # Post an initial "Thinking..." message to stream into
        client = self._current_client
        channel = self._current_channel
        thread_ts = self._current_thread_ts
        msg_ts = None  # timestamp of the streaming message

        if client and channel and thread_ts:
            try:
                result = client.chat_postMessage(
                    channel=channel,
                    text=":hourglass_flowing_sand: Thinking...",
                    thread_ts=thread_ts,
                )
                msg_ts = result.get("ts")
            except Exception:
                pass

        # Streaming callbacks — throttled edit-in-place
        last_update = [0.0]
        accumulated = [""]

        def on_text(delta: str, snapshot: str) -> None:
            accumulated[0] = snapshot
            if not msg_ts or not client:
                return
            now = time.monotonic()
            if now - last_update[0] >= 0.8:  # throttle: ~1.2 updates/sec
                try:
                    display = self._to_slack_markdown(snapshot)
                    # Slack limit: 4000 chars — truncate live preview
                    if len(display) > 3900:
                        display = display[:3900] + "\n..."
                    client.chat_update(
                        channel=channel, ts=msg_ts, text=display,
                    )
                    last_update[0] = now
                except Exception:
                    pass

        def on_tool_start(name: str, label: str) -> None:
            if not msg_ts or not client:
                return
            try:
                display = self._to_slack_markdown(accumulated[0])
                suffix = f"\n\n:gear: _{label}_"
                client.chat_update(
                    channel=channel, ts=msg_ts,
                    text=(display + suffix)[:3900],
                )
            except Exception:
                pass

        def on_tool_end(name: str, preview: str) -> None:
            if not msg_ts or not client:
                return
            try:
                # Remove tool indicator, show text so far
                display = self._to_slack_markdown(accumulated[0])
                client.chat_update(
                    channel=channel, ts=msg_ts, text=display[:3900],
                )
            except Exception:
                pass

        # Extended thinking configuration
        thinking_config = (
            {"type": "enabled", "budget_tokens": EXTENDED_THINKING_BUDGET}
            if ENABLE_EXTENDED_THINKING else None
        )

        # Combine user-defined tools with server-side web search
        all_tools = self._FDA_TOOLS + [{"type": "web_search_20250305", "name": "web_search"}]

        # Try streaming first, fall back to non-streaming
        try:
            response = self.backend.complete_with_tools_streaming(
                system=system,
                messages=messages,
                tools=all_tools,
                tool_executor=self._execute_tool,
                model=self.model,
                max_tokens=4096,
                max_iterations=10,
                temperature=0.7,
                on_text=on_text,
                on_tool_start=on_tool_start,
                on_tool_end=on_tool_end,
                thinking=thinking_config,
            )
        except Exception as e:
            logger.warning(f"[SlackBot] Streaming failed, falling back: {e}")
            response = self.backend.complete_with_tools(
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

        # Final polished edit (or delete streaming msg + post as new messages)
        formatted = self._to_slack_markdown(response)
        if msg_ts and client:
            try:
                chunks = self._split_message(formatted)
                # Update the streaming message with final first chunk
                client.chat_update(
                    channel=channel, ts=msg_ts, text=chunks[0],
                )
                # Post overflow chunks as new messages
                for chunk in chunks[1:]:
                    client.chat_postMessage(
                        channel=channel, text=chunk, thread_ts=thread_ts,
                    )
            except Exception:
                pass

        return response

    # ------------------------------------------------------------------
    # Answer question — dispatches to agentic or intent-based routing
    # ------------------------------------------------------------------

    def _answer_question(
        self,
        question: str,
        chat_id: str = "slack",
        attachment_blocks: list[dict] = None,
    ) -> str:
        """Answer a question using the best available backend."""
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

    # ------------------------------------------------------------------
    # Notification methods
    # ------------------------------------------------------------------

    def send_notification(self, channel: str, text: str, thread_ts: Optional[str] = None) -> bool:
        """Send a notification message to a Slack channel."""
        try:
            app = self._get_application()
            app.client.chat_postMessage(
                channel=channel,
                text=text,
                thread_ts=thread_ts,
            )
            return True
        except Exception as e:
            logger.error(f"[SlackBot] Failed to send message to {channel}: {e}")
            return False

    def broadcast_to_channel(self, text: str) -> bool:
        """Send a message to the configured channel."""
        if not self.channel_id:
            logger.warning("[SlackBot] No channel configured for broadcast")
            return False
        return self.send_notification(self.channel_id, text)

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def run_event_loop(self) -> None:
        """
        Run the Slack bot via Socket Mode with auto-reconnect.

        Uses the ``websockets``-based Socket Mode handler rather than
        the builtin one.  The builtin handler has a known issue where
        its WebSocket silently dies and ``is_connected()`` still
        returns True (it only checks ``self.sock is not None``).

        The ``websockets`` library has proper ping/pong keepalives and
        reliable close detection.  We run it in a dedicated asyncio
        event loop inside this thread.

        Blocks until interrupted.
        """
        logger.info("[SlackBot] Starting Socket Mode...")

        app = self._get_application()

        # Resolve bot user ID for mention stripping
        try:
            auth = app.client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info(f"[SlackBot] Bot user ID: {self._bot_user_id}")
        except Exception as e:
            logger.warning(f"[SlackBot] Could not resolve bot user ID: {e}")

        channel_info = f" (channel: {self.channel_id})" if self.channel_id else " (all channels)"

        # Use the websockets-based handler for reliable connections.
        # The builtin (socket-based) handler has a known issue where its
        # WebSocket silently dies while is_connected() still returns True.
        try:
            from slack_bolt.adapter.socket_mode.websockets import (
                SocketModeHandler as WsSocketModeHandler,
            )
            from slack_sdk.web.async_client import AsyncWebClient

            use_websockets = True
        except ImportError:
            logger.warning(
                "[SlackBot] websockets package not installed, "
                "falling back to builtin handler"
            )
            use_websockets = False

        if not use_websockets:
            from slack_bolt.adapter.socket_mode import SocketModeHandler  # type: ignore[assignment]

            self._run_event_loop_builtin(app, SocketModeHandler, channel_info)
            return

        # Outer reconnection loop — handles total connection failures
        import time as _time

        while True:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def _run():
                    # The websockets SocketModeClient.__init__ calls
                    # asyncio.ensure_future(), so the handler must be
                    # created inside an async context.  It also requires
                    # an AsyncWebClient (the sync App.client won't work).
                    async_web_client = AsyncWebClient(token=self.bot_token)
                    self._handler = WsSocketModeHandler(
                        app,
                        self.app_token,
                        web_client=async_web_client,
                    )
                    logger.info(f"[SlackBot] Socket Mode started{channel_info}")
                    await self._handler.connect_async()
                    logger.info("[SlackBot] Socket Mode connected ✓")
                    # Block forever — the websockets handler's internal
                    # monitor task handles reconnection automatically.
                    await asyncio.sleep(float("inf"))

                loop.run_until_complete(_run())

            except KeyboardInterrupt:
                logger.info("[SlackBot] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[SlackBot] Socket Mode error: {e}", exc_info=True)
                logger.info("[SlackBot] Retrying Socket Mode in 5 seconds...")
                _time.sleep(5)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        logger.info("[SlackBot] Socket Mode stopped")

    # ------------------------------------------------------------------
    # Fallback: builtin handler (if websockets not available)
    # ------------------------------------------------------------------

    def _run_event_loop_builtin(self, app, SocketModeHandler, channel_info: str) -> None:
        """Fallback event loop using the builtin Socket Mode handler."""
        import time as _time

        while True:
            try:
                self._handler = SocketModeHandler(app, self.app_token)
                logger.info(f"[SlackBot] Socket Mode started (builtin){channel_info}")
                self._handler.connect()
                logger.info("[SlackBot] Socket Mode connected ✓")

                consecutive_failures = 0
                while True:
                    _time.sleep(30)
                    sm_client = self._handler.client
                    sdk_connected = sm_client.is_connected()

                    if not sdk_connected:
                        consecutive_failures += 1
                        logger.warning(
                            f"[SlackBot] Health check failed "
                            f"(connected={sdk_connected}, "
                            f"failures={consecutive_failures})"
                        )
                        if consecutive_failures >= 2:
                            logger.warning("[SlackBot] Reconnecting...")
                            try:
                                sm_client.disconnect()
                            except Exception:
                                pass
                            break
                    else:
                        if consecutive_failures > 0:
                            logger.info("[SlackBot] Health check recovered")
                        consecutive_failures = 0

            except KeyboardInterrupt:
                logger.info("[SlackBot] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[SlackBot] Socket Mode error: {e}", exc_info=True)
                logger.info("[SlackBot] Retrying in 5 seconds...")
                _time.sleep(5)

        logger.info("[SlackBot] Socket Mode stopped")


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


def get_bot_tokens() -> Optional[tuple[str, str]]:
    """Get Slack bot and app tokens from environment or stored config.

    Returns:
        Tuple of (bot_token, app_token) or None if not configured.
    """
    bot_token = os.environ.get(SLACK_BOT_TOKEN_ENV)
    app_token = os.environ.get(SLACK_APP_TOKEN_ENV)

    if bot_token and app_token:
        return (bot_token, app_token)

    # Try stored config
    try:
        state = ProjectState()
        bot_token = bot_token or state.get_context("slack_bot_token")
        app_token = app_token or state.get_context("slack_app_token")
        if bot_token and app_token:
            return (bot_token, app_token)
    except Exception:
        pass

    return None
