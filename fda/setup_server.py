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
import threading
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from flask import Flask, jsonify, render_template_string, request, send_file
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
    <title>FDA // Command</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+KR:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --amber: #22c55e;
            --amber-dim: #16a34a;
            --amber-glow: rgba(34, 197, 94, 0.12);
            --amber-glow-strong: rgba(34, 197, 94, 0.3);
            --green: #22c55e;
            --green-dim: rgba(34, 197, 94, 0.12);
            --red: #ef4444;
            --red-dim: rgba(239, 68, 68, 0.15);
            --blue: #3b82f6;
            --blue-dim: rgba(59, 130, 246, 0.12);
            --orange: #f59e0b;
            --orange-dim: rgba(245, 158, 11, 0.12);
            --bg: #111319;
            --bg-raised: #181c25;
            --bg-surface: #1e232e;
            --bg-hover: #252a36;
            --border: #2a3040;
            --border-light: #354050;
            --text: #f1f5f9;
            --text-dim: #94a3b8;
            --text-faint: #64748b;
            --mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
            --sans: 'Noto Sans KR', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: var(--sans);
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
        }

        /* Noise overlay */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            background: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
            pointer-events: none;
            z-index: 9999;
        }

        /* Layout */
        .shell {
            display: grid;
            grid-template-columns: 220px 1fr;
            grid-template-rows: auto 1fr;
            min-height: 100vh;
        }

        /* Top bar */
        .topbar {
            grid-column: 1 / -1;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 1.5rem;
            height: 52px;
            border-bottom: 1px solid var(--border);
            background: var(--bg-raised);
        }

        .logo {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-mark {
            width: 28px;
            height: 28px;
            background: var(--green);
            border-radius: 6px;
            display: grid;
            place-items: center;
            font-family: var(--mono);
            font-weight: 700;
            font-size: 0.75rem;
            color: var(--bg);
            letter-spacing: -0.5px;
        }

        .logo-text {
            font-family: var(--mono);
            font-weight: 600;
            font-size: 0.9rem;
            letter-spacing: 2px;
            text-transform: uppercase;
            color: var(--text);
        }

        .logo-text span {
            color: var(--text-dim);
            font-weight: 400;
        }

        .topbar-right {
            display: flex;
            align-items: center;
            gap: 1rem;
        }

        .system-clock {
            font-family: var(--mono);
            font-size: 0.75rem;
            color: var(--text-dim);
            letter-spacing: 1px;
        }

        .health-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 8px var(--green);
            animation: pulse-glow 2s ease-in-out infinite;
        }

        .health-dot.offline { background: var(--red); box-shadow: 0 0 8px var(--red); }

        @keyframes pulse-glow {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        /* Sidebar */
        .sidebar {
            background: var(--bg-raised);
            border-right: 1px solid var(--border);
            padding: 1rem 0;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
        }

        .nav-section {
            padding: 0 0.75rem;
            margin-bottom: 1.5rem;
        }

        .nav-label {
            font-family: var(--mono);
            font-size: 0.6rem;
            font-weight: 600;
            letter-spacing: 2.5px;
            text-transform: uppercase;
            color: var(--text-faint);
            padding: 0 0.75rem;
            margin-bottom: 0.5rem;
        }

        .nav-item {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            padding: 0.55rem 0.75rem;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.82rem;
            font-weight: 400;
            color: var(--text-dim);
            transition: all 0.15s;
            border: 1px solid transparent;
            position: relative;
        }

        .nav-item:hover {
            background: var(--bg-hover);
            color: var(--text);
        }

        .nav-item.active {
            background: var(--amber-glow);
            color: var(--amber);
            border-color: rgba(34, 197, 94, 0.2);
            font-weight: 500;
        }

        .nav-item.active::before {
            content: '';
            position: absolute;
            left: -0.75rem;
            top: 50%;
            transform: translateY(-50%);
            width: 3px;
            height: 16px;
            background: var(--amber);
            border-radius: 0 2px 2px 0;
        }

        .nav-icon {
            width: 18px;
            height: 18px;
            display: grid;
            place-items: center;
            font-size: 0.85rem;
            opacity: 0.7;
        }

        .nav-item.active .nav-icon { opacity: 1; }

        .nav-badge {
            margin-left: auto;
            font-family: var(--mono);
            font-size: 0.6rem;
            padding: 0.15rem 0.4rem;
            border-radius: 3px;
            background: var(--bg-surface);
            color: var(--text-faint);
        }

        .nav-item.active .nav-badge {
            background: rgba(34, 197, 94, 0.2);
            color: var(--amber);
        }

        .sidebar-footer {
            margin-top: auto;
            padding: 1rem;
            border-top: 1px solid var(--border);
        }

        .version-tag {
            font-family: var(--mono);
            font-size: 0.65rem;
            color: var(--text-faint);
            text-align: center;
            letter-spacing: 0.5px;
        }

        /* Main content */
        .main {
            padding: 2rem 2.5rem;
            overflow-y: auto;
            max-height: calc(100vh - 52px);
        }

        .page-header {
            margin-bottom: 2rem;
        }

        .page-title {
            font-family: var(--mono);
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.5px;
            color: var(--text);
            margin-bottom: 0.25rem;
        }

        .page-subtitle {
            font-size: 0.85rem;
            color: var(--text-dim);
            font-weight: 300;
        }

        /* Cards */
        .card {
            background: var(--bg-raised);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1.25rem;
            margin-bottom: 1rem;
            transition: border-color 0.2s;
            border-top: 3px solid var(--green);
        }

        .card:hover {
            border-color: var(--border-light);
            border-top-color: var(--green);
        }

        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 1rem;
        }

        .card-title {
            font-family: var(--mono);
            font-size: 0.8rem;
            font-weight: 600;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            color: var(--text-dim);
        }

        /* Status grid */
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }

        .status-tile {
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 0.85rem 1rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
            transition: all 0.2s;
        }

        .status-tile.ok {
            border-color: rgba(34, 197, 94, 0.25);
        }

        .status-tile.err {
            border-color: rgba(239, 68, 68, 0.25);
        }

        .status-indicator {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .status-tile.ok .status-indicator {
            background: var(--green);
            box-shadow: 0 0 6px rgba(34, 197, 94, 0.5);
        }

        .status-tile.err .status-indicator {
            background: var(--red);
            box-shadow: 0 0 6px rgba(239, 68, 68, 0.4);
        }

        .status-label {
            font-size: 0.8rem;
            font-weight: 500;
            color: var(--text);
        }

        .status-sub {
            font-family: var(--mono);
            font-size: 0.65rem;
            color: var(--text-faint);
            margin-top: 0.1rem;
        }

        /* Config forms */
        .config-section {
            background: var(--bg-raised);
            border: 1px solid var(--border);
            border-radius: 10px;
            margin-bottom: 0.75rem;
            overflow: hidden;
        }

        .config-header {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            padding: 0.85rem 1.15rem;
            cursor: pointer;
            transition: background 0.15s;
        }

        .config-header:hover {
            background: var(--bg-hover);
        }

        .config-icon {
            width: 32px;
            height: 32px;
            border-radius: 6px;
            display: grid;
            place-items: center;
            font-size: 1rem;
            flex-shrink: 0;
        }

        .config-icon.anthropic { background: rgba(34, 197, 94, 0.12); }
        .config-icon.telegram { background: rgba(96, 165, 250, 0.12); }
        .config-icon.discord { background: rgba(139, 92, 246, 0.12); }
        .config-icon.openai { background: rgba(34, 197, 94, 0.12); }
        .config-icon.outlook { background: rgba(239, 68, 68, 0.12); }

        .device-code-box {
            margin-top: 0.75rem;
            padding: 1rem;
            background: var(--bg);
            border: 1px solid var(--amber-dim);
            border-radius: 6px;
        }

        .device-code-box .dc-label {
            font-family: var(--mono);
            font-size: 0.65rem;
            font-weight: 600;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: var(--text-faint);
            margin-bottom: 0.5rem;
        }

        .device-code-box .dc-code {
            font-family: var(--mono);
            font-size: 1.5rem;
            font-weight: 700;
            color: var(--amber);
            letter-spacing: 4px;
            text-align: center;
            padding: 0.5rem 0;
        }

        .device-code-box .dc-url {
            text-align: center;
            margin-top: 0.4rem;
        }

        .device-code-box .dc-url a {
            color: var(--blue);
            font-size: 0.78rem;
            text-decoration: none;
        }

        .device-code-box .dc-url a:hover { text-decoration: underline; }

        .device-code-box .dc-status {
            text-align: center;
            margin-top: 0.5rem;
            font-size: 0.72rem;
            color: var(--text-dim);
        }

        .device-code-box .dc-status .spinner {
            margin-right: 0.35rem;
            vertical-align: middle;
        }

        .device-code-box .dc-regen {
            text-align: center;
            margin-top: 0.75rem;
        }

        .device-code-box .dc-regen .btn {
            font-size: 0.68rem;
            color: var(--text-dim);
            border-color: var(--border);
        }

        .device-code-box .dc-regen .btn:hover {
            color: var(--amber);
            border-color: var(--amber-dim);
        }

        .outlook-account {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            padding: 0.65rem 0.85rem;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 5px;
            margin-top: 0.75rem;
        }

        .outlook-account .oa-email {
            font-family: var(--mono);
            font-size: 0.8rem;
            color: var(--text);
        }

        .outlook-account .oa-label {
            font-size: 0.65rem;
            color: var(--text-faint);
        }

        .config-name {
            font-weight: 500;
            font-size: 0.85rem;
            color: var(--text);
        }

        .config-desc {
            font-size: 0.72rem;
            color: var(--text-faint);
            margin-top: 0.1rem;
        }

        .config-status {
            margin-left: auto;
            font-family: var(--mono);
            font-size: 0.65rem;
            font-weight: 600;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            padding: 0.2rem 0.5rem;
            border-radius: 3px;
        }

        .config-status.ok { background: var(--green-dim); color: var(--green); }
        .config-status.missing { background: var(--red-dim); color: var(--red); }

        .config-chevron {
            color: var(--text-faint);
            transition: transform 0.2s;
            font-size: 0.8rem;
        }

        .config-section.open .config-chevron {
            transform: rotate(180deg);
        }

        .config-body {
            display: none;
            padding: 0 1.15rem 1.15rem;
            border-top: 1px solid var(--border);
        }

        .config-section.open .config-body {
            display: block;
        }

        .form-group {
            margin-top: 0.85rem;
        }

        .form-label {
            font-size: 0.72rem;
            font-weight: 500;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 0.4rem;
            display: block;
            font-family: var(--mono);
        }

        .input-row {
            display: flex;
            gap: 0.5rem;
        }

        input[type="text"],
        input[type="password"] {
            flex: 1;
            padding: 0.6rem 0.85rem;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 5px;
            color: var(--text);
            font-family: var(--mono);
            font-size: 0.8rem;
            transition: border-color 0.2s;
        }

        input:focus {
            outline: none;
            border-color: var(--amber-dim);
            box-shadow: 0 0 0 2px var(--amber-glow);
        }

        input::placeholder {
            color: var(--text-faint);
        }

        /* Buttons */
        .btn {
            padding: 0.5rem 1rem;
            border: 1px solid var(--border);
            border-radius: 5px;
            font-size: 0.78rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s;
            font-family: var(--sans);
            background: var(--bg-surface);
            color: var(--text-dim);
        }

        .btn:hover {
            background: var(--bg-hover);
            color: var(--text);
            border-color: var(--border-light);
        }

        .btn:active { transform: scale(0.98); }

        .btn-amber {
            background: var(--amber);
            color: var(--bg);
            border-color: var(--amber);
            font-weight: 600;
        }

        .btn-amber:hover {
            background: var(--amber-dim);
            border-color: var(--amber-dim);
            color: var(--bg);
        }

        .btn-sm {
            padding: 0.35rem 0.7rem;
            font-size: 0.72rem;
        }

        .btn-ghost {
            background: transparent;
            border-color: transparent;
            color: var(--text-dim);
            padding: 0.35rem 0.5rem;
        }

        .btn-ghost:hover {
            background: var(--bg-hover);
            color: var(--text);
        }

        .form-actions {
            display: flex;
            gap: 0.5rem;
            justify-content: flex-end;
            margin-top: 1rem;
        }

        .test-result {
            margin-top: 0.5rem;
            font-family: var(--mono);
            font-size: 0.72rem;
            padding: 0.4rem 0.6rem;
            border-radius: 4px;
        }

        .test-result.success {
            color: var(--green);
            background: var(--green-dim);
        }

        .test-result.error {
            color: var(--red);
            background: var(--red-dim);
        }

        .message {
            padding: 0.6rem 0.85rem;
            border-radius: 5px;
            margin-top: 0.75rem;
            font-size: 0.8rem;
            display: none;
        }

        .message.success {
            background: var(--green-dim);
            color: var(--green);
            display: block;
        }

        .message.error {
            background: var(--red-dim);
            color: var(--red);
            display: block;
        }

        .message.info {
            background: var(--blue-dim);
            color: var(--blue);
            display: block;
        }

        /* Quick actions */
        .actions-row {
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            margin-top: 1rem;
        }

        /* Setup steps */
        .steps {
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 5px;
            padding: 0.75rem 0.85rem;
            margin-top: 0.75rem;
        }

        .steps-title {
            font-family: var(--mono);
            font-size: 0.65rem;
            font-weight: 600;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: var(--text-faint);
            margin-bottom: 0.4rem;
        }

        .steps ol {
            margin-left: 1.1rem;
            font-size: 0.78rem;
            color: var(--text-dim);
        }

        .steps li {
            margin-bottom: 0.2rem;
            line-height: 1.5;
        }

        .steps a {
            color: var(--amber);
            text-decoration: none;
        }

        .steps a:hover { text-decoration: underline; }

        .steps code {
            font-family: var(--mono);
            font-size: 0.72rem;
            background: var(--bg-surface);
            padding: 0.1rem 0.35rem;
            border-radius: 3px;
            color: var(--amber);
        }

        /* Agent cards */
        .agent-grid {
            display: grid;
            gap: 0.75rem;
        }

        .agent-card {
            background: var(--bg-raised);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1.25rem;
            transition: border-color 0.2s;
            border-top: 3px solid var(--green);
        }

        .agent-card:hover {
            border-color: var(--border-light);
            border-top-color: var(--green);
        }

        .agent-top {
            display: flex;
            align-items: center;
            gap: 0.85rem;
            margin-bottom: 1rem;
        }

        .agent-avatar {
            width: 40px;
            height: 40px;
            border-radius: 8px;
            display: grid;
            place-items: center;
            font-size: 1.15rem;
            flex-shrink: 0;
        }

        .agent-avatar.orchestrator { background: linear-gradient(135deg, rgba(34,197,94,0.2), rgba(34,197,94,0.05)); border: 1px solid rgba(34,197,94,0.25); }
        .agent-avatar.worker { background: linear-gradient(135deg, rgba(96, 165, 250, 0.15), rgba(96, 165, 250, 0.05)); border: 1px solid rgba(96, 165, 250, 0.2); }
        .agent-avatar.worker-local { background: linear-gradient(135deg, rgba(168, 85, 247, 0.15), rgba(168, 85, 247, 0.05)); border: 1px solid rgba(168, 85, 247, 0.2); }
        .agent-avatar.discord { background: linear-gradient(135deg, rgba(139, 92, 246, 0.15), rgba(139, 92, 246, 0.05)); border: 1px solid rgba(139, 92, 246, 0.2); }
        .agent-avatar.telegram { background: linear-gradient(135deg, rgba(34, 197, 94, 0.15), rgba(34, 197, 94, 0.05)); border: 1px solid rgba(34, 197, 94, 0.2); }
        .agent-avatar.kakaotalk { background: linear-gradient(135deg, rgba(250, 224, 50, 0.15), rgba(250, 224, 50, 0.05)); border: 1px solid rgba(250, 224, 50, 0.2); }
        .agent-avatar.calendar { background: linear-gradient(135deg, rgba(239, 68, 68, 0.15), rgba(239, 68, 68, 0.05)); border: 1px solid rgba(239, 68, 68, 0.2); }

        .agent-info h3 {
            font-size: 0.9rem;
            font-weight: 600;
            color: var(--text);
            margin-bottom: 0.1rem;
        }

        .agent-info p {
            font-size: 0.75rem;
            color: var(--text-dim);
            font-weight: 300;
        }

        .agent-role-tag {
            margin-left: auto;
            font-family: var(--mono);
            font-size: 0.6rem;
            font-weight: 600;
            letter-spacing: 1px;
            text-transform: uppercase;
            padding: 0.2rem 0.5rem;
            border-radius: 3px;
            background: var(--bg-surface);
            color: var(--text-faint);
            border: 1px solid var(--border);
        }

        .task-list {
            list-style: none;
        }

        .task-item {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            padding: 0.55rem 0.75rem;
            border-radius: 5px;
            background: var(--bg);
            margin-bottom: 0.35rem;
            border: 1px solid var(--border);
            font-size: 0.8rem;
        }

        .task-dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .task-dot.completed { background: var(--green); }
        .task-dot.in_progress { background: var(--amber); box-shadow: 0 0 6px var(--amber-glow-strong); }
        .task-dot.pending { background: var(--text-faint); }
        .task-dot.blocked { background: var(--red); }

        .task-text {
            flex: 1;
            color: var(--text);
            font-weight: 400;
        }

        .task-time {
            font-family: var(--mono);
            font-size: 0.65rem;
            color: var(--text-faint);
        }

        .no-tasks {
            text-align: center;
            color: var(--text-faint);
            padding: 1.5rem;
            font-size: 0.8rem;
        }

        /* Chat */
        .chat-layout {
            display: grid;
            grid-template-columns: 160px 1fr;
            gap: 1rem;
            height: calc(100vh - 160px);
            min-height: 400px;
        }

        .chat-agents {
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }

        .chat-agent-btn {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            padding: 0.6rem 0.75rem;
            border-radius: 6px;
            cursor: pointer;
            border: 1px solid var(--border);
            background: var(--bg-raised);
            transition: all 0.15s;
            text-align: left;
            color: var(--text-dim);
        }

        .chat-agent-btn:hover {
            background: var(--bg-hover);
            color: var(--text);
        }

        .chat-agent-btn.selected {
            border-color: var(--amber-dim);
            background: var(--amber-glow);
            color: var(--amber);
        }

        .chat-agent-btn .ca-icon {
            font-size: 1rem;
        }

        .chat-agent-btn .ca-name {
            font-weight: 500;
            font-size: 0.78rem;
        }

        .chat-agent-btn .ca-role {
            font-size: 0.6rem;
            font-family: var(--mono);
            color: var(--text-faint);
        }

        .chat-panel {
            background: var(--bg-raised);
            border: 1px solid var(--border);
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .chat-top {
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--border);
            font-family: var(--mono);
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--text-dim);
            letter-spacing: 0.5px;
        }

        .chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 1rem;
        }

        .chat-messages::-webkit-scrollbar { width: 4px; }
        .chat-messages::-webkit-scrollbar-track { background: transparent; }
        .chat-messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

        .chat-msg {
            margin-bottom: 0.75rem;
            padding: 0.65rem 0.85rem;
            border-radius: 6px;
            max-width: 80%;
            font-size: 0.85rem;
            line-height: 1.5;
        }

        .chat-msg.user {
            background: var(--amber);
            color: var(--bg);
            margin-left: auto;
            border-bottom-right-radius: 2px;
        }

        .chat-msg.agent {
            background: var(--bg-surface);
            color: var(--text);
            border: 1px solid var(--border);
            border-bottom-left-radius: 2px;
        }

        .chat-msg.agent strong { color: var(--amber); font-weight: 600; }
        .chat-msg.agent code {
            background: rgba(34,197,94,0.1);
            padding: 0.1rem 0.35rem;
            border-radius: 3px;
            font-family: var(--mono);
            font-size: 0.78em;
        }

        .file-link {
            display: inline-flex;
            align-items: center;
            gap: 0.3rem;
            padding: 0.2rem 0.55rem;
            margin: 0.15rem 0;
            background: rgba(34,197,94,0.08);
            border: 1px solid rgba(34,197,94,0.25);
            border-radius: 5px;
            color: var(--amber);
            text-decoration: none;
            font-size: 0.82em;
            font-weight: 500;
            transition: all 0.15s;
            word-break: break-all;
        }
        .file-link:hover {
            background: rgba(34,197,94,0.18);
            border-color: var(--amber);
            color: #fff;
        }

        .chat-bottom {
            padding: 0.75rem 1rem;
            border-top: 1px solid var(--border);
            display: flex;
            gap: 0.5rem;
        }

        .chat-input {
            flex: 1;
            padding: 0.55rem 0.85rem;
            background: var(--bg);
            border: 1px solid var(--border);
            border-radius: 5px;
            color: var(--text);
            font-family: var(--sans);
            font-size: 0.85rem;
            resize: none;
        }

        .chat-input:focus {
            outline: none;
            border-color: var(--amber-dim);
        }

        .chat-empty {
            display: grid;
            place-items: center;
            height: 100%;
            color: var(--text-faint);
            font-size: 0.8rem;
        }

        /* Journal */
        .journal-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .journal-card {
            background: var(--bg-raised);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1.15rem;
            transition: border-color 0.2s;
            border-left: 3px solid var(--green);
        }

        .journal-card:hover {
            border-color: var(--border-light);
            border-left-color: var(--green);
        }

        .journal-top {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 0.65rem;
        }

        .journal-title {
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--text);
        }

        .journal-meta {
            text-align: right;
            flex-shrink: 0;
        }

        .journal-author-tag {
            font-family: var(--mono);
            font-size: 0.6rem;
            font-weight: 600;
            letter-spacing: 0.5px;
            padding: 0.15rem 0.4rem;
            border-radius: 3px;
            background: var(--amber-glow);
            color: var(--amber);
        }

        .journal-date {
            font-family: var(--mono);
            font-size: 0.65rem;
            color: var(--text-faint);
            margin-top: 0.2rem;
        }

        .journal-tags {
            display: flex;
            gap: 0.35rem;
            flex-wrap: wrap;
            margin-bottom: 0.65rem;
        }

        .journal-tag {
            font-family: var(--mono);
            font-size: 0.6rem;
            padding: 0.15rem 0.4rem;
            border-radius: 3px;
            background: var(--bg-surface);
            color: var(--text-dim);
            border: 1px solid var(--border);
        }

        .journal-body {
            font-size: 0.82rem;
            line-height: 1.7;
            color: var(--text-dim);
            white-space: pre-wrap;
            max-height: 160px;
            overflow-y: auto;
            background: var(--bg);
            border: 1px solid var(--border);
            padding: 0.75rem 1rem;
            border-radius: 5px;
            font-family: var(--sans);
        }

        .journal-body.expanded { max-height: none; }

        .journal-body::-webkit-scrollbar { width: 3px; }
        .journal-body::-webkit-scrollbar-track { background: transparent; }
        .journal-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

        .expand-btn {
            margin-top: 0.4rem;
            background: none;
            border: none;
            font-family: var(--mono);
            font-size: 0.7rem;
            color: var(--amber);
            cursor: pointer;
        }

        .expand-btn:hover { text-decoration: underline; }

        .refresh-bar {
            display: flex;
            justify-content: flex-end;
            margin-bottom: 1rem;
        }

        /* Spinner */
        .spinner {
            display: inline-block;
            width: 12px;
            height: 12px;
            border: 2px solid var(--border);
            border-top-color: var(--amber);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Page transitions */
        .tab-content {
            display: none;
            animation: fadeIn 0.2s ease;
        }

        .tab-content.active {
            display: block;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(4px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Golden queries */
        .golden-bar {
            padding: 0.5rem 1rem;
            border-bottom: 1px solid var(--border);
            display: flex;
            gap: 0.4rem;
            flex-wrap: wrap;
            align-items: center;
            min-height: 36px;
        }

        .golden-bar:empty { display: none; }

        .golden-label {
            font-family: var(--mono);
            font-size: 0.55rem;
            font-weight: 600;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: var(--text-faint);
            margin-right: 0.25rem;
            flex-shrink: 0;
        }

        .golden-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.3rem;
            padding: 0.2rem 0.55rem;
            border-radius: 50px;
            border: 1px solid var(--border);
            background: var(--bg-surface);
            color: var(--text-dim);
            font-size: 0.68rem;
            cursor: pointer;
            transition: all 0.15s;
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .golden-chip:hover {
            border-color: var(--green);
            color: var(--green);
            background: var(--green-dim);
        }

        .golden-chip.pinned {
            border-color: rgba(34, 197, 94, 0.3);
            background: var(--green-dim);
            color: var(--green);
        }

        .golden-chip .chip-count {
            font-family: var(--mono);
            font-size: 0.55rem;
            opacity: 0.6;
        }

        /* Scrollbar */
        .main::-webkit-scrollbar { width: 5px; }
        .main::-webkit-scrollbar-track { background: transparent; }
        .main::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
        .main::-webkit-scrollbar-thumb:hover { background: var(--border-light); }
    </style>
</head>
<body>
    <div class="shell">
        <!-- Top Bar -->
        <div class="topbar">
            <div class="logo">
                <div class="logo-mark">FD</div>
                <div class="logo-text">FDA <span>// command</span></div>
            </div>
            <div class="topbar-right">
                <div class="system-clock" id="system-clock"></div>
                <div class="health-dot" id="health-dot" title="System health"></div>
            </div>
        </div>

        <!-- Sidebar -->
        <div class="sidebar">
            <div class="nav-section">
                <div class="nav-label">System</div>
                <div class="nav-item active" onclick="switchTab('overview')">
                    <span class="nav-icon">&#x25A0;</span>
                    Overview
                </div>
                <div class="nav-item" onclick="switchTab('setup')">
                    <span class="nav-icon">&#x2699;</span>
                    Configuration
                </div>
            </div>
            <div class="nav-section">
                <div class="nav-label">Agents</div>
                <div class="nav-item" onclick="switchTab('agents')">
                    <span class="nav-icon">&#x25B6;</span>
                    Pipeline
                </div>
                <div class="nav-item" onclick="switchTab('chat')">
                    <span class="nav-icon">&#x276F;</span>
                    Chat
                </div>
            </div>
            <div class="nav-section">
                <div class="nav-label">Data</div>
                <div class="nav-item" onclick="switchTab('journal')">
                    <span class="nav-icon">&#x2630;</span>
                    Journal
                    <span class="nav-badge" id="journal-count">--</span>
                </div>
            </div>
            <div class="sidebar-footer">
                <div class="version-tag">FDA v1.0 // Datacore</div>
            </div>
        </div>

        <!-- Main Content -->
        <div class="main">

            <!-- Overview Tab -->
            <div id="tab-overview" class="tab-content active">
                <div class="page-header">
                    <div class="page-title">System Overview</div>
                    <div class="page-subtitle">Facilitating Director Agent &mdash; multi-agent orchestration for client automation</div>
                </div>

                <div class="status-grid" id="status-grid">
                    <div class="status-tile" id="tile-anthropic">
                        <div class="status-indicator"></div>
                        <div>
                            <div class="status-label">Claude</div>
                            <div class="status-sub" id="claude-mode-label">--</div>
                        </div>
                    </div>
                    <div class="status-tile" id="tile-openai">
                        <div class="status-indicator"></div>
                        <div>
                            <div class="status-label">OpenAI</div>
                            <div class="status-sub">Realtime Voice</div>
                        </div>
                    </div>
                    <div class="status-tile" id="tile-discord">
                        <div class="status-indicator"></div>
                        <div>
                            <div class="status-label">Discord</div>
                            <div class="status-sub">Voice + Text</div>
                        </div>
                    </div>
                    <div class="status-tile" id="tile-telegram">
                        <div class="status-indicator"></div>
                        <div>
                            <div class="status-label">Telegram</div>
                            <div class="status-sub">Notifications</div>
                        </div>
                    </div>
                    <div class="status-tile" id="tile-outlook">
                        <div class="status-indicator"></div>
                        <div>
                            <div class="status-label">Outlook</div>
                            <div class="status-sub">Calendar</div>
                        </div>
                    </div>
                </div>

                <!-- Architecture diagram -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">Agent Pipeline</div>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; padding: 0.5rem 0;">
                        <div style="background: var(--amber-glow); border: 1px solid rgba(34,197,94,0.25); border-radius: 5px; padding: 0.4rem 0.75rem; font-family: var(--mono); font-size: 0.72rem; color: var(--amber); font-weight: 600;">KakaoTalk</div>
                        <div style="color: var(--text-faint); font-family: var(--mono); font-size: 0.7rem;">&rarr;</div>
                        <div style="background: var(--amber-glow); border: 1px solid rgba(34,197,94,0.25); border-radius: 5px; padding: 0.4rem 0.75rem; font-family: var(--mono); font-size: 0.72rem; color: var(--amber); font-weight: 600;">Orchestrator</div>
                        <div style="color: var(--text-faint); font-family: var(--mono); font-size: 0.7rem;">&rarr;</div>
                        <div style="background: var(--blue-dim); border: 1px solid rgba(96,165,250,0.25); border-radius: 5px; padding: 0.4rem 0.75rem; font-family: var(--mono); font-size: 0.72rem; color: var(--blue); font-weight: 600;">Worker (SSH)</div>
                        <div style="color: var(--text-faint); font-family: var(--mono); font-size: 0.7rem;">&rarr;</div>
                        <div style="background: var(--green-dim); border: 1px solid rgba(34,197,94,0.25); border-radius: 5px; padding: 0.4rem 0.75rem; font-family: var(--mono); font-size: 0.72rem; color: var(--green); font-weight: 600;">Telegram Approval</div>
                        <div style="color: var(--text-faint); font-family: var(--mono); font-size: 0.7rem;">&rarr;</div>
                        <div style="background: var(--green-dim); border: 1px solid rgba(34,197,94,0.25); border-radius: 5px; padding: 0.4rem 0.75rem; font-family: var(--mono); font-size: 0.72rem; color: var(--green); font-weight: 600;">Deploy</div>
                    </div>
                    <div style="margin-top: 0.5rem; display: flex; gap: 0.5rem; flex-wrap: wrap;">
                        <div style="font-family: var(--mono); font-size: 0.62rem; color: var(--text-faint); background: var(--bg); padding: 0.25rem 0.5rem; border-radius: 3px; border: 1px solid var(--border);">Discord Voice &uarr;</div>
                        <div style="font-family: var(--mono); font-size: 0.62rem; color: var(--text-faint); background: var(--bg); padding: 0.25rem 0.5rem; border-radius: 3px; border: 1px solid var(--border);">Outlook Calendar &uarr;</div>
                        <div style="font-family: var(--mono); font-size: 0.62rem; color: var(--text-faint); background: var(--bg); padding: 0.25rem 0.5rem; border-radius: 3px; border: 1px solid var(--border);">Journal &uarr;</div>
                    </div>
                </div>

                <!-- Quick Actions -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">Quick Actions</div>
                    </div>
                    <div class="actions-row">
                        <button class="btn" onclick="startTelegram()">Start Telegram</button>
                        <button class="btn" onclick="startDiscord()">Start Discord</button>
                        <button class="btn" onclick="viewLogs()">View Logs</button>
                        <button class="btn" onclick="checkHealth()">Health Check</button>
                    </div>
                    <div id="action-result" class="message" style="margin-top: 0.75rem;"></div>
                </div>
            </div>

            <!-- Setup Tab -->
            <div id="tab-setup" class="tab-content">
                <div class="page-header">
                    <div class="page-title">Configuration</div>
                    <div class="page-subtitle">API keys and service credentials</div>
                </div>

                <!-- Anthropic -->
                <div class="config-section" id="section-anthropic">
                    <div class="config-header" onclick="toggleSection('anthropic')">
                        <div class="config-icon anthropic">A</div>
                        <div>
                            <div class="config-name">Anthropic</div>
                            <div class="config-desc">Claude API for agent intelligence</div>
                        </div>
                        <span class="config-status" id="status-anthropic">--</span>
                        <span class="config-chevron">&#x25BC;</span>
                    </div>
                    <div class="config-body">
                        <form id="form-anthropic" onsubmit="saveConfig(event, 'anthropic')">
                            <div class="form-group">
                                <label class="form-label">API Key</label>
                                <div class="input-row">
                                    <input type="password" id="anthropic-key" name="key" placeholder="sk-ant-...">
                                    <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibility('anthropic-key')">&#x1F441;</button>
                                </div>
                            </div>
                            <div class="form-actions">
                                <button type="button" class="btn btn-sm" onclick="testConnection('anthropic')">Test</button>
                                <button type="submit" class="btn btn-sm btn-amber">Save</button>
                            </div>
                            <div id="anthropic-result" class="test-result"></div>
                            <div id="anthropic-message" class="message"></div>
                        </form>
                    </div>
                </div>

                <!-- File Index (local semantic search) -->
                <div class="config-section" id="section-index">
                    <div class="config-header" onclick="toggleSection('index')">
                        <div class="config-icon" style="background: linear-gradient(135deg,#22c55e,#0ea5e9); color:#fff;">&#x1F50D;</div>
                        <div>
                            <div class="config-name">File Index</div>
                            <div class="config-desc">Daily semantic index of Documents / Downloads / Desktop — runs locally, free</div>
                        </div>
                        <span class="config-status" id="status-index">--</span>
                        <span class="config-chevron">&#x25BC;</span>
                    </div>
                    <div class="config-body">
                        <div style="font-size:0.72rem;color:var(--text-dim);margin-bottom:0.8rem;line-height:1.6;">
                            Builds a semantic search index using a local multilingual embedding model
                            (<code style="background:var(--bg);padding:0.1rem 0.35rem;border-radius:3px;">paraphrase-multilingual-MiniLM-L12-v2</code>,
                            384-dim, supports Korean/English/50+ languages). No API key, no internet, no cost. First run
                            downloads ~100MB of model weights.
                        </div>
                        <div style="display:flex;align-items:center;gap:1rem;margin-bottom:0.6rem;">
                            <div style="font-size:0.82rem;font-weight:500;">Index status</div>
                            <span id="index-status-badge" style="font-family:var(--mono);font-size:0.68rem;color:var(--text-faint);">--</span>
                        </div>
                        <div id="index-stats" style="font-size:0.72rem;color:var(--text-dim);margin-bottom:0.6rem;line-height:1.6;">Loading...</div>
                        <div class="form-actions">
                            <button type="button" class="btn btn-sm" onclick="refreshIndexStats()">Refresh Stats</button>
                            <button type="button" class="btn btn-sm btn-amber" id="btn-run-index" onclick="runIndex(false)">Index Now</button>
                            <button type="button" class="btn btn-sm" onclick="runIndex(true)">Force Full Reindex</button>
                        </div>
                        <div id="index-progress" style="margin-top:0.6rem;font-family:var(--mono);font-size:0.66rem;color:var(--text-faint);max-height:120px;overflow-y:auto;"></div>
                    </div>
                </div>

                <!-- OpenAI -->
                <div class="config-section" id="section-openai">
                    <div class="config-header" onclick="toggleSection('openai')">
                        <div class="config-icon openai">O</div>
                        <div>
                            <div class="config-name">OpenAI</div>
                            <div class="config-desc">Realtime Voice API for Discord meetings</div>
                        </div>
                        <span class="config-status" id="status-openai">--</span>
                        <span class="config-chevron">&#x25BC;</span>
                    </div>
                    <div class="config-body">
                        <form id="form-openai" onsubmit="saveConfig(event, 'openai')">
                            <div class="form-group">
                                <label class="form-label">API Key</label>
                                <div class="input-row">
                                    <input type="password" id="openai-key" name="key" placeholder="sk-...">
                                    <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibility('openai-key')">&#x1F441;</button>
                                </div>
                            </div>
                            <div class="form-actions">
                                <button type="button" class="btn btn-sm" onclick="testConnection('openai')">Test</button>
                                <button type="submit" class="btn btn-sm btn-amber">Save</button>
                            </div>
                            <div id="openai-result" class="test-result"></div>
                            <div id="openai-message" class="message"></div>
                        </form>
                    </div>
                </div>

                <!-- Discord -->
                <div class="config-section" id="section-discord">
                    <div class="config-header" onclick="toggleSection('discord')">
                        <div class="config-icon discord">D</div>
                        <div>
                            <div class="config-name">Discord</div>
                            <div class="config-desc">Voice channel meetings and text commands</div>
                        </div>
                        <span class="config-status" id="status-discord">--</span>
                        <span class="config-chevron">&#x25BC;</span>
                    </div>
                    <div class="config-body">
                        <div class="steps">
                            <div class="steps-title">Setup</div>
                            <ol>
                                <li>Go to <a href="https://discord.com/developers/applications" target="_blank">Discord Developer Portal</a></li>
                                <li>Create application &rarr; Bot tab &rarr; copy token</li>
                                <li>Enable Message Content + Voice intents</li>
                                <li>OAuth2 &rarr; copy Client ID</li>
                            </ol>
                        </div>
                        <form id="form-discord" onsubmit="saveConfig(event, 'discord')">
                            <div class="form-group">
                                <label class="form-label">Bot Token</label>
                                <div class="input-row">
                                    <input type="password" id="discord-token" name="token" placeholder="Bot token">
                                    <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibility('discord-token')">&#x1F441;</button>
                                </div>
                            </div>
                            <div class="form-group">
                                <label class="form-label">Client ID</label>
                                <input type="text" id="discord-client-id" name="client_id" placeholder="123456789012345678">
                            </div>
                            <div class="form-actions">
                                <button type="button" class="btn btn-sm" onclick="testConnection('discord')">Test</button>
                                <button type="button" class="btn btn-sm" onclick="getDiscordInvite()">Invite Link</button>
                                <button type="submit" class="btn btn-sm btn-amber">Save</button>
                            </div>
                            <div id="discord-result" class="test-result"></div>
                            <div id="discord-invite" class="test-result"></div>
                            <div id="discord-message" class="message"></div>
                        </form>
                    </div>
                </div>

                <!-- Telegram -->
                <div class="config-section" id="section-telegram">
                    <div class="config-header" onclick="toggleSection('telegram')">
                        <div class="config-icon telegram">T</div>
                        <div>
                            <div class="config-name">Telegram</div>
                            <div class="config-desc">Notifications and approval workflows</div>
                        </div>
                        <span class="config-status" id="status-telegram">--</span>
                        <span class="config-chevron">&#x25BC;</span>
                    </div>
                    <div class="config-body">
                        <div class="steps">
                            <div class="steps-title">Setup</div>
                            <ol>
                                <li>Message <strong>@BotFather</strong> on Telegram</li>
                                <li>Send <code>/newbot</code> and follow prompts</li>
                                <li>Copy the bot token</li>
                            </ol>
                        </div>
                        <form id="form-telegram" onsubmit="saveConfig(event, 'telegram')">
                            <div class="form-group">
                                <label class="form-label">Bot Token</label>
                                <div class="input-row">
                                    <input type="password" id="telegram-token" name="token" placeholder="123456789:ABCDefGHI...">
                                    <button type="button" class="btn btn-sm btn-ghost" onclick="toggleVisibility('telegram-token')">&#x1F441;</button>
                                </div>
                            </div>
                            <div class="form-actions">
                                <button type="button" class="btn btn-sm" onclick="testConnection('telegram')">Test</button>
                                <button type="submit" class="btn btn-sm btn-amber">Save</button>
                            </div>
                            <div id="telegram-result" class="test-result"></div>
                            <div id="telegram-message" class="message"></div>
                        </form>
                    </div>
                </div>

                <!-- Outlook Calendar -->
                <div class="config-section" id="section-outlook">
                    <div class="config-header" onclick="toggleSection('outlook')">
                        <div class="config-icon outlook">&#x2612;</div>
                        <div>
                            <div class="config-name">Outlook Calendar</div>
                            <div class="config-desc">Office 365 calendar monitoring and meeting prep</div>
                        </div>
                        <span class="config-status" id="status-outlook">--</span>
                        <span class="config-chevron">&#x25BC;</span>
                    </div>
                    <div class="config-body">
                        <div id="outlook-logged-in" style="display:none;">
                            <div class="outlook-account">
                                <div>
                                    <div class="oa-label">Signed in as</div>
                                    <div class="oa-email" id="outlook-email">--</div>
                                </div>
                                <button class="btn btn-sm" style="margin-left:auto;" onclick="outlookLogout()">Sign Out</button>
                            </div>
                        </div>
                        <div id="outlook-logged-out">
                            <div class="steps">
                                <div class="steps-title">How it works</div>
                                <ol>
                                    <li>Click <strong>Sign In</strong> below</li>
                                    <li>A device code will appear &mdash; copy it</li>
                                    <li>Open the Microsoft login link and paste the code</li>
                                    <li>Sign in with your Office 365 account</li>
                                </ol>
                            </div>
                            <div id="outlook-device-flow" style="display:none;">
                                <div class="device-code-box">
                                    <div class="dc-label">Enter this code at Microsoft</div>
                                    <div class="dc-code" id="outlook-device-code">------</div>
                                    <div class="dc-url"><a id="outlook-login-link" href="#" target="_blank">Open microsoft.com/devicelogin</a></div>
                                    <div class="dc-status"><span class="spinner"></span> Waiting for you to sign in...</div>
                                    <div class="dc-regen">
                                        <button type="button" class="btn btn-sm" id="outlook-regen-btn" onclick="regenerateOutlookCode()">Regenerate Code</button>
                                    </div>
                                </div>
                            </div>
                            <div class="form-actions" id="outlook-actions">
                                <button type="button" class="btn btn-sm btn-amber" id="outlook-signin-btn" onclick="outlookSignIn()">Sign In</button>
                            </div>
                        </div>
                        <div id="outlook-result" class="test-result"></div>
                        <div id="outlook-message" class="message"></div>
                    </div>
                </div>
            </div>

            <!-- Agents Tab -->
            <div id="tab-agents" class="tab-content">
                <div class="page-header">
                    <div class="page-title">Agent Pipeline</div>
                    <div class="page-subtitle">Active agents and their task queues</div>
                </div>
                <div class="refresh-bar">
                    <button class="btn btn-sm" onclick="loadAgentTasks()">Refresh</button>
                </div>

                <div class="agent-grid">
                    <!-- Orchestrator -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar orchestrator">&#x25A0;</div>
                            <div class="agent-info">
                                <h3>Orchestrator</h3>
                                <p>Classifies KakaoTalk messages, creates task briefs, coordinates pipeline</p>
                            </div>
                            <div class="agent-role-tag">Director</div>
                        </div>
                        <ul class="task-list" id="tasks-fda">
                            <li class="no-tasks">No tasks</li>
                        </ul>
                    </div>

                    <!-- Worker -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar worker">&#x2699;</div>
                            <div class="agent-info">
                                <h3>Worker Agent</h3>
                                <p>Analyzes codebases via SSH, generates fixes, prepares diffs for approval</p>
                            </div>
                            <div class="agent-role-tag">Executor</div>
                        </div>
                        <ul class="task-list" id="tasks-worker">
                            <li class="no-tasks">No tasks</li>
                        </ul>
                    </div>

                    <!-- Local Worker -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar worker-local">&#x1F4BB;</div>
                            <div class="agent-info">
                                <h3>Local Worker</h3>
                                <p>Analyzes and modifies local codebases on the Mac Mini filesystem</p>
                            </div>
                            <div class="agent-role-tag">Local</div>
                        </div>
                        <ul class="task-list" id="tasks-worker_local">
                            <li class="no-tasks">No tasks</li>
                        </ul>
                    </div>

                    <!-- Discord Voice -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar discord">&#x266A;</div>
                            <div class="agent-info">
                                <h3>Discord Voice</h3>
                                <p>Joins voice channels, takes meeting notes, answers questions via realtime API</p>
                            </div>
                            <div class="agent-role-tag">Channel</div>
                        </div>
                        <ul class="task-list" id="tasks-discord">
                            <li class="no-tasks">Listening</li>
                        </ul>
                    </div>

                    <!-- Telegram -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar telegram">&#x2709;</div>
                            <div class="agent-info">
                                <h3>Telegram Bot</h3>
                                <p>User Q&A, approval requests, push notifications for completed tasks</p>
                            </div>
                            <div class="agent-role-tag">Channel</div>
                        </div>
                        <ul class="task-list" id="tasks-telegram">
                            <li class="no-tasks">Standby</li>
                        </ul>
                    </div>

                    <!-- KakaoTalk -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar kakaotalk">&#x2709;</div>
                            <div class="agent-info">
                                <h3>KakaoTalk Reader</h3>
                                <p>Monitors client chat rooms for task requests and updates</p>
                            </div>
                            <div class="agent-role-tag">Ingest</div>
                        </div>
                        <ul class="task-list" id="tasks-kakaotalk">
                            <li class="no-tasks">Polling</li>
                        </ul>
                    </div>

                    <!-- Calendar -->
                    <div class="agent-card">
                        <div class="agent-top">
                            <div class="agent-avatar calendar">&#x2612;</div>
                            <div class="agent-info">
                                <h3>Outlook Calendar</h3>
                                <p>Monitors schedule, prepares meeting briefs, tracks deadlines</p>
                            </div>
                            <div class="agent-role-tag">Monitor</div>
                        </div>
                        <ul class="task-list" id="tasks-calendar">
                            <li class="no-tasks">Watching</li>
                        </ul>
                    </div>
                </div>
            </div>

            <!-- Chat Tab -->
            <div id="tab-chat" class="tab-content">
                <div class="page-header">
                    <div class="page-title">Agent Chat</div>
                    <div class="page-subtitle">Direct conversation with system agents</div>
                </div>
                <div class="chat-layout">
                    <div class="chat-agents">
                        <button class="chat-agent-btn selected" onclick="selectAgent('fda')">
                            <span class="ca-icon">&#x25A0;</span>
                            <div>
                                <div class="ca-name">FDA</div>
                                <div class="ca-role">director</div>
                            </div>
                        </button>
                        <button class="chat-agent-btn" onclick="selectAgent('worker')">
                            <span class="ca-icon">&#x2699;</span>
                            <div>
                                <div class="ca-name">Worker</div>
                                <div class="ca-role">remote SSH</div>
                            </div>
                        </button>
                        <button class="chat-agent-btn" onclick="selectAgent('worker_local')">
                            <span class="ca-icon">&#x1F4BB;</span>
                            <div>
                                <div class="ca-name">Local</div>
                                <div class="ca-role">local files</div>
                            </div>
                        </button>
                    </div>
                    <div class="chat-panel">
                        <div class="chat-top" id="chat-header">// FDA</div>
                        <div class="golden-bar" id="golden-bar"></div>
                        <div class="chat-messages" id="chat-messages">
                            <div class="chat-empty">Start a conversation</div>
                        </div>
                        <div class="chat-bottom">
                            <textarea class="chat-input" id="chat-input" placeholder="Send a message..." rows="1" onkeydown="handleChatKeydown(event)"></textarea>
                            <button class="btn btn-amber" onclick="sendChatMessage()">Send</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Journal Tab -->
            <div id="tab-journal" class="tab-content">
                <div class="page-header">
                    <div class="page-title">Journal</div>
                    <div class="page-subtitle">System decisions, insights, and knowledge base</div>
                </div>
                <div class="refresh-bar">
                    <button class="btn btn-sm" onclick="loadJournalEntries()">Refresh</button>
                </div>
                <div class="journal-list" id="journal-entries">
                    <div class="no-tasks">Loading...</div>
                </div>
            </div>

        </div>
    </div>

    <script>
        // Clock
        function updateClock() {
            const now = new Date();
            const h = String(now.getHours()).padStart(2, '0');
            const m = String(now.getMinutes()).padStart(2, '0');
            const s = String(now.getSeconds()).padStart(2, '0');
            document.getElementById('system-clock').textContent = h + ':' + m + ':' + s;
        }
        setInterval(updateClock, 1000);
        updateClock();

        // Init
        document.addEventListener('DOMContentLoaded', function() {
            loadStatus();
            loadGoldenQueries();
            refreshIndexStats();
        });

        async function loadStatus() {
            try {
                const r = await fetch('/api/status');
                const d = await r.json();
                setTile('anthropic', d.anthropic?.configured);
                setTile('openai', d.openai?.configured);
                setTile('discord', d.discord?.configured);
                setTile('telegram', d.telegram?.configured);
                setTile('outlook', d.outlook?.configured);

                setConfigStatus('anthropic', d.anthropic?.configured);
                // Show Claude backend mode
                const modeLabel = document.getElementById('claude-mode-label');
                if (modeLabel) {
                    const mode = d.anthropic?.mode;
                    if (mode === 'cli') modeLabel.textContent = 'Max (CLI)';
                    else if (mode === 'api') modeLabel.textContent = 'API';
                    else modeLabel.textContent = 'Not configured';
                }
                setConfigStatus('openai', d.openai?.configured);
                setConfigStatus('discord', d.discord?.configured);
                setConfigStatus('telegram', d.telegram?.configured);
                setConfigStatus('outlook', d.outlook?.configured);

                // Update Outlook UI based on login state
                if (d.outlook?.configured) {
                    document.getElementById('outlook-logged-in').style.display = 'block';
                    document.getElementById('outlook-logged-out').style.display = 'none';
                    if (d.outlook.account) document.getElementById('outlook-email').textContent = d.outlook.account;
                } else {
                    document.getElementById('outlook-logged-in').style.display = 'none';
                    document.getElementById('outlook-logged-out').style.display = 'block';
                }

                const allOk = d.anthropic?.configured && d.openai?.configured;
                document.getElementById('health-dot').className = allOk ? 'health-dot' : 'health-dot offline';
            } catch (e) {
                console.error('Status load failed:', e);
            }
        }

        function setTile(svc, ok) {
            const t = document.getElementById('tile-' + svc);
            if (t) t.className = 'status-tile ' + (ok ? 'ok' : 'err');
        }

        function setConfigStatus(svc, ok) {
            const el = document.getElementById('status-' + svc);
            if (el) {
                el.className = 'config-status ' + (ok ? 'ok' : 'missing');
                el.textContent = ok ? 'OK' : 'MISSING';
            }
        }

        // Config sections
        function toggleSection(name) {
            document.getElementById('section-' + name).classList.toggle('open');
        }

        function toggleVisibility(id) {
            const el = document.getElementById(id);
            el.type = el.type === 'password' ? 'text' : 'password';
        }

        async function saveConfig(event, service) {
            event.preventDefault();
            const form = event.target;
            const data = Object.fromEntries(new FormData(form).entries());
            const msg = document.getElementById(service + '-message');
            msg.className = 'message info';
            msg.textContent = 'Saving...';
            msg.style.display = 'block';

            try {
                const r = await fetch('/api/config/' + service, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const res = await r.json();
                msg.className = 'message ' + (res.success ? 'success' : 'error');
                msg.textContent = res.message || res.error || (res.success ? 'Saved' : 'Failed');
                if (res.success) loadStatus();
            } catch (e) {
                msg.className = 'message error';
                msg.textContent = e.message;
            }
        }

        // --- File Index ---
        async function refreshIndexStats() {
            try {
                const r = await fetch('/api/index/stats');
                const d = await r.json();
                if (!d.success) return;
                const el = document.getElementById('index-stats');
                const badge = document.getElementById('index-status-badge');
                const statusEl = document.getElementById('status-index');
                const total = d.total || 0;
                if (statusEl) {
                    statusEl.textContent = total > 0 ? total + ' files' : 'Not indexed';
                    statusEl.style.color = total > 0 ? 'var(--amber)' : 'var(--text-faint)';
                }
                let html = 'Indexed files: <strong style="color:var(--amber);">' + total + '</strong>';
                if (d.by_extension && d.by_extension.length) {
                    html += ' &middot; ' + d.by_extension.slice(0,5).map(function(r) {
                        return (r.extension || 'none') + ' (' + r.count + ')';
                    }).join(', ');
                }
                if (d.last_run) {
                    const lr = d.last_run;
                    badge.textContent = 'last run ' + (lr.finished_at || lr.started_at);
                    if (lr.error) badge.textContent += ' — error: ' + lr.error;
                } else {
                    badge.textContent = 'never run';
                }
                el.innerHTML = html;
            } catch (e) { /* ignore */ }
        }

        let _indexPollTimer = null;
        async function runIndex(force) {
            const btn = document.getElementById('btn-run-index');
            const prog = document.getElementById('index-progress');
            prog.textContent = 'Starting indexer...';
            btn.disabled = true;
            try {
                const r = await fetch('/api/index/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({force: !!force})
                });
                const res = await r.json();
                if (!res.success) {
                    prog.textContent = 'Error: ' + res.error;
                    btn.disabled = false;
                    return;
                }
                if (_indexPollTimer) clearInterval(_indexPollTimer);
                _indexPollTimer = setInterval(pollIndexProgress, 1500);
            } catch (e) {
                prog.textContent = 'Error: ' + e.message;
                btn.disabled = false;
            }
        }

        async function pollIndexProgress() {
            try {
                const r = await fetch('/api/index/progress');
                const d = await r.json();
                const prog = document.getElementById('index-progress');
                if (d.progress && d.progress.length) {
                    prog.innerHTML = d.progress.slice(-15).map(function(l) {
                        return esc(l);
                    }).join('<br>');
                    prog.scrollTop = prog.scrollHeight;
                }
                if (!d.running) {
                    clearInterval(_indexPollTimer);
                    _indexPollTimer = null;
                    document.getElementById('btn-run-index').disabled = false;
                    refreshIndexStats();
                }
            } catch (e) { /* ignore */ }
        }

        async function testConnection(service) {
            const rd = document.getElementById(service + '-result');
            rd.className = 'test-result';
            rd.textContent = 'Testing...';

            let body = {};
            if (service === 'anthropic') body.key = document.getElementById('anthropic-key').value;
            else if (service === 'telegram') body.token = document.getElementById('telegram-token').value;
            else if (service === 'discord') body.token = document.getElementById('discord-token').value;
            else if (service === 'openai') body.key = document.getElementById('openai-key').value;

            try {
                const r = await fetch('/api/test/' + service, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                const res = await r.json();
                rd.className = 'test-result ' + (res.success ? 'success' : 'error');
                rd.textContent = (res.success ? 'OK: ' : 'FAIL: ') + (res.message || res.error);
            } catch (e) {
                rd.className = 'test-result error';
                rd.textContent = 'ERR: ' + e.message;
            }
        }

        async function getDiscordInvite() {
            const rd = document.getElementById('discord-invite');
            const cid = document.getElementById('discord-client-id').value;
            if (!cid) { rd.className = 'test-result error'; rd.textContent = 'Enter Client ID first'; return; }
            try {
                const r = await fetch('/api/discord/invite?client_id=' + cid);
                const res = await r.json();
                if (res.url) {
                    rd.className = 'test-result success';
                    rd.innerHTML = 'Invite: <a href="' + res.url + '" target="_blank" style="color: var(--green);">' + res.url + '</a>';
                } else {
                    rd.className = 'test-result error';
                    rd.textContent = res.error || 'Failed';
                }
            } catch (e) { rd.className = 'test-result error'; rd.textContent = e.message; }
        }

        async function startTelegram() {
            showAction('Starting Telegram...', 'info');
            try {
                const r = await fetch('/api/start/telegram', {method:'POST'});
                const res = await r.json();
                showAction(res.message || 'Done', res.success ? 'success' : 'error');
            } catch (e) { showAction(e.message, 'error'); }
        }

        async function startDiscord() {
            showAction('Starting Discord...', 'info');
            try {
                const r = await fetch('/api/start/discord', {method:'POST'});
                const res = await r.json();
                showAction(res.message || 'Done', res.success ? 'success' : 'error');
            } catch (e) { showAction(e.message, 'error'); }
        }

        function viewLogs() { window.open('/api/logs', '_blank'); }

        async function checkHealth() {
            showAction('Checking...', 'info');
            try {
                const r = await fetch('/api/health');
                const res = await r.json();
                let m = 'Database: ' + (res.database ? 'OK' : 'FAIL') + '\\n';
                m += 'Anthropic: ' + (res.anthropic ? 'OK' : 'FAIL') + '\\n';
                m += 'Telegram: ' + (res.telegram ? 'OK' : 'FAIL') + '\\n';
                m += 'Discord: ' + (res.discord ? 'OK' : 'FAIL');
                showAction(m, res.healthy ? 'success' : 'error');
            } catch (e) { showAction(e.message, 'error'); }
        }

        function showAction(msg, type) {
            const d = document.getElementById('action-result');
            d.className = 'message ' + type;
            d.textContent = msg;
            d.style.display = 'block';
            d.style.whiteSpace = 'pre-line';
        }

        // Outlook Calendar
        let outlookPollTimer = null;

        async function outlookSignIn() {
            const btn = document.getElementById('outlook-signin-btn');
            btn.disabled = true;
            btn.textContent = 'Starting...';
            const rd = document.getElementById('outlook-result');
            rd.className = 'test-result';
            rd.textContent = '';

            try {
                const r = await fetch('/api/calendar/login', { method: 'POST' });
                const res = await r.json();
                if (res.success && res.user_code) {
                    document.getElementById('outlook-device-flow').style.display = 'block';
                    document.getElementById('outlook-device-code').textContent = res.user_code;
                    const link = document.getElementById('outlook-login-link');
                    link.href = res.verification_uri;
                    link.textContent = 'Open ' + res.verification_uri;
                    btn.style.display = 'none';
                    // Poll for completion
                    outlookPollTimer = setInterval(pollOutlookLogin, 3000);
                } else if (res.success && res.already_logged_in) {
                    rd.className = 'test-result success';
                    rd.textContent = 'Already signed in as ' + (res.account || '');
                    btn.disabled = false;
                    btn.textContent = 'Sign In';
                    loadStatus();
                } else {
                    rd.className = 'test-result error';
                    rd.textContent = res.error || 'Failed to start login';
                    btn.disabled = false;
                    btn.textContent = 'Sign In';
                }
            } catch (e) {
                rd.className = 'test-result error';
                rd.textContent = 'Error: ' + e.message;
                btn.disabled = false;
                btn.textContent = 'Sign In';
            }
        }

        async function regenerateOutlookCode() {
            const regenBtn = document.getElementById('outlook-regen-btn');
            regenBtn.disabled = true;
            regenBtn.textContent = 'Regenerating...';

            // Stop current polling
            if (outlookPollTimer) {
                clearInterval(outlookPollTimer);
                outlookPollTimer = null;
            }

            try {
                // Reset backend state first
                await fetch('/api/calendar/login/reset', { method: 'POST' });

                // Start a new device flow
                const r = await fetch('/api/calendar/login', { method: 'POST' });
                const res = await r.json();
                if (res.success && res.user_code) {
                    document.getElementById('outlook-device-code').textContent = res.user_code;
                    const link = document.getElementById('outlook-login-link');
                    link.href = res.verification_uri;
                    link.textContent = 'Open ' + res.verification_uri;
                    // Restart polling
                    outlookPollTimer = setInterval(pollOutlookLogin, 3000);
                } else {
                    const rd = document.getElementById('outlook-result');
                    rd.className = 'test-result error';
                    rd.textContent = res.error || 'Failed to regenerate code';
                }
            } catch (e) {
                const rd = document.getElementById('outlook-result');
                rd.className = 'test-result error';
                rd.textContent = 'Error: ' + e.message;
            }

            regenBtn.disabled = false;
            regenBtn.textContent = 'Regenerate Code';
        }

        async function pollOutlookLogin() {
            try {
                const r = await fetch('/api/calendar/login/status');
                const res = await r.json();
                if (res.status === 'completed') {
                    clearInterval(outlookPollTimer);
                    outlookPollTimer = null;
                    document.getElementById('outlook-device-flow').style.display = 'none';
                    const rd = document.getElementById('outlook-result');
                    rd.className = 'test-result success';
                    rd.textContent = 'Signed in as ' + (res.account || 'Office 365');
                    const btn = document.getElementById('outlook-signin-btn');
                    btn.style.display = '';
                    btn.disabled = false;
                    btn.textContent = 'Sign In';
                    loadStatus();
                } else if (res.status === 'expired') {
                    // Code expired — auto-regenerate
                    clearInterval(outlookPollTimer);
                    outlookPollTimer = null;
                    regenerateOutlookCode();
                } else if (res.status === 'failed') {
                    clearInterval(outlookPollTimer);
                    outlookPollTimer = null;
                    document.getElementById('outlook-device-flow').style.display = 'none';
                    const rd = document.getElementById('outlook-result');
                    rd.className = 'test-result error';
                    rd.textContent = res.error || 'Login failed';
                    const btn = document.getElementById('outlook-signin-btn');
                    btn.style.display = '';
                    btn.disabled = false;
                    btn.textContent = 'Sign In';
                }
                // status === 'pending' → keep polling
            } catch (e) { /* keep polling */ }
        }

        async function outlookLogout() {
            try {
                const r = await fetch('/api/calendar/logout', { method: 'POST' });
                const res = await r.json();
                const rd = document.getElementById('outlook-result');
                rd.className = 'test-result ' + (res.success ? 'success' : 'error');
                rd.textContent = res.message || res.error || 'Done';
                loadStatus();
            } catch (e) {
                const rd = document.getElementById('outlook-result');
                rd.className = 'test-result error';
                rd.textContent = e.message;
            }
        }

        // Tab navigation
        function switchTab(name) {
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            event.currentTarget.classList.add('active');
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.getElementById('tab-' + name).classList.add('active');

            if (name === 'agents') loadAgentTasks();
            else if (name === 'journal') loadJournalEntries();
        }

        // Agent tasks
        async function loadAgentTasks() {
            try {
                const r = await fetch('/api/agents/tasks');
                const d = await r.json();
                renderTasks('fda', d.fda || []);
                renderTasks('worker', d.worker || d.executor || []);
                renderTasks('worker_local', d.worker_local || []);
            } catch (e) { console.error(e); }
        }

        function renderTasks(agent, tasks) {
            const c = document.getElementById('tasks-' + agent);
            if (!c) return;
            if (!tasks.length) { c.innerHTML = '<li class="no-tasks">No tasks</li>'; return; }
            c.innerHTML = tasks.map(t => '<li class="task-item">' +
                '<span class="task-dot ' + (t.status || 'pending') + '"></span>' +
                '<span class="task-text">' + esc(t.title || t.description || 'Task') + '</span>' +
                '<span class="task-time">' + (t.created_at ? fmtDate(t.created_at) : '') + '</span>' +
                '</li>').join('');
        }

        // Chat
        let currentAgent = 'fda';
        const chatHistories = { fda: [], worker: [], worker_local: [] };

        function selectAgent(agent) {
            currentAgent = agent;
            document.querySelectorAll('.chat-agent-btn').forEach(b => b.classList.remove('selected'));
            event.currentTarget.classList.add('selected');
            const names = { fda: 'FDA', worker: 'Worker', worker_local: 'Local Worker' };
            document.getElementById('chat-header').textContent = '// ' + (names[agent] || agent);
            const inp = document.getElementById('chat-input');
            if (agent === 'worker_local') {
                inp.placeholder = '/organize ~/path | /analyze ~/path task | /ls ~/path | /help';
            } else {
                inp.placeholder = 'Send a message...';
            }
            renderChat();
            loadGoldenQueries();
        }

        // Golden queries
        async function loadGoldenQueries() {
            const bar = document.getElementById('golden-bar');
            if (!bar) return;
            try {
                const r = await fetch('/api/queries?limit=8');
                const d = await r.json();
                if (!d.success) { bar.innerHTML = ''; return; }
                const all = d.frequent || [];
                if (!all.length) { bar.innerHTML = ''; return; }
                bar.innerHTML = '<span class="golden-label">Frequent</span>' +
                    all.map(q => {
                        const label = q.query.length > 35 ? q.query.substring(0, 35) + '...' : q.query;
                        const pinCls = q.pinned ? ' pinned' : '';
                        return '<span class="golden-chip' + pinCls + '" ' +
                            'title="' + esc(q.query) + ' (' + q.hit_count + 'x)" ' +
                            'onclick="useGoldenQuery(' + JSON.stringify(q.query) + ', ' + JSON.stringify(q.agent) + ')">' +
                            esc(label) +
                            (q.hit_count > 1 ? ' <span class="chip-count">' + q.hit_count + '</span>' : '') +
                            '</span>';
                    }).join('');
            } catch (e) { bar.innerHTML = ''; }
        }

        function useGoldenQuery(query, agent) {
            if (agent !== currentAgent) {
                // Switch to the right agent first
                const btns = document.querySelectorAll('.chat-agent-btn');
                const agents = ['fda', 'worker', 'worker_local'];
                const idx = agents.indexOf(agent);
                if (idx >= 0 && btns[idx]) btns[idx].click();
            }
            document.getElementById('chat-input').value = query;
            document.getElementById('chat-input').focus();
        }

        function linkifyPaths(html) {
            // Detect absolute file paths like /Users/... and make them clickable
            return html.replace(
                /(\/Users\/[^\s<&,)]+)/g,
                function(match) {
                    const fname = match.split('/').pop();
                    const ext = fname.split('.').pop().toLowerCase();
                    const icon = {html:'&#x1F310;', pdf:'&#x1F4C4;', py:'&#x1F40D;', js:'&#x26A1;', ts:'&#x26A1;',
                                  json:'&#x1F4CB;', csv:'&#x1F4CA;', md:'&#x1F4DD;', txt:'&#x1F4DD;'}[ext] || '&#x1F4C1;';
                    return '<a class="file-link" href="/api/files/view?path=' + encodeURIComponent(match) +
                           '" target="_blank" title="' + match + '">' + icon + ' ' + fname + '</a>';
                }
            );
        }

        function formatChat(text) {
            let html = esc(text);
            // Convert **bold** markers
            html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
            // Convert `code` markers
            html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
            // Linkify file paths
            html = linkifyPaths(html);
            // Convert newlines to breaks
            html = html.replace(/\\n/g, '<br>');
            return html;
        }

        function renderChat() {
            const c = document.getElementById('chat-messages');
            const h = chatHistories[currentAgent] || [];
            if (!h.length) { c.innerHTML = '<div class="chat-empty">Start a conversation</div>'; return; }
            c.innerHTML = h.map(m => '<div class="chat-msg ' + m.role + '">' + formatChat(m.content) + '</div>').join('');
            c.scrollTop = c.scrollHeight;
        }

        function handleChatKeydown(e) {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
        }

        async function sendChatMessage() {
            const inp = document.getElementById('chat-input');
            const msg = inp.value.trim();
            if (!msg) return;

            if (!chatHistories[currentAgent]) chatHistories[currentAgent] = [];
            chatHistories[currentAgent].push({role:'user', content:msg});
            renderChat();
            inp.value = '';

            const c = document.getElementById('chat-messages');
            const td = document.createElement('div');
            td.className = 'chat-msg agent';
            td.innerHTML = '<span class="spinner"></span> Thinking...';
            c.appendChild(td);
            c.scrollTop = c.scrollHeight;

            try {
                const r = await fetch('/api/agents/chat', {
                    method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({agent: currentAgent, message: msg})
                });
                const res = await r.json();
                td.remove();
                chatHistories[currentAgent].push({role:'agent', content: res.success ? res.response : ('Error: ' + (res.error || 'Failed'))});
                renderChat();
                loadGoldenQueries();
            } catch (e) {
                td.remove();
                chatHistories[currentAgent].push({role:'agent', content:'Error: ' + e.message});
                renderChat();
            }
        }

        // Journal
        async function loadJournalEntries() {
            const c = document.getElementById('journal-entries');
            c.innerHTML = '<div class="no-tasks">Loading...</div>';
            try {
                const r = await fetch('/api/journal/entries');
                const d = await r.json();
                const badge = document.getElementById('journal-count');
                if (badge) badge.textContent = (d.entries || []).length;

                if (!d.entries || !d.entries.length) { c.innerHTML = '<div class="no-tasks">No entries yet</div>'; return; }
                c.innerHTML = d.entries.map((e, i) => {
                    const body = e.content || '';
                    const isLong = body.length > 300;
                    const isChat = e.is_chat || false;
                    const hasRaw = e.has_raw || false;
                    return '<div class="journal-card">' +
                        '<div class="journal-top"><div class="journal-title">' + esc(e.summary || 'Untitled') + '</div>' +
                        '<div class="journal-meta"><span class="journal-author-tag">' + esc(e.author || '?') + '</span>' +
                        '<div class="journal-date">' + (e.timestamp ? fmtDate(e.timestamp) : '') + '</div></div></div>' +
                        (e.tags && e.tags.length ? '<div class="journal-tags">' + e.tags.map(t => '<span class="journal-tag">' + esc(t) + '</span>').join('') + '</div>' : '') +
                        (body ? '<div class="journal-body" id="jb-' + i + '">' + esc(body) + '</div>' : '') +
                        '<div style="display:flex;gap:0.5rem;margin-top:0.4rem;">' +
                        (isLong ? '<button class="expand-btn" onclick="toggleJournal(' + i + ')">show more</button>' : '') +
                        (hasRaw ? '<button class="expand-btn" data-entry="' + esc(e.id) + '" data-idx="' + i + '" onclick="toggleRaw(this)">view raw chat</button>' : '') +
                        '</div>' +
                        (hasRaw ? '<div class="journal-body" id="jb-raw-' + i + '" style="display:none;font-family:var(--mono);font-size:0.72rem;margin-top:0.35rem;"></div>' : '') +
                        '</div>';
                }).join('');
            } catch (e) { c.innerHTML = '<div class="no-tasks">Error: ' + e.message + '</div>'; }
        }

        function toggleJournal(i) {
            const el = document.getElementById('jb-' + i);
            if (!el) return;
            const parent = el.parentElement;
            const btn = parent.querySelector('.expand-btn[onclick*="toggleJournal"]');
            if (el.classList.contains('expanded')) { el.classList.remove('expanded'); if (btn) btn.textContent = 'show more'; }
            else { el.classList.add('expanded'); if (btn) btn.textContent = 'show less'; }
        }

        async function toggleRaw(btn) {
            const idx = btn.dataset.idx;
            const entryId = btn.dataset.entry;
            const rawEl = document.getElementById('jb-raw-' + idx);
            if (!rawEl) return;

            if (rawEl.style.display === 'none') {
                // Load raw content if not yet loaded
                if (!rawEl.dataset.loaded) {
                    rawEl.textContent = 'Loading...';
                    rawEl.style.display = 'block';
                    try {
                        const r = await fetch('/api/journal/entry/' + encodeURIComponent(entryId) + '/raw');
                        const res = await r.json();
                        rawEl.textContent = res.success ? res.content : ('Error: ' + res.error);
                        rawEl.dataset.loaded = '1';
                    } catch (e) {
                        rawEl.textContent = 'Error: ' + e.message;
                    }
                } else {
                    rawEl.style.display = 'block';
                }
                btn.textContent = 'hide raw chat';
            } else {
                rawEl.style.display = 'none';
                btn.textContent = 'view raw chat';
            }
        }

        function esc(t) {
            if (!t) return '';
            const d = document.createElement('div');
            d.textContent = t;
            return d.innerHTML;
        }

        function fmtDate(s) {
            try {
                const d = new Date(s);
                return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
            } catch { return s; }
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

    # Enable CORS for API routes (allows chat.html opened as file:// or from other origins)
    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    # Initialize state
    state = ProjectState()

    # Outlook calendar login state (thread-safe)
    import threading
    _outlook_login = {
        "status": "idle",       # idle | pending | completed | failed
        "user_code": None,
        "verification_uri": None,
        "account": None,
        "error": None,
        "thread": None,
        "expires_at": 0,
        "flow_id": 0,           # incremented on each new flow to prevent stale thread writes
    }
    _outlook_lock = threading.Lock()

    def _get_outlook_status():
        """Get Outlook calendar status for the status API."""
        try:
            from fda.outlook import OutlookCalendar
            cal = OutlookCalendar()
            logged_in = cal.is_logged_in()
            account = None
            if logged_in:
                app = cal._get_msal_app()
                accounts = app.get_accounts()
                if accounts:
                    account = accounts[0].get("username", "")
            return {"configured": logged_in, "account": account}
        except Exception:
            return {"configured": False, "account": None}

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

    @app.route("/chat")
    def chat_page():
        """Serve the standalone chat interface."""
        chat_html_path = Path(__file__).parent.parent / "chat.html"
        if chat_html_path.exists():
            return chat_html_path.read_text(encoding="utf-8")
        return "chat.html not found. Place it in the project root.", 404

    @app.route("/api/status")
    def get_status():
        """Get configuration status for all services."""
        from fda.claude_backend import ClaudeCodeCLIBackend
        cli_available = ClaudeCodeCLIBackend.is_available()
        api_key_set = bool(
            os.environ.get(ANTHROPIC_API_KEY_ENV)
            or state.get_context("anthropic_api_key")
        )
        return jsonify({
            "anthropic": {
                "configured": cli_available or api_key_set,
                "mode": "cli" if cli_available else ("api" if api_key_set else "none"),
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
            "outlook": _get_outlook_status(),
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

    @app.route("/api/index/stats")
    def get_index_stats():
        """Return file indexer stats."""
        try:
            stats = state.get_file_embeddings_stats()
            from fda.config import FILE_INDEXER_EMBEDDING_MODEL
            stats["model"] = FILE_INDEXER_EMBEDDING_MODEL
            return jsonify({"success": True, **stats})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    _indexer_lock = threading.Lock()
    _indexer_state = {"running": False, "progress": [], "last_stats": None}

    @app.route("/api/index/run", methods=["POST"])
    def run_index():
        """Trigger a file index run (in background)."""
        with _indexer_lock:
            if _indexer_state["running"]:
                return jsonify({"success": False, "error": "Indexer already running"})
            _indexer_state["running"] = True
            _indexer_state["progress"] = []

        data = request.get_json() or {}
        force = bool(data.get("force"))

        def worker():
            try:
                from fda.file_indexer import FileIndexer
                indexer = FileIndexer(state)
                def cb(msg):
                    _indexer_state["progress"].append(msg)
                    if len(_indexer_state["progress"]) > 50:
                        _indexer_state["progress"] = _indexer_state["progress"][-50:]
                stats = indexer.run(force=force, progress_cb=cb)
                _indexer_state["last_stats"] = stats.to_dict()
                _indexer_state["progress"].append(
                    f"Done: scanned={stats.scanned} embedded={stats.embedded} "
                    f"skipped={stats.skipped} deleted={stats.deleted}"
                )
            except Exception as e:
                logger.exception("Indexer run failed")
                _indexer_state["progress"].append(f"Error: {e}")
            finally:
                _indexer_state["running"] = False

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return jsonify({"success": True, "message": "Indexer started"})

    @app.route("/api/index/progress")
    def get_index_progress():
        """Stream of recent progress lines for the running indexer."""
        return jsonify({
            "success": True,
            "running": _indexer_state["running"],
            "progress": list(_indexer_state["progress"]),
            "last_stats": _indexer_state["last_stats"],
        })

    @app.route("/api/index/search")
    def search_index():
        """Semantic search against the file index."""
        query = request.args.get("q", "").strip()
        k = int(request.args.get("k", 10))
        if not query:
            return jsonify({"success": False, "error": "Missing q parameter"})
        try:
            from fda.file_indexer import FileIndexer
            indexer = FileIndexer(state)
            results = indexer.search(query, k=k)
            return jsonify({"success": True, "query": query, "results": results})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

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

    # ============================================
    # Calendar (Outlook) API
    # ============================================

    @app.route("/api/calendar/login", methods=["POST"])
    def calendar_login():
        """Initiate Outlook calendar device code login."""
        try:
            from fda.outlook import OutlookCalendar
        except ImportError:
            return jsonify({"success": False, "error": "msal package not installed. Run: pip install msal"})

        try:
            cal = OutlookCalendar()

            # Check if already logged in
            if cal.is_logged_in():
                app_msal = cal._get_msal_app()
                accounts = app_msal.get_accounts()
                account = accounts[0].get("username", "") if accounts else ""
                return jsonify({"success": True, "already_logged_in": True, "account": account})

            # Check if login already in progress and code is still valid
            import time as _time
            with _outlook_lock:
                if _outlook_login["status"] == "pending":
                    if _outlook_login["expires_at"] > _time.time():
                        return jsonify({
                            "success": True,
                            "user_code": _outlook_login["user_code"],
                            "verification_uri": _outlook_login["verification_uri"],
                        })
                    else:
                        # Code expired — reset so a fresh one is generated below
                        _outlook_login["status"] = "idle"
                        _outlook_login["user_code"] = None
                        _outlook_login["verification_uri"] = None
                        _outlook_login["expires_at"] = 0

            # Start device flow
            app_msal = cal._get_msal_app()
            flow = app_msal.initiate_device_flow(scopes=cal.SCOPES)
            if "user_code" not in flow:
                return jsonify({"success": False, "error": "Failed to create device flow"})

            with _outlook_lock:
                _outlook_login["flow_id"] += 1
                current_flow_id = _outlook_login["flow_id"]
                _outlook_login["status"] = "pending"
                _outlook_login["user_code"] = flow["user_code"]
                _outlook_login["verification_uri"] = flow["verification_uri"]
                _outlook_login["expires_at"] = flow.get("expires_at", _time.time() + flow.get("expires_in", 900))
                _outlook_login["account"] = None
                _outlook_login["error"] = None

            # Run token acquisition in background thread
            def _acquire(fid=current_flow_id):
                try:
                    result = app_msal.acquire_token_by_device_flow(flow)
                    with _outlook_lock:
                        # Only update state if this is still the active flow
                        if _outlook_login["flow_id"] != fid:
                            return
                        if "access_token" in result:
                            cal._set_token(result)
                            cal._save_token_cache()
                            accounts = app_msal.get_accounts()
                            acct = accounts[0].get("username", "") if accounts else ""
                            _outlook_login["status"] = "completed"
                            _outlook_login["account"] = acct
                        else:
                            _outlook_login["status"] = "failed"
                            _outlook_login["error"] = result.get("error_description", "Login failed")
                except Exception as e:
                    with _outlook_lock:
                        if _outlook_login["flow_id"] != fid:
                            return
                        _outlook_login["status"] = "failed"
                        _outlook_login["error"] = str(e)

            t = threading.Thread(target=_acquire, daemon=True)
            t.start()
            with _outlook_lock:
                _outlook_login["thread"] = t

            return jsonify({
                "success": True,
                "user_code": flow["user_code"],
                "verification_uri": flow["verification_uri"],
            })

        except Exception as e:
            logger.exception(f"Calendar login error: {e}")
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/calendar/login/status")
    def calendar_login_status():
        """Poll for device code login completion."""
        import time as _time
        with _outlook_lock:
            status = _outlook_login["status"]
            # Detect expired code while still pending
            if status == "pending" and _outlook_login["expires_at"] <= _time.time():
                status = "expired"
            return jsonify({
                "status": status,
                "account": _outlook_login["account"],
                "error": _outlook_login["error"],
            })

    @app.route("/api/calendar/login/reset", methods=["POST"])
    def calendar_login_reset():
        """Reset device code login state so a new code can be generated."""
        with _outlook_lock:
            _outlook_login["status"] = "idle"
            _outlook_login["user_code"] = None
            _outlook_login["verification_uri"] = None
            _outlook_login["account"] = None
            _outlook_login["error"] = None
            _outlook_login["expires_at"] = 0
        return jsonify({"success": True})

    @app.route("/api/calendar/logout", methods=["POST"])
    def calendar_logout():
        """Log out of Outlook calendar."""
        try:
            from fda.outlook import OutlookCalendar
            cal = OutlookCalendar()
            cal.logout()
            with _outlook_lock:
                _outlook_login["status"] = "idle"
                _outlook_login["account"] = None
            return jsonify({"success": True, "message": "Signed out of Office 365"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

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

        # Check Claude backend (CLI or API)
        from fda.claude_backend import ClaudeCodeCLIBackend
        health["anthropic"] = ClaudeCodeCLIBackend.is_available() or bool(
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

    # ============================================
    # Agent Tasks API
    # ============================================

    @app.route("/api/agents/tasks")
    def get_agent_tasks():
        """Get tasks grouped by agent."""
        try:
            all_tasks = state.get_tasks()

            # Group tasks by assigned agent
            tasks_by_agent = {
                "fda": [],
                "worker": [],
                "worker_local": [],
            }

            for task in all_tasks:
                agent = (task.get("assigned_to") or "fda").lower()
                # Map legacy agent names to current architecture
                if agent in ("executor", "librarian"):
                    agent = "worker"
                if agent in tasks_by_agent:
                    tasks_by_agent[agent].append(task)
                else:
                    tasks_by_agent["fda"].append(task)

            return jsonify(tasks_by_agent)
        except Exception as e:
            logger.exception(f"Error getting agent tasks: {e}")
            return jsonify({"fda": [], "worker": [], "worker_local": [], "error": str(e)})

    # ============================================
    # Chat API
    # ============================================

    @app.route("/api/agents/chat", methods=["POST"])
    def agent_chat():
        """Send a message to an agent and get a response."""
        try:
            data = request.get_json()
            agent_name = data.get("agent", "fda")
            message = data.get("message", "").strip()

            if not message:
                return jsonify({"success": False, "error": "Message is required"})

            # Record query for golden queries cache
            # Skip utility commands like /help, /ls
            if not message.startswith(("/help", "/ls")):
                try:
                    state.record_query(message, agent=agent_name)
                except Exception:
                    pass  # Don't fail the request if caching fails

            # Route to appropriate agent
            if agent_name == "fda":
                try:
                    from fda.fda_agent import FDAAgent
                    agent = FDAAgent()
                    response = agent.ask(message)
                    return jsonify({"success": True, "response": response})
                except Exception as e:
                    logger.exception(f"FDA Agent error: {e}")
                    return jsonify({"success": False, "error": str(e)})

            elif agent_name == "worker":
                try:
                    from fda.claude_backend import get_claude_backend
                    backend = get_claude_backend()
                    response = backend.complete(
                        system="You are the Worker Agent for Datacore's FDA system. You analyze codebases on remote VMs via SSH, identify relevant files, generate fixes, and prepare diffs for approval. Be concise and technical.",
                        messages=[{"role": "user", "content": message}],
                        model="claude-3-5-haiku-20241022",
                        max_tokens=1024,
                    )
                    return jsonify({"success": True, "response": response})
                except Exception as e:
                    return jsonify({"success": False, "error": str(e)})

            elif agent_name == "worker_local":
                try:
                    from fda.local_worker_agent import LocalWorkerAgent

                    stripped = message.strip()

                    # /help — show available commands
                    if stripped.lower() in ("/help", "help"):
                        return jsonify({"success": True, "response": (
                            "Local Worker commands:\n\n"
                            "/organize <path> [instructions]\n"
                            "  Sort files into a clean folder structure\n"
                            "  e.g. /organize ~/Downloads\n"
                            "  e.g. /organize ~/Desktop sort by file type\n\n"
                            "/analyze <path> <task>\n"
                            "  Explore a codebase and analyze/fix code\n"
                            "  e.g. /analyze ~/Projects/myapp find unused imports\n\n"
                            "/ls <path>\n"
                            "  Quick directory listing\n"
                            "  e.g. /ls ~/Documents\n\n"
                            "Or just type a question — the agent will try to help."
                        )})

                    # /organize <path> [instructions]
                    if stripped.startswith("/organize"):
                        args = stripped[len("/organize"):].strip()
                        if not args:
                            return jsonify({"success": True, "response": (
                                "Usage: /organize <path> [instructions]\n\n"
                                "Examples:\n"
                                "  /organize ~/Downloads\n"
                                "  /organize ~/Desktop sort by file type\n"
                                "  /organize ~/Documents/projects group related files"
                            )})
                        parts = args.split(None, 1)
                        target = str(Path(parts[0]).expanduser().resolve())
                        instructions = parts[1] if len(parts) > 1 else ""

                        worker = LocalWorkerAgent(projects=[target])
                        result = worker.organize_files(
                            target_path=target,
                            instructions=instructions,
                        )
                        if result.get("success"):
                            moves = result.get("moves", [])
                            summary = result.get("summary", "Done.")
                            response = summary
                            if moves:
                                response += f"\n\nMoved {len(moves)} files."
                        else:
                            response = f"Error: {result.get('error', 'Unknown error')}"
                        return jsonify({"success": True, "response": response})

                    # /analyze <path> <task>
                    if stripped.startswith("/analyze"):
                        args = stripped[len("/analyze"):].strip()
                        if not args:
                            return jsonify({"success": True, "response": (
                                "Usage: /analyze <path> <task>\n\n"
                                "Examples:\n"
                                "  /analyze ~/Projects/myapp find unused imports\n"
                                "  /analyze ~/Documents/agenthub/fda-system explain the agent pipeline"
                            )})
                        parts = args.split(None, 1)
                        project_path = str(Path(parts[0]).expanduser().resolve())
                        task = parts[1] if len(parts) > 1 else "Analyze this project"

                        worker = LocalWorkerAgent(projects=[project_path])
                        result = worker.analyze_and_fix(
                            project_path=project_path,
                            task_brief=task,
                        )
                        if result.get("success"):
                            response = result.get("analysis", result.get("explanation", "Done."))
                        else:
                            response = f"Error: {result.get('error', 'Unknown error')}"
                        return jsonify({"success": True, "response": response})

                    # /ls <path> — quick listing
                    if stripped.startswith("/ls"):
                        args = stripped[len("/ls"):].strip()
                        target = Path(args or "~").expanduser().resolve()
                        if not target.is_dir():
                            return jsonify({"success": True, "response": f"Not a directory: {target}"})
                        try:
                            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                            lines = []
                            for e in entries[:50]:
                                suffix = "/" if e.is_dir() else ""
                                lines.append(f"  {e.name}{suffix}")
                            response = f"{target}/\n" + "\n".join(lines)
                            if len(list(target.iterdir())) > 50:
                                response += f"\n  ... and {len(list(target.iterdir())) - 50} more"
                        except PermissionError:
                            response = f"Permission denied: {target}"
                        return jsonify({"success": True, "response": response})

                    # Plain text — use Claude CLI for general Q&A
                    from fda.claude_backend import get_claude_backend
                    backend = get_claude_backend()
                    response = backend.complete(
                        system=(
                            "You are the Local Worker Agent for Datacore's FDA system. "
                            "You work with local files on this machine.\n\n"
                            "If the user wants to organize files or analyze code, "
                            "tell them to use these commands:\n"
                            "  /organize <path> [instructions] — sort files\n"
                            "  /analyze <path> <task> — explore/fix code\n"
                            "  /ls <path> — list a directory\n"
                            "  /help — show all commands"
                        ),
                        messages=[{"role": "user", "content": message}],
                        model="claude-haiku-4-5-20251001",
                        max_tokens=1024,
                    )
                    return jsonify({"success": True, "response": response})
                except Exception as e:
                    logger.exception(f"Local Worker error: {e}")
                    return jsonify({"success": False, "error": str(e)})

            else:
                return jsonify({"success": False, "error": f"Unknown agent: {agent_name}"})

        except Exception as e:
            logger.exception(f"Chat error: {e}")
            return jsonify({"success": False, "error": str(e)})

    # ============================================
    # Golden Queries API
    # ============================================

    @app.route("/api/queries")
    def get_queries():
        """Get golden queries (frequent + recent)."""
        agent = request.args.get("agent")
        limit = int(request.args.get("limit", 10))
        try:
            frequent = state.get_golden_queries(limit=limit, agent=agent)
            recent = state.get_recent_queries(limit=5, agent=agent)
            return jsonify({
                "success": True,
                "frequent": frequent,
                "recent": recent,
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/queries/pin", methods=["POST"])
    def pin_query():
        """Pin or unpin a golden query."""
        data = request.get_json()
        query_id = data.get("id")
        pinned = data.get("pinned", True)
        if not query_id:
            return jsonify({"success": False, "error": "id is required"})
        try:
            state.pin_query(int(query_id), pinned=pinned)
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/queries/delete", methods=["POST"])
    def delete_query():
        """Delete a golden query."""
        data = request.get_json()
        query_id = data.get("id")
        if not query_id:
            return jsonify({"success": False, "error": "id is required"})
        try:
            state.delete_query(int(query_id))
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    # ============================================
    # File Serving API
    # ============================================

    @app.route("/api/files/view")
    def view_file():
        """Serve a local file for preview. Only serves files under the user's home directory."""
        import mimetypes
        file_path = request.args.get("path", "")
        if not file_path:
            return "Missing path parameter", 400

        home = os.path.expanduser("~")
        resolved = os.path.realpath(file_path)

        # Security: only serve files under the user's home directory
        if not resolved.startswith(home + "/"):
            return "Access denied", 403
        if not os.path.isfile(resolved):
            return "File not found", 404

        mime_type, _ = mimetypes.guess_type(resolved)
        mime_type = mime_type or "application/octet-stream"

        # For HTML files, serve inline so they render in the browser
        if mime_type == "text/html":
            return send_file(resolved, mimetype=mime_type)
        # For PDFs, images — serve inline
        if mime_type.startswith(("application/pdf", "image/")):
            return send_file(resolved, mimetype=mime_type)
        # For text files, serve as plain text
        if mime_type.startswith("text/"):
            return send_file(resolved, mimetype=mime_type)
        # Everything else — trigger download
        return send_file(resolved, as_attachment=True)

    # ============================================
    # Journal API
    # ============================================

    # Cache for summarized journal entries
    _summary_cache: dict[str, str] = {}

    def _parse_journal_file(entry_file: Path) -> dict | None:
        """Parse a journal markdown file into a dict."""
        try:
            content = entry_file.read_text()
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    try:
                        frontmatter = yaml.safe_load(parts[1])
                        body = parts[2].strip()
                    except Exception:
                        frontmatter = {}
                        body = content
                else:
                    frontmatter = {}
                    body = content
            else:
                frontmatter = {}
                body = content

            tags = frontmatter.get("tags", [])
            is_chat = "kakaotalk" in tags or "client-chat" in tags

            return {
                "id": entry_file.stem,
                "summary": frontmatter.get("title") or frontmatter.get("summary") or entry_file.stem,
                "author": frontmatter.get("author", "Unknown"),
                "tags": tags,
                "timestamp": frontmatter.get("created_at") or frontmatter.get("timestamp", ""),
                "is_chat": is_chat,
                "content": body,
            }
        except Exception as e:
            logger.warning(f"Error reading journal file {entry_file}: {e}")
            return None

    def _summarize_chat(entry_id: str, body: str) -> str:
        """Summarize a KakaoTalk chat body using Claude. Returns cached result if available."""
        if entry_id in _summary_cache:
            return _summary_cache[entry_id]

        try:
            from fda.claude_backend import get_claude_backend
            backend = get_claude_backend()
            summary = backend.complete(
                system="You summarize KakaoTalk client chat logs for a software consultancy. Extract key action items, requests, issues, and decisions. Write in bullet points. Be concise. If the chat is in Korean, write the summary in Korean.",
                messages=[{"role": "user", "content": f"Summarize this client chat:\n\n{body[:4000]}"}],
                model="claude-3-5-haiku-20241022",
                max_tokens=512,
            )
            _summary_cache[entry_id] = summary
            return summary
        except Exception as e:
            logger.warning(f"Failed to summarize {entry_id}: {e}")
            return _extract_preview(body)

    def _extract_preview(body: str) -> str:
        """Extract a simple preview from chat body without AI."""
        lines = body.strip().split("\n")
        # Skip headers, grab first few message lines
        msg_lines = [l.strip() for l in lines if l.strip() and not l.startswith("#") and not l.startswith("Chat room:") and not l.startswith("Messages:")]
        return "\n".join(msg_lines[:8]) + ("\n..." if len(msg_lines) > 8 else "")

    @app.route("/api/journal/entries")
    def get_journal_entries():
        """Get all journal entries with summarized content for chat entries."""
        try:
            from fda.config import JOURNAL_DIR

            journal_path = Path(JOURNAL_DIR)
            entries = []

            if journal_path.exists():
                for entry_file in sorted(journal_path.glob("*.md"), reverse=True):
                    entry = _parse_journal_file(entry_file)
                    if not entry:
                        continue

                    body = entry["content"]
                    if entry["is_chat"]:
                        # For chat entries, show summarized content
                        entry["content"] = _summarize_chat(entry["id"], body)
                        entry["has_raw"] = True
                    else:
                        entry["content"] = body[:2000] + ("..." if len(body) > 2000 else "")
                        entry["has_raw"] = False

                    entries.append(entry)

            return jsonify({"entries": entries[:50]})

        except Exception as e:
            logger.exception(f"Error getting journal entries: {e}")
            return jsonify({"entries": [], "error": str(e)})

    @app.route("/api/journal/entry/<entry_id>/raw")
    def get_journal_entry_raw(entry_id: str):
        """Get raw content for a journal entry (for viewing original chat)."""
        try:
            from fda.config import JOURNAL_DIR
            journal_path = Path(JOURNAL_DIR)
            entry_file = journal_path / f"{entry_id}.md"
            if not entry_file.exists():
                return jsonify({"success": False, "error": "Entry not found"}), 404

            entry = _parse_journal_file(entry_file)
            if not entry:
                return jsonify({"success": False, "error": "Parse error"}), 500

            return jsonify({"success": True, "content": entry["content"][:5000]})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

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
