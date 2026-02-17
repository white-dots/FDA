"""
Discord Voice Bot - PRIMARY user interface for FDA system.

This is the main way users interact with FDA:
- Voice commands in Discord channels via OpenAI Realtime API
- Low-latency speech-to-speech conversation (~300-600ms)
- Meeting attendance and transcription
- Real-time voice responses

The Discord bot routes user requests to FDA, which then collaborates
with Librarian and Executor peers to fulfill them.

Voice Architecture:
  Discord Voice (48kHz stereo) <-> VoiceStreamSink (resamples)
      <-> OpenAI Realtime API (24kHz mono, WebSocket)
"""

import asyncio
import io
import logging
import os
import tempfile
import time
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

try:
    from discord.sinks import Sink as _PycordSink
    _HAS_PYCORD_SINK = True
except ImportError:
    _PycordSink = object
    _HAS_PYCORD_SINK = False

logger = logging.getLogger(__name__)


class VoiceStreamSink(_PycordSink):
    """
    Audio sink that streams Discord voice to OpenAI Realtime API.

    Extends py-cord's Sink to receive audio via VoiceClient.start_recording().
    Instead of buffering and doing Whisper batch transcription, this streams
    audio directly to the Realtime API WebSocket for real-time processing.

    The Realtime API handles:
    - Voice Activity Detection (VAD)
    - Speech-to-text transcription
    - LLM response generation
    - Text-to-speech output
    All in a single WebSocket connection with ~300-600ms latency.
    """

    def __init__(self, agent: "DiscordVoiceAgent", loop: asyncio.AbstractEventLoop):
        if _HAS_PYCORD_SINK:
            super().__init__(filters=None)
        self.agent = agent
        self.loop = loop

    def write(self, data: Any, user: Any) -> None:
        """Called by py-cord when audio data is received from a user.

        Streams audio directly to the Realtime API session.
        This method is called from py-cord's DecodeManager thread.
        """
        try:
            if user is None:
                return
            # Skip bot audio
            if hasattr(user, 'bot') and user.bot:
                return

            # Convert PCM data to bytes
            pcm_data = data.pcm if hasattr(data, 'pcm') else bytes(data)

            # Stream to Realtime API (handles resampling internally)
            if self.agent._realtime_session and self.agent._realtime_session.connected:
                self.agent._realtime_session.send_audio(pcm_data)

        except Exception as e:
            logger.error(f"[VoiceStreamSink] Error in write(): {e}", exc_info=True)

    def cleanup(self) -> None:
        """Clean up resources."""
        if _HAS_PYCORD_SINK:
            try:
                super().cleanup()
            except Exception:
                pass


class VoiceListeningSink(_PycordSink):
    """
    Legacy audio sink for Whisper-based transcription (meeting mode).

    Used only for meeting transcription mode where we need full transcripts
    of all speakers without the Realtime API's conversational behavior.
    """

    SAMPLE_RATE = 48000
    CHANNELS = 2
    SAMPLE_WIDTH = 2
    SILENCE_THRESHOLD = 500
    SILENCE_DURATION = 1.5
    MIN_SPEECH_DURATION = 0.5
    MAX_SPEECH_DURATION = 30.0

    def __init__(self, agent: "DiscordVoiceAgent", loop: asyncio.AbstractEventLoop):
        if _HAS_PYCORD_SINK:
            super().__init__(filters=None)
        self.agent = agent
        self.loop = loop
        self.user_buffers: dict[int, list[bytes]] = defaultdict(list)
        self.user_silence_start: dict[int, float] = {}
        self.user_speech_start: dict[int, float] = {}
        self.processing_users: set[int] = set()

    def write(self, data: Any, user: Any) -> None:
        """Called by py-cord when audio data is received."""
        try:
            if user is None:
                return
            if hasattr(user, 'bot') and user.bot:
                return

            user_id = user.id if hasattr(user, 'id') else int(user)
            if user_id in self.processing_users:
                return

            pcm_data = data.pcm if hasattr(data, 'pcm') else bytes(data)
            rms = self._calculate_rms(pcm_data)
            current_time = time.monotonic()
            display_name = user.display_name if hasattr(user, 'display_name') else str(user_id)

            if rms > self.SILENCE_THRESHOLD:
                self.user_buffers[user_id].append(pcm_data)
                self.user_silence_start.pop(user_id, None)
                if user_id not in self.user_speech_start:
                    self.user_speech_start[user_id] = current_time
                speech_duration = current_time - self.user_speech_start.get(user_id, current_time)
                if speech_duration > self.MAX_SPEECH_DURATION:
                    self._schedule_processing(user_id, display_name)
            else:
                if user_id in self.user_speech_start:
                    if user_id not in self.user_silence_start:
                        self.user_silence_start[user_id] = current_time
                    else:
                        silence_duration = current_time - self.user_silence_start[user_id]
                        speech_duration = self.user_silence_start[user_id] - self.user_speech_start.get(user_id, 0)
                        if silence_duration > self.SILENCE_DURATION and speech_duration > self.MIN_SPEECH_DURATION:
                            self._schedule_processing(user_id, display_name)
        except Exception as e:
            logger.error(f"[VoiceSink] Error in write(): {e}", exc_info=True)

    def _calculate_rms(self, pcm_data: bytes) -> float:
        if len(pcm_data) < 2:
            return 0
        samples = struct.unpack(f"{len(pcm_data) // 2}h", pcm_data)
        if not samples:
            return 0
        sum_squares = sum(s * s for s in samples)
        return (sum_squares / len(samples)) ** 0.5

    def _schedule_processing(self, user_id: int, username: str) -> None:
        if user_id in self.processing_users:
            return
        self.processing_users.add(user_id)
        audio_data = b"".join(self.user_buffers[user_id])
        self.user_buffers[user_id] = []
        self.user_silence_start.pop(user_id, None)
        self.user_speech_start.pop(user_id, None)
        asyncio.run_coroutine_threadsafe(
            self._process_audio(user_id, username, audio_data),
            self.loop
        )

    async def _process_audio(self, user_id: int, username: str, audio_data: bytes) -> None:
        try:
            if len(audio_data) < self.SAMPLE_RATE * self.SAMPLE_WIDTH * self.CHANNELS * 0.3:
                return
            wav_data = self._pcm_to_wav(audio_data)
            text = await self.agent._transcribe_audio_async(wav_data)
            if not text or len(text.strip()) < 2:
                return
            logger.info(f"[DiscordBot] Meeting transcription from {username}: {text}")
            self.agent._add_to_transcript(username, text)
        except Exception as e:
            logger.error(f"[DiscordBot] Error processing audio: {e}")
        finally:
            self.processing_users.discard(user_id)

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(self.CHANNELS)
            wav_file.setsampwidth(self.SAMPLE_WIDTH)
            wav_file.setframerate(self.SAMPLE_RATE)
            wav_file.writeframes(pcm_data)
        wav_buffer.seek(0)
        return wav_buffer.read()

    def cleanup(self) -> None:
        self.user_buffers.clear()
        self.user_silence_start.clear()
        self.user_speech_start.clear()
        self.processing_users.clear()
        if _HAS_PYCORD_SINK:
            try:
                super().cleanup()
            except Exception:
                pass


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

# Instructions for the OpenAI Realtime API voice session
# These are sent as the 'instructions' field in session.update
REALTIME_VOICE_INSTRUCTIONS = """You are FDA (Facilitating Director Agent), a personal AI assistant for John, a CEO and data scientist at Datacore.

## Personality & Tone
- Warm, professional, and efficient
- Speak naturally — like a helpful colleague in the same room
- Keep responses to 2-3 sentences unless the user asks for more detail
- Use simple, clear language that sounds natural when spoken aloud
- Address the user as "John" when appropriate

## Context
- John works on SmartStore (Naver Commerce API integration), Wholesum, and other projects
- He manages a team and is interested in cold outreach, B2C/B2B sales, ad analytics, LinkedIn
- Technical terms he uses: Python, backend, frontend, deploy, production, Datacore, SmartStore, aonebnh, sofsys

## Language & Pronunciation
- Always respond in English
- The user speaks English with a Korean accent — be patient with unclear audio
- "FDA" is pronounced "eff-dee-ay" (the agent's name)
- If you can't understand what the user said, politely ask them to repeat

## Behavior Rules
- Never produce background sounds, music, or sound effects
- Never switch language unless explicitly asked
- If unsure about what the user is asking, ask a brief clarifying question
- For complex questions that require file search or command execution, acknowledge that you would need to check and provide what you know
- When greeted (just "hey" or "hi"), respond warmly: "Hey John, how can I help?"

## Response Style
- For quick questions: 1-2 sentences
- For explanations: 2-3 sentences, summarize key points
- Never give lists longer than 3 items in voice (suggest text for longer lists)
- Use natural conversational fillers when needed ("Let me think about that...", "Good question...")
"""


class DiscordVoiceAgent(BaseAgent):
    """
    Discord Voice Agent - PRIMARY user interface for FDA.

    Uses OpenAI Realtime API for low-latency voice conversation:
    - Speech-to-speech via WebSocket (~300-600ms latency)
    - Server-side VAD (no wake word needed while in voice)
    - Streaming audio I/O
    - Meeting attendance and transcription (Whisper fallback)
    - Delegation to peer agents (Librarian, Executor) via FDAAgent
    """

    def __init__(
        self,
        bot_token: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        project_state_path: Optional[Path] = None,
        fda_agent: Optional[Any] = None,
    ):
        """
        Initialize the Discord Voice Agent.

        Args:
            bot_token: Discord bot token. If not provided, reads from
                      DISCORD_BOT_TOKEN environment variable.
            openai_api_key: OpenAI API key for Whisper/TTS. If not provided,
                           reads from OPENAI_API_KEY environment variable.
            project_state_path: Path to the project state database.
            fda_agent: Optional FDAAgent instance for full delegation support.
                      If not provided, one will be created automatically.
        """
        super().__init__(
            name="DiscordBot",
            model=MODEL_FDA,
            system_prompt=DISCORD_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

        # Initialize or create FDAAgent for proper peer delegation
        self._fda_agent = fda_agent
        if self._fda_agent is None:
            try:
                from fda.fda_agent import FDAAgent
                self._fda_agent = FDAAgent(state_path=project_state_path)
                logger.info("[DiscordBot] Created FDAAgent for peer delegation")
            except Exception as e:
                logger.warning(f"[DiscordBot] Could not create FDAAgent: {e}. "
                             "Falling back to local answering.")

        self.bot_token = bot_token or os.environ.get(DISCORD_BOT_TOKEN_ENV)
        if not self.bot_token:
            raise ValueError(
                f"Discord bot token required. Set {DISCORD_BOT_TOKEN_ENV} "
                "environment variable or pass bot_token parameter."
            )

        self.openai_api_key = (
            openai_api_key
            or os.environ.get(OPENAI_API_KEY_ENV)
            or self.state.get_context("openai_api_key")
        )
        self._openai_client = None

        self._bot = None
        self._voice_client = None
        self._voice_sink: Optional[Any] = None  # VoiceStreamSink or VoiceListeningSink
        self._current_session_id: Optional[str] = None
        self._transcript_buffer: list[dict[str, Any]] = []
        self._response_channel = None  # Channel to send text responses

        # OpenAI Realtime API session
        self._realtime_session = None  # RealtimeVoiceSession
        self._playback_task: Optional[asyncio.Task] = None  # Audio playback loop

        # Meeting mode state
        self._meeting_mode = False
        self._meeting_start_time: Optional[datetime] = None

        # Voice listening state
        self._listening_enabled = True  # Enable voice listening by default

        # Use the state DB for conversation history (persists all day)
        # The FDA agent's state DB is preferred since it feeds into the journal
        self._state_db = self._fda_agent.state if self._fda_agent else self.state

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
        intents.members = True

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

        @self._bot.command(name="dailybrief")
        async def daily_brief(ctx):
            """Give a live daily briefing via voice and text."""
            await self._cmd_dailybrief(ctx)

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
            await self._stop_voice_listening()
            await ctx.send("Voice listening **disabled**. Use `!ask` to talk to me instead.")

        else:
            await ctx.send("Usage: `!listen on` or `!listen off`")

    def _save_message(self, channel_id: int, role: str, content: str, username: str = None) -> None:
        """Persist a message to the state DB."""
        try:
            self._state_db.add_discord_message(
                channel_id=str(channel_id),
                role=role,
                content=content,
                username=username,
            )
        except Exception as e:
            logger.error(f"[DiscordBot] Failed to save message: {e}")

    def _get_conversation_history(self, channel_id: int) -> list[dict[str, str]]:
        """Load today's conversation history from the state DB for context."""
        try:
            # Get recent messages for this channel (last 20 for LLM context window)
            messages = self._state_db.get_discord_messages_recent(
                channel_id=str(channel_id),
                limit=20,
            )
            return [
                {"role": msg["role"], "content": msg["content"]}
                for msg in messages
            ]
        except Exception as e:
            logger.error(f"[DiscordBot] Failed to load history: {e}")
            return []

    async def _cmd_dailybrief(self, ctx: Any) -> None:
        """Handle !dailybrief command - generate and speak a daily briefing."""
        if not self._fda_agent:
            await ctx.send("FDA agent is not available.")
            return

        # Get user name for personalized message
        user_name = self._fda_agent.state.get_context("user_name") or "there"
        await ctx.send(f"Good to see you, {user_name}! Let me prepare your daily briefing...")

        try:
            # Generate the brief in a background thread (blocking LLM call)
            loop = asyncio.get_event_loop()
            brief = await loop.run_in_executor(None, self._fda_agent.generate_daily_brief)

            # Send as text in the channel
            if len(brief) > 2000:
                for i in range(0, len(brief), 1900):
                    await ctx.send(brief[i:i+1900])
            else:
                await ctx.send(brief)

            # If in voice channel, speak it out loud
            if self._voice_client and self._voice_client.is_connected():
                # Trim for TTS — keep under ~500 chars for natural pacing
                tts_text = brief if len(brief) < 1000 else brief[:1000]
                await self._speak_text(tts_text)

        except Exception as e:
            logger.error(f"[DiscordBot] Error generating daily brief: {e}")
            await ctx.send(f"Sorry, I couldn't generate your daily briefing: {e}")

    def _get_greeting(self) -> str:
        """Get a personalized greeting using the user's name from state DB."""
        user_name = None
        if self._fda_agent:
            user_name = self._fda_agent.state.get_context("user_name")
        if not user_name:
            user_name = self.state.get_context("user_name")
        if user_name:
            return f"Hey {user_name}! Give me a moment to think... 🤔"
        return "Give me a moment to think... 🤔"

    async def _handle_plain_message(self, message: Any, content: str) -> None:
        """Handle plain text messages (not commands)."""
        try:
            # Send immediate personalized greeting so user knows we're working
            greeting = self._get_greeting()
            thinking_msg = await message.reply(greeting)

            # Load conversation history from state DB
            channel_id = message.channel.id
            history = self._get_conversation_history(channel_id)

            # Save user message to DB right away
            username = str(message.author.display_name) if hasattr(message.author, 'display_name') else str(message.author)
            self._save_message(channel_id, "user", content, username=username)

            # Run the blocking LLM call in a thread to not freeze Discord
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, self._answer_question, content, history
            )

            # Save assistant response to DB
            self._save_message(channel_id, "assistant", response)

            # Edit the "Thinking..." message with the actual response
            if len(response) > 2000:
                await thinking_msg.edit(content=response[:1900])
                for i in range(1900, len(response), 1900):
                    await message.reply(response[i:i+1900])
            else:
                await thinking_msg.edit(content=response)

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
        if not ctx.guild:
            await ctx.send("This command only works in a server, not in DMs.")
            return

        # Get the Member object — ctx.author might be a User without .voice
        # Try multiple methods to get the member with voice state
        member = None

        # Method 1: ctx.author might already be a Member
        if hasattr(ctx.author, "voice"):
            member = ctx.author

        # Method 2: look up from guild cache
        if member is None:
            member = ctx.guild.get_member(ctx.author.id)

        # Method 3: fetch from API if cache miss
        if member is None:
            try:
                member = await ctx.guild.fetch_member(ctx.author.id)
            except Exception as e:
                logger.error(f"[DiscordBot] Failed to fetch member: {e}")

        if member is None or not hasattr(member, "voice"):
            await ctx.send("Could not get your member info. Make sure you're in a server channel.")
            return

        if not member.voice:
            await ctx.send("You need to be in a voice channel first!")
            return

        channel = member.voice.channel

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
                await ctx.send(f"Joined **{channel.name}**. I'm listening! Just speak naturally and I'll respond.")
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

            # Stop voice listening (async now for Realtime session cleanup)
            await self._stop_voice_listening()

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

        greeting = self._get_greeting()
        thinking_msg = await ctx.send(greeting)

        try:
            # Load conversation history from state DB
            channel_id = ctx.channel.id
            history = self._get_conversation_history(channel_id)

            # Save user message to DB
            username = str(ctx.author.display_name) if hasattr(ctx.author, 'display_name') else str(ctx.author)
            self._save_message(channel_id, "user", question, username=username)

            # Run the blocking LLM call in a thread so Discord stays responsive
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, self._answer_question, question, history
            )

            # Save assistant response to DB
            self._save_message(channel_id, "assistant", response)

            # Edit the greeting message with the actual response
            if len(response) > 2000:
                await thinking_msg.edit(content=response[:1900])
                for i in range(1900, len(response), 1900):
                    await ctx.send(response[i:i+1900])
            else:
                await thinking_msg.edit(content=response)

            # If in voice, also speak the response
            if self._voice_client and self._voice_client.is_connected():
                if len(response) < 500:
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
`!join` - Join your voice channel (starts real-time voice conversation!)
`!leave` - Leave voice channel
`!say <text>` - Speak text in voice channel
`!listen on/off` - Toggle voice listening

**Voice Conversation:**
When I'm in a voice channel, just **speak naturally** and I'll respond!
No wake word needed — powered by OpenAI Realtime API for low-latency conversation.

**Text Commands:**
`!ask <question>` - Ask FDA anything (or just type without !)
`!dailybrief` - Get your personalized daily briefing (voice + text)
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

    def _answer_question_voice(self, question: str, conversation_history: list[dict[str, str]] = None) -> str:
        """Fast voice-optimized answer — single LLM call, no delegation chain.

        Skips Librarian/Executor/Claude Code for speed. Uses conversation
        history and project context to answer directly.
        """
        agent = self._fda_agent or self
        context = {}

        # Add user context
        user_name = agent.state.get_context("user_name")
        if user_name:
            context["user"] = {
                "name": user_name,
                "role": agent.state.get_context("user_role"),
                "goals": agent.state.get_context("user_goals"),
            }

        # Add conversation history
        if conversation_history:
            convo_lines = []
            for msg in conversation_history[-10:]:  # Last 10 messages for speed
                role = "User" if msg.get("role") == "user" else "FDA"
                convo_lines.append(f"{role}: {msg.get('content', '')[:200]}")
            context["recent_conversation"] = "\n".join(convo_lines)

        # Add project context (lightweight)
        try:
            project_ctx = agent.get_project_context()
            context.update(project_ctx)
        except Exception:
            pass

        # Single fast LLM call
        voice_prompt = (
            f"{question}\n\n"
            "[VOICE MODE: Keep your response concise and conversational — "
            "2-3 sentences max. This will be spoken aloud via TTS.]"
        )

        try:
            return agent.chat_with_context(voice_prompt, context)
        except Exception as e:
            logger.error(f"[DiscordBot] Voice answer failed: {e}")
            return "Sorry, I had trouble with that. Could you try again?"

    def _answer_question(self, question: str, conversation_history: list[dict[str, str]] = None) -> str:
        """Answer a question using FDAAgent's full delegation pipeline.

        Delegates to FDAAgent.ask() which handles routing to:
        - Librarian (file search, knowledge queries)
        - Executor (command execution, Claude Code)
        - Direct API response (simple questions)

        Args:
            question: The user's question.
            conversation_history: Recent conversation exchanges for context continuity.
        """
        # Use FDAAgent for full peer delegation support
        if self._fda_agent is not None:
            try:
                return self._fda_agent.ask(question, conversation_history=conversation_history)
            except Exception as e:
                logger.error(f"[DiscordBot] FDAAgent.ask() failed: {e}")
                # Fall through to local fallback

        # Fallback: answer directly if FDAAgent is unavailable
        logger.warning("[DiscordBot] FDAAgent unavailable, answering directly")
        context = self.get_project_context()

        # Search journal for relevant context
        relevant = self.search_journal(question, top_n=3)
        if relevant:
            context["relevant_history"] = [
                {"summary": e.get("summary"), "author": e.get("author")}
                for e in relevant
            ]

        return self.chat_with_context(question, context)

    def _detect_wake_word(self, text: str) -> tuple[bool, str]:
        """
        Check if text contains a wake word and extract the command.

        Args:
            text: Transcribed text to check.

        Returns:
            Tuple of (wake_word_detected, command_after_wake_word)
        """
        text_lower = text.lower().strip()
        # Normalize: remove dots/periods and collapse multiple spaces
        text_normalized = text_lower.replace(".", " ").replace(",", " ")
        import re as _re
        text_normalized = _re.sub(r'\s+', ' ', text_normalized).strip()

        for wake_word in self.WAKE_WORDS:
            # Check both original and normalized text
            if wake_word in text_lower or wake_word in text_normalized:
                # Find position in whichever matched
                matched_text = text_normalized if wake_word in text_normalized else text_lower
                idx = matched_text.find(wake_word)
                command = matched_text[idx + len(wake_word):].strip()
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
        """Start listening to voice in the connected channel.

        Creates an OpenAI Realtime API session and streams Discord audio to it.
        The Realtime API handles VAD, STT, LLM response, and TTS — all in one
        WebSocket connection for ultra-low latency.
        """
        if not self._voice_client or not self._voice_client.is_connected():
            return

        try:
            loop = asyncio.get_event_loop()

            # Create and connect the Realtime API session
            from fda.realtime_voice import RealtimeVoiceSession

            # Build personalized instructions
            instructions = REALTIME_VOICE_INSTRUCTIONS
            user_name = None
            if self._fda_agent:
                user_name = self._fda_agent.state.get_context("user_name")
            if not user_name:
                user_name = self.state.get_context("user_name")
            if user_name:
                instructions = instructions.replace("John", user_name)

            self._realtime_session = RealtimeVoiceSession(
                api_key=self.openai_api_key,
                on_audio_out=self._on_realtime_audio_out,
                on_transcript_in=self._on_realtime_transcript_in,
                on_transcript_out=self._on_realtime_transcript_out,
                on_speech_started=self._on_realtime_speech_started,
                on_response_done=self._on_realtime_response_done,
                on_error=self._on_realtime_error,
                instructions=instructions,
            )

            await self._realtime_session.connect()

            # Inject recent conversation context so the AI knows what's been discussed
            channel_id = self._response_channel.id if self._response_channel else 0
            if channel_id:
                history = self._get_conversation_history(channel_id)
                if history:
                    context_text = "Recent conversation context:\n"
                    for msg in history[-5:]:
                        role = "User" if msg.get("role") == "user" else "FDA"
                        context_text += f"{role}: {msg.get('content', '')[:200]}\n"
                    await self._realtime_session.inject_context(context_text, role="user")
                    await self._realtime_session.inject_context(
                        "Got it, I have the conversation context. I'm ready to continue helping.",
                        role="assistant",
                    )

            # Create the streaming audio sink and start recording from Discord
            self._voice_sink = VoiceStreamSink(self, loop)
            self._voice_client.start_recording(
                self._voice_sink,
                self._on_voice_receive_finished,
                None,
            )

            # Start the audio playback loop (streams Realtime API audio to Discord)
            self._playback_task = asyncio.create_task(self._audio_playback_loop())

            logger.info("[DiscordBot] Realtime voice session started")

        except Exception as e:
            logger.error(f"[DiscordBot] Failed to start Realtime voice: {e}", exc_info=True)
            self._realtime_session = None
            self._voice_sink = None

            # Fallback: try legacy Whisper-based listening
            logger.info("[DiscordBot] Falling back to legacy Whisper-based voice...")
            await self._start_voice_listening_legacy()

    async def _start_voice_listening_legacy(self) -> None:
        """Legacy Whisper-based voice listening (fallback / meeting mode)."""
        if not self._voice_client or not self._voice_client.is_connected():
            return

        try:
            loop = asyncio.get_event_loop()
            self._voice_sink = VoiceListeningSink(self, loop)
            self._voice_client.start_recording(
                self._voice_sink,
                self._on_voice_receive_finished,
                None,
            )
            logger.info("[DiscordBot] Legacy voice listening started")
        except Exception as e:
            logger.error(f"[DiscordBot] Failed to start legacy voice listening: {e}")
            self._voice_sink = None

    async def _stop_voice_listening(self) -> None:
        """Stop voice listening and disconnect Realtime session."""
        # Stop the playback task
        if self._playback_task:
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass
            self._playback_task = None

        # Disconnect Realtime session
        if self._realtime_session:
            try:
                await self._realtime_session.disconnect()
            except Exception as e:
                logger.error(f"[RealtimeVoice] Error disconnecting: {e}")
            self._realtime_session = None

        # Stop Discord recording
        if self._voice_client and self._voice_sink:
            try:
                self._voice_client.stop_recording()
            except Exception as e:
                logger.error(f"[DiscordBot] Error stopping recording: {e}")

        if self._voice_sink:
            self._voice_sink.cleanup()
            self._voice_sink = None

        logger.info("[DiscordBot] Voice listening stopped")

    def _on_voice_receive_finished(self, sink: Any, *args) -> None:
        """Callback when voice recording is stopped (py-cord callback)."""
        logger.info("[DiscordBot] Voice receive finished")

    # ----- Realtime API audio playback -----

    async def _audio_playback_loop(self) -> None:
        """Monitor and manage audio playback from Realtime API to Discord.

        Checks for buffered audio from the Realtime session and starts
        Discord playback when audio is available. The actual PCM reading
        is done by _RealtimePCMSource in Discord's player thread.
        """
        logger.info("[DiscordBot] Audio playback loop started")

        try:
            while self._realtime_session and self._realtime_session.connected:
                if not self._voice_client or not self._voice_client.is_connected():
                    await asyncio.sleep(0.1)
                    continue

                # If there's audio to play and we're not already playing
                if (self._realtime_session.has_output_audio()
                        and not self._voice_client.is_playing()):
                    try:
                        source = _RealtimePCMSource(self._realtime_session)
                        self._voice_client.play(
                            source,
                            after=lambda e: logger.debug("[DiscordBot] PCM playback segment finished")
                        )
                    except Exception as e:
                        if "Already playing" not in str(e):
                            logger.error(f"[DiscordBot] Playback error: {e}")

                await asyncio.sleep(0.02)  # 20ms check interval

        except asyncio.CancelledError:
            logger.info("[DiscordBot] Audio playback loop cancelled")
        except Exception as e:
            logger.error(f"[DiscordBot] Audio playback loop error: {e}", exc_info=True)

    # ----- Realtime API callbacks -----

    async def _on_realtime_audio_out(self, pcm_48k_stereo: bytes) -> None:
        """Called when Realtime API produces audio output.

        Audio is buffered in the session. The playback loop handles starting
        Discord playback when audio is available. This callback is for logging only.
        """
        pass  # Playback managed by _audio_playback_loop

    async def _on_realtime_transcript_in(self, transcript: str) -> None:
        """Called when user's speech is transcribed by the Realtime API."""
        logger.info(f"[DiscordBot] User said: {transcript}")

        # Save to conversation history
        channel_id = self._response_channel.id if self._response_channel else 0
        if channel_id:
            self._save_message(channel_id, "user", transcript)

        # Add to transcript buffer
        self._add_to_transcript("User", transcript)

        # Post to text channel
        if self._response_channel:
            try:
                await self._response_channel.send(f"**User**: {transcript}")
            except Exception:
                pass

    async def _on_realtime_transcript_out(self, transcript: str) -> None:
        """Called when assistant's speech transcript is complete."""
        logger.info(f"[DiscordBot] FDA said: {transcript}")

        # Save to conversation history
        channel_id = self._response_channel.id if self._response_channel else 0
        if channel_id:
            self._save_message(channel_id, "assistant", transcript)

        # Add to transcript buffer
        self._add_to_transcript("FDA", transcript)

        # Post to text channel
        if self._response_channel:
            try:
                await self._response_channel.send(f"**FDA**: {transcript}")
            except Exception:
                pass

    async def _on_realtime_speech_started(self) -> None:
        """Called when VAD detects user speech (useful for interruptions)."""
        # If Discord is playing our audio, stop it to let the user speak
        if self._voice_client and self._voice_client.is_playing():
            self._voice_client.stop()
            logger.debug("[DiscordBot] Stopped playback for user interruption")

    async def _on_realtime_response_done(self, response: dict) -> None:
        """Called when a full Realtime API response is complete."""
        usage = response.get("usage", {})
        logger.info(
            f"[DiscordBot] Response complete. "
            f"Tokens: {usage.get('total_tokens', '?')}"
        )

    async def _on_realtime_error(self, error_msg: str) -> None:
        """Called when the Realtime API reports an error."""
        logger.error(f"[DiscordBot] Realtime API error: {error_msg}")
        if self._response_channel:
            try:
                await self._response_channel.send(f"⚠️ Voice error: {error_msg[:200]}")
            except Exception:
                pass

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
                language="en",
                prompt=(
                    "Hey FDA, FDA, daily brief, SmartStore, Datacore, "
                    "cold mail, cold email, sending cold mails, B2C, B2B, "
                    "outreach, prospecting, client acquisition, "
                    "aonebnh, sofsys, Naver, Commerce API, "
                    "Wholesum, sales dashboard, ad stats, "
                    "CEO, data scientist, LinkedIn, resume, "
                    "journal, librarian, executor, agent, "
                    "Python, backend, frontend, deploy, production"
                ),
            )

            return response.text

        except Exception as e:
            logger.error(f"[DiscordBot] Transcription error: {e}")
            return ""

    async def _handle_voice_command(self, username: str, command: str) -> None:
        """Handle a voice command after wake word detection."""
        if not command:
            # Just wake word with no command — greet by name
            user_name = None
            if self._fda_agent:
                user_name = self._fda_agent.state.get_context("user_name")
            if not user_name:
                user_name = self.state.get_context("user_name")
            greeting = f"Hey {user_name}, how may I assist you today?" if user_name else "Hey, how may I assist you today?"
            await self._speak_text(greeting)
            return

        logger.info(f"[DiscordBot] Processing voice command from {username}: {command}")

        try:
            # Load conversation history from state DB
            channel_id = self._response_channel.id if self._response_channel else 0
            history = self._get_conversation_history(channel_id) if channel_id else []

            # Save user voice message to DB
            if channel_id:
                self._save_message(channel_id, "user", command, username=username)

            # Get response from FDA using fast voice path (single LLM call)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: self._answer_question_voice(command, history)
            )

            # Save assistant response to DB
            if channel_id:
                self._save_message(channel_id, "assistant", response)

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
                speed=1.15,
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
                language="en",
                prompt=(
                    "Hey FDA, FDA, daily brief, SmartStore, Datacore, "
                    "cold mail, cold email, sending cold mails, B2C, B2B, "
                    "outreach, prospecting, client acquisition, "
                    "aonebnh, sofsys, Naver, Commerce API, "
                    "Wholesum, sales dashboard, ad stats, "
                    "CEO, data scientist, LinkedIn, resume, "
                    "journal, librarian, executor, agent, "
                    "Python, backend, frontend, deploy, production"
                ),
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


class _RealtimePCMSource:
    """Discord AudioSource that reads PCM from a RealtimeVoiceSession's output buffer.

    Implements discord.AudioSource interface by providing a read() method
    that returns 20ms frames of 48kHz stereo 16-bit PCM audio.
    """

    # 20ms frame at 48kHz, stereo, 16-bit = 3840 bytes
    FRAME_SIZE = 3840

    def __init__(self, session):
        self._session = session
        self._buffer = bytearray()
        self._finished = False

    def read(self) -> bytes:
        """Read one 20ms audio frame for Discord.

        Returns exactly FRAME_SIZE bytes of PCM data, or empty bytes
        if no more audio is available (which stops the player).
        """
        if self._finished:
            return b""

        # Try to pull from session's synchronous buffer
        try:
            # We need to access the buffer synchronously since read() is called
            # from Discord's player thread (not the asyncio loop)
            buf = self._session._output_audio_buffer
            if len(buf) >= self.FRAME_SIZE:
                # Take exactly one frame
                frame = bytes(buf[:self.FRAME_SIZE])
                del buf[:self.FRAME_SIZE]
                return frame
            elif len(buf) > 0:
                # Pad with silence to fill the frame
                frame = bytes(buf) + b"\x00" * (self.FRAME_SIZE - len(buf))
                buf.clear()
                return frame
            else:
                # No audio available — return silence briefly, then stop
                # Return empty to indicate we're done for now
                return b""
        except Exception:
            return b""

    def is_opus(self) -> bool:
        """We provide raw PCM, not Opus."""
        return False

    def cleanup(self) -> None:
        """Clean up when player stops."""
        self._finished = True


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
