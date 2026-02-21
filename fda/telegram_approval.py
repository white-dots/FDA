"""
Telegram approval workflow for FDA system.

Extends the Telegram bot with approval capabilities:
- Sends code change summaries to the user
- Handles /approve and /reject commands
- Manages a queue of pending approvals
- Triggers deployment after approval

This module is designed to be integrated with the existing TelegramBotAgent.
"""

import json
import logging
import uuid
from typing import Any, Optional, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    """A code change awaiting user approval."""
    approval_id: str
    client_id: str
    client_name: str
    task_brief: str
    explanation: str
    diff: str
    file_changes: dict[str, str]
    confidence: str
    warnings: list[str]
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # Local worker support (backward-compatible defaults)
    project_path: Optional[str] = None
    is_local: bool = False

    @property
    def short_id(self) -> str:
        """Short ID for use in Telegram commands."""
        return self.approval_id[:8]

    def format_telegram_message(self) -> str:
        """Format the approval request for Telegram (mobile-friendly)."""
        # Truncate diff for readability on mobile
        diff_preview = self.diff
        if len(diff_preview) > 1500:
            diff_preview = diff_preview[:1500] + "\n... [truncated]"

        warnings_str = ""
        if self.warnings:
            warnings_str = "\n⚠️ Warnings:\n" + "\n".join(
                f"  - {w}" for w in self.warnings
            )

        files_str = ", ".join(self.file_changes.keys())

        # Distinguish local vs remote targets
        if self.is_local:
            from pathlib import Path
            project_name = Path(self.project_path).name if self.project_path else "local"
            target_label = f"🏠 LOCAL ({project_name})"
        else:
            target_label = self.client_name

        return f"""🔧 *Code change for {target_label}*

*Request:* {self.task_brief[:200]}

*What changed:* {self.explanation[:300]}

*Files:* {files_str}
*Confidence:* {self.confidence}
{warnings_str}

```
{diff_preview}
```

Reply with:
/approve {self.short_id}
/reject {self.short_id} [reason]
/details {self.short_id}
"""


class ApprovalManager:
    """
    Manages the approval queue for code changes.

    Stores pending approvals and handles approve/reject actions.
    """

    def __init__(self):
        self._pending: dict[str, PendingApproval] = {}
        self._on_approve: Optional[Callable[[PendingApproval], Awaitable[None]]] = None
        self._on_reject: Optional[Callable[[PendingApproval, str], Awaitable[None]]] = None

    def set_handlers(
        self,
        on_approve: Callable[[PendingApproval], Awaitable[None]],
        on_reject: Callable[[PendingApproval, str], Awaitable[None]],
    ) -> None:
        """Set callback handlers for approve/reject actions."""
        self._on_approve = on_approve
        self._on_reject = on_reject

    def add_approval(
        self,
        client_id: str,
        client_name: str,
        task_brief: str,
        explanation: str,
        diff: str,
        file_changes: dict[str, str],
        confidence: str = "medium",
        warnings: Optional[list[str]] = None,
        project_path: Optional[str] = None,
        is_local: bool = False,
    ) -> PendingApproval:
        """
        Add a new pending approval.

        Args:
            client_id: Client identifier.
            client_name: Client display name.
            task_brief: Original task description.
            explanation: What was changed and why.
            diff: Unified diff of changes.
            file_changes: Dict of filepath -> new content.
            confidence: Agent's confidence level.
            warnings: Any concerns.
            project_path: Local project path (for local worker tasks).
            is_local: Whether this is a local worker task.

        Returns:
            The created PendingApproval.
        """
        approval_id = uuid.uuid4().hex
        approval = PendingApproval(
            approval_id=approval_id,
            client_id=client_id,
            client_name=client_name,
            task_brief=task_brief,
            explanation=explanation,
            diff=diff,
            file_changes=file_changes,
            confidence=confidence,
            warnings=warnings or [],
            project_path=project_path,
            is_local=is_local,
        )

        self._pending[approval_id] = approval
        logger.info(f"Approval queued: {approval.short_id} for {client_name}")
        return approval

    def get_pending(self, short_id: str) -> Optional[PendingApproval]:
        """Find a pending approval by its short ID."""
        for approval_id, approval in self._pending.items():
            if approval_id.startswith(short_id):
                return approval
        return None

    def list_pending(self) -> list[PendingApproval]:
        """Get all pending approvals."""
        return list(self._pending.values())

    async def approve(self, short_id: str) -> Optional[PendingApproval]:
        """
        Approve a pending change.

        Args:
            short_id: Short approval ID.

        Returns:
            The approved PendingApproval, or None if not found.
        """
        approval = self.get_pending(short_id)
        if not approval:
            return None

        del self._pending[approval.approval_id]

        if self._on_approve:
            await self._on_approve(approval)

        logger.info(f"Approval accepted: {approval.short_id} for {approval.client_name}")
        return approval

    async def reject(self, short_id: str, reason: str = "") -> Optional[PendingApproval]:
        """
        Reject a pending change.

        Args:
            short_id: Short approval ID.
            reason: Rejection reason.

        Returns:
            The rejected PendingApproval, or None if not found.
        """
        approval = self.get_pending(short_id)
        if not approval:
            return None

        del self._pending[approval.approval_id]

        if self._on_reject:
            await self._on_reject(approval, reason)

        logger.info(
            f"Approval rejected: {approval.short_id} for {approval.client_name}"
            f" reason: {reason}"
        )
        return approval


def register_approval_handlers(application: Any, approval_manager: ApprovalManager) -> None:
    """
    Register approval-related command handlers with the Telegram application.

    Call this after creating the Telegram Application to add
    /approve, /reject, /details, /pending commands.

    Args:
        application: The python-telegram-bot Application instance.
        approval_manager: The ApprovalManager instance.
    """
    from telegram.ext import CommandHandler

    async def handle_approve(update: Any, context: Any) -> None:
        """Handle /approve <short_id> command."""
        if not context.args:
            await update.message.reply_text(
                "Usage: /approve <id>\n\n"
                "Use /pending to see pending approvals."
            )
            return

        short_id = context.args[0]
        approval = await approval_manager.approve(short_id)

        if approval:
            await update.message.reply_text(
                f"✅ Approved! Deploying changes to {approval.client_name}...\n"
                f"Files: {', '.join(approval.file_changes.keys())}\n\n"
                "I'll notify you when deployment is complete."
            )
        else:
            await update.message.reply_text(
                f"❌ No pending approval found with ID: {short_id}\n"
                "Use /pending to see current approvals."
            )

    async def handle_reject(update: Any, context: Any) -> None:
        """Handle /reject <short_id> [reason] command."""
        if not context.args:
            await update.message.reply_text(
                "Usage: /reject <id> [reason]"
            )
            return

        short_id = context.args[0]
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason given"

        approval = await approval_manager.reject(short_id, reason)

        if approval:
            await update.message.reply_text(
                f"🚫 Rejected change for {approval.client_name}.\n"
                f"Reason: {reason}"
            )
        else:
            await update.message.reply_text(
                f"❌ No pending approval found with ID: {short_id}"
            )

    async def handle_details(update: Any, context: Any) -> None:
        """Handle /details <short_id> - show full diff."""
        if not context.args:
            await update.message.reply_text("Usage: /details <id>")
            return

        short_id = context.args[0]
        approval = approval_manager.get_pending(short_id)

        if approval:
            # Send full diff (may need multiple messages for long diffs)
            full_info = (
                f"📋 *Full details for {approval.client_name}*\n\n"
                f"*Task:* {approval.task_brief}\n\n"
                f"*Explanation:* {approval.explanation}\n\n"
                f"*Files changed:*\n"
            )

            for filepath in approval.file_changes:
                full_info += f"  - {filepath}\n"

            full_info += f"\n*Full diff:*\n```\n{approval.diff}\n```"

            # Telegram has a 4096 char limit per message
            if len(full_info) > 4000:
                # Split into multiple messages
                await update.message.reply_text(full_info[:4000])
                remaining = full_info[4000:]
                while remaining:
                    chunk = remaining[:4000]
                    remaining = remaining[4000:]
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(full_info)
        else:
            await update.message.reply_text(
                f"❌ No pending approval found with ID: {short_id}"
            )

    async def handle_pending(update: Any, context: Any) -> None:
        """Handle /pending - list all pending approvals."""
        pending = approval_manager.list_pending()

        if not pending:
            await update.message.reply_text("No pending approvals.")
            return

        lines = [f"📋 *Pending approvals ({len(pending)}):*\n"]
        for approval in pending:
            lines.append(
                f"  `{approval.short_id}` — {approval.client_name}: "
                f"{approval.task_brief[:80]}..."
            )

        await update.message.reply_text("\n".join(lines))

    async def handle_pause(update: Any, context: Any) -> None:
        """Handle /pause - pause processing new KakaoTalk messages."""
        # Store pause state in context.bot_data
        context.bot_data["paused"] = True
        await update.message.reply_text(
            "⏸ FDA paused. I'll stop processing new KakaoTalk messages.\n"
            "Use /resume to restart."
        )

    async def handle_resume(update: Any, context: Any) -> None:
        """Handle /resume - resume processing."""
        context.bot_data["paused"] = False
        await update.message.reply_text(
            "▶️ FDA resumed. Processing KakaoTalk messages again."
        )

    async def handle_clients(update: Any, context: Any) -> None:
        """Handle /clients - show client overview."""
        # This will be populated by FDA agent
        await update.message.reply_text(
            "Use this to show client overview. "
            "(Will be connected to ClientManager)"
        )

    # Register all handlers
    application.add_handler(CommandHandler("approve", handle_approve))
    application.add_handler(CommandHandler("reject", handle_reject))
    application.add_handler(CommandHandler("details", handle_details))
    application.add_handler(CommandHandler("pending", handle_pending))
    application.add_handler(CommandHandler("pause", handle_pause))
    application.add_handler(CommandHandler("resume", handle_resume))
    application.add_handler(CommandHandler("clients", handle_clients))


def register_local_task_handler(
    application: Any,
    dispatch_fn: Callable[[str, Optional[str]], dict[str, Any]],
) -> None:
    """
    Register /local command handler with the Telegram application.

    Allows users to dispatch tasks to the local worker agent via Telegram.
    Usage: /local <task description>

    Args:
        application: The python-telegram-bot Application instance.
        dispatch_fn: Callable(task_brief, project_path) -> result dict.
                     This is orchestrator._handle_local_task_request.
    """
    from telegram.ext import CommandHandler

    async def handle_local(update: Any, context: Any) -> None:
        """Handle /local <task description> command."""
        if not context.args:
            await update.message.reply_text(
                "Usage: /local <task description>\n\n"
                "Example:\n"
                "/local fix the health endpoint\n"
                "/local add logging to config.py\n"
                "/local refactor the journal writer"
            )
            return

        task_brief = " ".join(context.args)
        await update.message.reply_text(
            f"🏠 Analyzing local codebase...\n\n"
            f"*Task:* {task_brief}\n\n"
            "This may take a moment (file scan + Claude analysis).",
            parse_mode="Markdown",
        )

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, dispatch_fn, task_brief, None)

            if result.get("success"):
                files = ", ".join(result.get("files", []))
                await update.message.reply_text(
                    f"✅ Analysis complete! Approval queued.\n\n"
                    f"*Files:* {files}\n"
                    f"*ID:* `{result.get('approval_id', '?')}`\n\n"
                    "Check the approval message above, or use /pending.",
                    parse_mode="Markdown",
                )
            else:
                error = result.get("error", "Unknown error")
                await update.message.reply_text(
                    f"❌ Failed to generate fix.\n\n"
                    f"Error: {error[:500]}"
                )
        except Exception as e:
            logger.error(f"Error in /local command: {e}")
            await update.message.reply_text(f"❌ Error: {e}")

    application.add_handler(CommandHandler("local", handle_local))
