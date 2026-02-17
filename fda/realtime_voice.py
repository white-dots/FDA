"""
OpenAI Realtime API voice session for Discord integration.

Bridges Discord's voice audio (48kHz stereo PCM) with the OpenAI
Realtime API (24kHz mono PCM) via WebSocket. Handles:
- Audio resampling (48kHz stereo <-> 24kHz mono)
- WebSocket event protocol
- Server VAD (voice activity detection)
- Streaming audio playback to Discord
- Interruption handling
- Session lifecycle management
"""

import asyncio
import base64
import json
import logging
import struct
import time
from typing import Any, Callable, Coroutine, Optional

try:
    import websockets
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False

from fda.config import (
    OPENAI_REALTIME_MODEL,
    OPENAI_REALTIME_VOICE,
    OPENAI_REALTIME_URL,
    OPENAI_API_KEY_ENV,
)

logger = logging.getLogger(__name__)


def _downsample_48k_stereo_to_24k_mono(pcm_48k_stereo: bytes) -> bytes:
    """Convert 48kHz stereo 16-bit PCM to 24kHz mono 16-bit PCM.

    Discord sends 48kHz, 2-channel, 16-bit signed LE.
    OpenAI Realtime API expects 24kHz, 1-channel, 16-bit signed LE.

    Strategy: average stereo channels to mono, then take every other sample
    (48k / 2 = 24k).
    """
    if len(pcm_48k_stereo) < 4:
        return b""

    # Unpack as signed 16-bit LE samples
    num_samples = len(pcm_48k_stereo) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm_48k_stereo)

    # Step 1: stereo to mono (average L+R pairs)
    mono_samples = []
    for i in range(0, len(samples) - 1, 2):
        avg = (samples[i] + samples[i + 1]) // 2
        mono_samples.append(avg)

    # Step 2: downsample 48kHz to 24kHz (take every other sample)
    downsampled = mono_samples[::2]

    # Pack back to bytes
    return struct.pack(f"<{len(downsampled)}h", *downsampled)


def _upsample_24k_mono_to_48k_stereo(pcm_24k_mono: bytes) -> bytes:
    """Convert 24kHz mono 16-bit PCM to 48kHz stereo 16-bit PCM.

    OpenAI Realtime API sends 24kHz, 1-channel, 16-bit signed LE.
    Discord expects 48kHz, 2-channel, 16-bit signed LE.

    Strategy: duplicate each sample (24k * 2 = 48k), then duplicate
    mono to both channels.
    """
    if len(pcm_24k_mono) < 2:
        return b""

    num_samples = len(pcm_24k_mono) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm_24k_mono)

    # Upsample 24kHz to 48kHz (duplicate each sample) + mono to stereo
    stereo_48k = []
    for s in samples:
        # Each mono sample becomes 2 stereo frames (L, R, L, R)
        stereo_48k.extend([s, s, s, s])  # 2x upsample * 2 channels

    return struct.pack(f"<{len(stereo_48k)}h", *stereo_48k)


class RealtimeVoiceSession:
    """Manages a single OpenAI Realtime API WebSocket session.

    Handles the bidirectional audio bridge between Discord and OpenAI:
    - Receives PCM audio from Discord -> resamples -> sends to Realtime API
    - Receives audio from Realtime API -> resamples -> plays on Discord

    Usage:
        session = RealtimeVoiceSession(api_key, on_audio_out=play_cb, ...)
        await session.connect()
        session.send_audio(pcm_48k_stereo_bytes)  # from Discord sink
        ...
        await session.disconnect()
    """

    def __init__(
        self,
        api_key: str,
        *,
        on_audio_out: Optional[Callable[[bytes], Coroutine]] = None,
        on_transcript_in: Optional[Callable[[str], Coroutine]] = None,
        on_transcript_out: Optional[Callable[[str], Coroutine]] = None,
        on_speech_started: Optional[Callable[[], Coroutine]] = None,
        on_speech_stopped: Optional[Callable[[], Coroutine]] = None,
        on_response_done: Optional[Callable[[dict], Coroutine]] = None,
        on_error: Optional[Callable[[str], Coroutine]] = None,
        instructions: str = "",
        voice: str = OPENAI_REALTIME_VOICE,
        model: str = OPENAI_REALTIME_MODEL,
    ):
        """
        Args:
            api_key: OpenAI API key.
            on_audio_out: Callback for output audio (48kHz stereo PCM bytes, ready for Discord).
            on_transcript_in: Callback for user speech transcript.
            on_transcript_out: Callback for assistant speech transcript.
            on_speech_started: Callback when user starts speaking (for interruptions).
            on_speech_stopped: Callback when user stops speaking.
            on_response_done: Callback when a full response is complete.
            on_error: Callback for errors.
            instructions: System instructions/personality for the assistant.
            voice: Voice to use for audio output.
            model: Realtime model identifier.
        """
        if not _HAS_WEBSOCKETS:
            raise ImportError(
                "websockets package required for Realtime API. "
                "Install with: pip install websockets"
            )

        self._api_key = api_key
        self._ws: Optional[Any] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Callbacks
        self._on_audio_out = on_audio_out
        self._on_transcript_in = on_transcript_in
        self._on_transcript_out = on_transcript_out
        self._on_speech_started = on_speech_started
        self._on_speech_stopped = on_speech_stopped
        self._on_response_done = on_response_done
        self._on_error = on_error

        # Session configuration
        self._instructions = instructions
        self._voice = voice
        self._model = model

        # State tracking for interruptions
        self._current_response_id: Optional[str] = None
        self._current_item_id: Optional[str] = None
        self._audio_playback_ms: float = 0.0
        self._is_playing = False

        # Output audio buffer for streaming playback
        self._output_audio_buffer = bytearray()
        self._output_buffer_lock = asyncio.Lock()

        # Accumulated transcript parts
        self._current_out_transcript = ""

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Connect to the OpenAI Realtime API via WebSocket."""
        url = f"{OPENAI_REALTIME_URL}?model={self._model}"

        logger.info(f"[RealtimeVoice] Connecting to {url}")

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "OpenAI-Beta": "realtime=v1",
                },
                max_size=None,  # No message size limit (audio can be large)
                ping_interval=20,
                ping_timeout=20,
            )
            self._connected = True
            self._loop = asyncio.get_event_loop()

            # Start the receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Wait for session.created event, then configure the session
            # (The receive loop will handle session.created and send session.update)
            logger.info("[RealtimeVoice] WebSocket connected, waiting for session setup...")

        except Exception as e:
            logger.error(f"[RealtimeVoice] Connection failed: {e}")
            self._connected = False
            raise

    async def disconnect(self) -> None:
        """Disconnect from the Realtime API."""
        self._connected = False

        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._output_audio_buffer.clear()
        logger.info("[RealtimeVoice] Disconnected")

    async def _send_event(self, event: dict) -> None:
        """Send a JSON event to the WebSocket."""
        if not self._ws or not self._connected:
            return

        try:
            await self._ws.send(json.dumps(event))
        except Exception as e:
            logger.error(f"[RealtimeVoice] Failed to send event: {e}")
            if self._on_error:
                await self._on_error(str(e))

    async def _configure_session(self) -> None:
        """Send session.update to configure the Realtime session."""
        session_config = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": self._instructions,
                "voice": self._voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "gpt-4o-mini-transcribe",
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                    "create_response": True,
                },
                "temperature": 0.8,
                "max_response_output_tokens": 512,
            },
        }

        await self._send_event(session_config)
        logger.info("[RealtimeVoice] Session configured")

    def send_audio(self, pcm_48k_stereo: bytes) -> None:
        """Send Discord audio to the Realtime API (thread-safe).

        Call this from VoiceListeningSink.write() (background thread).
        Resamples 48kHz stereo to 24kHz mono and sends via WebSocket.

        Args:
            pcm_48k_stereo: Raw PCM audio from Discord (48kHz, 16-bit, stereo).
        """
        if not self._connected or not self._ws:
            return

        try:
            # Resample: 48kHz stereo -> 24kHz mono
            pcm_24k_mono = _downsample_48k_stereo_to_24k_mono(pcm_48k_stereo)
            if not pcm_24k_mono:
                return

            # Base64 encode
            audio_b64 = base64.b64encode(pcm_24k_mono).decode("ascii")

            # Schedule sending in the event loop (this is called from a background thread)
            event = {
                "type": "input_audio_buffer.append",
                "audio": audio_b64,
            }

            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._send_event(event),
                    self._loop,
                )
        except Exception as e:
            logger.error(f"[RealtimeVoice] Error sending audio: {e}")

    async def get_output_audio_chunk(self, max_bytes: int = 7680) -> Optional[bytes]:
        """Get a chunk of output audio for Discord playback.

        Returns 48kHz stereo PCM ready for Discord voice, or None if
        buffer is empty.

        Args:
            max_bytes: Maximum bytes to return (default ~20ms at 48kHz stereo).
        """
        async with self._output_buffer_lock:
            if not self._output_audio_buffer:
                return None

            chunk = bytes(self._output_audio_buffer[:max_bytes])
            del self._output_audio_buffer[:max_bytes]
            return chunk

    def has_output_audio(self) -> bool:
        """Check if there's buffered output audio waiting."""
        return len(self._output_audio_buffer) > 0

    async def handle_interruption(self) -> None:
        """Handle user interruption (stop current response playback).

        Called when VAD detects user speech while the assistant is speaking.
        """
        if self._current_item_id:
            # Truncate the server's view of what the user heard
            await self._send_event({
                "type": "conversation.item.truncate",
                "item_id": self._current_item_id,
                "content_index": 0,
                "audio_end_ms": int(self._audio_playback_ms),
            })

        # Cancel in-progress response
        if self._current_response_id:
            await self._send_event({
                "type": "response.cancel",
            })

        # Clear output buffer
        async with self._output_buffer_lock:
            self._output_audio_buffer.clear()

        self._is_playing = False
        self._audio_playback_ms = 0.0

    async def inject_context(self, text: str, role: str = "user") -> None:
        """Inject text context into the conversation (for pre-populating history).

        Args:
            text: The text content to inject.
            role: "user" or "assistant".
        """
        await self._send_event({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": text}],
            },
        })

    async def _receive_loop(self) -> None:
        """Main loop to receive and process WebSocket events from OpenAI."""
        try:
            async for raw_message in self._ws:
                try:
                    event = json.loads(raw_message)
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    logger.warning("[RealtimeVoice] Received non-JSON message")
                except Exception as e:
                    logger.error(f"[RealtimeVoice] Error handling event: {e}", exc_info=True)
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"[RealtimeVoice] WebSocket closed: {e}")
        except asyncio.CancelledError:
            logger.info("[RealtimeVoice] Receive loop cancelled")
        except Exception as e:
            logger.error(f"[RealtimeVoice] Receive loop error: {e}", exc_info=True)
        finally:
            self._connected = False

    async def _handle_event(self, event: dict) -> None:
        """Handle a single Realtime API event."""
        event_type = event.get("type", "")

        if event_type == "session.created":
            logger.info("[RealtimeVoice] Session created, configuring...")
            await self._configure_session()

        elif event_type == "session.updated":
            logger.info("[RealtimeVoice] Session configured successfully")

        elif event_type == "error":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            error_code = event.get("error", {}).get("code", "")
            logger.error(f"[RealtimeVoice] API error [{error_code}]: {error_msg}")
            if self._on_error:
                await self._on_error(f"[{error_code}] {error_msg}")

        # --- Audio output events ---
        elif event_type == "response.audio.delta":
            # Streaming audio chunk from the assistant
            audio_b64 = event.get("delta", "")
            if audio_b64:
                pcm_24k_mono = base64.b64decode(audio_b64)
                # Upsample to 48kHz stereo for Discord
                pcm_48k_stereo = _upsample_24k_mono_to_48k_stereo(pcm_24k_mono)
                if pcm_48k_stereo:
                    async with self._output_buffer_lock:
                        self._output_audio_buffer.extend(pcm_48k_stereo)
                    self._is_playing = True

                    # Call the output audio callback
                    if self._on_audio_out:
                        await self._on_audio_out(pcm_48k_stereo)

        elif event_type == "response.audio.done":
            logger.info("[RealtimeVoice] Audio response complete")
            self._is_playing = False

        # --- Transcript events ---
        elif event_type == "response.audio_transcript.delta":
            self._current_out_transcript += event.get("delta", "")

        elif event_type == "response.audio_transcript.done":
            full_transcript = event.get("transcript", self._current_out_transcript)
            logger.info(f"[RealtimeVoice] Assistant said: {full_transcript[:100]}")
            self._current_out_transcript = ""
            if self._on_transcript_out:
                await self._on_transcript_out(full_transcript)

        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript.strip():
                logger.info(f"[RealtimeVoice] User said: {transcript[:100]}")
                if self._on_transcript_in:
                    await self._on_transcript_in(transcript)

        elif event_type == "conversation.item.input_audio_transcription.failed":
            error = event.get("error", {}).get("message", "Unknown")
            logger.warning(f"[RealtimeVoice] Input transcription failed: {error}")

        # --- VAD events ---
        elif event_type == "input_audio_buffer.speech_started":
            logger.debug("[RealtimeVoice] Speech started (VAD)")
            if self._on_speech_started:
                await self._on_speech_started()
            # Handle interruption if we're currently playing audio
            if self._is_playing:
                await self.handle_interruption()

        elif event_type == "input_audio_buffer.speech_stopped":
            logger.debug("[RealtimeVoice] Speech stopped (VAD)")
            if self._on_speech_stopped:
                await self._on_speech_stopped()

        elif event_type == "input_audio_buffer.committed":
            logger.debug("[RealtimeVoice] Audio buffer committed")

        # --- Response lifecycle ---
        elif event_type == "response.created":
            self._current_response_id = event.get("response", {}).get("id")
            logger.debug(f"[RealtimeVoice] Response started: {self._current_response_id}")

        elif event_type == "response.output_item.added":
            item = event.get("item", {})
            self._current_item_id = item.get("id")

        elif event_type == "response.done":
            response = event.get("response", {})
            usage = response.get("usage", {})
            logger.info(
                f"[RealtimeVoice] Response done. "
                f"Input tokens: {usage.get('input_tokens', '?')}, "
                f"Output tokens: {usage.get('output_tokens', '?')}"
            )
            self._current_response_id = None
            if self._on_response_done:
                await self._on_response_done(response)

        elif event_type == "rate_limits.updated":
            # Log rate limit info for debugging
            limits = event.get("rate_limits", [])
            for limit in limits:
                if limit.get("remaining", 999) < 10:
                    logger.warning(
                        f"[RealtimeVoice] Rate limit warning: "
                        f"{limit.get('name')}: {limit.get('remaining')} remaining"
                    )

        # Ignore other events silently
        elif event_type in (
            "conversation.created",
            "conversation.item.created",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_item.done",
        ):
            pass
        else:
            logger.debug(f"[RealtimeVoice] Unhandled event: {event_type}")
