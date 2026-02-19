"""
Claude backend abstraction — use Max subscription (Claude Code CLI) or API.

Priority order:
1. Claude Code CLI (`claude --print`) — uses your Max subscription, no API cost
2. Anthropic API (`anthropic.Anthropic`) — pay-per-token fallback

The backend is selected once at startup and shared across all agents.
Set FDA_CLAUDE_BACKEND=api to force API mode, or FDA_CLAUDE_BACKEND=cli to force CLI.
By default, the system auto-detects: if `claude` is on PATH, it uses CLI.
"""

import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------


class ClaudeBackend:
    """Unified interface for calling Claude (CLI or API)."""

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        """
        Send a prompt to Claude and return the text response.

        Args:
            system: System prompt.
            messages: List of {"role": ..., "content": ...} dicts.
            model: Model name (used by API backend; ignored by CLI backend).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            Claude's response text.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# CLI backend — uses `claude --print` (Max subscription)
# ---------------------------------------------------------------------------


class ClaudeCodeCLIBackend(ClaudeBackend):
    """
    Call Claude via the Claude Code CLI (`claude --print`).

    This uses your Max subscription credits — no API key or per-token cost.
    The CLI must be installed and authenticated (`claude` on PATH).
    """

    def __init__(self, timeout: int = 120):
        self._timeout = timeout

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        # Build a single prompt from system + messages
        prompt = self._build_prompt(system, messages)

        cmd = ["claude", "--print"]
        # Pass the system prompt via --system-prompt if provided
        if system:
            cmd.extend(["--system-prompt", system])
            # Only send the user messages as the prompt
            prompt = self._build_prompt("", messages)

        cmd.append(prompt)

        logger.debug(f"[ClaudeCodeCLI] Running: claude --print ({len(prompt)} chars)")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()
                logger.error(f"[ClaudeCodeCLI] Exit code {result.returncode}: {stderr}")
                raise RuntimeError(
                    f"Claude Code CLI failed (exit {result.returncode}): {stderr}"
                )

            output = result.stdout.strip()
            if not output:
                raise RuntimeError("Claude Code CLI returned empty output")

            logger.debug(f"[ClaudeCodeCLI] Got {len(output)} chars response")
            return output

        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Claude Code CLI timed out after {self._timeout}s"
            )
        except FileNotFoundError:
            raise RuntimeError(
                "Claude Code CLI not found. Install it or set FDA_CLAUDE_BACKEND=api"
            )

    @staticmethod
    def _build_prompt(system: str, messages: list[dict[str, str]]) -> str:
        """Flatten system + messages into a single prompt string."""
        parts = []
        if system:
            parts.append(f"<system>\n{system}\n</system>\n")
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                parts.append(content)
            elif role == "assistant":
                parts.append(f"[Previous assistant response]\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def is_available() -> bool:
        """Check if the Claude Code CLI is installed and on PATH."""
        return shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# API backend — uses anthropic Python SDK (pay-per-token)
# ---------------------------------------------------------------------------


class AnthropicAPIBackend(ClaudeBackend):
    """
    Call Claude via the Anthropic Python SDK.

    Requires ANTHROPIC_API_KEY environment variable or a key passed in.
    This is the pay-per-token fallback.
    """

    def __init__(self, api_key: Optional[str] = None):
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        model: str = "claude-3-5-haiku-20241022",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> str:
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            temperature=temperature,
        )
        return response.content[0].text

    @property
    def raw_client(self):
        """Access the underlying anthropic.Anthropic client (for code that needs it)."""
        return self._client


# ---------------------------------------------------------------------------
# Factory — auto-detect or honour FDA_CLAUDE_BACKEND env var
# ---------------------------------------------------------------------------

_backend_instance: Optional[ClaudeBackend] = None


def get_claude_backend() -> ClaudeBackend:
    """
    Get (or create) the singleton Claude backend.

    Selection logic:
    1. FDA_CLAUDE_BACKEND=cli  → force Claude Code CLI
    2. FDA_CLAUDE_BACKEND=api  → force Anthropic API
    3. Auto-detect: CLI if `claude` is on PATH, else API

    Returns:
        A ClaudeBackend instance (shared singleton).
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    preference = os.environ.get("FDA_CLAUDE_BACKEND", "auto").lower()

    if preference == "cli":
        if not ClaudeCodeCLIBackend.is_available():
            raise RuntimeError(
                "FDA_CLAUDE_BACKEND=cli but `claude` not found on PATH. "
                "Install Claude Code CLI or set FDA_CLAUDE_BACKEND=api."
            )
        logger.info("Using Claude Code CLI backend (Max subscription)")
        _backend_instance = ClaudeCodeCLIBackend()

    elif preference == "api":
        from fda.config import ANTHROPIC_API_KEY_ENV

        api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"FDA_CLAUDE_BACKEND=api but {ANTHROPIC_API_KEY_ENV} not set."
            )
        logger.info("Using Anthropic API backend (pay-per-token)")
        _backend_instance = AnthropicAPIBackend(api_key=api_key)

    else:
        # Auto-detect
        if ClaudeCodeCLIBackend.is_available():
            logger.info("Auto-detected Claude Code CLI — using Max subscription ✓")
            _backend_instance = ClaudeCodeCLIBackend()
        else:
            from fda.config import ANTHROPIC_API_KEY_ENV

            api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
            if api_key:
                logger.info(
                    "Claude Code CLI not found — falling back to Anthropic API"
                )
                _backend_instance = AnthropicAPIBackend(api_key=api_key)
            else:
                raise RuntimeError(
                    "No Claude backend available. Either:\n"
                    "  1. Install Claude Code CLI (`claude`) for Max subscription usage\n"
                    f"  2. Set {ANTHROPIC_API_KEY_ENV} for API usage"
                )

    return _backend_instance


def reset_backend() -> None:
    """Reset the singleton (useful for testing)."""
    global _backend_instance
    _backend_instance = None
