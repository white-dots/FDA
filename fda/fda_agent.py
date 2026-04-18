"""
FDA (Facilitating Director Agent) implementation.

FDA is the user-facing interface of the system. It communicates with users
via Discord voice (primary) and Telegram (secondary), and delegates work
to the Worker agent for code analysis, fixes, and deployments.
"""

import logging
import time
import json
import re
import os
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

import requests
import shutil
import sys

from fda.base_agent import BaseAgent
from fda.config import (
    MODEL_FDA,
    DEFAULT_CHECK_INTERVAL_MINUTES,
    PROJECT_ROOT,
    DEFAULT_NOTETAKING_TIME,
)
from fda.outlook import OutlookCalendar
from fda.comms.message_bus import MessageTypes, Agents

logger = logging.getLogger(__name__)


FDA_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent), a personal AI assistant living on the user's computer.

You are the user-facing interface of a unified system. You work with:
- **Worker agent (remote)**: SSHes into client Azure VMs to analyze code, generate fixes, and deploy changes
- **Worker agent (local)**: Operates on the local Mac Mini filesystem for FDA's own codebase
- **Shell commands**: You can execute commands directly on client VMs (ls, cat, grep, airflow CLI, etc.)

The system also monitors:
- **KakaoTalk**: Client messages are classified and turned into tasks for the Worker
- **Outlook calendar**: Upcoming meetings are tracked; you prepare briefs with SharePoint files
- **Discord voice**: You join meetings, take notes, and answer questions in real-time

Your interfaces:
- **Slack**: Primary text interface (Socket Mode) with agentic tool-use
- **Telegram**: Q&A and code approval workflow (/approve, /reject)
- **Discord**: Voice meetings + text commands

All worker results (investigations, code changes, deployments, errors) are automatically journaled. You can search the journal to recall past findings without re-running expensive tasks.

Your personality:
- Helpful and proactive, like a skilled executive assistant
- Conversational and natural - not robotic or overly formal
- You remember context from past conversations and the journal
- You anticipate needs and offer suggestions
- You're direct and concise, but warm

When responding:
- Talk naturally, like a helpful colleague
- Don't use excessive formatting unless it helps clarity
- Be brief for simple questions, detailed when needed
- For VM queries, prefer quick shell commands over full code analysis
- Check the journal before re-running tasks that may have been done before
"""


class FDAAgent(BaseAgent):
    """
    Facilitating Director Agent - the user-facing agent.

    FDA is the primary interface for users via Discord voice and Telegram.
    It delegates code work to the Worker agent.
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

        1. Process messages from Worker agent
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

    def onboard_interactive(self, skip_profile: bool = False) -> dict[str, Any]:
        """
        Interactive 7-step onboarding wizard.

        Steps:
        1. System check — Python, packages, PROJECT_ROOT
        2. API keys — Anthropic (required), OpenAI (optional)
        3. Channel selection — Telegram, Discord, Slack inline setup
        4. Daily notetaking — choose channels for auto-summarization
        5. User profile — name, role, goals (skippable with --skip-profile)
        6. Daemon installation — launchd (macOS) / systemd (Linux)
        7. Completion summary

        Args:
            skip_profile: If True, skip the profile questions (step 5).

        Returns:
            Dictionary containing onboarding results.
        """
        print("\n" + "=" * 60)
        print("  FDA Onboarding Wizard")
        print("=" * 60)
        print("\nThis wizard will set up everything FDA needs to run.")
        print("You can press Ctrl+C at any time to cancel.\n")

        responses: dict[str, Any] = {}

        # Step 1: System Check
        self._onboard_step_system_check()

        # Step 2: API Keys
        self._onboard_step_api_keys(responses)

        # Step 3: Channel Selection + Bot Setup
        self._onboard_step_channels(responses)

        # Step 4: Daily Notetaking Channels
        self._onboard_step_notetaking(responses)

        # Step 5: User Profile (skip if --skip-profile)
        if not skip_profile:
            self._onboard_step_profile(responses)

        # Step 6: Daemon Installation
        self._onboard_step_daemon(responses)

        # Step 7: Completion Summary
        self._onboard_step_complete(responses)

        return {
            "status": "completed",
            "responses": responses,
            "timestamp": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Onboarding step helpers
    # ------------------------------------------------------------------

    def _onboard_step_system_check(self) -> None:
        """Step 1: Check system requirements and show status."""
        print("-" * 40)
        print("STEP 1/7 — SYSTEM CHECK")
        print("-" * 40)

        # Python version
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        py_ok = sys.version_info >= (3, 9)
        print(f"  Python:       {py_ver}  {'✓' if py_ok else '✗ (need 3.9+)'}")

        # Platform
        platform_name = {"darwin": "macOS", "linux": "Linux"}.get(
            sys.platform, sys.platform
        )
        print(f"  Platform:     {platform_name}")

        # Project root
        print(f"  Data dir:     {PROJECT_ROOT}")

        # Key packages
        for pkg_name in ("anthropic", "pandas", "requests"):
            try:
                mod = __import__(pkg_name)
                ver = getattr(mod, "__version__", "?")
                print(f"  {pkg_name:13s} {ver}  ✓")
            except ImportError:
                print(f"  {pkg_name:13s} not installed  ✗")

        # Claude Code CLI
        cli_path = shutil.which("claude")
        if cli_path:
            print(f"  Claude CLI:   {cli_path}  ✓")
        else:
            print("  Claude CLI:   not found (will use API)")

        print()

    def _onboard_step_api_keys(self, responses: dict[str, Any]) -> None:
        """Step 2: Configure API keys with validation."""
        print("-" * 40)
        print("STEP 2/7 — API KEYS")
        print("-" * 40)

        # --- Anthropic API Key ---
        cli_available = shutil.which("claude") is not None
        existing_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or self.state.get_context("anthropic_api_key")
        )

        if cli_available:
            print("  ✓ Claude Code CLI detected — no API key needed for LLM calls.")
            print("    (CLI uses your Max subscription automatically.)")
            responses["anthropic_method"] = "cli"
        elif existing_key:
            masked = existing_key[:8] + "..." + existing_key[-4:]
            print(f"  ✓ Anthropic API key already configured: {masked}")
            responses["anthropic_method"] = "api"
        else:
            print("\n  FDA needs an Anthropic API key to call Claude.")
            print("  Get one at: https://console.anthropic.com/settings/keys\n")
            while True:
                key = input("  Anthropic API key (sk-ant-...) > ").strip()
                if not key:
                    print("  Skipped. You'll need to set ANTHROPIC_API_KEY later.")
                    break
                if self._validate_anthropic_key(key):
                    self.state.set_context("anthropic_api_key", key)
                    print("  ✓ API key validated and saved!")
                    responses["anthropic_method"] = "api"
                    break
                else:
                    print("  ✗ Invalid key — try again or press Enter to skip.")

        # --- OpenAI API Key (optional) ---
        existing_openai = (
            os.environ.get("OPENAI_API_KEY")
            or self.state.get_context("openai_api_key")
        )
        if existing_openai:
            masked = existing_openai[:8] + "..." + existing_openai[-4:]
            print(f"\n  ✓ OpenAI API key already configured: {masked}")
            responses["has_openai"] = True
        else:
            print("\n  Optional: OpenAI API key (for voice/TTS in Discord).")
            key = input("  OpenAI API key (sk-...) or press Enter to skip > ").strip()
            if key:
                self.state.set_context("openai_api_key", key)
                print("  ✓ OpenAI key saved!")
                responses["has_openai"] = True
            else:
                responses["has_openai"] = False

        print()

    def _validate_anthropic_key(self, key: str) -> bool:
        """Validate an Anthropic API key with a minimal test call."""
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=key)
            client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.debug(f"API key validation failed: {e}")
            return False

    def _onboard_step_channels(self, responses: dict[str, Any]) -> None:
        """Step 3: Select and configure messaging channels."""
        print("-" * 40)
        print("STEP 3/7 — MESSAGING CHANNELS")
        print("-" * 40)
        print("\n  Which messaging platforms do you want to connect?")
        print("  [1] Telegram")
        print("  [2] Discord")
        print("  [3] Slack")
        print("  [4] None (CLI only)\n")

        selection = input("  Enter numbers (comma-separated, e.g. 1,2) > ").strip()
        selected = {s.strip() for s in selection.split(",")} if selection else set()

        responses["channels_enabled"] = []

        # --- Telegram ---
        if "1" in selected:
            print("\n  --- Telegram Setup ---")
            print("  1. Open Telegram and search for @BotFather")
            print("  2. Send /newbot and follow the prompts")
            print("  3. Copy the bot token provided\n")

            token = input("  Telegram bot token > ").strip()
            if token:
                if self._validate_telegram_token(token):
                    self.state.set_context("telegram_bot_token", token)
                    responses["channels_enabled"].append("telegram")
                    print("  ✓ Telegram bot connected!")
                else:
                    print("  ✗ Token validation failed — you can set it later with 'fda telegram setup'")
            else:
                print("  Skipped.")

        # --- Discord ---
        if "2" in selected:
            print("\n  --- Discord Setup ---")
            print("  1. Go to https://discord.com/developers/applications")
            print("  2. Click 'New Application' → name it → go to 'Bot' tab")
            print("  3. Click 'Reset Token' and copy it")
            print("  4. Go to 'OAuth2' → 'General' → copy the Client ID")
            print("  5. Under 'Bot', enable: Message Content Intent, Server Members Intent\n")

            bot_token = input("  Discord bot token > ").strip()
            client_id = input("  Discord client ID > ").strip()
            if bot_token and client_id:
                self.state.set_context("discord_bot_token", bot_token)
                self.state.set_context("discord_client_id", client_id)
                responses["channels_enabled"].append("discord")
                # Generate invite URL
                perms = 3263552  # Send Messages, Speak, Connect, Read History, etc.
                invite_url = (
                    f"https://discord.com/api/oauth2/authorize"
                    f"?client_id={client_id}&permissions={perms}"
                    f"&scope=bot%20applications.commands"
                )
                print(f"  ✓ Discord bot configured!")
                print(f"  Invite URL: {invite_url}")
            else:
                print("  Skipped.")

        # --- Slack ---
        if "3" in selected:
            print("\n  --- Slack Setup ---")
            print("  1. Go to https://api.slack.com/apps → Create New App → From Manifest")
            print("  2. Paste the manifest (Socket Mode, events, commands)")
            print("  3. Install to workspace")
            print("  4. Copy Bot Token (xoxb-...) from OAuth & Permissions")
            print("  5. Copy App Token (xapp-...) from Basic Information → App-Level Tokens\n")

            bot_token = input("  Slack bot token (xoxb-...) > ").strip()
            app_token = input("  Slack app token (xapp-...) > ").strip()
            if bot_token and app_token:
                if bot_token.startswith("xoxb-") and app_token.startswith("xapp-"):
                    self.state.set_context("slack_bot_token", bot_token)
                    self.state.set_context("slack_app_token", app_token)
                    responses["channels_enabled"].append("slack")
                    print("  ✓ Slack bot configured!")
                else:
                    print("  ✗ Token format looks wrong — expected xoxb-/xapp- prefixes.")
                    print("    You can set them later with 'fda setup'.")
            else:
                print("  Skipped.")

        if "4" in selected or not responses["channels_enabled"]:
            print("\n  No messaging channels configured. You can add them later")
            print("  with 'fda telegram setup', 'fda discord setup', etc.")

        print()

    def _validate_telegram_token(self, token: str) -> bool:
        """Validate a Telegram bot token via the getMe API."""
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=5,
            )
            data = resp.json()
            if data.get("ok"):
                bot_name = data["result"].get("username", "unknown")
                print(f"  Bot username: @{bot_name}")
                return True
            return False
        except Exception as e:
            logger.debug(f"Telegram token validation failed: {e}")
            return False

    def _onboard_step_notetaking(self, responses: dict[str, Any]) -> None:
        """Step 4: Configure channels for daily note summaries."""
        print("-" * 40)
        print("STEP 4/7 — DAILY NOTETAKING")
        print("-" * 40)
        print("\n  FDA can auto-summarize conversations from specific channels")
        print("  into daily journal entries (runs at 9 PM by default).\n")

        # Show available options based on what's enabled
        options: list[tuple[str, str, str]] = []  # (platform, channel_id, label)

        # Check for KakaoTalk client rooms
        try:
            from fda.clients.client_config import ClientManager

            cm = ClientManager()
            for client in cm.list_clients():
                if client.kakaotalk_room:
                    options.append(
                        ("kakaotalk", client.kakaotalk_room, f"KakaoTalk: {client.name}")
                    )
        except Exception:
            pass

        # Channels enabled in step 3
        channels_enabled = responses.get("channels_enabled", [])
        if "telegram" in channels_enabled:
            options.append(("telegram", "all_dms", "Telegram: all DMs"))
        if "discord" in channels_enabled:
            options.append(("discord", "all_channels", "Discord: all channels"))
        if "slack" in channels_enabled:
            options.append(("slack", "all_channels", "Slack: all channels"))

        if not options:
            print("  No channels available for notetaking yet.")
            print("  You can add them later: fda config notetaking add <platform> <channel_id>\n")
            return

        print("  Available channels:")
        for i, (platform, ch_id, label) in enumerate(options, 1):
            print(f"  [{i}] {label}")
        print(f"  [{len(options) + 1}] Skip\n")

        selection = input("  Select channels (comma-separated, e.g. 1,2) > ").strip()
        if not selection or selection == str(len(options) + 1):
            print("  Skipped.\n")
            return

        selected_indices = {s.strip() for s in selection.split(",")}
        notetaking_channels: list[dict[str, str]] = []

        for idx_str in selected_indices:
            try:
                idx = int(idx_str) - 1
                if 0 <= idx < len(options):
                    platform, ch_id, label = options[idx]
                    notetaking_channels.append({
                        "platform": platform,
                        "channel_id": ch_id,
                        "label": label,
                    })
            except ValueError:
                continue

        if notetaking_channels:
            self.state.set_notetaking_channels(notetaking_channels)
            responses["notetaking_channels"] = notetaking_channels
            print(f"  ✓ {len(notetaking_channels)} channel(s) configured for daily notetaking!")
        else:
            print("  No valid channels selected.")

        # Notetaking time
        print(f"\n  Default summary time: 9:00 PM")
        nt_time = input("  Change time? (HH:MM or Enter to keep default) > ").strip()
        if nt_time:
            self.state.set_context("notetaking_time", nt_time)
            print(f"  ✓ Notetaking time set to {nt_time}")

        print()

    def _onboard_step_profile(self, responses: dict[str, Any]) -> None:
        """Step 5: User profile questions (who you are, goals, preferences)."""
        print("-" * 40)
        print("STEP 5/7 — ABOUT YOU")
        print("-" * 40)

        # Basic info
        responses["name"] = input("\n  What should I call you? > ").strip()
        responses["role"] = input("  What do you do? (e.g., 'software engineer') > ").strip()
        responses["context"] = input("  Tell me briefly about your current work: > ").strip()

        # Goals
        print()
        responses["goals"] = input("  What are your priorities right now? > ").strip()
        responses["challenges"] = input("  What's frustrating about your workflow? > ").strip()

        # Calendar
        print()
        cal = input("  Do you use Outlook/Office 365 calendar? (y/n) > ").strip().lower()
        responses["uses_outlook"] = cal in ("y", "yes")

        # Preferences
        responses["communication_style"] = (
            input("  Communication style? (brief/detailed/adaptive) > ").strip()
            or "adaptive"
        )
        responses["check_in_time"] = input(
            "  Daily check-in time? (e.g., '9:00 AM' or 'skip') > "
        ).strip()

        # Timezone
        print("\n  Timezone (e.g., Asia/Seoul, America/New_York)")
        print("  Press Enter to auto-detect.")
        tz_response = input("  Timezone > ").strip()

        if tz_response.lower() == "auto" or not tz_response:
            responses["timezone"] = self._detect_system_timezone()
            if responses["timezone"]:
                print(f"  Detected: {responses['timezone']}")
            else:
                print("  Could not detect — using system default.")
        else:
            from fda.utils.timezone import validate_timezone

            validated_tz = validate_timezone(tz_response)
            if validated_tz:
                responses["timezone"] = validated_tz
                print(f"  Using: {validated_tz}")
            else:
                print(f"  Warning: '{tz_response}' invalid. Using system default.")
                responses["timezone"] = None

        # Store in state
        self.state.set_context("user_name", responses.get("name", ""))
        self.state.set_context("user_role", responses.get("role", ""))
        self.state.set_context("user_context", responses.get("context", ""))
        self.state.set_context("user_goals", responses.get("goals", ""))
        self.state.set_context("user_challenges", responses.get("challenges", ""))
        self.state.set_context("communication_style", responses.get("communication_style", "adaptive"))
        self.state.set_context("user_timezone", responses.get("timezone"))
        self.state.set_context("onboarded", True)
        self.state.set_context("onboarded_at", datetime.now().isoformat())

        # Save journal entry
        name = responses.get("name", "User")
        self.log_to_journal(
            summary=f"Onboarding interview with {name}",
            content=(
                f"# Onboarding — {name}\n\n"
                f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"- **Role:** {responses.get('role', 'N/A')}\n"
                f"- **Context:** {responses.get('context', 'N/A')}\n"
                f"- **Goals:** {responses.get('goals', 'N/A')}\n"
                f"- **Challenges:** {responses.get('challenges', 'N/A')}\n"
                f"- **Style:** {responses.get('communication_style', 'adaptive')}\n"
                f"- **Timezone:** {responses.get('timezone') or 'system default'}\n"
            ),
            tags=["onboarding", "user-profile"],
            relevance_decay="slow",
        )

        print()

    def _onboard_step_daemon(self, responses: dict[str, Any]) -> None:
        """Step 6: Optionally install FDA as a background service."""
        print("-" * 40)
        print("STEP 6/7 — BACKGROUND SERVICE")
        print("-" * 40)

        platform_name = {"darwin": "macOS (launchd)", "linux": "Linux (systemd)"}.get(
            sys.platform, sys.platform
        )
        print(f"\n  FDA can run as a background service on {platform_name}.")
        print("  It will auto-start on boot and restart on crash.\n")

        install = input("  Install FDA as a background service? (y/n) > ").strip().lower()
        if install not in ("y", "yes"):
            print("  Skipped. Run 'fda start' manually when needed.\n")
            responses["daemon_installed"] = False
            return

        from fda.daemon import install_daemon, start_daemon

        if install_daemon(verbose=True):
            print("  ✓ Service installed!")
            responses["daemon_installed"] = True

            start_now = input("  Start it now? (y/n) > ").strip().lower()
            if start_now in ("y", "yes"):
                if start_daemon():
                    print("  ✓ FDA is running in the background!")
                    responses["daemon_started"] = True
                else:
                    print("  ✗ Failed to start. Check logs and try 'fda start'.")
                    responses["daemon_started"] = False
            else:
                responses["daemon_started"] = False
        else:
            print("  ✗ Failed to install service.")
            responses["daemon_installed"] = False

        print()

    def _onboard_step_complete(self, responses: dict[str, Any]) -> None:
        """Step 7: Show summary and next steps."""
        print("=" * 60)
        print("  SETUP COMPLETE")
        print("=" * 60)

        # Summary table
        print("\n  Configuration Summary:")
        print("  " + "-" * 36)

        # API
        api_method = responses.get("anthropic_method", "not set")
        api_icon = "✓" if api_method != "not set" else "✗"
        print(f"  {api_icon} Anthropic:  {api_method}")

        openai_icon = "✓" if responses.get("has_openai") else "—"
        print(f"  {openai_icon} OpenAI:     {'configured' if responses.get('has_openai') else 'skipped'}")

        # Channels
        channels = responses.get("channels_enabled", [])
        for ch in ["telegram", "discord", "slack"]:
            icon = "✓" if ch in channels else "—"
            print(f"  {icon} {ch.capitalize():11s} {'connected' if ch in channels else 'skipped'}")

        # Notetaking
        nt_channels = responses.get("notetaking_channels", [])
        if nt_channels:
            print(f"  ✓ Notetaking:  {len(nt_channels)} channel(s)")
        else:
            print("  — Notetaking:  none")

        # Profile
        name = responses.get("name", "")
        if name:
            print(f"  ✓ Profile:     {name}")
        else:
            print("  — Profile:     skipped")

        # Daemon
        daemon_icon = "✓" if responses.get("daemon_installed") else "—"
        daemon_status = "installed" if responses.get("daemon_installed") else "skipped"
        if responses.get("daemon_started"):
            daemon_status += " (running)"
        print(f"  {daemon_icon} Daemon:      {daemon_status}")

        print("  " + "-" * 36)

        # Personalized welcome (if profile was filled)
        if name:
            print("\n  Generating personalized welcome...\n")
            try:
                synthesis_prompt = (
                    f"The user {name} just completed onboarding. "
                    f"Role: {responses.get('role', 'N/A')}. "
                    f"Goals: {responses.get('goals', 'N/A')}. "
                    f"Channels: {', '.join(channels) or 'CLI only'}. "
                    f"Write a brief, warm welcome (2-3 sentences) and suggest "
                    f"one thing they could try first. Keep it conversational."
                )
                welcome = self.chat(synthesis_prompt, include_history=False)
                print(f"  {welcome}")
            except Exception:
                print(f"  Welcome, {name}! FDA is ready to help.")

        # Next steps
        print("\n  Next steps:")
        if not responses.get("daemon_started"):
            print("  • Run 'fda start' to launch all agents")
        if responses.get("uses_outlook"):
            print("  • Run 'fda calendar login' to connect your calendar")
        if not channels:
            print("  • Run 'fda onboard --force --skip-profile' to add channels later")
        print("  • Run 'fda ask \"...\"' to chat anytime")
        print("  • Run 'fda config notetaking add <platform> <channel>' to add notetaking")
        print()

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
            peer_json = json.dumps(peer_result, indent=2, ensure_ascii=False)[:2000]
            if peer_result.get("success"):
                enhanced_question = f"""{question}

[File search results:]
{peer_json}

Present these results naturally to the user.
IMPORTANT: Always include the FULL ABSOLUTE PATH for each file (e.g. /Users/.../file.html).
Never shorten or use relative paths — the UI turns full paths into clickable links.
Group files by location and highlight the most relevant ones."""
            else:
                enhanced_question = f"""{question}

[File search results:]
{peer_json}

The search did not find matching files. Tell the user what was searched and suggest:
1. Check the exact spelling or try a different keyword
2. Provide a specific directory path to search in
Do NOT suggest using slash commands. Be helpful and conversational."""
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
            # Only use stored path if it seems relevant to the question.
            # If the question names a specific subject (e.g. "lion_chemtech"), don't
            # constrain the search to an unrelated stored path.
            stored_path = self.state.get_context("active_project_path")
            if stored_path and os.path.exists(stored_path):
                # Check if the stored path name appears in the question
                stored_name = os.path.basename(stored_path).lower()
                if stored_name and stored_name in question.lower():
                    explicit_path = stored_path
                    logger.info(f"[FDA] Using stored active project path (matches question): {explicit_path}")
                else:
                    logger.info(f"[FDA] Stored path '{stored_path}' doesn't match question, searching broadly")
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

        Uses macOS Spotlight (mdfind) for fast indexed search, with find as fallback.
        Always returns a result dict so the caller can tell the user what was searched.

        Args:
            question: The user's question about files.
            explicit_path: An explicit path the user referenced in the question.

        Returns:
            Search results dict (always non-None for librarian intent).
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

            # Strategy 0: Try semantic search via local embeddings first.
            # This catches conceptual queries like "AX proposal" or "사자 화학"
            # even when the user's words don't appear literally in the filename.
            # Only runs if the index has been built (has any embeddings).
            try:
                from fda.file_indexer import FileIndexer
                # Skip if the index is empty (avoid loading a 100MB model for nothing)
                idx_stats = self.state.get_file_embeddings_stats()
                if idx_stats.get("total", 0) > 0:
                    indexer = FileIndexer(self.state)
                    semantic_hits = indexer.search(question, k=20)
                    # Only use semantic results if at least one score is clearly relevant.
                    # MiniLM cosine scores for strong matches are typically > 0.50.
                    if semantic_hits and semantic_hits[0]["score"] > 0.50:
                        strong = [h for h in semantic_hits if h["score"] > 0.40]
                        logger.info(
                            f"[FDA] Semantic search returned {len(strong)} strong matches "
                            f"(top score: {semantic_hits[0]['score']:.2f})"
                        )
                        return {
                            "success": True,
                            "search_term": question[:80],
                            "files_found": [h["path"] for h in strong[:30]],
                            "count": len(strong),
                            "summary": f"Semantic search found {len(strong)} relevant file(s)",
                            "method": "semantic",
                        }
            except Exception as e:
                logger.debug(f"[FDA] Semantic search skipped: {e}")

            # Extract search term(s) via LLM — ask for multiple keywords
            extract_prompt = (
                "Extract the filename, file pattern, or search keywords from this question. "
                "Reply with ONLY the search terms separated by commas. "
                "Always include the most specific/unique term first. "
                "For example: 'lion_chemtech, proposal' or 'resume.pdf' or 'smartstore, api'. "
                "No explanation.\n\n"
                f"Question: {question[:300]}\n\n"
                "Search terms:"
            )
            raw_terms = self.chat(
                extract_prompt, include_history=False, max_tokens=50, temperature=0.0
            ).strip().strip("'\"")

            if not raw_terms:
                return {
                    "success": False,
                    "search_term": "",
                    "files_found": [],
                    "count": 0,
                    "summary": "Could not extract a search term from the question.",
                }

            # Parse multiple search terms
            terms = [t.strip().strip("'\"") for t in raw_terms.split(",") if t.strip()]
            primary_term = terms[0] if terms else raw_terms
            logger.info(f"[FDA] Filesystem search terms: {terms}")

            home = os.path.expanduser("~")
            found_files = []

            # Strategy 1: macOS Spotlight (mdfind) — instant indexed search
            for term in terms:
                if found_files:
                    break
                try:
                    result = subprocess.run(
                        ["mdfind", "-name", term],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.stdout.strip():
                        found_files = [
                            f for f in result.stdout.strip().split("\n")
                            if f.startswith(home)
                            and "/." not in f.split(home, 1)[-1][:50]
                        ]
                        if found_files:
                            logger.info(f"[FDA] mdfind found {len(found_files)} files for '{term}'")
                except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
                    logger.debug(f"[FDA] mdfind failed for '{term}': {e}")

            # Strategy 2: Spotlight content search — finds files containing the term
            if not found_files:
                try:
                    result = subprocess.run(
                        ["mdfind", primary_term],
                        capture_output=True, text=True, timeout=8,
                    )
                    if result.stdout.strip():
                        found_files = [
                            f for f in result.stdout.strip().split("\n")
                            if f.startswith(home)
                            and "/." not in f.split(home, 1)[-1][:50]
                            and not any(skip in f for skip in [
                                "/node_modules/", "/__pycache__/", "/.venv/",
                                "/Library/", "/venv/",
                            ])
                        ]
                        if found_files:
                            logger.info(f"[FDA] mdfind content search found {len(found_files)} files")
                except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
                    logger.debug(f"[FDA] mdfind content search failed: {e}")

            # Strategy 3: find command fallback
            if not found_files:
                for term in terms:
                    if found_files:
                        break
                    try:
                        result = subprocess.run(
                            ["find", home, "-maxdepth", "5",
                             "-iname", f"*{term}*",
                             "-not", "-path", "*/.*",
                             "-not", "-path", "*/node_modules/*",
                             "-not", "-path", "*/__pycache__/*",
                             "-not", "-path", "*/.venv/*"],
                            capture_output=True, text=True, timeout=15,
                        )
                        if result.stdout.strip():
                            found_files = result.stdout.strip().split("\n")
                    except (subprocess.TimeoutExpired, Exception) as e:
                        logger.warning(f"[FDA] find command failed for '{term}': {e}")

            # Deduplicate while preserving order
            found_files = list(dict.fromkeys(found_files))

            if found_files:
                return {
                    "success": True,
                    "search_term": primary_term,
                    "files_found": found_files[:30],
                    "count": len(found_files),
                    "summary": f"Found {len(found_files)} file(s) matching '{primary_term}'",
                }

            # Nothing found — still return a result so the caller knows we searched
            logger.info(f"[FDA] No files matching '{primary_term}' found in filesystem")
            return {
                "success": False,
                "search_term": primary_term,
                "files_found": [],
                "count": 0,
                "summary": f"Searched the entire home directory for '{primary_term}' but found no matching files.",
            }

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

        Searches SharePoint/OneDrive for relevant files, extracts their content,
        and uses that context (along with journal history) to generate a
        comprehensive meeting brief.

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

        # Search SharePoint/OneDrive for relevant files
        sharepoint_files: list[dict[str, Any]] = []
        sharepoint_context = ""
        if self.calendar and event_details:
            try:
                sharepoint_files = self.calendar.search_files_for_meeting(
                    event_details, max_files=5, max_text_length=3000,
                )
                if sharepoint_files:
                    sharepoint_context = self._format_sharepoint_context(sharepoint_files)
                    logger.info(
                        f"[FDA] Found {len(sharepoint_files)} relevant files "
                        f"for meeting: {meeting_subject}"
                    )
            except Exception as e:
                logger.warning(f"SharePoint search failed for meeting prep: {e}")

        context["sharepoint_files"] = sharepoint_files

        # Build the prompt with SharePoint file context
        attendee_list = ", ".join(
            a.get("name", a.get("email", ""))
            for a in event_details.get("attendees", [])
        )

        prompt = f"""Prepare a briefing for this upcoming meeting:

Meeting: {event_details.get('subject', 'Unknown')}
Time: {event_details.get('start', 'Unknown')}
Attendees: {attendee_list}
Location: {event_details.get('location', 'Unknown')}
Description: {event_details.get('body_preview', event_details.get('body', 'N/A'))}
"""

        if sharepoint_context:
            prompt += f"""
## Relevant Files Found in SharePoint/OneDrive

The following files were found that may be relevant to this meeting.
Use their content to provide specific, data-informed preparation.

{sharepoint_context}
"""

        prompt += """
Please provide:
1. **Meeting Brief**: Key context and background (incorporate insights from any relevant files found)
2. **Suggested Agenda**: Discussion topics in priority order
3. **Key Points to Address**: Important items that must be covered
4. **Potential Questions**: Questions that might come up
5. **Recommended Actions**: Outcomes to aim for
6. **Supporting Data**: Relevant metrics, status updates, or data from the files above
7. **Referenced Files**: List any SharePoint/OneDrive files that attendees should review before the meeting"""

        response = self.chat_with_context(prompt, context)

        # Store the preparation
        prep_id = self.state.record_meeting_prep(
            event_id=event_id,
            brief=response,
            created_by=self.name,
        )

        # Log to journal
        file_names = [f.get("name", "?") for f in sharepoint_files]
        journal_content = f"## Meeting Preparation\n\n{response}"
        if file_names:
            journal_content += f"\n\n### Referenced Files\n" + "\n".join(
                f"- {name}" for name in file_names
            )

        self.log_to_journal(
            summary=f"Meeting prep: {event_details.get('subject', event_id)}",
            content=journal_content,
            tags=["meeting-prep", "briefing", "sharepoint"],
            relevance_decay="fast",
        )

        return {
            "status": "completed",
            "prep_id": prep_id,
            "event_id": event_id,
            "brief": response,
            "referenced_files": [
                {"name": f.get("name"), "url": f.get("web_url")}
                for f in sharepoint_files
            ],
            "timestamp": datetime.now().isoformat(),
        }

    @staticmethod
    def _format_sharepoint_context(files: list[dict[str, Any]]) -> str:
        """
        Format SharePoint file search results into a readable context block
        for the LLM prompt.

        Args:
            files: List of file dicts from search_files_for_meeting().

        Returns:
            Formatted string with file metadata and content excerpts.
        """
        sections = []
        for i, f in enumerate(files, 1):
            header = f"### File {i}: {f.get('name', 'Unknown')}"
            meta_parts = []
            if f.get("modified_by"):
                meta_parts.append(f"Last modified by: {f['modified_by']}")
            if f.get("last_modified"):
                meta_parts.append(f"Modified: {f['last_modified'][:10]}")
            if f.get("web_url"):
                meta_parts.append(f"Link: {f['web_url']}")

            meta = " | ".join(meta_parts) if meta_parts else ""

            content = f.get("text_content")
            if content:
                content_section = f"**Content excerpt:**\n```\n{content}\n```"
            elif f.get("hit_summary"):
                content_section = f"**Search snippet:** {f['hit_summary']}"
            else:
                content_section = "*Could not extract text content*"

            sections.append(f"{header}\n{meta}\n{content_section}")

        return "\n\n".join(sections)

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
