"""
KakaoTalk message reader.

Monitors KakaoTalk Desktop exports for new messages from client chat rooms.
Uses macOS AppleScript to trigger the export, then parses the resulting
.txt file for new messages.

Architecture:
    1. AppleScript opens KakaoTalk, navigates to a chat room, triggers export (Cmd+S)
    2. Export saved to a known directory per client
    3. Parser extracts new messages since last check
    4. New messages are stored in SQLite and returned for processing
"""

import subprocess
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from fda.kakaotalk.parser import KakaoTalkParser, KakaoMessage

logger = logging.getLogger(__name__)


class KakaoTalkReader:
    """
    Reads new messages from KakaoTalk Desktop via export files.

    Two modes of operation:
    1. Manual mode: User exports chat manually, reader picks up the file
    2. Auto mode: Reader triggers export via AppleScript (macOS only)
    """

    def __init__(
        self,
        export_dir: Optional[Path] = None,
        auto_export: bool = False,
    ):
        """
        Initialize the KakaoTalk reader.

        Args:
            export_dir: Directory where KakaoTalk exports are saved.
                        Defaults to ~/Documents/fda-exports/kakaotalk/
            auto_export: If True, attempt to trigger exports via AppleScript.
                         If False (default), only read existing export files.
        """
        if export_dir is None:
            export_dir = Path.home() / "Documents" / "fda-exports" / "kakaotalk"

        self.export_dir = export_dir
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.auto_export = auto_export
        self.parser = KakaoTalkParser()

        # Track last processed timestamp per room
        self._last_checked: dict[str, datetime] = {}

    def get_new_messages(
        self,
        room_name: str,
        export_file: Optional[Path] = None,
    ) -> list[KakaoMessage]:
        """
        Get new messages from a chat room since the last check.

        Args:
            room_name: KakaoTalk chat room name.
            export_file: Explicit path to export file. If None, looks in export_dir.

        Returns:
            List of new KakaoMessage objects since last check.
        """
        if export_file is None:
            export_file = self._find_latest_export(room_name)

        if export_file is None or not export_file.exists():
            logger.debug(f"No export file found for room: {room_name}")
            return []

        # Get the timestamp of last processed message
        since = self._last_checked.get(room_name)

        if since is None:
            # First run — only get messages from the last hour to avoid
            # flooding with historical messages
            since = datetime.now().replace(second=0, microsecond=0)
            # Actually, on first run, get the last 10 messages for context
            all_messages = self.parser.parse_file(export_file)
            if all_messages:
                # Return last 10 messages for initial context
                recent = all_messages[-10:]
                self._last_checked[room_name] = all_messages[-1].timestamp
                return recent
            return []

        # Get messages since last check
        new_messages = self.parser.parse_and_diff(export_file, since)

        if new_messages:
            self._last_checked[room_name] = new_messages[-1].timestamp
            logger.info(
                f"Found {len(new_messages)} new messages in '{room_name}'"
            )

        return new_messages

    def _find_latest_export(self, room_name: str) -> Optional[Path]:
        """
        Find the latest export file for a given room.

        KakaoTalk exports are typically named like:
        - "KakaoTalk_Chat_Room Name.txt"
        - "카카오톡 대화_Room Name.txt"

        Args:
            room_name: Chat room name to look for.

        Returns:
            Path to the latest export file, or None.
        """
        # Look for files matching common KakaoTalk export naming patterns
        candidates = []

        for pattern in [
            f"*{room_name}*.txt",
            f"KakaoTalk*{room_name}*.txt",
            f"카카오톡*{room_name}*.txt",
        ]:
            candidates.extend(self.export_dir.glob(pattern))

        if not candidates:
            return None

        # Return the most recently modified file
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def trigger_export(self, room_name: str) -> bool:
        """
        Trigger a KakaoTalk chat export via AppleScript (macOS only).

        This uses macOS accessibility features to:
        1. Activate KakaoTalk
        2. Search for the chat room
        3. Trigger the export shortcut

        Args:
            room_name: Chat room name to export.

        Returns:
            True if the export was triggered successfully.

        Note:
            This requires accessibility permissions for Terminal/Python
            in System Preferences > Privacy & Security > Accessibility.
        """
        if not self.auto_export:
            logger.debug("Auto-export is disabled")
            return False

        # Sanitize the room name for the filename
        safe_name = room_name.replace(" ", "_").replace("/", "_")
        export_path = self.export_dir / f"KakaoTalk_Chat_{safe_name}.txt"

        applescript = f'''
        tell application "KakaoTalk"
            activate
        end tell

        delay 1

        tell application "System Events"
            tell process "KakaoTalk"
                -- Search for the chat room
                keystroke "f" using command down
                delay 0.5
                keystroke "{room_name}"
                delay 1
                key code 36  -- Enter to open the chat room
                delay 1

                -- Trigger export: Menu > Chat > Export Chat
                -- Note: The exact menu path may vary by KakaoTalk version.
                -- This is the keyboard shortcut approach:
                keystroke "s" using command down
                delay 1

                -- In the save dialog, set the filename and location
                keystroke "g" using {{command down, shift down}}
                delay 0.5
                keystroke "{str(self.export_dir)}"
                key code 36  -- Enter
                delay 0.5

                -- Set filename
                keystroke "a" using command down
                keystroke "{export_path.name}"
                delay 0.3

                -- Click Save (or press Enter)
                key code 36
                delay 1
            end tell
        end tell
        '''

        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                logger.info(f"Export triggered for room: {room_name}")
                return True
            else:
                logger.error(
                    f"AppleScript export failed for {room_name}: {result.stderr}"
                )
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"Export timed out for room: {room_name}")
            return False
        except FileNotFoundError:
            logger.error("osascript not found — are you on macOS?")
            return False

    def poll_all_rooms(
        self,
        room_names: list[str],
        trigger_exports: bool = False,
    ) -> dict[str, list[KakaoMessage]]:
        """
        Poll all registered chat rooms for new messages.

        Args:
            room_names: List of KakaoTalk room names to check.
            trigger_exports: Whether to trigger fresh exports first.

        Returns:
            Dict mapping room_name -> list of new messages.
        """
        results: dict[str, list[KakaoMessage]] = {}

        for room_name in room_names:
            if trigger_exports:
                self.trigger_export(room_name)
                time.sleep(2)  # Wait for export to complete

            new_messages = self.get_new_messages(room_name)
            if new_messages:
                results[room_name] = new_messages

        return results

    def set_last_checked(self, room_name: str, timestamp: datetime) -> None:
        """
        Manually set the last checked timestamp for a room.

        Useful for resuming after a restart.

        Args:
            room_name: Chat room name.
            timestamp: Timestamp to set.
        """
        self._last_checked[room_name] = timestamp

    def get_last_checked(self, room_name: str) -> Optional[datetime]:
        """Get the last checked timestamp for a room."""
        return self._last_checked.get(room_name)
