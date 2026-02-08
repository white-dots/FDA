"""
Librarian Agent implementation.

The Librarian agent manages knowledge artifacts, generates reports,
and maintains the project journal.
"""

import logging
import time
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timedelta

from fda.base_agent import BaseAgent
from fda.config import MODEL_LIBRARIAN, DEFAULT_CHECK_INTERVAL_MINUTES

logger = logging.getLogger(__name__)


LIBRARIAN_SYSTEM_PROMPT = """You are the Librarian Agent for a project management system.

Your responsibilities include:
1. **Knowledge Management**: Maintain and organize project documentation
2. **Report Generation**: Create daily, weekly, and monthly reports
3. **Meeting Briefs**: Prepare briefing materials for meetings
4. **Journal Maintenance**: Keep the project journal organized and searchable
5. **Information Retrieval**: Help find relevant historical information

When generating content:
- Be clear and well-organized
- Use appropriate formatting (headers, lists, etc.)
- Include relevant data and metrics
- Highlight key insights and recommendations
- Make content actionable where appropriate

When responding:
- Structure information logically
- Prioritize the most important information first
- Include supporting data when available
- Cross-reference related entries when helpful
"""


class LibrarianAgent(BaseAgent):
    """
    Librarian Agent for knowledge management and report generation.

    The Librarian agent maintains the project journal, generates reports,
    creates meeting briefs, and manages the knowledge index.
    """

    def __init__(self, project_state_path: Optional[Path] = None):
        """
        Initialize the Librarian agent.

        Args:
            project_state_path: Path to the project state database.
        """
        super().__init__(
            name="Librarian",
            model=MODEL_LIBRARIAN,
            system_prompt=LIBRARIAN_SYSTEM_PROMPT,
            project_state_path=project_state_path,
        )

    def run_event_loop(self) -> None:
        """
        Run the main event loop for the Librarian.

        Continuously processes requests for reports and journal updates.
        """
        logger.info("[Librarian] Starting event loop...")

        check_interval = DEFAULT_CHECK_INTERVAL_MINUTES * 60

        while self._running:
            try:
                # Process pending messages
                messages = self.get_pending_messages()
                for message in messages:
                    self._handle_message(message)

                # Periodic maintenance tasks
                self._run_maintenance()

                time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("[Librarian] Received shutdown signal")
                break
            except Exception as e:
                logger.error(f"[Librarian] Error in event loop: {e}")
                time.sleep(60)

        logger.info("[Librarian] Event loop stopped")

    def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming message."""
        msg_type = message.get("type", "")
        subject = message.get("subject", "")
        body = message.get("body", "")
        from_agent = message.get("from", "")

        logger.info(f"[Librarian] Received message from {from_agent}: {subject}")

        self.message_bus.mark_read(message["id"])

        if msg_type == "report_request":
            # Generate requested report
            report_type = body or "daily"
            report = self.generate_report(report_type)
            self.send_message(
                to_agent=from_agent,
                msg_type="report",
                subject=f"{report_type.title()} Report",
                body=report,
            )

        elif msg_type == "meeting_brief_request":
            # Generate meeting brief
            event = {"id": body, "subject": subject}
            brief = self.generate_meeting_brief(event)
            self.send_message(
                to_agent=from_agent,
                msg_type="meeting_brief",
                subject=f"Brief: {subject}",
                body=brief,
            )

        elif msg_type == "search_request":
            # Search the journal
            results = self.search_journal(body, top_n=5)
            response = self._format_search_results(results)
            self.send_message(
                to_agent=from_agent,
                msg_type="search_results",
                subject=f"Search: {body}",
                body=response,
            )

    def _run_maintenance(self) -> None:
        """Run periodic maintenance tasks."""
        # Update the journal index
        try:
            self.update_index()
        except Exception as e:
            logger.error(f"[Librarian] Error updating index: {e}")

    def generate_report(self, report_type: str) -> str:
        """
        Generate a report of the specified type.

        Args:
            report_type: Type of report (daily, weekly, monthly, project).

        Returns:
            The generated report as a string.
        """
        logger.info(f"[Librarian] Generating {report_type} report")

        # Gather data based on report type
        context = self._gather_report_data(report_type)

        prompt = self._get_report_prompt(report_type)

        response = self.chat_with_context(prompt, context)

        # Log the report to journal
        self.log_to_journal(
            summary=f"{report_type.title()} Report - {datetime.now().strftime('%Y-%m-%d')}",
            content=f"## {report_type.title()} Report\n\n{response}",
            tags=["report", report_type],
            relevance_decay="medium" if report_type == "daily" else "slow",
        )

        return response

    def _gather_report_data(self, report_type: str) -> dict[str, Any]:
        """Gather data for a specific report type."""
        context: dict[str, Any] = {}

        # Get tasks
        tasks = self.state.get_tasks()
        context["total_tasks"] = len(tasks)
        context["tasks_by_status"] = {}
        for task in tasks:
            status = task.get("status", "unknown")
            if status not in context["tasks_by_status"]:
                context["tasks_by_status"][status] = []
            context["tasks_by_status"][status].append(task)

        # Get alerts
        context["alerts"] = self.state.get_alerts(acknowledged=False)

        # Get decisions
        if report_type in ["weekly", "monthly", "project"]:
            context["decisions"] = self.state.get_decisions(limit=10)
        else:
            context["decisions"] = self.state.get_decisions(limit=5)

        # Get journal entries based on timeframe
        if report_type == "daily":
            context["journal_entries"] = self.journal_retriever.index.get_recent(limit=10)
        elif report_type == "weekly":
            week_ago = datetime.now() - timedelta(days=7)
            context["journal_entries"] = self.journal_retriever.index.get_by_date_range(
                week_ago, datetime.now()
            )
        elif report_type == "monthly":
            month_ago = datetime.now() - timedelta(days=30)
            context["journal_entries"] = self.journal_retriever.index.get_by_date_range(
                month_ago, datetime.now()
            )
        else:  # project
            context["journal_entries"] = self.journal_retriever.index.entries

        # Get KPI data
        context["kpis"] = {
            "completion_rate": self.state.get_latest_kpi("completion_rate"),
            "block_rate": self.state.get_latest_kpi("block_rate"),
            "total_tasks": self.state.get_latest_kpi("total_tasks"),
        }

        return context

    def _get_report_prompt(self, report_type: str) -> str:
        """Get the appropriate prompt for a report type."""
        base_prompt = """Generate a comprehensive {type} report for this project.

Include the following sections:
1. **Executive Summary**: Brief overview of the period
2. **Accomplishments**: What was completed
3. **Current Status**: Where things stand now
4. **Challenges & Blockers**: Issues encountered
5. **Upcoming Work**: What's planned next
6. **Metrics & KPIs**: Key numbers and trends
7. **Recommendations**: Suggested actions

Be specific, cite data where available, and make recommendations actionable."""

        if report_type == "daily":
            return base_prompt.format(type="daily") + """

Focus on:
- Tasks completed today
- Tasks in progress
- Immediate blockers
- Tomorrow's priorities"""

        elif report_type == "weekly":
            return base_prompt.format(type="weekly") + """

Focus on:
- Week's key accomplishments
- Progress against weekly goals
- Trends over the week
- Next week's priorities"""

        elif report_type == "monthly":
            return base_prompt.format(type="monthly") + """

Focus on:
- Month's major milestones
- Progress against monthly goals
- Trend analysis
- Strategic observations
- Next month's objectives"""

        else:  # project
            return base_prompt.format(type="project") + """

Focus on:
- Overall project health
- Progress against project goals
- Major decisions made
- Lessons learned
- Critical path items
- Risk assessment"""

    def generate_meeting_brief(self, event: dict[str, Any]) -> str:
        """
        Generate a brief for an upcoming meeting.

        Args:
            event: Calendar event details.

        Returns:
            The meeting brief as a string.
        """
        event_id = event.get("id", "unknown")
        subject = event.get("subject", "Unknown Meeting")

        logger.info(f"[Librarian] Generating brief for: {subject}")

        # Gather context
        context = self.get_project_context()

        # Search for relevant journal entries
        relevant = self.search_journal(subject, top_n=5)
        context["relevant_entries"] = [
            {
                "summary": e.get("summary"),
                "author": e.get("author"),
                "date": e.get("created_at", "")[:10],
            }
            for e in relevant
        ]

        # Add event details
        context["event"] = event

        prompt = f"""Prepare a comprehensive meeting brief for:

Meeting: {subject}
Event ID: {event_id}
Time: {event.get('start', 'Unknown')}
Location: {event.get('location', 'Unknown')}
Attendees: {', '.join(a.get('name', a.get('email', '')) for a in event.get('attendees', []))}

Generate a brief that includes:
1. **Meeting Purpose**: What this meeting is about
2. **Background**: Relevant context and history
3. **Key Discussion Points**: Topics to cover
4. **Current Status**: Relevant project status
5. **Open Questions**: Items needing decisions
6. **Action Items to Discuss**: Outstanding tasks
7. **Preparation Notes**: What attendees should review beforehand

Base the brief on the project context and relevant historical entries provided."""

        response = self.chat_with_context(prompt, context)

        # Store the brief
        self.state.record_meeting_prep(
            event_id=event_id,
            brief=response,
            created_by=self.name,
        )

        return response

    def write_journal_entry(self, entry: dict[str, Any]) -> Path:
        """
        Write an entry to the project journal.

        Args:
            entry: Dictionary containing entry metadata and content.
                  Required keys: summary, content
                  Optional keys: tags, relevance_decay

        Returns:
            Path to the written journal file.
        """
        summary = entry.get("summary", "Journal Entry")
        content = entry.get("content", "")
        tags = entry.get("tags", ["librarian"])
        relevance_decay = entry.get("relevance_decay", "medium")

        # Optionally enhance the content with AI
        if entry.get("enhance", False):
            enhanced_content = self._enhance_journal_entry(summary, content)
            content = enhanced_content

        return self.journal_writer.write_entry(
            author=self.name,
            tags=tags,
            summary=summary,
            content=content,
            relevance_decay=relevance_decay,
        )

    def _enhance_journal_entry(self, summary: str, content: str) -> str:
        """Enhance a journal entry with AI assistance."""
        prompt = f"""Enhance this journal entry for better clarity and organization:

Summary: {summary}

Content:
{content}

Please:
1. Improve the structure and formatting
2. Add appropriate headers if needed
3. Ensure key points are clear
4. Add any helpful context
5. Keep the factual content unchanged

Return the enhanced entry."""

        return self.chat(prompt, include_history=False)

    def update_index(self) -> None:
        """
        Update the journal index with recent entries.

        Scans the journal directory for entries not in the index.
        """
        logger.debug("[Librarian] Updating journal index")

        journal_dir = self.journal_writer.journal_dir
        index = self.journal_retriever.index

        # Get current indexed filenames
        indexed_files = {e.get("filename") for e in index.entries}

        # Scan journal directory
        new_entries = 0
        for filepath in journal_dir.glob("*.md"):
            if filepath.name not in indexed_files:
                # Read and index this entry
                try:
                    entry_data = self.journal_writer.read_entry(filepath.name)
                    metadata = entry_data.get("metadata", {})

                    index.add_entry({
                        "filename": filepath.name,
                        "author": metadata.get("author", "unknown"),
                        "tags": metadata.get("tags", []),
                        "summary": metadata.get("title", filepath.stem),
                        "created_at": metadata.get("created_at", datetime.now().isoformat()),
                        "relevance_decay": metadata.get("relevance_decay", "medium"),
                    })
                    new_entries += 1
                except Exception as e:
                    logger.error(f"Failed to index {filepath}: {e}")

        if new_entries > 0:
            logger.info(f"[Librarian] Added {new_entries} new entries to index")

    def alert_fda(self, message: str, level: str = "warning") -> None:
        """
        Send an alert message to the FDA agent.

        Args:
            message: The alert message.
            level: Alert level (info, warning, critical).
        """
        # Add to alerts
        self.add_alert(level, message)

        # Send message to FDA
        self.send_message(
            to_agent="FDA",
            msg_type="alert",
            subject=f"{level.title()} Alert from Librarian",
            body=message,
            priority="high" if level == "critical" else "medium",
        )

    def _format_search_results(self, results: list[dict[str, Any]]) -> str:
        """Format search results for display."""
        if not results:
            return "No matching entries found."

        lines = [f"Found {len(results)} matching entries:\n"]

        for i, entry in enumerate(results, 1):
            lines.append(f"{i}. **{entry.get('summary', 'Untitled')}**")
            lines.append(f"   Author: {entry.get('author')} | Date: {entry.get('created_at', '')[:10]}")
            lines.append(f"   Tags: {', '.join(entry.get('tags', []))}")
            lines.append(f"   Score: {entry.get('combined_score', 0):.3f}")
            lines.append("")

        return "\n".join(lines)

    def summarize_entries(
        self,
        query: Optional[str] = None,
        tags: Optional[list[str]] = None,
        days: int = 7,
    ) -> str:
        """
        Summarize journal entries matching criteria.

        Args:
            query: Optional search query.
            tags: Optional tag filter.
            days: Number of days to look back.

        Returns:
            Summary of matching entries.
        """
        # Get relevant entries
        if query or tags:
            entries = self.search_journal(query or "", tags, top_n=20)
        else:
            start_date = datetime.now() - timedelta(days=days)
            entries = self.journal_retriever.index.get_by_date_range(
                start_date, datetime.now()
            )

        if not entries:
            return "No entries found matching the criteria."

        # Build context for summarization
        entry_summaries = []
        for entry in entries[:20]:
            content = self.journal_retriever._read_entry_content(
                entry.get("filename", "")
            )
            entry_summaries.append({
                "summary": entry.get("summary"),
                "author": entry.get("author"),
                "date": entry.get("created_at", "")[:10],
                "content_preview": content[:500] if content else "",
            })

        context = {"entries": entry_summaries}

        prompt = """Summarize these journal entries:

Provide:
1. **Overview**: What these entries cover
2. **Key Themes**: Common topics or patterns
3. **Important Findings**: Notable insights
4. **Decisions Made**: Any decisions documented
5. **Open Items**: Things that may need follow-up

Be concise but comprehensive."""

        return self.chat_with_context(prompt, context)

    def create_knowledge_digest(self) -> str:
        """
        Create a digest of important project knowledge.

        Returns:
            Knowledge digest as a string.
        """
        # Gather different types of entries
        context: dict[str, Any] = {}

        # Get slow-decay entries (important long-term knowledge)
        all_entries = self.journal_retriever.index.entries
        slow_decay = [
            e for e in all_entries
            if e.get("relevance_decay") == "slow"
        ]
        context["slow_decay_entries"] = slow_decay[:10]

        # Get entries by key tags
        key_tags = ["decision", "strategic", "architecture", "onboarding"]
        for tag in key_tags:
            tagged = [e for e in all_entries if tag in e.get("tags", [])]
            context[f"{tag}_entries"] = tagged[:5]

        # Get decisions
        context["decisions"] = self.state.get_decisions(limit=10)

        prompt = """Create a knowledge digest for this project.

This digest should capture the most important, enduring information that
new team members or stakeholders should know.

Include:
1. **Project Overview**: What this project is about
2. **Key Decisions**: Important decisions and their rationale
3. **Architecture & Design**: Technical or structural decisions
4. **Lessons Learned**: Important insights from the journey
5. **Important Contacts**: Key people and their roles (if known)
6. **Critical Information**: Anything else essential to know

Focus on information that remains relevant over time."""

        digest = self.chat_with_context(prompt, context)

        # Log the digest
        self.log_to_journal(
            summary="Knowledge Digest",
            content=f"## Project Knowledge Digest\n\n{digest}",
            tags=["digest", "knowledge", "reference"],
            relevance_decay="slow",
        )

        return digest
