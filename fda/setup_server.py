"""
Web-based setup server for FDA system.

Provides a user-friendly HTML interface for configuring:
- Telegram bot token
- Discord bot token and client ID
- OpenAI API key
- Anthropic API key
- Office 365 calendar connection
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

try:
    from flask import Flask, jsonify, render_template_string, request
except ImportError:
    Flask = None

from fda.config import (
    ANTHROPIC_API_KEY_ENV,
    DATA_DIR,
    DISCORD_BOT_TOKEN_ENV,
    DISCORD_CLIENT_ID_ENV,
    OPENAI_API_KEY_ENV,
    TELEGRAM_BOT_TOKEN_ENV,
)
from fda.state.project_state import ProjectState

logger = logging.getLogger(__name__)

# HTML Template for setup page
SETUP_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FDA System Setup</title>
    <style>
        :root {
            --primary: #4f46e5;
            --primary-dark: #4338ca;
            --success: #10b981;
            --warning: #f59e0b;
            --error: #ef4444;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text: #1e293b;
            --text-muted: #64748b;
            --border: #e2e8f0;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            padding: 2rem;
        }

        .container {
            max-width: 800px;
            margin: 0 auto;
        }

        header {
            text-align: center;
            margin-bottom: 2rem;
        }

        h1 {
            font-size: 2rem;
            margin-bottom: 0.5rem;
            color: var(--primary);
        }

        .subtitle {
            color: var(--text-muted);
        }

        .card {
            background: var(--card-bg);
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }

        .card h2 {
            font-size: 1.25rem;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .card h2 .icon {
            font-size: 1.5rem;
        }

        .form-group {
            margin-bottom: 1rem;
        }

        label {
            display: block;
            font-weight: 500;
            margin-bottom: 0.5rem;
            color: var(--text);
        }

        .label-hint {
            font-weight: normal;
            color: var(--text-muted);
            font-size: 0.875rem;
        }

        input[type="text"],
        input[type="password"] {
            width: 100%;
            padding: 0.75rem 1rem;
            border: 1px solid var(--border);
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.2s, box-shadow 0.2s;
        }

        input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
        }

        .input-group {
            display: flex;
            gap: 0.5rem;
        }

        .input-group input {
            flex: 1;
        }

        button {
            padding: 0.75rem 1.5rem;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            transition: background-color 0.2s, transform 0.1s;
        }

        button:active {
            transform: scale(0.98);
        }

        .btn-primary {
            background: var(--primary);
            color: white;
        }

        .btn-primary:hover {
            background: var(--primary-dark);
        }

        .btn-secondary {
            background: var(--border);
            color: var(--text);
        }

        .btn-secondary:hover {
            background: #cbd5e1;
        }

        .btn-success {
            background: var(--success);
            color: white;
        }

        .btn-small {
            padding: 0.5rem 1rem;
            font-size: 0.875rem;
        }

        .status {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.875rem;
            font-weight: 500;
        }

        .status-configured {
            background: #d1fae5;
            color: #065f46;
        }

        .status-missing {
            background: #fee2e2;
            color: #991b1b;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }

        .status-configured .status-dot {
            background: var(--success);
        }

        .status-missing .status-dot {
            background: var(--error);
        }

        .help-text {
            font-size: 0.875rem;
            color: var(--text-muted);
            margin-top: 0.5rem;
        }

        .help-link {
            color: var(--primary);
            text-decoration: none;
        }

        .help-link:hover {
            text-decoration: underline;
        }

        .message {
            padding: 1rem;
            border-radius: 8px;
            margin-top: 1rem;
            display: none;
        }

        .message.success {
            background: #d1fae5;
            color: #065f46;
            display: block;
        }

        .message.error {
            background: #fee2e2;
            color: #991b1b;
            display: block;
        }

        .divider {
            height: 1px;
            background: var(--border);
            margin: 1.5rem 0;
        }

        .actions {
            display: flex;
            gap: 1rem;
            justify-content: flex-end;
        }

        .test-result {
            margin-top: 0.5rem;
            font-size: 0.875rem;
        }

        .test-result.success {
            color: var(--success);
        }

        .test-result.error {
            color: var(--error);
        }

        .setup-steps {
            background: #f1f5f9;
            border-radius: 8px;
            padding: 1rem;
            margin-top: 1rem;
        }

        .setup-steps h4 {
            font-size: 0.875rem;
            margin-bottom: 0.5rem;
            color: var(--text);
        }

        .setup-steps ol {
            margin-left: 1.25rem;
            font-size: 0.875rem;
            color: var(--text-muted);
        }

        .setup-steps li {
            margin-bottom: 0.25rem;
        }

        .toggle-visibility {
            background: none;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            padding: 0.5rem;
            font-size: 1.25rem;
        }

        .toggle-visibility:hover {
            color: var(--text);
        }

        footer {
            text-align: center;
            margin-top: 2rem;
            color: var(--text-muted);
            font-size: 0.875rem;
        }

        .loading {
            display: inline-block;
            width: 1rem;
            height: 1rem;
            border: 2px solid #f3f3f3;
            border-top: 2px solid var(--primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin-left: 0.5rem;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>FDA System Setup</h1>
            <p class="subtitle">Configure your FDA Multi-Agent System</p>
        </header>

        <!-- Status Overview -->
        <div class="card">
            <h2><span class="icon">📊</span> Configuration Status</h2>
            <div id="status-overview" style="display: flex; flex-wrap: wrap; gap: 1rem;">
                <span class="status" id="status-anthropic">
                    <span class="status-dot"></span>
                    Anthropic API
                </span>
                <span class="status" id="status-telegram">
                    <span class="status-dot"></span>
                    Telegram Bot
                </span>
                <span class="status" id="status-discord">
                    <span class="status-dot"></span>
                    Discord Bot
                </span>
                <span class="status" id="status-openai">
                    <span class="status-dot"></span>
                    OpenAI API
                </span>
            </div>
        </div>

        <!-- Anthropic API Key -->
        <div class="card">
            <h2><span class="icon">🤖</span> Anthropic API Key</h2>
            <p class="help-text" style="margin-bottom: 1rem;">
                Required for FDA agent functionality.
                <a href="https://console.anthropic.com/settings/keys" target="_blank" class="help-link">Get your API key</a>
            </p>
            <form id="form-anthropic" onsubmit="saveConfig(event, 'anthropic')">
                <div class="form-group">
                    <div class="input-group">
                        <input type="password" id="anthropic-key" name="key" placeholder="sk-ant-...">
                        <button type="button" class="toggle-visibility" onclick="toggleVisibility('anthropic-key')">👁️</button>
                    </div>
                </div>
                <div class="actions">
                    <button type="button" class="btn-secondary btn-small" onclick="testConnection('anthropic')">Test Connection</button>
                    <button type="submit" class="btn-primary btn-small">Save</button>
                </div>
                <div id="anthropic-result" class="test-result"></div>
                <div id="anthropic-message" class="message"></div>
            </form>
        </div>

        <!-- Telegram Bot -->
        <div class="card">
            <h2><span class="icon">📱</span> Telegram Bot</h2>
            <div class="setup-steps">
                <h4>Setup Instructions:</h4>
                <ol>
                    <li>Open Telegram and search for <strong>@BotFather</strong></li>
                    <li>Send <code>/newbot</code> and follow the prompts</li>
                    <li>Copy the bot token provided</li>
                </ol>
            </div>
            <form id="form-telegram" onsubmit="saveConfig(event, 'telegram')" style="margin-top: 1rem;">
                <div class="form-group">
                    <label>Bot Token</label>
                    <div class="input-group">
                        <input type="password" id="telegram-token" name="token" placeholder="123456789:ABCDefGHI...">
                        <button type="button" class="toggle-visibility" onclick="toggleVisibility('telegram-token')">👁️</button>
                    </div>
                </div>
                <div class="actions">
                    <button type="button" class="btn-secondary btn-small" onclick="testConnection('telegram')">Test Connection</button>
                    <button type="submit" class="btn-primary btn-small">Save</button>
                </div>
                <div id="telegram-result" class="test-result"></div>
                <div id="telegram-message" class="message"></div>
            </form>
        </div>

        <!-- Discord Bot -->
        <div class="card">
            <h2><span class="icon">🎮</span> Discord Bot</h2>
            <div class="setup-steps">
                <h4>Setup Instructions:</h4>
                <ol>
                    <li>Go to <a href="https://discord.com/developers/applications" target="_blank" class="help-link">Discord Developer Portal</a></li>
                    <li>Click "New Application" and name it</li>
                    <li>Go to "Bot" tab → "Add Bot" → Copy the token</li>
                    <li>Enable "Message Content Intent" in Bot settings</li>
                    <li>Go to OAuth2 → Copy the Client ID</li>
                </ol>
            </div>
            <form id="form-discord" onsubmit="saveConfig(event, 'discord')" style="margin-top: 1rem;">
                <div class="form-group">
                    <label>Bot Token</label>
                    <div class="input-group">
                        <input type="password" id="discord-token" name="token" placeholder="Your Discord bot token">
                        <button type="button" class="toggle-visibility" onclick="toggleVisibility('discord-token')">👁️</button>
                    </div>
                </div>
                <div class="form-group">
                    <label>Client ID <span class="label-hint">(for invite link generation)</span></label>
                    <input type="text" id="discord-client-id" name="client_id" placeholder="123456789012345678">
                </div>
                <div class="actions">
                    <button type="button" class="btn-secondary btn-small" onclick="testConnection('discord')">Test Connection</button>
                    <button type="button" class="btn-secondary btn-small" onclick="getDiscordInvite()">Get Invite Link</button>
                    <button type="submit" class="btn-primary btn-small">Save</button>
                </div>
                <div id="discord-result" class="test-result"></div>
                <div id="discord-invite" class="test-result"></div>
                <div id="discord-message" class="message"></div>
            </form>
        </div>

        <!-- OpenAI API Key -->
        <div class="card">
            <h2><span class="icon">🎙️</span> OpenAI API Key</h2>
            <p class="help-text" style="margin-bottom: 1rem;">
                Required for Discord voice features (Whisper STT & TTS).
                <a href="https://platform.openai.com/api-keys" target="_blank" class="help-link">Get your API key</a>
            </p>
            <form id="form-openai" onsubmit="saveConfig(event, 'openai')">
                <div class="form-group">
                    <div class="input-group">
                        <input type="password" id="openai-key" name="key" placeholder="sk-...">
                        <button type="button" class="toggle-visibility" onclick="toggleVisibility('openai-key')">👁️</button>
                    </div>
                </div>
                <div class="actions">
                    <button type="button" class="btn-secondary btn-small" onclick="testConnection('openai')">Test Connection</button>
                    <button type="submit" class="btn-primary btn-small">Save</button>
                </div>
                <div id="openai-result" class="test-result"></div>
                <div id="openai-message" class="message"></div>
            </form>
        </div>

        <!-- Quick Actions -->
        <div class="card">
            <h2><span class="icon">🚀</span> Quick Actions</h2>
            <div style="display: flex; flex-wrap: wrap; gap: 1rem;">
                <button class="btn-primary" onclick="startTelegram()">Start Telegram Bot</button>
                <button class="btn-primary" onclick="startDiscord()">Start Discord Bot</button>
                <button class="btn-secondary" onclick="viewLogs()">View Logs</button>
                <button class="btn-secondary" onclick="checkHealth()">System Health Check</button>
            </div>
            <div id="action-result" class="message" style="margin-top: 1rem;"></div>
        </div>

        <footer>
            <p>FDA Multi-Agent System v0.1.0</p>
            <p style="margin-top: 0.5rem;">
                <a href="https://github.com/your-org/fda-system" class="help-link">Documentation</a> ·
                <a href="/api/status" class="help-link">API Status</a>
            </p>
        </footer>
    </div>

    <script>
        // Load current status on page load
        document.addEventListener('DOMContentLoaded', loadStatus);

        async function loadStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();

                updateStatusBadge('anthropic', data.anthropic?.configured);
                updateStatusBadge('telegram', data.telegram?.configured);
                updateStatusBadge('discord', data.discord?.configured);
                updateStatusBadge('openai', data.openai?.configured);
            } catch (error) {
                console.error('Failed to load status:', error);
            }
        }

        function updateStatusBadge(service, configured) {
            const badge = document.getElementById(`status-${service}`);
            if (configured) {
                badge.className = 'status status-configured';
            } else {
                badge.className = 'status status-missing';
            }
        }

        function toggleVisibility(inputId) {
            const input = document.getElementById(inputId);
            input.type = input.type === 'password' ? 'text' : 'password';
        }

        async function saveConfig(event, service) {
            event.preventDefault();
            const form = event.target;
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());

            const messageDiv = document.getElementById(`${service}-message`);
            messageDiv.className = 'message';
            messageDiv.textContent = 'Saving...';
            messageDiv.style.display = 'block';

            try {
                const response = await fetch(`/api/config/${service}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    messageDiv.className = 'message success';
                    messageDiv.textContent = result.message || 'Configuration saved successfully!';
                    loadStatus();
                } else {
                    messageDiv.className = 'message error';
                    messageDiv.textContent = result.error || 'Failed to save configuration';
                }
            } catch (error) {
                messageDiv.className = 'message error';
                messageDiv.textContent = 'Network error: ' + error.message;
            }
        }

        async function testConnection(service) {
            const resultDiv = document.getElementById(`${service}-result`);
            resultDiv.className = 'test-result';
            resultDiv.textContent = 'Testing...';

            // Get the current value from the input field
            let body = {};
            if (service === 'anthropic') {
                body.key = document.getElementById('anthropic-key').value;
            } else if (service === 'telegram') {
                body.token = document.getElementById('telegram-token').value;
            } else if (service === 'discord') {
                body.token = document.getElementById('discord-token').value;
            } else if (service === 'openai') {
                body.key = document.getElementById('openai-key').value;
            }

            try {
                const response = await fetch(`/api/test/${service}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                const result = await response.json();

                if (result.success) {
                    resultDiv.className = 'test-result success';
                    resultDiv.textContent = '✓ ' + (result.message || 'Connection successful!');
                } else {
                    resultDiv.className = 'test-result error';
                    resultDiv.textContent = '✗ ' + (result.error || 'Connection failed');
                }
            } catch (error) {
                resultDiv.className = 'test-result error';
                resultDiv.textContent = '✗ Network error: ' + error.message;
            }
        }

        async function getDiscordInvite() {
            const resultDiv = document.getElementById('discord-invite');
            const clientId = document.getElementById('discord-client-id').value;

            if (!clientId) {
                resultDiv.className = 'test-result error';
                resultDiv.textContent = 'Please enter Client ID first';
                return;
            }

            try {
                const response = await fetch(`/api/discord/invite?client_id=${clientId}`);
                const result = await response.json();

                if (result.url) {
                    resultDiv.className = 'test-result success';
                    resultDiv.innerHTML = `Invite URL: <a href="${result.url}" target="_blank" class="help-link">${result.url}</a>`;
                } else {
                    resultDiv.className = 'test-result error';
                    resultDiv.textContent = result.error || 'Failed to generate invite link';
                }
            } catch (error) {
                resultDiv.className = 'test-result error';
                resultDiv.textContent = 'Error: ' + error.message;
            }
        }

        async function startTelegram() {
            showActionResult('Starting Telegram bot...', 'info');
            try {
                const response = await fetch('/api/start/telegram', {method: 'POST'});
                const result = await response.json();
                showActionResult(result.message || 'Telegram bot started', result.success ? 'success' : 'error');
            } catch (error) {
                showActionResult('Failed to start Telegram bot: ' + error.message, 'error');
            }
        }

        async function startDiscord() {
            showActionResult('Starting Discord bot...', 'info');
            try {
                const response = await fetch('/api/start/discord', {method: 'POST'});
                const result = await response.json();
                showActionResult(result.message || 'Discord bot started', result.success ? 'success' : 'error');
            } catch (error) {
                showActionResult('Failed to start Discord bot: ' + error.message, 'error');
            }
        }

        async function viewLogs() {
            window.open('/api/logs', '_blank');
        }

        async function checkHealth() {
            showActionResult('Checking system health...', 'info');
            try {
                const response = await fetch('/api/health');
                const result = await response.json();

                let message = 'System Health:\\n';
                message += `Database: ${result.database ? '✓' : '✗'}\\n`;
                message += `Anthropic: ${result.anthropic ? '✓' : '✗'}\\n`;
                message += `Telegram: ${result.telegram ? '✓' : '✗'}\\n`;
                message += `Discord: ${result.discord ? '✓' : '✗'}`;

                showActionResult(message, result.healthy ? 'success' : 'error');
            } catch (error) {
                showActionResult('Health check failed: ' + error.message, 'error');
            }
        }

        function showActionResult(message, type) {
            const div = document.getElementById('action-result');
            div.className = `message ${type}`;
            div.textContent = message;
            div.style.display = 'block';
            div.style.whiteSpace = 'pre-line';
        }
    </script>
</body>
</html>
"""


def create_setup_app() -> Any:
    """Create and configure the Flask setup application."""
    if Flask is None:
        raise ImportError(
            "Flask is required for the setup server. "
            "Install with: pip install flask"
        )

    app = Flask(__name__)
    app.secret_key = os.urandom(24)

    # Initialize state
    state = ProjectState()

    # Global error handler to ensure JSON responses for API routes
    @app.errorhandler(Exception)
    def handle_exception(e):
        """Return JSON for API errors."""
        if request.path.startswith("/api/"):
            logger.exception(f"API error: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        # For non-API routes, re-raise the exception
        raise e

    @app.errorhandler(404)
    def handle_404(e):
        """Return JSON for API 404 errors."""
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Not found"}), 404
        return "Not found", 404

    @app.errorhandler(500)
    def handle_500(e):
        """Return JSON for API 500 errors."""
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Internal server error"}), 500
        return "Internal server error", 500

    @app.route("/")
    def index():
        """Serve the setup page."""
        return render_template_string(SETUP_PAGE_HTML)

    @app.route("/api/status")
    def get_status():
        """Get configuration status for all services."""
        return jsonify({
            "anthropic": {
                "configured": bool(
                    os.environ.get(ANTHROPIC_API_KEY_ENV)
                    or state.get_context("anthropic_api_key")
                )
            },
            "telegram": {
                "configured": bool(
                    os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
                    or state.get_context("telegram_bot_token")
                )
            },
            "discord": {
                "configured": bool(
                    os.environ.get(DISCORD_BOT_TOKEN_ENV)
                    or state.get_context("discord_bot_token")
                ),
                "client_id_configured": bool(
                    os.environ.get(DISCORD_CLIENT_ID_ENV)
                    or state.get_context("discord_client_id")
                )
            },
            "openai": {
                "configured": bool(
                    os.environ.get(OPENAI_API_KEY_ENV)
                    or state.get_context("openai_api_key")
                )
            },
        })

    @app.route("/api/config/anthropic", methods=["POST"])
    def save_anthropic_config():
        """Save Anthropic API key."""
        data = request.get_json()
        key = data.get("key", "").strip()

        if not key:
            return jsonify({"success": False, "error": "API key is required"})

        state.set_context("anthropic_api_key", key)
        return jsonify({"success": True, "message": "Anthropic API key saved"})

    @app.route("/api/config/telegram", methods=["POST"])
    def save_telegram_config():
        """Save Telegram bot token."""
        data = request.get_json()
        token = data.get("token", "").strip()

        if not token:
            return jsonify({"success": False, "error": "Bot token is required"})

        state.set_context("telegram_bot_token", token)
        return jsonify({"success": True, "message": "Telegram bot token saved"})

    @app.route("/api/config/discord", methods=["POST"])
    def save_discord_config():
        """Save Discord bot configuration."""
        data = request.get_json()
        token = data.get("token", "").strip()
        client_id = data.get("client_id", "").strip()

        if not token:
            return jsonify({"success": False, "error": "Bot token is required"})

        state.set_context("discord_bot_token", token)
        if client_id:
            state.set_context("discord_client_id", client_id)

        return jsonify({"success": True, "message": "Discord configuration saved"})

    @app.route("/api/config/openai", methods=["POST"])
    def save_openai_config():
        """Save OpenAI API key."""
        data = request.get_json()
        key = data.get("key", "").strip()

        if not key:
            return jsonify({"success": False, "error": "API key is required"})

        state.set_context("openai_api_key", key)
        return jsonify({"success": True, "message": "OpenAI API key saved"})

    @app.route("/api/test/anthropic", methods=["GET", "POST"])
    def test_anthropic():
        """Test Anthropic API connection."""
        try:
            # Accept key from POST body, query param, or use stored value
            if request.method == "POST":
                data = request.get_json() or {}
                key = data.get("key", "").strip() if data.get("key") else ""
            else:
                key = request.args.get("key", "").strip()

            if not key:
                key = os.environ.get(ANTHROPIC_API_KEY_ENV) or state.get_context("anthropic_api_key")

            if not key:
                return jsonify({"success": False, "error": "API key not configured"})

            try:
                import anthropic
            except ImportError:
                return jsonify({"success": False, "error": "anthropic package not installed. Run: pip install anthropic"})

            client = anthropic.Anthropic(api_key=key)
            # Simple test - make minimal request
            response = client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}]
            )
            return jsonify({"success": True, "message": "Connection successful"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/test/telegram", methods=["GET", "POST"])
    def test_telegram():
        """Test Telegram bot connection."""
        try:
            if request.method == "POST":
                data = request.get_json() or {}
                token = data.get("token", "").strip() if data.get("token") else ""
            else:
                token = request.args.get("token", "").strip()

            if not token:
                token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV) or state.get_context("telegram_bot_token")

            if not token:
                return jsonify({"success": False, "error": "Bot token not configured"})

            import requests as req
            response = req.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=10
            )
            resp_data = response.json()

            if resp_data.get("ok"):
                bot_name = resp_data.get("result", {}).get("username", "Unknown")
                return jsonify({
                    "success": True,
                    "message": f"Connected as @{bot_name}"
                })
            else:
                return jsonify({
                    "success": False,
                    "error": resp_data.get("description", "Unknown error")
                })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/test/discord", methods=["GET", "POST"])
    def test_discord():
        """Test Discord bot connection."""
        try:
            if request.method == "POST":
                data = request.get_json() or {}
                token = data.get("token", "").strip() if data.get("token") else ""
            else:
                token = request.args.get("token", "").strip()

            if not token:
                token = os.environ.get(DISCORD_BOT_TOKEN_ENV) or state.get_context("discord_bot_token")

            if not token:
                return jsonify({"success": False, "error": "Bot token not configured"})

            import requests as req
            response = req.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
                timeout=10
            )

            if response.status_code == 200:
                resp_data = response.json()
                bot_name = resp_data.get("username", "Unknown")
                return jsonify({
                    "success": True,
                    "message": f"Connected as {bot_name}"
                })
            else:
                return jsonify({
                    "success": False,
                    "error": f"HTTP {response.status_code}: {response.text}"
                })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/test/openai", methods=["GET", "POST"])
    def test_openai():
        """Test OpenAI API connection."""
        try:
            if request.method == "POST":
                data = request.get_json() or {}
                key = data.get("key", "").strip() if data.get("key") else ""
            else:
                key = request.args.get("key", "").strip()

            if not key:
                key = os.environ.get(OPENAI_API_KEY_ENV) or state.get_context("openai_api_key")

            if not key:
                return jsonify({"success": False, "error": "API key not configured"})

            try:
                from openai import OpenAI
            except ImportError:
                return jsonify({"success": False, "error": "openai package not installed. Run: pip install openai"})

            client = OpenAI(api_key=key)
            # List models as a simple test
            models = client.models.list()
            return jsonify({"success": True, "message": "Connection successful"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/discord/invite")
    def get_discord_invite():
        """Generate Discord bot invite URL."""
        client_id = request.args.get("client_id") or os.environ.get(
            DISCORD_CLIENT_ID_ENV
        ) or state.get_context("discord_client_id")

        if not client_id:
            return jsonify({"error": "Client ID is required"})

        # Permissions: Connect, Speak, Send Messages, Read Message History
        permissions = 3148800
        url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions={permissions}&scope=bot"

        return jsonify({"url": url})

    @app.route("/api/health")
    def health_check():
        """Check overall system health."""
        health = {
            "database": False,
            "anthropic": False,
            "telegram": False,
            "discord": False,
            "healthy": False,
        }

        # Check database
        try:
            state.get_tasks()
            health["database"] = True
        except Exception:
            pass

        # Check API keys configured
        health["anthropic"] = bool(
            os.environ.get(ANTHROPIC_API_KEY_ENV)
            or state.get_context("anthropic_api_key")
        )
        health["telegram"] = bool(
            os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
            or state.get_context("telegram_bot_token")
        )
        health["discord"] = bool(
            os.environ.get(DISCORD_BOT_TOKEN_ENV)
            or state.get_context("discord_bot_token")
        )

        health["healthy"] = health["database"] and health["anthropic"]

        return jsonify(health)

    @app.route("/api/logs")
    def get_logs():
        """Get recent logs."""
        log_file = DATA_DIR / "fda.log"
        if log_file.exists():
            try:
                # Read last 100 lines
                lines = log_file.read_text().splitlines()[-100:]
                return "<pre>" + "\n".join(lines) + "</pre>"
            except Exception as e:
                return f"Error reading logs: {e}"
        return "No logs available"

    @app.route("/api/start/telegram", methods=["POST"])
    def start_telegram():
        """Start Telegram bot (returns instructions)."""
        token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV) or state.get_context("telegram_bot_token")

        if not token:
            return jsonify({
                "success": False,
                "error": "Telegram bot token not configured"
            })

        return jsonify({
            "success": True,
            "message": "To start the Telegram bot, run:\n\nfda telegram start\n\n(Run in a separate terminal)"
        })

    @app.route("/api/start/discord", methods=["POST"])
    def start_discord():
        """Start Discord bot (returns instructions)."""
        token = os.environ.get(DISCORD_BOT_TOKEN_ENV) or state.get_context("discord_bot_token")

        if not token:
            return jsonify({
                "success": False,
                "error": "Discord bot token not configured"
            })

        return jsonify({
            "success": True,
            "message": "To start the Discord bot, run:\n\nfda discord start\n\n(Run in a separate terminal)"
        })

    return app


def run_setup_server(host: str = "0.0.0.0", port: int = 9999, debug: bool = False) -> None:
    """
    Run the setup server.

    Args:
        host: Host to bind to (default: 0.0.0.0 for all interfaces)
        port: Port to run on (default: 9999)
        debug: Enable debug mode
    """
    app = create_setup_app()

    print(f"\n{'='*50}")
    print("FDA Setup Server")
    print(f"{'='*50}")
    print(f"\nOpen your browser to: http://localhost:{port}")
    print(f"Or from other devices: http://<your-ip>:{port}")
    print("\nPress Ctrl+C to stop the server")
    print(f"{'='*50}\n")

    app.run(host=host, port=port, debug=debug)
