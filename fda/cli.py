"""
Command-line interface for the FDA system.

Provides subcommands for initialization, starting the system, querying,
and generating reports.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from fda.config import (
    PROJECT_ROOT,
    JOURNAL_DIR,
    STATE_DB_PATH,
    MESSAGE_BUS_PATH,
    DEFAULT_DAILY_CHECKIN_TIME,
    DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES,
    MODEL_FDA,
    MODEL_EXECUTOR,
    MODEL_LIBRARIAN,
)
from fda.state.project_state import ProjectState
from fda.comms.message_bus import MessageBus
from fda.scheduler import Scheduler
from fda.journal.writer import JournalWriter
from fda.journal.retriever import JournalRetriever


def handle_init(args: argparse.Namespace) -> int:
    """Initialize a new FDA project."""
    project_path = Path(args.path).resolve()
    print(f"Initializing FDA project at {project_path}")

    # Create directory structure
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "journal").mkdir(exist_ok=True)
    (project_path / "logs").mkdir(exist_ok=True)

    # Initialize project state
    db_path = project_path / "state.db"
    state = ProjectState(db_path)

    # Set initial context
    state.set_context("project_path", str(project_path))
    state.set_context("initialized_at", __import__("datetime").datetime.now().isoformat())
    state.set_context("version", "0.1.0")

    # Initialize message bus
    bus_path = project_path / "message_bus.json"
    MessageBus(bus_path)

    print(f"  Created state database: {db_path}")
    print(f"  Created message bus: {bus_path}")
    print(f"  Created journal directory: {project_path / 'journal'}")
    print("Project initialized successfully!")

    return 0


def handle_start(args: argparse.Namespace) -> int:
    """Start the FDA system."""
    print("Starting FDA system...")

    # Initialize components
    state = ProjectState()
    scheduler = Scheduler()

    # Register scheduled tasks
    def daily_checkin():
        print(f"[{__import__('datetime').datetime.now()}] Daily checkin triggered")

    def calendar_check():
        print(f"[{__import__('datetime').datetime.now()}] Calendar check triggered")

    scheduler.register_daily_checkin(DEFAULT_DAILY_CHECKIN_TIME, daily_checkin)
    scheduler.register_calendar_watcher(DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES, calendar_check)

    print(f"  Daily checkin scheduled at {DEFAULT_DAILY_CHECKIN_TIME}")
    print(f"  Calendar check every {DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES} minutes")

    if args.daemon:
        print("Running in daemon mode (background)...")
        thread = scheduler.run_in_background()
        print("FDA system started in background. Press Ctrl+C to stop.")
        try:
            thread.join()
        except KeyboardInterrupt:
            print("\nStopping FDA system...")
            scheduler.stop()
    else:
        print("Running in foreground. Press Ctrl+C to stop.")
        try:
            scheduler.run()
        except KeyboardInterrupt:
            print("\nStopping FDA system...")
            scheduler.stop()

    return 0


def handle_onboard(args: argparse.Namespace) -> int:
    """Run interactive onboarding."""
    from fda.fda_agent import FDAAgent

    try:
        agent = FDAAgent()

        # Check if already onboarded
        if agent.is_onboarded() and not args.force:
            print("You've already completed onboarding.")
            user_name = agent.state.get_context("user_name") or "there"
            print(f"Welcome back, {user_name}!")
            print("\nTo redo onboarding, run: fda onboard --force")
            print("To chat with me, run: fda ask \"<your question>\"")
            return 0

        agent.onboard_interactive()
        return 0
    except KeyboardInterrupt:
        print("\n\nOnboarding cancelled. Run 'fda onboard' when you're ready.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def handle_ask(args: argparse.Namespace) -> int:
    """Ask the FDA agent a question."""
    from fda.fda_agent import FDAAgent

    try:
        agent = FDAAgent()

        # Check if onboarded, suggest it if not
        if not agent.is_onboarded():
            print("Hi! I notice we haven't been introduced yet.")
            print("Run 'fda onboard' first so I can learn about you and how to help.")
            print()
            proceed = input("Or would you like to skip that and just ask your question? (y/n) > ").strip().lower()
            if proceed not in ("y", "yes"):
                return 0
            print()

        # Get user name for personalization
        user_name = agent.state.get_context("user_name")

        print(f"Question: {args.question}")
        print()

        response = agent.ask(args.question)
        print(response)
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 1


def handle_status(args: argparse.Namespace) -> int:
    """Show system status."""
    print("FDA System Status")
    print("=" * 50)

    # Project state
    state = ProjectState()

    # Tasks summary
    all_tasks = state.get_tasks()
    pending = [t for t in all_tasks if t.get("status") == "pending"]
    in_progress = [t for t in all_tasks if t.get("status") == "in_progress"]
    completed = [t for t in all_tasks if t.get("status") == "completed"]
    blocked = [t for t in all_tasks if t.get("status") == "blocked"]

    print("\nTasks:")
    print(f"  Total: {len(all_tasks)}")
    print(f"  Pending: {len(pending)}")
    print(f"  In Progress: {len(in_progress)}")
    print(f"  Completed: {len(completed)}")
    print(f"  Blocked: {len(blocked)}")

    # Alerts
    alerts = state.get_alerts(acknowledged=False)
    critical_alerts = [a for a in alerts if a.get("level") == "critical"]
    warning_alerts = [a for a in alerts if a.get("level") == "warning"]

    print("\nAlerts:")
    print(f"  Unacknowledged: {len(alerts)}")
    print(f"  Critical: {len(critical_alerts)}")
    print(f"  Warnings: {len(warning_alerts)}")

    # Message bus
    try:
        bus = MessageBus()
        fda_pending = len(bus.get_pending("fda"))
        executor_pending = len(bus.get_pending("executor"))
        librarian_pending = len(bus.get_pending("librarian"))

        print("\nMessage Bus:")
        print(f"  FDA pending: {fda_pending}")
        print(f"  Executor pending: {executor_pending}")
        print(f"  Librarian pending: {librarian_pending}")
    except Exception:
        print("\nMessage Bus: Not initialized")

    # Journal
    retriever = JournalRetriever()
    recent_entries = retriever.index.get_recent(limit=5)

    print("\nJournal:")
    print(f"  Total entries: {len(retriever.index.entries)}")
    if recent_entries:
        print("  Recent entries:")
        for entry in recent_entries[:3]:
            print(f"    - {entry.get('summary', 'Untitled')}")

    return 0


def handle_meeting_prep(args: argparse.Namespace) -> int:
    """Prepare for an upcoming meeting."""
    event_id = args.id
    print(f"Preparing meeting brief for event: {event_id}")
    print()

    # Check for existing prep
    state = ProjectState()
    existing_prep = state.get_meeting_prep(event_id)

    if existing_prep:
        print("Existing preparation found:")
        print(f"  Created by: {existing_prep.get('created_by')}")
        print(f"  Created at: {existing_prep.get('created_at')}")
        print()
        print("Brief:")
        print(existing_prep.get("brief", "No brief available"))
    else:
        print("No existing preparation found.")
        print("Note: Full meeting prep generation requires Outlook calendar integration (Week 3).")

        # Create a placeholder prep
        brief = f"""Meeting Preparation for {event_id}
========================================

Status: Pending calendar integration

To complete preparation:
1. Configure Outlook calendar access
2. Run meeting-prep again with calendar enabled

Context from project state:
- Tasks in progress: {len(state.get_tasks(status="in_progress"))}
- Unacknowledged alerts: {len(state.get_alerts(acknowledged=False))}
"""
        prep_id = state.record_meeting_prep(event_id, brief, "cli")
        print(f"Created placeholder preparation: {prep_id}")

    return 0


def handle_report(args: argparse.Namespace) -> int:
    """Generate a report."""
    report_type = args.type
    print(f"Generating {report_type} report...")
    print()

    state = ProjectState()
    retriever = JournalRetriever()

    # Gather data
    tasks = state.get_tasks()
    alerts = state.get_alerts()
    decisions = state.get_decisions(limit=10)

    print(f"FDA System {report_type.title()} Report")
    print("=" * 50)

    # Tasks section
    print("\n## Tasks Summary")
    status_counts = {}
    for task in tasks:
        status = task.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    # High priority tasks
    high_priority = [t for t in tasks if t.get("priority") == "high"]
    if high_priority:
        print("\n## High Priority Tasks")
        for task in high_priority[:5]:
            print(f"  - [{task.get('status')}] {task.get('title')}")

    # Alerts section
    unacked_alerts = [a for a in alerts if not a.get("acknowledged")]
    if unacked_alerts:
        print("\n## Active Alerts")
        for alert in unacked_alerts[:5]:
            print(f"  [{alert.get('level')}] {alert.get('message')}")

    # Recent decisions
    if decisions:
        print("\n## Recent Decisions")
        for decision in decisions[:3]:
            print(f"  - {decision.get('title')}")
            print(f"    Rationale: {decision.get('rationale', '')[:100]}...")

    # Journal entries
    recent_entries = retriever.index.get_recent(limit=5)
    if recent_entries:
        print("\n## Recent Journal Entries")
        for entry in recent_entries:
            print(f"  - {entry.get('summary')} ({entry.get('author')})")

    print("\n" + "=" * 50)
    print("Report generated successfully.")

    return 0


def handle_journal_search(args: argparse.Namespace) -> int:
    """Search the journal."""
    query = args.query
    print(f"Searching journal for: '{query}'")
    print()

    retriever = JournalRetriever()
    results = retriever.retrieve(query_text=query, top_n=10)

    if not results:
        print("No matching entries found.")
        return 0

    print(f"Found {len(results)} matching entries:")
    print()

    for i, entry in enumerate(results, 1):
        print(f"{i}. {entry.get('summary', 'Untitled')}")
        print(f"   Author: {entry.get('author')} | Tags: {', '.join(entry.get('tags', []))}")
        print(f"   Created: {entry.get('created_at', 'Unknown')[:10]}")
        print(f"   Score: {entry.get('combined_score', 0):.3f} "
              f"(relevance: {entry.get('relevance_score', 0):.3f}, "
              f"recency: {entry.get('recency_score', 0):.3f})")
        print()

    return 0


def handle_journal_write(args: argparse.Namespace) -> int:
    """Write a new journal entry."""
    writer = JournalWriter()

    # Parse tags
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    filepath = writer.write_entry(
        author=args.author,
        tags=tags,
        summary=args.summary,
        content=args.content,
        relevance_decay=args.decay,
    )

    print(f"Journal entry created: {filepath}")
    return 0


def handle_config(args: argparse.Namespace) -> int:
    """Show or update configuration."""
    print("FDA System Configuration")
    print("=" * 50)

    print("\nPaths:")
    print(f"  Project Root: {PROJECT_ROOT}")
    print(f"  Journal Directory: {JOURNAL_DIR}")
    print(f"  State Database: {STATE_DB_PATH}")
    print(f"  Message Bus: {MESSAGE_BUS_PATH}")

    print("\nModels:")
    print(f"  FDA Agent: {MODEL_FDA}")
    print(f"  Executor Agent: {MODEL_EXECUTOR}")
    print(f"  Librarian Agent: {MODEL_LIBRARIAN}")

    print("\nScheduling:")
    print(f"  Daily Checkin: {DEFAULT_DAILY_CHECKIN_TIME}")
    print(f"  Calendar Check Interval: {DEFAULT_CALENDAR_CHECK_INTERVAL_MINUTES} min")

    # Show project context if available
    try:
        state = ProjectState()
        project_path = state.get_context("project_path")
        initialized_at = state.get_context("initialized_at")

        if project_path:
            print("\nProject Context:")
            print(f"  Project Path: {project_path}")
            print(f"  Initialized: {initialized_at}")
    except Exception:
        print("\nProject Context: Not initialized")

    return 0


def handle_task_add(args: argparse.Namespace) -> int:
    """Add a new task."""
    state = ProjectState()
    task_id = state.add_task(
        title=args.title,
        description=args.description or "",
        owner=args.owner,
        priority=args.priority,
        due_date=args.due,
    )
    print(f"Task created: {task_id}")
    print(f"  Title: {args.title}")
    print(f"  Owner: {args.owner}")
    print(f"  Priority: {args.priority}")
    return 0


def handle_task_list(args: argparse.Namespace) -> int:
    """List tasks."""
    state = ProjectState()
    tasks = state.get_tasks(status=args.status)

    if not tasks:
        print("No tasks found.")
        return 0

    print(f"Tasks ({len(tasks)} total):")
    print()

    for task in tasks:
        status_icon = {
            "pending": "○",
            "in_progress": "◐",
            "completed": "●",
            "blocked": "✗",
        }.get(task.get("status", ""), "?")

        priority_icon = {
            "high": "!!!",
            "medium": "!!",
            "low": "!",
        }.get(task.get("priority", ""), "")

        print(f"  {status_icon} [{task.get('id')}] {task.get('title')} {priority_icon}")
        print(f"    Owner: {task.get('owner')} | Status: {task.get('status')}")
        if task.get("due_date"):
            print(f"    Due: {task.get('due_date')}")
        print()

    return 0


def handle_task_update(args: argparse.Namespace) -> int:
    """Update a task."""
    state = ProjectState()

    updates = {}
    if args.status:
        updates["status"] = args.status
    if args.owner:
        updates["owner"] = args.owner
    if args.priority:
        updates["priority"] = args.priority

    if not updates:
        print("No updates specified. Use --status, --owner, or --priority.")
        return 1

    state.update_task(args.id, **updates)
    print(f"Task {args.id} updated:")
    for key, value in updates.items():
        print(f"  {key}: {value}")

    return 0


def handle_discord_setup(args: argparse.Namespace) -> int:
    """Set up Discord bot token and client ID."""
    from fda.discord_bot import setup_bot_token, setup_client_id, get_bot_token

    existing = get_bot_token()
    if existing and not args.force:
        print("Discord bot already configured.")
        print("Use --force to reconfigure.")
        return 0

    print("Discord Bot Setup")
    print("=" * 40)
    print("\nTo create a Discord bot:")
    print("1. Go to https://discord.com/developers/applications")
    print("2. Click 'New Application' and name it")
    print("3. Go to 'Bot' tab and click 'Add Bot'")
    print("4. Copy the bot token")
    print("5. Go to 'OAuth2' > 'General' and copy the Client ID\n")

    token = input("Enter your bot token: ").strip()
    if not token:
        print("No token provided. Aborting.")
        return 1

    client_id = input("Enter your client ID: ").strip()
    if not client_id:
        print("No client ID provided. Aborting.")
        return 1

    setup_bot_token(token)
    setup_client_id(client_id)

    print("\n✓ Discord bot configured!")
    print("Run 'fda discord invite' to get the invite link.")
    return 0


def handle_discord_status(args: argparse.Namespace) -> int:
    """Check Discord bot status."""
    from fda.discord_bot import get_bot_token, get_client_id

    token = get_bot_token()
    client_id = get_client_id()

    if token:
        masked = token[:8] + "..." + token[-4:]
        print(f"✓ Discord bot configured")
        print(f"  Token: {masked}")
        if client_id:
            print(f"  Client ID: {client_id}")

        # Show recent sessions
        state = ProjectState()
        sessions = state.get_discord_sessions(limit=5)
        if sessions:
            print(f"\n  Recent sessions:")
            for session in sessions:
                status = "🔊" if session.get("status") == "active" else "✓"
                print(f"    {status} {session.get('channel_name', 'Unknown')} - {session.get('started_at', '')[:10]}")
    else:
        print("✗ Discord bot not configured")
        print("  Run: fda discord setup")

    return 0


def handle_discord_start(args: argparse.Namespace) -> int:
    """Start the Discord bot."""
    from fda.discord_bot import DiscordVoiceAgent, get_bot_token

    token = get_bot_token()
    if not token:
        print("Discord bot not configured. Run: fda discord setup")
        return 1

    print("Starting Discord bot...")
    print("Press Ctrl+C to stop.\n")

    try:
        bot = DiscordVoiceAgent(bot_token=token)
        bot.start()
    except KeyboardInterrupt:
        print("\nStopping bot...")
    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


def handle_discord_invite(args: argparse.Namespace) -> int:
    """Generate Discord bot invite link."""
    from fda.discord_bot import get_client_id

    client_id = get_client_id()
    if not client_id:
        print("Client ID not configured. Run: fda discord setup")
        return 1

    permissions = 3148800  # Connect, Speak, Send Messages, Read History
    url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&permissions={permissions}&scope=bot"

    print("Discord Bot Invite Link")
    print("=" * 40)
    print(f"\n{url}\n")
    print("Open this link in your browser to add the bot to your server.")
    return 0


def handle_telegram_setup(args: argparse.Namespace) -> int:
    """Set up Telegram bot token."""
    from fda.telegram_bot import setup_bot_token, get_bot_token

    # Check if already configured
    existing = get_bot_token()
    if existing and not args.force:
        print("Telegram bot already configured.")
        print("Use --force to reconfigure.")
        return 0

    print("Telegram Bot Setup")
    print("=" * 40)
    print("\nTo create a Telegram bot:")
    print("1. Open Telegram and search for @BotFather")
    print("2. Send /newbot and follow the prompts")
    print("3. Copy the bot token provided\n")

    token = input("Enter your bot token: ").strip()

    if not token:
        print("No token provided. Aborting.")
        return 1

    # Validate token format (basic check)
    if ":" not in token or len(token) < 30:
        print("Invalid token format. Please check and try again.")
        return 1

    setup_bot_token(token)
    print("\n✓ Telegram bot token saved!")
    print("Run 'fda telegram start' to start the bot.")
    return 0


def handle_telegram_status(args: argparse.Namespace) -> int:
    """Check Telegram bot status."""
    from fda.telegram_bot import get_bot_token

    token = get_bot_token()

    if token:
        # Mask the token for display
        masked = token[:8] + "..." + token[-4:]
        print(f"✓ Telegram bot configured")
        print(f"  Token: {masked}")

        # Show registered users
        state = ProjectState()
        users = state.get_telegram_users(active_only=True)
        print(f"  Active users: {len(users)}")

        for user in users[:5]:
            name = user.get("first_name") or user.get("username") or user.get("chat_id")
            print(f"    - {name}")
        if len(users) > 5:
            print(f"    ... and {len(users) - 5} more")
    else:
        print("✗ Telegram bot not configured")
        print("  Run: fda telegram setup")

    return 0


def handle_telegram_start(args: argparse.Namespace) -> int:
    """Start the Telegram bot."""
    from fda.telegram_bot import TelegramBotAgent, get_bot_token

    token = get_bot_token()
    if not token:
        print("Telegram bot not configured. Run: fda telegram setup")
        return 1

    print("Starting Telegram bot...")
    print("Press Ctrl+C to stop.\n")

    try:
        bot = TelegramBotAgent(bot_token=token)
        bot.start()
    except KeyboardInterrupt:
        print("\nStopping bot...")
    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


def handle_telegram_test(args: argparse.Namespace) -> int:
    """Send a test message to all registered users."""
    import asyncio
    from fda.telegram_bot import TelegramBotAgent, get_bot_token

    token = get_bot_token()
    if not token:
        print("Telegram bot not configured. Run: fda telegram setup")
        return 1

    state = ProjectState()
    users = state.get_telegram_users(active_only=True)

    if not users:
        print("No registered users. Have users send /start to your bot first.")
        return 1

    print(f"Sending test message to {len(users)} user(s)...")

    try:
        bot = TelegramBotAgent(bot_token=token)
        count = asyncio.run(bot.broadcast_message(
            "🧪 *Test Message*\n\nThis is a test from FDA Project Assistant."
        ))
        print(f"✓ Message sent to {count} user(s)")
    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


def handle_calendar_login(args: argparse.Namespace) -> int:
    """Log in to Office 365 calendar."""
    try:
        from fda.outlook import OutlookCalendar
    except ImportError as e:
        print(f"Error: {e}")
        print("Install required packages: pip install msal requests")
        return 1

    calendar = OutlookCalendar()

    if calendar.is_logged_in() and not args.force:
        print("Already logged in to Office 365 calendar.")
        print("Use --force to log in with a different account.")
        return 0

    success = calendar.authenticate(force_new=args.force)
    return 0 if success else 1


def handle_calendar_logout(args: argparse.Namespace) -> int:
    """Log out of Office 365 calendar."""
    try:
        from fda.outlook import OutlookCalendar
    except ImportError as e:
        print(f"Error: {e}")
        return 1

    calendar = OutlookCalendar()
    calendar.logout()
    return 0


def handle_calendar_status(args: argparse.Namespace) -> int:
    """Check calendar connection status."""
    try:
        from fda.outlook import OutlookCalendar
    except ImportError as e:
        print(f"Error: {e}")
        print("Install required packages: pip install msal requests")
        return 1

    calendar = OutlookCalendar()

    if calendar.is_logged_in():
        print("✓ Connected to Office 365 calendar")
        # Try to get user info
        try:
            app = calendar._get_msal_app()
            accounts = app.get_accounts()
            if accounts:
                print(f"  Account: {accounts[0].get('username', 'Unknown')}")
        except Exception:
            pass
    else:
        print("✗ Not connected to Office 365 calendar")
        print("  Run: fda calendar login")

    return 0


def handle_calendar_today(args: argparse.Namespace) -> int:
    """Show today's calendar events."""
    try:
        from fda.outlook import OutlookCalendar
    except ImportError as e:
        print(f"Error: {e}")
        return 1

    calendar = OutlookCalendar()

    if not calendar.is_logged_in():
        print("Not logged in. Run: fda calendar login")
        return 1

    if not calendar.authenticate():
        return 1

    events = calendar.get_events_today()

    if not events:
        print("No events scheduled for today.")
        return 0

    print(f"Today's Events ({len(events)} total):")
    print()

    for event in events:
        start = event.get("start", "")[:16].replace("T", " ")
        end = event.get("end", "")[:16].replace("T", " ")
        print(f"  {start} - {end[-5:]}")
        print(f"    {event.get('subject', 'No Subject')}")
        if event.get("location"):
            print(f"    📍 {event.get('location')}")
        if event.get("is_online"):
            print(f"    🔗 Online meeting")
        print()

    return 0


def handle_calendar_upcoming(args: argparse.Namespace) -> int:
    """Show upcoming calendar events."""
    try:
        from fda.outlook import OutlookCalendar
    except ImportError as e:
        print(f"Error: {e}")
        return 1

    calendar = OutlookCalendar()

    if not calendar.is_logged_in():
        print("Not logged in. Run: fda calendar login")
        return 1

    if not calendar.authenticate():
        return 1

    events = calendar.get_upcoming_events(within_minutes=args.minutes)

    if not events:
        print(f"No events in the next {args.minutes} minutes.")
        return 0

    print(f"Upcoming Events (next {args.minutes} min):")
    print()

    for event in events:
        start = event.get("start", "")[:16].replace("T", " ")
        print(f"  {start}")
        print(f"    {event.get('subject', 'No Subject')}")
        if event.get("location"):
            print(f"    📍 {event.get('location')}")
        print()

    return 0


def handle_setup_server(args: argparse.Namespace) -> int:
    """Start the web-based setup server."""
    try:
        from fda.setup_server import run_setup_server
    except ImportError as e:
        print(f"Error: {e}")
        print("Install Flask with: pip install flask")
        return 1

    run_setup_server(
        host=args.host,
        port=args.port,
        debug=args.debug,
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """
    Main entry point for the FDA CLI.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parser = argparse.ArgumentParser(
        prog="fda",
        description="FDA Multi-Agent System for Project Coordination",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # init command
    init_parser = subparsers.add_parser("init", help="Initialize a new FDA project")
    init_parser.add_argument(
        "path",
        type=Path,
        help="Path to initialize the project at",
    )
    init_parser.set_defaults(func=handle_init)

    # start command
    start_parser = subparsers.add_parser("start", help="Start the FDA system")
    start_parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in daemon mode",
    )
    start_parser.set_defaults(func=handle_start)

    # onboard command - interactive setup
    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Interactive setup - FDA learns about you and your goals",
    )
    onboard_parser.add_argument(
        "--force",
        action="store_true",
        help="Redo onboarding even if already completed",
    )
    onboard_parser.set_defaults(func=handle_onboard)

    # ask command
    ask_parser = subparsers.add_parser("ask", help="Ask the FDA agent a question")
    ask_parser.add_argument(
        "question",
        help="Question to ask the FDA agent",
    )
    ask_parser.set_defaults(func=handle_ask)

    # status command
    status_parser = subparsers.add_parser("status", help="Show system status")
    status_parser.set_defaults(func=handle_status)

    # meeting-prep command
    meeting_parser = subparsers.add_parser(
        "meeting-prep",
        help="Prepare for an upcoming meeting",
    )
    meeting_parser.add_argument(
        "--id",
        required=True,
        help="Event ID to prepare for",
    )
    meeting_parser.set_defaults(func=handle_meeting_prep)

    # report command
    report_parser = subparsers.add_parser("report", help="Generate a report")
    report_parser.add_argument(
        "type",
        choices=["daily", "weekly", "monthly", "project"],
        help="Type of report to generate",
    )
    report_parser.set_defaults(func=handle_report)

    # journal command
    journal_parser = subparsers.add_parser("journal", help="Journal operations")
    journal_subparsers = journal_parser.add_subparsers(dest="journal_command")

    # journal search
    search_parser = journal_subparsers.add_parser("search", help="Search the journal")
    search_parser.add_argument(
        "query",
        help="Search query",
    )
    search_parser.set_defaults(func=handle_journal_search)

    # journal write
    write_parser = journal_subparsers.add_parser("write", help="Write a journal entry")
    write_parser.add_argument("--author", required=True, help="Author name")
    write_parser.add_argument("--tags", required=True, help="Comma-separated tags")
    write_parser.add_argument("--summary", required=True, help="Entry summary")
    write_parser.add_argument("--content", required=True, help="Entry content")
    write_parser.add_argument(
        "--decay",
        choices=["fast", "medium", "slow"],
        default="medium",
        help="Relevance decay rate",
    )
    write_parser.set_defaults(func=handle_journal_write)

    # task command
    task_parser = subparsers.add_parser("task", help="Task management")
    task_subparsers = task_parser.add_subparsers(dest="task_command")

    # task add
    task_add_parser = task_subparsers.add_parser("add", help="Add a new task")
    task_add_parser.add_argument("title", help="Task title")
    task_add_parser.add_argument("--description", "-d", help="Task description")
    task_add_parser.add_argument("--owner", "-o", required=True, help="Task owner")
    task_add_parser.add_argument(
        "--priority", "-p",
        choices=["low", "medium", "high"],
        default="medium",
        help="Task priority",
    )
    task_add_parser.add_argument("--due", help="Due date (ISO format)")
    task_add_parser.set_defaults(func=handle_task_add)

    # task list
    task_list_parser = task_subparsers.add_parser("list", help="List tasks")
    task_list_parser.add_argument(
        "--status", "-s",
        choices=["pending", "in_progress", "completed", "blocked"],
        help="Filter by status",
    )
    task_list_parser.set_defaults(func=handle_task_list)

    # task update
    task_update_parser = task_subparsers.add_parser("update", help="Update a task")
    task_update_parser.add_argument("id", help="Task ID")
    task_update_parser.add_argument(
        "--status", "-s",
        choices=["pending", "in_progress", "completed", "blocked"],
        help="New status",
    )
    task_update_parser.add_argument("--owner", "-o", help="New owner")
    task_update_parser.add_argument(
        "--priority", "-p",
        choices=["low", "medium", "high"],
        help="New priority",
    )
    task_update_parser.set_defaults(func=handle_task_update)

    # config command
    config_parser = subparsers.add_parser("config", help="Show or update configuration")
    config_parser.set_defaults(func=handle_config)

    # discord command
    discord_parser = subparsers.add_parser("discord", help="Discord voice bot operations")
    discord_subparsers = discord_parser.add_subparsers(dest="discord_command")

    # discord setup
    dc_setup_parser = discord_subparsers.add_parser("setup", help="Configure Discord bot")
    dc_setup_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing configuration",
    )
    dc_setup_parser.set_defaults(func=handle_discord_setup)

    # discord status
    dc_status_parser = discord_subparsers.add_parser("status", help="Show Discord bot status")
    dc_status_parser.set_defaults(func=handle_discord_status)

    # discord start
    dc_start_parser = discord_subparsers.add_parser("start", help="Start the Discord bot")
    dc_start_parser.set_defaults(func=handle_discord_start)

    # discord invite
    dc_invite_parser = discord_subparsers.add_parser("invite", help="Generate bot invite link")
    dc_invite_parser.set_defaults(func=handle_discord_invite)

    # telegram command
    telegram_parser = subparsers.add_parser("telegram", help="Telegram bot operations")
    telegram_subparsers = telegram_parser.add_subparsers(dest="telegram_command")

    # telegram setup
    tg_setup_parser = telegram_subparsers.add_parser("setup", help="Configure Telegram bot token")
    tg_setup_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing configuration",
    )
    tg_setup_parser.set_defaults(func=handle_telegram_setup)

    # telegram status
    tg_status_parser = telegram_subparsers.add_parser("status", help="Show Telegram bot status")
    tg_status_parser.set_defaults(func=handle_telegram_status)

    # telegram start
    tg_start_parser = telegram_subparsers.add_parser("start", help="Start the Telegram bot")
    tg_start_parser.set_defaults(func=handle_telegram_start)

    # telegram test
    tg_test_parser = telegram_subparsers.add_parser("test", help="Send test message to users")
    tg_test_parser.set_defaults(func=handle_telegram_test)

    # calendar command
    calendar_parser = subparsers.add_parser("calendar", help="Office 365 calendar operations")
    calendar_subparsers = calendar_parser.add_subparsers(dest="calendar_command")

    # calendar login
    cal_login_parser = calendar_subparsers.add_parser("login", help="Log in to Office 365 calendar")
    cal_login_parser.add_argument(
        "--force",
        action="store_true",
        help="Force new login even if already logged in",
    )
    cal_login_parser.set_defaults(func=handle_calendar_login)

    # calendar logout
    cal_logout_parser = calendar_subparsers.add_parser("logout", help="Log out of Office 365 calendar")
    cal_logout_parser.set_defaults(func=handle_calendar_logout)

    # calendar status
    cal_status_parser = calendar_subparsers.add_parser("status", help="Check calendar connection status")
    cal_status_parser.set_defaults(func=handle_calendar_status)

    # calendar today
    cal_today_parser = calendar_subparsers.add_parser("today", help="Show today's events")
    cal_today_parser.set_defaults(func=handle_calendar_today)

    # calendar upcoming
    cal_upcoming_parser = calendar_subparsers.add_parser("upcoming", help="Show upcoming events")
    cal_upcoming_parser.add_argument(
        "--minutes",
        type=int,
        default=60,
        help="Minutes to look ahead (default: 60)",
    )
    cal_upcoming_parser.set_defaults(func=handle_calendar_upcoming)

    # setup command - web-based setup server
    setup_parser = subparsers.add_parser(
        "setup",
        help="Start the web-based setup server for configuration",
    )
    setup_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    setup_parser.add_argument(
        "--port",
        type=int,
        default=9999,
        help="Port to run on (default: 9999)",
    )
    setup_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )
    setup_parser.set_defaults(func=handle_setup_server)

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
