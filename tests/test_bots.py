"""
Tests for bot initialization and command routing.

These tests verify that bots accept their callbacks, route commands
to the right handlers, and format responses correctly — without
actually connecting to Telegram/Slack/Discord APIs.
"""

import os
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Slack Bot
# ---------------------------------------------------------------------------

class TestSlackBot:
    """Tests for SlackBotAgent init and command routing."""

    @pytest.fixture
    def slack_bot(self):
        """Create SlackBotAgent with all deps mocked."""
        with patch("fda.base_agent.get_claude_backend", return_value=MagicMock()), \
             patch("fda.base_agent.ProjectState"), \
             patch("fda.base_agent.MessageBus"), \
             patch("fda.base_agent.JournalWriter"), \
             patch("fda.base_agent.JournalRetriever"):
            from fda.slack_bot import SlackBotAgent
            bot = SlackBotAgent(
                bot_token="xoxb-test",
                app_token="xapp-test",
                local_task_dispatch=MagicMock(return_value={"success": True, "investigation": True, "explanation": "done"}),
                remote_task_dispatch=MagicMock(),
                local_organize_dispatch=MagicMock(return_value={
                    "success": True, "summary": "Organized", "moves": [],
                    "deletions": [], "repos_skipped": [],
                }),
            )
            return bot

    def test_init_stores_callbacks(self, slack_bot):
        assert slack_bot._local_task_dispatch is not None
        assert slack_bot._remote_task_dispatch is not None
        assert slack_bot._local_organize_dispatch is not None

    def test_handle_organize_no_args(self, slack_bot):
        say = MagicMock()
        slack_bot._handle_organize("!organize", "C123", say, "ts", MagicMock())
        say.assert_called_once()
        assert "Usage" in say.call_args[1]["text"]

    def test_handle_organize_no_dispatch(self, slack_bot):
        slack_bot._local_organize_dispatch = None
        say = MagicMock()
        slack_bot._handle_organize("!organize ~/Downloads", "C123", say, "ts", MagicMock())
        assert "not available" in say.call_args_list[-1][1]["text"]

    def test_handle_organize_dispatches(self, slack_bot):
        say = MagicMock()
        client = MagicMock()
        slack_bot._handle_organize(
            "!organize ~/Downloads sort by type",
            "C123", say, "ts", client,
        )
        slack_bot._local_organize_dispatch.assert_called_once()
        args = slack_bot._local_organize_dispatch.call_args
        assert "Downloads" in args[0][0]
        assert "sort by type" in args[0][1]

    def test_handle_help_includes_organize(self, slack_bot):
        say = MagicMock()
        slack_bot._handle_help(say, "ts")
        text = say.call_args[1]["text"]
        assert "!organize" in text

    def test_split_message(self, slack_bot):
        """Test message splitting for Slack's 4000 char limit."""
        long_msg = "x" * 8000
        chunks = slack_bot._split_message(long_msg)
        assert len(chunks) >= 2
        assert all(len(c) <= 4000 for c in chunks)


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------

class TestDiscordBot:
    """Tests for DiscordVoiceAgent init and command presence."""

    @pytest.fixture
    def discord_bot(self):
        """Create DiscordVoiceAgent with all deps mocked."""
        with patch("fda.base_agent.get_claude_backend", return_value=MagicMock()), \
             patch("fda.base_agent.ProjectState"), \
             patch("fda.base_agent.MessageBus"), \
             patch("fda.base_agent.JournalWriter"), \
             patch("fda.base_agent.JournalRetriever"), \
             patch("fda.fda_agent.FDAAgent", MagicMock()):
            try:
                from fda.discord_bot import DiscordVoiceAgent
                bot = DiscordVoiceAgent(
                    bot_token="test-token",
                    local_task_dispatch=MagicMock(),
                    remote_task_dispatch=MagicMock(),
                    local_organize_dispatch=MagicMock(),
                )
                return bot
            except ImportError:
                pytest.skip("discord.py not installed")

    def test_init_stores_organize_dispatch(self, discord_bot):
        assert discord_bot._local_organize_dispatch is not None

    def test_has_organize_method(self, discord_bot):
        assert hasattr(discord_bot, "_cmd_organize")


# ---------------------------------------------------------------------------
# Telegram Bot
# ---------------------------------------------------------------------------

class TestTelegramBot:
    """Tests for TelegramBotAgent init and command registration."""

    @pytest.fixture
    def telegram_bot(self):
        """Create TelegramBotAgent with deps mocked."""
        with patch("fda.base_agent.get_claude_backend", return_value=MagicMock()), \
             patch("fda.base_agent.ProjectState") as MockState, \
             patch("fda.base_agent.MessageBus"), \
             patch("fda.base_agent.JournalWriter"), \
             patch("fda.base_agent.JournalRetriever"):
            MockState.return_value.get_context.return_value = None
            try:
                from fda.telegram_bot import TelegramBotAgent
                bot = TelegramBotAgent(
                    bot_token="123:test-token",
                    local_task_dispatch=MagicMock(),
                    remote_task_dispatch=MagicMock(),
                    local_organize_dispatch=MagicMock(),
                )
                return bot
            except ImportError:
                pytest.skip("python-telegram-bot not installed")

    def test_init_stores_organize_dispatch(self, telegram_bot):
        assert telegram_bot._local_organize_dispatch is not None

    def test_has_organize_handler(self, telegram_bot):
        assert hasattr(telegram_bot, "_handle_organize")
