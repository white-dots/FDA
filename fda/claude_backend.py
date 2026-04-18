"""
Claude backend abstraction — use Max subscription (Claude Code CLI) or API.

Priority order:
1. Claude Code CLI (`claude --print`) — uses your Max subscription, no API cost
2. Anthropic API (`anthropic.Anthropic`) — pay-per-token fallback

The backend is selected once at startup and shared across all agents.
Set FDA_CLAUDE_BACKEND=api to force API mode, or FDA_CLAUDE_BACKEND=cli to force CLI.
By default, the system auto-detects: if `claude` is on PATH, it uses CLI.
"""

import json
import logging
import os
import shutil
import subprocess
import time as _time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class ToolLoopTimeoutError(Exception):
    """Raised when the complete_with_tools loop exceeds its time budget."""

    def __init__(self, elapsed: float, budget: float, iterations: int):
        self.elapsed = elapsed
        self.budget = budget
        self.iterations = iterations
        super().__init__(
            f"Tool-use loop timed out after {elapsed:.0f}s "
            f"(budget: {budget:.0f}s, iterations completed: {iterations})"
        )


def _friendly_tool_name(tool_name: str, tool_input: dict) -> str:
    """Return a short human-readable label for a tool call."""
    if tool_name == "run_local_command":
        cmd = tool_input.get("command", "")[:60]
        return f"Running `{cmd}`..."
    if tool_name == "run_remote_command":
        cmd = tool_input.get("command", "")[:60]
        return f"Running `{cmd}` on VM..."
    if tool_name == "run_local_task":
        task = tool_input.get("task", "")[:60]
        return f"Analyzing locally: {task}..."
    if tool_name == "run_remote_task":
        task = tool_input.get("task", "")[:60]
        return f"Analyzing on VM: {task}..."
    if tool_name == "search_journal":
        q = tool_input.get("query", "")[:40]
        return f"Searching journal: {q}..."
    if tool_name == "get_calendar_events":
        return "Checking calendar..."
    if tool_name == "get_tasks":
        return "Checking tasks..."
    if tool_name == "get_alerts":
        return "Checking alerts..."
    if tool_name == "read_kakao_chat":
        return "Reading KakaoTalk messages..."
    # Worker agent tools (agentic file operations)
    if tool_name == "list_directory":
        path = tool_input.get("path", ".")[:40]
        return f"📂 Listing {path}..."
    if tool_name == "read_file":
        path = tool_input.get("path", "")[:50]
        return f"📖 Reading {path}..."
    if tool_name == "search_files":
        pattern = tool_input.get("pattern", "")[:30]
        return f"🔍 Searching for '{pattern}'..."
    if tool_name == "write_file":
        path = tool_input.get("path", "")[:50]
        return f"✏️ Writing {path}..."
    if tool_name == "run_command":
        cmd = tool_input.get("command", "")[:60]
        return f"⚡ Running `{cmd}`..."
    return f"Using {tool_name}..."


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

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], str],
        model: str = "",
        max_tokens: int = 4096,
        max_iterations: int = 5,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> str:
        """Run an agentic tool-use loop.

        Subclasses that support tool use (API backend) override this.
        The default fallback ignores tools and calls ``complete()``.
        """
        return self.complete(
            system=system,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
        )

    def complete_with_tools_streaming(self, **kwargs) -> str:
        """Streaming version of complete_with_tools.

        Default fallback strips streaming callbacks and delegates to
        the non-streaming version (for CLI backend compatibility).
        """
        for key in ("on_text", "on_tool_start", "on_tool_end", "thinking"):
            kwargs.pop(key, None)
        kwargs.pop("progress_callback", None)
        return self.complete_with_tools(**kwargs)


# ---------------------------------------------------------------------------
# CLI backend — uses `claude --print` (Max subscription)
# ---------------------------------------------------------------------------


class ClaudeCodeCLIBackend(ClaudeBackend):
    """
    Call Claude via the Claude Code CLI (`claude --print`).

    This uses your Max subscription credits — no API key or per-token cost.
    The CLI must be installed and authenticated (`claude` on PATH).
    """

    def __init__(self, timeout: int = 180):
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
        # Build the user prompt from messages
        prompt = self._build_prompt("", messages)

        cmd = ["claude", "--print"]
        # Pass the system prompt via --system-prompt if provided
        if system:
            cmd.extend(["--system-prompt", system])

        # Prompt is passed via stdin to avoid OS argument length limits

        logger.debug(f"[ClaudeCodeCLI] Running: claude --print ({len(prompt)} chars via stdin)")

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
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

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], str],
        model: str = "",
        max_tokens: int = 4096,
        max_iterations: int = 5,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> str:
        """Run tool-use loop via the Anthropic API.

        The CLI backend (``claude --print``) doesn't support tool-use natively,
        so we delegate to an ``AnthropicAPIBackend`` for tool-use calls.
        If no API key is available, falls back to a plain completion (no tools).
        """
        api_backend = self._get_api_backend_for_tools()
        if api_backend is None:
            logger.warning(
                "Tool-use requested but no ANTHROPIC_API_KEY set — "
                "falling back to plain completion (tools will be ignored)"
            )
            return self.complete(
                system=system, messages=messages, model=model, max_tokens=max_tokens,
            )
        return api_backend.complete_with_tools(
            system=system,
            messages=messages,
            tools=tools,
            tool_executor=tool_executor,
            model=model,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            timeout=timeout,
            **kwargs,
        )

    def _get_api_backend_for_tools(self) -> Optional["AnthropicAPIBackend"]:
        """Get or create an API backend for tool-use calls."""
        if not hasattr(self, "_api_backend"):
            from fda.config import ANTHROPIC_API_KEY_ENV
            from fda.state.project_state import ProjectState

            api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
            if not api_key:
                try:
                    state = ProjectState()
                    api_key = state.get_context("anthropic_api_key")
                except Exception:
                    pass
            if api_key:
                self._api_backend: Optional[AnthropicAPIBackend] = AnthropicAPIBackend(api_key=api_key)
            else:
                self._api_backend = None
        return self._api_backend

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
        model: str = "claude-sonnet-4-5-20250929",
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

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], str],
        model: str = "claude-sonnet-4-5-20250929",
        max_tokens: int = 4096,
        max_iterations: int = 5,
        temperature: float = 0.7,
        progress_callback: Optional[Callable[[str], None]] = None,
        thinking: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Run an agentic tool-use loop via the Anthropic API.

        Sends the request with tool definitions. When Claude responds with
        tool_use blocks, executes them via *tool_executor* and feeds the
        results back.  Loops until Claude produces a final text response
        or *max_iterations* is reached.

        Args:
            progress_callback: Optional callback(msg: str) for live progress
                updates (e.g. "Searching journal...", "Running ls...").
            thinking: Optional dict to enable extended thinking, e.g.
                ``{"type": "enabled", "budget_tokens": 10000}``.
                When set, temperature is forced to 1 (API requirement).
            timeout: Optional wall-clock timeout in seconds for the entire
                tool-use loop.  Raises ``ToolLoopTimeoutError`` when exceeded.
        """
        msgs = list(messages)
        _start = _time.monotonic()

        # Extended thinking requires temperature=1 and higher max_tokens
        effective_temperature = temperature
        effective_max_tokens = max_tokens
        if thinking:
            effective_temperature = 1
            budget = thinking.get("budget_tokens", 10000)
            effective_max_tokens = max(max_tokens, budget + 4096)

        for iteration in range(max_iterations):
            # Check timeout at the top of each iteration
            if timeout is not None:
                elapsed = _time.monotonic() - _start
                if elapsed >= timeout:
                    raise ToolLoopTimeoutError(elapsed, timeout, iteration)

            create_kwargs: dict[str, Any] = dict(
                model=model,
                max_tokens=effective_max_tokens,
                system=system,
                messages=msgs,
                tools=tools,
                temperature=effective_temperature,
            )
            if thinking:
                create_kwargs["thinking"] = thinking
            response = self._client.messages.create(**create_kwargs)

            # Collect tool-use blocks
            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_blocks:
                # Final answer — extract text
                text_parts = [b.text for b in response.content if b.type == "text"]
                return "\n".join(text_parts) or ""

            # Build assistant message with all content blocks
            msgs.append({"role": "assistant", "content": response.content})

            # Execute each tool and collect results
            tool_results = []
            for block in tool_blocks:
                # Report progress so the UI can show activity
                if progress_callback:
                    try:
                        _tool_label = _friendly_tool_name(block.name, block.input)
                        progress_callback(_tool_label)
                    except Exception as exc:
                        logger.debug(f"Progress callback error (ignored): {exc}")
                try:
                    result = tool_executor(block.name, block.input)
                except Exception as e:
                    logger.error(f"Tool {block.name} failed: {e}")
                    result = f"Error: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })

            msgs.append({"role": "user", "content": tool_results})

            # Check timeout after tool batch execution
            if timeout is not None:
                elapsed = _time.monotonic() - _start
                if elapsed >= timeout:
                    raise ToolLoopTimeoutError(elapsed, timeout, iteration + 1)

        # Max iterations reached — do one final call WITHOUT tools to force
        # Claude to summarize what it has so far instead of losing all the work
        logger.warning("Tool-use loop hit max iterations — forcing final summary")
        try:
            msgs.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "You've reached the maximum number of tool calls. "
                        "Please summarize what you've found so far and give "
                        "the user a complete answer based on the information "
                        "you already gathered."
                    ),
                }],
            })
            final = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=msgs,
            )
            text_parts = [b.text for b in final.content if b.type == "text"]
            if text_parts:
                return "\n".join(text_parts)
        except Exception as e:
            logger.error(f"Final summary call failed: {e}")
        return "(Reached maximum tool iterations — please try a simpler question)"

    def complete_with_tools_streaming(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], str],
        model: str = "claude-sonnet-4-5-20250929",
        max_tokens: int = 4096,
        max_iterations: int = 10,
        temperature: float = 0.7,
        on_text: Optional[Callable[[str, str], None]] = None,
        on_tool_start: Optional[Callable[[str, dict], None]] = None,
        on_tool_end: Optional[Callable[[str, str], None]] = None,
        thinking: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Run an agentic tool-use loop with real-time token streaming.

        Like ``complete_with_tools`` but uses the streaming API so callers
        can update the UI as tokens arrive.

        Args:
            on_text: Called for each text delta: ``on_text(delta, snapshot)``.
                     *delta* is the new chunk, *snapshot* is all text so far.
            on_tool_start: Called when a tool_use block begins:
                           ``on_tool_start(tool_name, tool_input)``.
            on_tool_end: Called after a tool finishes execution:
                         ``on_tool_end(tool_name, result_preview)``.
            thinking: Optional dict to enable extended thinking, e.g.
                ``{"type": "enabled", "budget_tokens": 10000}``.
                When set, temperature is forced to 1 (API requirement).
            timeout: Optional wall-clock timeout in seconds for the entire
                tool-use loop.  Raises ``ToolLoopTimeoutError`` when exceeded.

        Returns:
            The complete text response (all text blocks concatenated).
        """
        msgs = list(messages)
        full_text = ""  # Accumulates ALL text across iterations
        _start = _time.monotonic()

        # Extended thinking requires temperature=1 and higher max_tokens
        effective_temperature = temperature
        effective_max_tokens = max_tokens
        if thinking:
            effective_temperature = 1
            budget = thinking.get("budget_tokens", 10000)
            effective_max_tokens = max(max_tokens, budget + 4096)

        for iteration in range(max_iterations):
            # Check timeout at the top of each iteration
            if timeout is not None:
                elapsed = _time.monotonic() - _start
                if elapsed >= timeout:
                    raise ToolLoopTimeoutError(elapsed, timeout, iteration)

            try:
                stream_kwargs: dict[str, Any] = dict(
                    model=model,
                    max_tokens=effective_max_tokens,
                    system=system,
                    messages=msgs,
                    tools=tools,
                    temperature=effective_temperature,
                )
                if thinking:
                    stream_kwargs["thinking"] = thinking
                with self._client.messages.stream(**stream_kwargs) as stream:
                    for event in stream:
                        if event.type == "text":
                            full_text += event.text
                            if on_text:
                                try:
                                    on_text(event.text, full_text)
                                except Exception as exc:
                                    logger.debug(f"on_text callback error (ignored): {exc}")

                        elif event.type == "content_block_start":
                            block = event.content_block
                            block_type = getattr(block, "type", None)
                            if block_type == "tool_use":
                                if on_tool_start:
                                    try:
                                        label = _friendly_tool_name(block.name, {})
                                        on_tool_start(block.name, label)
                                    except Exception as exc:
                                        logger.debug(f"on_tool_start callback error (ignored): {exc}")
                            elif block_type == "thinking":
                                if on_tool_start:
                                    try:
                                        on_tool_start("thinking", "🧠 Thinking deeply...")
                                    except Exception as exc:
                                        logger.debug(f"on_tool_start callback error (ignored): {exc}")
                            elif block_type == "server_tool_use":
                                if on_tool_start:
                                    try:
                                        on_tool_start("web_search", "🔍 Searching the web...")
                                    except Exception as exc:
                                        logger.debug(f"on_tool_start callback error (ignored): {exc}")

                    # Stream done — get final accumulated message
                    final_message = stream.get_final_message()

            except ToolLoopTimeoutError:
                raise  # Let timeout errors propagate
            except Exception as e:
                # Streaming failed — fall back to non-streaming
                logger.error(f"Streaming failed (iteration {iteration}): {e}")
                remaining_timeout = None
                if timeout is not None:
                    remaining_timeout = timeout - (_time.monotonic() - _start)
                    if remaining_timeout <= 0:
                        raise ToolLoopTimeoutError(
                            _time.monotonic() - _start, timeout, iteration
                        )
                return self.complete_with_tools(
                    system=system,
                    messages=msgs,
                    tools=tools,
                    tool_executor=tool_executor,
                    model=model,
                    max_tokens=max_tokens,
                    max_iterations=max_iterations - iteration,
                    temperature=temperature,
                    thinking=thinking,
                    timeout=remaining_timeout,
                )

            # Check for tool-use blocks
            tool_blocks = [b for b in final_message.content if b.type == "tool_use"]

            if not tool_blocks:
                # No tools — final answer
                return full_text

            # Append assistant message with all content blocks
            msgs.append({"role": "assistant", "content": final_message.content})

            # Execute each tool
            tool_results = []
            for block in tool_blocks:
                try:
                    result = tool_executor(block.name, block.input)
                except Exception as e:
                    logger.error(f"Tool {block.name} failed: {e}")
                    result = f"Error: {e}"

                if on_tool_end:
                    try:
                        on_tool_end(block.name, str(result)[:100])
                    except Exception as exc:
                        logger.debug(f"on_tool_end callback error (ignored): {exc}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })

            msgs.append({"role": "user", "content": tool_results})

            # Check timeout after tool batch execution
            if timeout is not None:
                elapsed = _time.monotonic() - _start
                if elapsed >= timeout:
                    raise ToolLoopTimeoutError(elapsed, timeout, iteration + 1)

        # Max iterations — graceful summary (streamed)
        logger.warning("Streaming tool-use loop hit max iterations — forcing final summary")
        try:
            msgs.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "You've reached the maximum number of tool calls. "
                        "Please summarize what you've found so far and give "
                        "the user a complete answer based on the information "
                        "you already gathered."
                    ),
                }],
            })
            with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=msgs,
            ) as stream:
                for event in stream:
                    if event.type == "text":
                        full_text += event.text
                        if on_text:
                            try:
                                on_text(event.text, full_text)
                            except Exception:
                                pass
            return full_text or "(Reached maximum tool iterations)"
        except Exception as e:
            logger.error(f"Final streaming summary failed: {e}")
            return full_text or "(Reached maximum tool iterations — please try a simpler question)"

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
