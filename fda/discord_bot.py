"""
Discord Voice Bot - PRIMARY user interface for FDA system.

This is the main way users interact with FDA:
- Voice commands in Discord channels
- Wake word detection ("Hey FDA" or "FDA")
- Meeting attendance and transcription
- Real-time voice responses via TTS

The Discord bot routes user requests to FDA, which then collaborates
with Librarian and Executor peers to fulfill them.
"""

import asyncio
import io
import logging
import os
import tempfile
import json
import wave
import struct
from pathlib import Path
from typing import Any, Optional
from datetime import datetime
from collections import defaultdict

from fda.base_agent import BaseAgent
from fda.config import (
    DISCORD_BOT_TOKEN_ENV,
    DISCORD_CLIENT_ID_ENV,
    OPENAI_API_KEY_ENV,
    MODEL_FDA,
    MODEL_MEETING_SUMMARY,
    JOURNAL_DIR,
)
from fda.state.project_state import ProjectState
from fda.comms.message_bus import MessageTypes, Agents

logger = logging.getLogger(__name__)


class VoiceListeningSink:
    """
    Audio sink that collects voice data from Discord users.

    Buffers audio per-user and detects silence to determine when
    someone has finished speaking.
    """

    # Audio settings (Discord uses 48kHz, 16-bit, stereo)
    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2  # 16-bit = 2 bytes

    # Silence detection settings
    SILENCE_THRESHOLD = 500  # RMS threshold for silence
    SILENCE_DURATION = 1.5  # Seconds of silence to consider speech ended
    MIN_SPEECH_DURATION = 0.5  # Minimum speech duration to process
    MAX_SPEECH_DURATION = 30.0  # Maximum duration before forcing processing

    def __init__(self, agent: "DiscordVoiceAgent", loop: asyncio.AbstractEventLoop):
        self.agent = agent
        self.loop = loop
        self.user_buffers: dict[int, list[bytes]] = defaultdict(list)
        self.user_silence_start: dict[int, float] = {}
        self.user_speech_start: dict[int, float] = {}
        self.processing_users: set[int] = set()

    def write(self, user: Any, data: Any) -> None:
        """Called by Discord when audio data is received from a user."""
        if user is None or user.bot:
            return

        user_id = user.id

        # Don't buffer if we're already processing this user's audio
        if user_id in self.processing_users:
            return

        # Convert PCM data to bytes
        pcm_data = data.pcm if hasattr(data, 'pcm') else bytes(data)

        # Calculate RMS to detect silence
        rms = self._calculate_rms(pcm_data)
        current_time = asyncio.get_event_loop().time()

        if rms > self.SILENCE_THRESHOLD:
            # User is speaking
            self.user_buffers[user_id].append(pcm_data)
            self.user_silence_start.pop(user_id, None)

            if user_id not in self.user_speech_start:
                self.user_speech_start[user_id] = current_time

            # Check for max duration
            speech_duration = current_time - self.user_speech_start.get(user_id, current_time)
            if speech_duration > self.MAX_SPEECH_DURATION:
                self._schedule_processing(user_id, user.display_name)
        else:
            # Silence detected
            if user_id in self.user_speech_start:
                if user_id not in self.user_silence_start:
                    self.user_silence_start[user_id] = current_time
                else:
                    silence_duration = current_time - self.user_silence_start[user_id]
                    speech_duration = self.user_silence_start[user_id] - self.user_speech_start.get(user_id, 0)

                    # If enough silence and minimum speech duration met
                    if silence_duration > self.SILENCE_DURATION and speech_duration > self.MIN_SPEECH_DURATION:
                        self._schedule_processing(user_id, user.display_name)

    def _calculate_rms(self, pcm_data: bytes) -> float:
        """Calculate RMS (Root Mean Square) of audio data."""
        if len(pcm_data) < 2:
            return 0

        # Convert bytes to 16-bit samples
        samples = struct.unpack(f"{len(pcm_data) // 2}h", pcm_data)

        if not samples:
            return 0

        # Calculate RMS
        sum_squares = sum(s * s for s in samples)
        return (sum_squares / len(samples)) ** 0.5

    def _schedule_processing(self, user_id: int, username: str) -> None:
        """Schedule audio processing in the event loop."""
        if user_id in self.processing_users:
            return

        self.processing_users.add(user_id)
        audio_data = b"".join(self.user_buffers[user_id])

        # Clear buffers
        self.user_buffers[user_id] = []
        self.user_silence_start.pop(user_id, None)
        self.user_speech_start.pop(user_id, None)

        # Schedule async processing
        asyncio.run_coroutine_threadsafe(
            self._process_audio(user_id, username, audio_data),
            self.loop
        )

    async def _process_audio(self, user_id: int, username: str, audio_data: bytes) -> None:
        """Process collected audio data."""
        try:
            if len(audio_data) < self.SAMPLE_RATE * self.SAMPLE_WIDTH * self.CHANNELS * 0.3:
                # Too short, skip
                return

            # Convert to WAV format for Whisper
            wav_data = self._pcm_to_wav(audio_data)

            # Transcribe
            text = await self.agent._transcribe_audio_async(wav_data)

            if not text or len(text.strip()) < 2:
                return

            logger.info(f"[DiscordBot] Transcribed from {username}: {text}")

            # Add to transcript buffer if in meeting mode
            if self.agent._meeting_mode:
                self.agent._add_to_transcript(username, text)

            # Check for wake word
            wake_detected, command = self.agent._detect_wake_word(text)

            if wake_detected:
                logger.info(f"[DiscordBot] Wake word detected! Command: {command}")
                await self.agent._handle_voice_command(username, command or text)

        except Exception as e:
            logger.error(f"[DiscordBot] Error processing audio: {e}")
        finally:
            self.processing_users.discard(user_id)

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """Convert raw PCM data to WAV format."""
        wav_buffer = io.BytesIO()

        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(self.CHANNELS)
            wav_file.setsampwidth(self.SAMPLE_WIDTH)
            wav_file.setframerate(self.SAMPLE_RATE)
            wav_file.writeframes(pcm_data)

        wav_buffer.seek(0)
        return wav_buffer.read()

    def cleanup(self) -> None:
        """Clean up resources."""
        self.user_buffers.clear()
        self.user_silence_start.clear()
        self.user_speech_start.clear()
        self.processing_users.clear()


DISCORD_SYSTEM_PROMPT = """You are FDA (Facilitating Director Agent) - the user's personal AI assistant, speaking via Discord voice.

Discord voice is your PRIMARY interface with the user. They talk to you, you listen and respond.

Keep your spoken responses:
- Concise and conversational (suitable for text-to-speech)
- Clear and easy to understand when spoken aloud
- Natural, like talking to a helpful colleague
- Focused on the key information

You work with two peer agents:
- Librarian: Knows about files and documents on the computer
- Executor: Can run commands and make changes

When the user asks for something:
- If about files/search → Librarian helps
- If about running commands → Executor helps
- If you can answer directly → Do so

You can also attend meetings (stay in voice channel, transcribe, summarize later).

Remember: You're the voice and face of the system. Be warm, helpful, and proactive.
"""


class DiscordVoiceAgent(BaseAgent):
    """
    Discord Voice Agent - PRIMARY user interface for FDA.

    This is the main way users interact with FDA:
    - Voice commands with wake word detection
    - Meeting attendance and transcription
    - Real-time voice responses
    - Delegation to peer agents (Librarian, Executor)
    """

    # Wake words that trigger FDA to respond
    WAKE_WORDS = ["hey fda", "fda", "hey f d a", "f d a"]

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
        self._voice_sink: Optional[VoiceListeningSink] = None
        self._current_session_id: Optional[str] = None
        self._transcript_buffer: list[dict[str, Any]] = []
        self._response_channel = None  # Channel to send text responses

        # Meeting mode state
        self._meeting_mode = False
        self._meeting_start_time: Optional[datetime] = None

        # Voice listening state
        self._listening_enabled = True  # Enable voice listening by default

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
            logger.info(f"[DiscordBot] Received message: '{message.content}' from {message.author}")
            print(f"[DEBUG] Message received: '{message.content}' from {message.author}")

            if message.author == self._bot.user:
                return

            # Process commands first
            await self._bot.process_commands(message)

            # If it's a command, don't also treat it as a question
            if message.content.startswith("!"):
                return

            # Handle plain text messages as questions (like Telegram bot)
            # Remove any bot mention from the message
            content = message.content
            if self._bot.user in message.mentions:
                content = content.replace(f"<@{self._bot.user.id}>", "").strip()
                content = content.replace(f"<@!{self._bot.user.id}>", "").strip()

            # Respond to any message with content
            if content:
                print(f"[DEBUG] Processing plain message: '{content}'")
                await self._handle_plain_message(message, content)

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

        @self._bot.command(name="meeting")
        async def meeting_mode(ctx, action: str = None):
            """Start or end meeting mode."""
            await self._cmd_meeting(ctx, action)

        @self._bot.command(name="search")
        async def search_files(ctx, *, query: str = None):
            """Search for files (via Librarian)."""
            await self._cmd_search(ctx, query)

        @self._bot.command(name="run")
        async def run_command(ctx, *, command: str = None):
            """Run a command (via Executor)."""
            await self._cmd_run(ctx, command)

        @self._bot.command(name="peers")
        async def show_peers(ctx):
            """Show peer agent status."""
            await self._cmd_peers(ctx)

        @self._bot.command(name="listen")
        async def toggle_listen(ctx, action: str = None):
            """Toggle voice listening on/off."""
            await self._cmd_listen(ctx, action)

        return self._bot

    async def _cmd_listen(self, ctx: Any, action: Optional[str]) -> None:
        """Handle !listen command to toggle voice listening."""
        if action is None:
            # Show current status
            status = "enabled" if self._listening_enabled else "disabled"
            await ctx.send(f"Voice listening is currently **{status}**.\nUse `!listen on` or `!listen off` to change.")
            return

        action = action.lower()
        if action in ("on", "enable", "start"):
            self._listening_enabled = True
            await ctx.send("Voice listening **enabled**. Say \"Hey FDA\" when I'm in a voice channel!")

            # Start listening if already in voice
            if self._voice_client and self._voice_client.is_connected():
                await self._start_voice_listening()

        elif action in ("off", "disable", "stop"):
            self._listening_enabled = False
            self._stop_voice_listening()
            await ctx.send("Voice listening **disabled**. Use `!ask` to talk to me instead.")

        else:
            await ctx.send("Usage: `!listen on` or `!listen off`")

    async def _handle_plain_message(self, message: Any, content: str) -> None:
        """Handle plain text messages (not commands)."""
        try:
            # Show typing indicator
            async with message.channel.typing():
                response = self._answer_question(content)

            # Send response
            if len(response) > 2000:
                # Split long responses
                for i in range(0, len(response), 1900):
                    await message.reply(response[i:i+1900])
            else:
                await message.reply(response)

            # If in voice, also speak the response
            if self._voice_client and self._voice_client.is_connected():
                # Only speak shorter responses
                if len(response) < 500:
                    await self._speak_text(response)

        except Exception as e:
            logger.error(f"[DiscordBot] Error handling message: {e}")
            await message.reply("Sorry, I encountered an error processing your message.")

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
            self._response_channel = ctx.channel

            # Start voice listening
            if self._listening_enabled:
                await self._start_voice_listening()
                await ctx.send(f"Joined **{channel.name}**. I'm listening! Say \"Hey FDA\" to talk to me.")
            else:
                await ctx.send(f"Joined **{channel.name}**. Use `!ask` to talk to me.")

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

            # Stop voice listening
            self._stop_voice_listening()

            await self._voice_client.disconnect()
            self._voice_client = None
            self._response_channel = None

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
        help_text = """**FDA Discord Bot - Your Personal AI Assistant**

**Voice Commands:**
`!join` - Join your voice channel (starts listening!)
`!leave` - Leave voice channel
`!say <text>` - Speak text in voice channel
`!listen on/off` - Toggle voice listening

**Voice Call:**
When I'm in a voice channel, just say **"Hey FDA"** followed by your question!
I'll listen, understand, and respond by voice.

**Text Commands:**
`!ask <question>` - Ask FDA anything (or just type without !)
`!status` - Show project status

**Meeting Mode:**
`!meeting start` - Start meeting mode (transcribe everything)
`!meeting end` - End meeting and get summary

**Peer Agents:**
`!search <query>` - Search files (via Librarian)
`!run <command>` - Run a command (via Executor)
`!peers` - Show peer agent status
"""
        await ctx.send(help_text)

    async def _cmd_meeting(self, ctx: Any, action: Optional[str]) -> None:
        """Handle !meeting command for meeting mode."""
        if not action or action.lower() not in ["start", "end"]:
            await ctx.send("Usage: `!meeting start` or `!meeting end`")
            return

        if action.lower() == "start":
            if self._meeting_mode:
                await ctx.send("Meeting mode is already active.")
                return

            if not self._voice_client or not self._voice_client.is_connected():
                await ctx.send("I need to be in a voice channel first. Use `!join`")
                return

            self._meeting_mode = True
            self._meeting_start_time = datetime.now()
            self._transcript_buffer = []

            await ctx.send("📋 **Meeting mode started!** I'll transcribe the conversation.")
            if self._voice_client:
                await self._speak_text("Meeting mode started. I'm listening and will take notes.")

        else:  # end
            if not self._meeting_mode:
                await ctx.send("No meeting in progress.")
                return

            self._meeting_mode = False
            await ctx.send("📋 **Meeting mode ended.** Generating summary...")

            # Generate meeting summary
            summary = await self._generate_meeting_summary()

            # Send summary
            if len(summary) > 1900:
                # Split long summaries
                for i in range(0, len(summary), 1900):
                    await ctx.send(summary[i:i+1900])
            else:
                await ctx.send(summary)

            # Save transcript
            transcript_path = await self._save_transcript()
            if transcript_path:
                await ctx.send(f"Transcript saved: `{transcript_path}`")

    async def _cmd_search(self, ctx: Any, query: Optional[str]) -> None:
        """Handle !search command - delegate to Librarian."""
        if not query:
            await ctx.send("Usage: `!search <query>`\nExample: `!search python files`")
            return

        await ctx.send(f"🔍 Asking Librarian to search for: {query}")

        # Check if Librarian is running
        librarian_status = self.state.get_agent_status(Agents.LIBRARIAN)
        if not librarian_status or librarian_status.get("status") != "running":
            await ctx.send("⚠️ Librarian agent is not running. Start it with `fda start --all`")
            return

        # Send search request
        msg_id = self.message_bus.request_search(
            from_agent=self.name.lower(),
            query=query,
        )

        # Wait for response (with timeout)
        response = self.message_bus.wait_for_response(
            agent_name=self.name.lower(),
            request_id=msg_id,
            timeout_seconds=15.0,
        )

        if response:
            try:
                result_data = json.loads(response.get("body", "{}"))
                result = result_data.get("result", {})

                # Format results for Discord
                if isinstance(result, dict):
                    summary = result.get("summary", "No results")
                    files = result.get("files", [])[:10]
                    matches = result.get("matches", [])[:5]

                    response_text = f"**Search Results:** {summary}\n"

                    if files:
                        response_text += "\n**Files found:**\n"
                        for f in files[:10]:
                            response_text += f"• `{f}`\n"

                    if matches:
                        response_text += "\n**Matches:**\n"
                        for m in matches[:5]:
                            response_text += f"• `{m.get('file')}:{m.get('line_number')}` - {m.get('content', '')[:50]}\n"

                    await ctx.send(response_text[:1900])
                else:
                    await ctx.send(f"Results: {str(result)[:1900]}")

            except Exception as e:
                await ctx.send(f"Error parsing results: {e}")
        else:
            await ctx.send("⏱️ Search timed out. Librarian may be busy.")

    async def _cmd_run(self, ctx: Any, command: Optional[str]) -> None:
        """Handle !run command - delegate to Executor."""
        if not command:
            await ctx.send("Usage: `!run <command>`\nExample: `!run ls -la`")
            return

        # Safety check - don't auto-run dangerous commands
        dangerous = ["rm -rf", "sudo", "mkfs", "dd if="]
        if any(d in command.lower() for d in dangerous):
            await ctx.send("⚠️ That command looks potentially dangerous. Please use `fda executor run` directly from the CLI.")
            return

        await ctx.send(f"⚡ Asking Executor to run: `{command}`")

        # Check if Executor is running
        executor_status = self.state.get_agent_status(Agents.EXECUTOR)
        if not executor_status or executor_status.get("status") != "running":
            await ctx.send("⚠️ Executor agent is not running. Start it with `fda start --all`")
            return

        # Send execute request
        msg_id = self.message_bus.request_execute(
            from_agent=self.name.lower(),
            command=command,
        )

        # Wait for response
        response = self.message_bus.wait_for_response(
            agent_name=self.name.lower(),
            request_id=msg_id,
            timeout_seconds=30.0,
        )

        if response:
            try:
                result_data = json.loads(response.get("body", "{}"))
                result = result_data.get("result", {})

                if isinstance(result, dict):
                    success = result.get("success", False)
                    stdout = result.get("stdout", "")[:1500]
                    stderr = result.get("stderr", "")[:500]

                    status_emoji = "✅" if success else "❌"
                    response_text = f"{status_emoji} **Command:** `{command}`\n"

                    if stdout:
                        response_text += f"```\n{stdout}\n```"
                    if stderr:
                        response_text += f"\n**Errors:**\n```\n{stderr}\n```"

                    await ctx.send(response_text[:1900])
                else:
                    await ctx.send(f"Result: {str(result)[:1900]}")

            except Exception as e:
                await ctx.send(f"Error parsing results: {e}")
        else:
            await ctx.send("⏱️ Command timed out.")

    async def _cmd_peers(self, ctx: Any) -> None:
        """Handle !peers command - show peer agent status."""
        statuses = self.state.get_all_agent_statuses()

        if not statuses:
            await ctx.send("No peer agents registered yet.")
            return

        lines = ["**Peer Agent Status**\n"]
        for status in statuses:
            name = status.get("agent_name", "unknown")
            state = status.get("status", "unknown")
            task = status.get("current_task", "")
            heartbeat = status.get("last_heartbeat", "")

            emoji = "🟢" if state == "running" else "🔴" if state == "stopped" else "🟡"
            line = f"{emoji} **{name}**: {state}"
            if task:
                line += f" - {task[:30]}"
            lines.append(line)

        await ctx.send("\n".join(lines))

    def _answer_question(self, question: str) -> str:
        """Answer a question using FDA agent capabilities and peer agents."""
        question_lower = question.lower()
        peer_result = None

        # Check if we should delegate to peers
        if any(phrase in question_lower for phrase in [
            "find file", "search", "where is", "what files", "python file"
        ]):
            # Delegate to Librarian
            librarian_status = self.state.get_agent_status(Agents.LIBRARIAN)
            if librarian_status and librarian_status.get("status") == "running":
                msg_id = self.message_bus.request_search(
                    from_agent=self.name.lower(),
                    query=question,
                )
                response = self.message_bus.wait_for_response(
                    agent_name=self.name.lower(),
                    request_id=msg_id,
                    timeout_seconds=10.0,
                )
                if response:
                    try:
                        peer_result = json.loads(response.get("body", "{}")).get("result")
                    except Exception:
                        pass

        context = self.get_project_context()

        # Add peer result if available
        if peer_result:
            context["peer_agent_result"] = peer_result

        # Search journal
        relevant = self.search_journal(question, top_n=3)
        if relevant:
            context["relevant_history"] = [
                {"summary": e.get("summary"), "author": e.get("author")}
                for e in relevant
            ]

        # If we have peer results, incorporate them
        if peer_result:
            enhanced_question = f"""{question}

[Librarian found this information:]
{json.dumps(peer_result, indent=2)[:1000]}

Please summarize naturally for voice response."""
        else:
            enhanced_question = question

        return self.chat_with_context(enhanced_question, context)

    def _detect_wake_word(self, text: str) -> tuple[bool, str]:
        """
        Check if text contains a wake word and extract the command.

        Args:
            text: Transcribed text to check.

        Returns:
            Tuple of (wake_word_detected, command_after_wake_word)
        """
        text_lower = text.lower().strip()

        for wake_word in self.WAKE_WORDS:
            if wake_word in text_lower:
                # Extract the command after the wake word
                idx = text_lower.find(wake_word)
                command = text[idx + len(wake_word):].strip()
                # Remove leading punctuation
                command = command.lstrip(",.!? ")
                return (True, command) if command else (True, "")

        return (False, "")

    async def _generate_meeting_summary(self) -> str:
        """Generate a summary of the meeting from the transcript."""
        if not self._transcript_buffer:
            return "No transcript available to summarize."

        # Build transcript text
        transcript_text = "\n".join([
            f"[{entry['timestamp'][11:19]}] {entry['speaker']}: {entry['text']}"
            for entry in self._transcript_buffer
        ])

        # Calculate meeting duration
        duration_str = ""
        if self._meeting_start_time:
            duration = datetime.now() - self._meeting_start_time
            minutes = int(duration.total_seconds() // 60)
            duration_str = f"Duration: {minutes} minutes\n"

        context = {
            "transcript": transcript_text[:8000],  # Limit for context window
            "entry_count": len(self._transcript_buffer),
            "duration": duration_str,
        }

        prompt = """Summarize this meeting transcript:

Please provide:
1. **Meeting Summary** (2-3 sentences)
2. **Key Discussion Points** (bullet points)
3. **Decisions Made** (if any)
4. **Action Items** (if any)
5. **Follow-ups Needed** (if any)

Keep it concise but capture the important points."""

        # Use Sonnet for meeting summaries - better at catching nuance in long transcripts
        summary = self.chat_with_context(prompt, context, model_override=MODEL_MEETING_SUMMARY)

        # Format the response
        return f"""📋 **Meeting Summary**
{duration_str}Transcript entries: {len(self._transcript_buffer)}

{summary}"""

    async def _start_voice_listening(self) -> None:
        """Start listening to voice in the connected channel."""
        if not self._voice_client or not self._voice_client.is_connected():
            return

        try:
            # Create voice sink
            loop = asyncio.get_event_loop()
            self._voice_sink = VoiceListeningSink(self, loop)

            # Start listening (Discord.py voice receive)
            self._voice_client.start_recording(
                self._voice_sink,
                self._on_voice_receive_finished,
                None
            )
            logger.info("[DiscordBot] Voice listening started")

        except Exception as e:
            logger.error(f"[DiscordBot] Failed to start voice listening: {e}")
            # Voice receiving might not be available
            self._voice_sink = None

    def _stop_voice_listening(self) -> None:
        """Stop voice listening."""
        if self._voice_client and self._voice_client.is_recording():
            try:
                self._voice_client.stop_recording()
            except Exception as e:
                logger.error(f"[DiscordBot] Error stopping recording: {e}")

        if self._voice_sink:
            self._voice_sink.cleanup()
            self._voice_sink = None

        logger.info("[DiscordBot] Voice listening stopped")

    def _on_voice_receive_finished(self, sink: Any, *args) -> None:
        """Callback when voice recording is stopped."""
        logger.info("[DiscordBot] Voice receive finished")

    async def _transcribe_audio_async(self, audio_data: bytes) -> str:
        """Transcribe audio using Whisper API (async wrapper)."""
        return await asyncio.get_event_loop().run_in_executor(
            None,
            self._transcribe_audio_sync,
            audio_data
        )

    def _transcribe_audio_sync(self, audio_data: bytes) -> str:
        """Transcribe audio using Whisper API (sync version)."""
        try:
            client = self._get_openai_client()

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

    async def _handle_voice_command(self, username: str, command: str) -> None:
        """Handle a voice command after wake word detection."""
        if not command:
            # Just wake word with no command
            await self._speak_text("Yes? How can I help you?")
            return

        logger.info(f"[DiscordBot] Processing voice command from {username}: {command}")

        try:
            # Get response from FDA
            response = self._answer_question(command)

            # Speak the response
            await self._speak_text(response)

            # Also send to text channel if available
            if self._response_channel:
                await self._response_channel.send(f"**{username}**: {command}\n\n**FDA**: {response}")

            # Add to transcript
            self._add_to_transcript(username, f"[Voice] {command}")
            self._add_to_transcript("FDA", response)

        except Exception as e:
            logger.error(f"[DiscordBot] Error handling voice command: {e}")
            await self._speak_text("Sorry, I had trouble processing that. Could you try again?")

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

        This is the PRIMARY user interface for FDA.
        """
        logger.info("[DiscordBot] Starting event loop (PRIMARY interface)...")

        # Update agent status
        self.state.update_agent_status("discord", "running", "Discord bot starting")

        bot = self._get_bot()

        # Add on_ready handler to update status
        @bot.event
        async def on_ready():
            logger.info(f"[DiscordBot] Logged in as {bot.user}")
            print(f"Discord bot ready: {bot.user}")
            self.state.update_agent_status("discord", "running", f"Connected as {bot.user}")

        try:
            bot.run(self.bot_token)
        except KeyboardInterrupt:
            logger.info("[DiscordBot] Received shutdown signal")
        except Exception as e:
            logger.error(f"[DiscordBot] Error in event loop: {e}")

        self.state.update_agent_status("discord", "stopped")
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
