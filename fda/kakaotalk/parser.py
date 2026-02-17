"""
KakaoTalk chat export parser.

Parses the .txt files exported from KakaoTalk Desktop (PC/Mac) into
structured message objects. The export format is:

    --------------- 2026년 2월 15일 토요일 ---------------
    [김대리] [오후 2:30] 재고 페이지에서 수량 필드가 안 보여요
    [김대리] [오후 2:31] 스크린샷 첨부했습니다
    [박과장] [오후 3:00] 확인 부탁드립니다

Note: The exact format may vary slightly between KakaoTalk versions.
This parser handles the most common format used in 2025-2026.
"""

import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional
from pathlib import Path


@dataclass
class KakaoMessage:
    """A single parsed KakaoTalk message."""
    sender: str
    timestamp: datetime
    text: str
    raw_line: str

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "timestamp": self.timestamp.isoformat(),
            "text": self.text,
            "raw_line": self.raw_line,
        }


class KakaoTalkParser:
    """
    Parses KakaoTalk .txt export files into structured messages.

    Handles:
    - Date headers: --------------- 2026년 2월 15일 토요일 ---------------
    - Messages: [Name] [Time] message text
    - Multi-line messages (continuation lines without [Name] prefix)
    - AM/PM in Korean (오전/오후)
    """

    # Date header: --------------- 2026년 2월 15일 토요일 ---------------
    DATE_HEADER_PATTERN = re.compile(
        r"-+\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*\S+\s*-+"
    )

    # Message line: [Name] [오후 2:30] message text
    MESSAGE_PATTERN = re.compile(
        r"^\[(.+?)\]\s*\[(오전|오후)\s*(\d{1,2}):(\d{2})\]\s*(.*)"
    )

    # System messages (user joined, left, etc.) — skip these
    SYSTEM_PATTERNS = [
        re.compile(r".*님이 들어왔습니다\.?$"),
        re.compile(r".*님이 나갔습니다\.?$"),
        re.compile(r".*님을 초대했습니다\.?$"),
        re.compile(r"^채팅방 관리자가.*$"),
        re.compile(r"^사진$|^동영상$|^파일$"),  # media-only messages
    ]

    def __init__(self):
        self._current_date: Optional[date] = None

    def parse_file(self, file_path: Path) -> list[KakaoMessage]:
        """
        Parse a KakaoTalk export file into messages.

        Args:
            file_path: Path to the .txt export file.

        Returns:
            List of KakaoMessage objects, sorted chronologically.
        """
        if not file_path.exists():
            return []

        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        return self.parse_lines(lines)

    def parse_lines(self, lines: list[str]) -> list[KakaoMessage]:
        """
        Parse raw lines from a KakaoTalk export.

        Args:
            lines: List of text lines from the export.

        Returns:
            List of KakaoMessage objects.
        """
        messages: list[KakaoMessage] = []
        self._current_date = None
        current_message: Optional[KakaoMessage] = None

        for line in lines:
            line = line.rstrip("\n\r")

            # Skip empty lines
            if not line.strip():
                continue

            # Check for date header
            date_match = self.DATE_HEADER_PATTERN.match(line)
            if date_match:
                # Save any pending multi-line message
                if current_message:
                    messages.append(current_message)
                    current_message = None

                year = int(date_match.group(1))
                month = int(date_match.group(2))
                day = int(date_match.group(3))
                self._current_date = date(year, month, day)
                continue

            # Check for message line
            msg_match = self.MESSAGE_PATTERN.match(line)
            if msg_match and self._current_date:
                # Save any pending multi-line message
                if current_message:
                    messages.append(current_message)

                sender = msg_match.group(1)
                ampm = msg_match.group(2)
                hour = int(msg_match.group(3))
                minute = int(msg_match.group(4))
                text = msg_match.group(5)

                # Convert Korean AM/PM to 24-hour
                if ampm == "오후" and hour != 12:
                    hour += 12
                elif ampm == "오전" and hour == 12:
                    hour = 0

                timestamp = datetime(
                    self._current_date.year,
                    self._current_date.month,
                    self._current_date.day,
                    hour,
                    minute,
                )

                # Skip system messages
                if self._is_system_message(text):
                    current_message = None
                    continue

                current_message = KakaoMessage(
                    sender=sender,
                    timestamp=timestamp,
                    text=text,
                    raw_line=line,
                )
                continue

            # Continuation line (part of a multi-line message)
            if current_message and line.strip():
                current_message.text += "\n" + line.strip()
                current_message.raw_line += "\n" + line

        # Don't forget the last message
        if current_message:
            messages.append(current_message)

        return messages

    def _is_system_message(self, text: str) -> bool:
        """Check if a message is a system notification."""
        for pattern in self.SYSTEM_PATTERNS:
            if pattern.match(text.strip()):
                return True
        return False

    def parse_and_diff(
        self,
        file_path: Path,
        since: datetime,
    ) -> list[KakaoMessage]:
        """
        Parse a file and return only messages after a given timestamp.

        This is the primary method used by the reader — parse the full
        export but only return new messages since the last check.

        Args:
            file_path: Path to the export file.
            since: Only return messages after this timestamp.

        Returns:
            List of new messages since the given timestamp.
        """
        all_messages = self.parse_file(file_path)
        return [msg for msg in all_messages if msg.timestamp > since]

    def get_last_message_time(self, file_path: Path) -> Optional[datetime]:
        """
        Get the timestamp of the last message in an export file.

        Useful for tracking what's been processed.

        Args:
            file_path: Path to the export file.

        Returns:
            Timestamp of the last message, or None if no messages found.
        """
        messages = self.parse_file(file_path)
        if messages:
            return messages[-1].timestamp
        return None
