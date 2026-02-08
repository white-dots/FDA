"""
Discord Voice Bot integration for FDA system.

Provides Discord voice channel integration for the FDA agent, allowing
it to join voice channels, transcribe conversations, and speak responses.
"""

import asyncio
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.config import (
    DISCORD_BOT_TOKEN_ENV,
    DISCORD_CLIENT_ID_ENV,
    OPENAI_API_KEY_ENV,
    MODEL_FDA,
    JOURNAL_DIR,
)
from fda.state.project_state import ProjectState

logger = logging.getLogger(__name__)


DISCORD_SYSTEM_PROMPT = """You are the FDA (Facilitating Director Agent) participating in a Discord voice channel.

Keep responses:
- Concise (suitable for text-to-speech)
- Clear and easy to understand when spoken aloud
- Focused on the key information

You have access to project tasks, alerts, decisions, and historical journal entries.
When asked questions, provide helpful answers about the project status.
"""


class DiscordVoiceAgent(BaseAgent):
    """
    Discord Voice Agent for FDA system.

    Provides Discord voice channel integration for:
    - Joining voice channels
    - Transcribing voice conversations (via Whisper)
    - Speaking responses (via TTS)
    - Responding to text commands
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        project_state_path: Optional[Path] = None,
    ):
        """
        Initialize the Discord Voice Agent.

        Args:
            bot_token: Discord bot token. If not provided, reads from
                      DISCORD_BOT_TOKEN environment variable.
            openai_api_key: OpenAI API key for Whisper/TTS. If not provided,
                           reads from OPENAI_API_KEY environment variable.
            project_state_path: Path to the project state database.
        """
        super().__init__(
            name="DiscordBot",
            model=MODEL_FDA,
            system_prompt=DISCORD_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

        self.bot_token = bot_token or os.environ.get(DISCORD_BOT_TOKEN_ENV)
        if not self.bot_token:
            raise ValueError(
                f"Discord bot token required. Set {DISCORD_BOT_TOKEN_ENV} "
                "environment variable or pass bot_token parameter."
            )

        self.openai_api_key = openai_api_key or os.environ.get(OPENAI_API_KEY_ENV)
        self._openai_client = None

        self._bot = None
        self._voice_client = None
        self._current_session_id: Optional[str] = None
        self._transcript_buffer: list[dict[str, Any]] = []

    def _get_openai_client(self) -> Any:
        """Get or create OpenAI client for Whisper/TTS."""
        if self._openai_client is not None:
            return self._openai_client

        if not self.openai_api_key:
            raise ValueError(
                f"OpenAI API key required for voice features. "
                f"Set {OPENAI_API_KEY_ENV} environment variable."
            )

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package required for voice features. "
                "Install with: pip install openai"
            )

        self._openai_client = OpenAI(api_key=self.openai_api_key)
        return self._openai_client

    def _get_bot(self) -> Any:
        """Get or create the Discord bot instance."""
        if self._bot is not None:
            return self._bot

        try:
            import discord
            from discord.ext import commands
        except ImportError:
            raise ImportError(
                "discord.py is required for DiscordVoiceAgent. "
                "Install with: pip install discord.py[voice]"
            )

        # Set up intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True

        self._bot = commands.Bot(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        # Register event handlers
        @self._bot.event
        async def on_ready():
            logger.info(f"[DiscordBot] Logged in as {self._bot.user}")
            print(f"Discord bot ready: {self._bot.user}")

        @self._bot.event
        async def on_message(message):
            if message.author == self._bot.user:
                return
            await self._bot.process_commands(message)

        # Register commands
        @self._bot.command(name="join")
        async def join_voice(ctx):
            """Join the user's voice channel."""
            await self._cmd_join(ctx)

        @self._bot.command(name="leave")
        async def leave_voice(ctx):
            """Leave the current voice channel."""
            await self._cmd_leave(ctx)

        @self._bot.command(name="ask")
        async def ask_question(ctx, *, question: str = None):
            """Ask FDA a question."""
            await self._cmd_ask(ctx, question)

        @self._bot.command(name="status")
        async def show_status(ctx):
            """Show project status."""
            await self._cmd_status(ctx)

        @self._bot.command(name="say")
        async def say_text(ctx, *, text: str = None):
            """Make FDA speak in voice channel."""
            await self._cmd_say(ctx, text)

        @self._bot.command(name="help")
        async def show_help(ctx):
            """Show available commands."""
            await self._cmd_help(ctx)

        return self._bot

    async def _cmd_join(self, ctx: Any) -> None:
        """Handle !join command."""
        if not ctx.author.voice:
            await ctx.send("You need to be in a voice channel first!")
            return

        channel = ctx.author.voice.channel

        try:
            if self._voice_client and self._voice_client.is_connected():
                await self._voice_client.move_to(channel)
            else:
                self._voice_client = await channel.connect()

            # Start a new session
            self._current_session_id = self.state.start_discord_session(
                guild_id=str(ctx.guild.id),
                channel_id=str(channel.id),
                channel_name=channel.name,
            )
            self._transcript_buffer = []

            await ctx.send(f"Joined **{channel.name}**. I'm listening!")
            logger.info(f"[DiscordBot] Joined voice channel: {channel.name}")

        except Exception as e:
            logger.error(f"[DiscordBot] Failed to join voice: {e}")
            await ctx.send(f"Failed to join voice channel: {e}")

    async def _cmd_leave(self, ctx: Any) -> None:
        """Handle !leave command."""
        if not self._voice_client or not self._voice_client.is_connected():
            await ctx.send("I'm not in a voice channel.")
            return

        try:
            channel_name = self._voice_client.channel.name
            await self._voice_client.disconnect()
            self._voice_client = None

            # End the session and save transcript
            if self._current_session_id:
                transcript_path = await self._save_transcript()
                self.state.end_discord_session(
                    self._current_session_id,
                    transcript_path=transcript_path,
                )
                self._current_session_id = None

            await ctx.send(f"Left **{channel_name}**. Goodbye!")
            logger.info(f"[DiscordBot] Left voice channel: {channel_name}")

        except Exception as e:
            logger.error(f"[DiscordBot] Failed to leave voice: {e}")
            await ctx.send(f"Error leaving channel: {e}")

    async def _cmd_ask(self, ctx: Any, question: Optional[str]) -> None:
        """Handle !ask command."""
        if not question:
            await ctx.send("Please provide a question. Example: `!ask What are our blockers?`")
            return

        await ctx.send("Thinking...")

        try:
            response = self._answer_question(question)

            # Send text response
            await ctx.send(response)

            # If in voice, also speak the response
            if self._voice_client and self._voice_client.is_connected():
                await self._speak_text(response)

        except Exception as e:
            logger.error(f"[DiscordBot] Error answering question: {e}")
            await ctx.send("Sorry, I encountered an error.")

    async def _cmd_status(self, ctx: Any) -> None:
        """Handle !status command."""
        try:
            tasks = self.state.get_tasks()
            alerts = self.state.get_alerts(acknowledged=False)

            # Count by status
            status_counts = {}
            for task in tasks:
                status = task.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            # Build response
            lines = ["**Project Status**\n"]
            lines.append(f"Total tasks: {len(tasks)}")

            for status, count in sorted(status_counts.items()):
                lines.append(f"  • {status}: {count}")

            if alerts:
                lines.append(f"\n⚠️ {len(alerts)} unacknowledged alert(s)")

            await ctx.send("\n".join(lines))

        except Exception as e:
            logger.error(f"[DiscordBot] Error getting status: {e}")
            await ctx.send("Error retrieving status.")

    async def _cmd_say(self, ctx: Any, text: Optional[str]) -> None:
        """Handle !say command - speak text in voice channel."""
        if not text:
            await ctx.send("Please provide text to speak. Example: `!say Hello everyone`")
            return

        if not self._voice_client or not self._voice_client.is_connected():
            await ctx.send("I need to be in a voice channel first. Use `!join`")
            return

        try:
            await self._speak_text(text)
            await ctx.send("🔊 Speaking...")
        except Exception as e:
            logger.error(f"[DiscordBot] Error speaking: {e}")
            await ctx.send(f"Error speaking: {e}")

    async def _cmd_help(self, ctx: Any) -> None:
        """Handle !help command."""
        help_text = """**FDA Discord Bot Commands**

`!join` - Join your voice channel
`!leave` - Leave voice channel
`!ask <question>` - Ask about the project
`!status` - Show project status
`!say <text>` - Speak text in voice channel
`!help` - Show this help message
"""
        await ctx.send(help_text)

    def _answer_question(self, question: str) -> str:
        """Answer a question using FDA agent capabilities."""
        context = self.get_project_context()

        # Search journal
        relevant = self.search_journal(question, top_n=3)
        if relevant:
            context["relevant_history"] = [
                {"summary": e.get("summary"), "author": e.get("author")}
                for e in relevant
            ]

        return self.chat_with_context(question, context)

    async def _speak_text(self, text: str) -> None:
        """Convert text to speech and play in voice channel."""
        if not self._voice_client or not self._voice_client.is_connected():
            return

        try:
            import discord

            # Get TTS audio from OpenAI
            client = self._get_openai_client()

            response = client.audio.speech.create(
                model="tts-1",
                voice="alloy",
                input=text,
            )

            # Save to temp file
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(response.content)
                temp_path = f.name

            # Play audio
            audio_source = discord.FFmpegPCMAudio(temp_path)
            self._voice_client.play(audio_source)

            # Wait for playback to finish
            while self._voice_client.is_playing():
                await asyncio.sleep(0.1)

            # Clean up
            os.unlink(temp_path)

        except Exception as e:
            logger.error(f"[DiscordBot] TTS error: {e}")
            raise

    async def _transcribe_audio(self, audio_data: bytes) -> str:
        """Transcribe audio using Whisper API."""
        try:
            client = self._get_openai_client()

            # Create a file-like object
            audio_file = io.BytesIO(audio_data)
            audio_file.name = "audio.wav"

            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )

            return response.text

        except Exception as e:
            logger.error(f"[DiscordBot] Transcription error: {e}")
            return ""

    def _add_to_transcript(self, speaker: str, text: str) -> None:
        """Add an entry to the transcript buffer."""
        self._transcript_buffer.append({
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "text": text,
        })

    async def _save_transcript(self) -> Optional[str]:
        """Save the transcript buffer to a file."""
        if not self._transcript_buffer:
            return None

        try:
            # Create transcript filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"discord_transcript_{timestamp}.md"
            filepath = JOURNAL_DIR / filename

            # Build markdown content
            lines = [
                "# Discord Voice Session Transcript",
                f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                "---",
                "",
            ]

            for entry in self._transcript_buffer:
                time_str = entry["timestamp"][11:19]  # HH:MM:SS
                lines.append(f"**[{time_str}] {entry['speaker']}**: {entry['text']}")
                lines.append("")

            # Write file
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text("\n".join(lines))

            # Log to journal
            self.log_to_journal(
                summary=f"Discord voice session transcript",
                content="\n".join(lines),
                tags=["discord", "voice", "transcript"],
                relevance_decay="medium",
            )

            logger.info(f"[DiscordBot] Transcript saved: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"[DiscordBot] Failed to save transcript: {e}")
            return None

    def run_event_loop(self) -> None:
        """
        Run the Discord bot event loop.

        This starts the bot and processes incoming events.
        """
        logger.info("[DiscordBot] Starting event loop...")

        bot = self._get_bot()

        try:
            bot.run(self.bot_token)
        except KeyboardInterrupt:
            logger.info("[DiscordBot] Received shutdown signal")
        except Exception as e:
            logger.error(f"[DiscordBot] Error in event loop: {e}")

        logger.info("[DiscordBot] Event loop stopped")

    def get_invite_url(self, client_id: Optional[str] = None) -> str:
        """
        Generate bot invite URL.

        Args:
            client_id: Discord application client ID. If not provided,
                      reads from DISCORD_CLIENT_ID environment variable.

        Returns:
            OAuth2 invite URL for the bot.
        """
        cid = client_id or os.environ.get(DISCORD_CLIENT_ID_ENV)
        if not cid:
            raise ValueError(
                f"Client ID required. Set {DISCORD_CLIENT_ID_ENV} environment variable."
            )

        # Permissions: Connect, Speak, Send Messages, Read Message History
        permissions = 3148800
        return f"https://discord.com/api/oauth2/authorize?client_id={cid}&permissions={permissions}&scope=bot"


def get_bot_token() -> Optional[str]:
    """Get Discord bot token from environment or stored config."""
    token = os.environ.get(DISCORD_BOT_TOKEN_ENV)
    if token:
        return token

    try:
        state = ProjectState()
        token = state.get_context("discord_bot_token")
        if token:
            return token
    except Exception:
        pass

    return None


def setup_bot_token(token: str) -> None:
    """Store Discord bot token in project state."""
    state = ProjectState()
    state.set_context("discord_bot_token", token)
    logger.info("[DiscordBot] Bot token stored in project state")


def get_client_id() -> Optional[str]:
    """Get Discord client ID from environment or stored config."""
    cid = os.environ.get(DISCORD_CLIENT_ID_ENV)
    if cid:
        return cid

    try:
        state = ProjectState()
        cid = state.get_context("discord_client_id")
        if cid:
            return cid
    except Exception:
        pass

    return None


def setup_client_id(client_id: str) -> None:
    """Store Discord client ID in project state."""
    state = ProjectState()
    state.set_context("discord_client_id", client_id)
    logger.info("[DiscordBot] Client ID stored in project state")
