"""
File transfer between Mac Mini and Azure VMs via SCP.

Handles uploading modified files to VMs and downloading files for
local analysis. Uses the system's scp command for reliability.
"""

import subprocess
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class FileTransfer:
    """
    SCP-based file transfer for deploying code changes.

    Uses the system's scp command with SSH key authentication.
    """

    def __init__(
        self,
        host: str,
        user: str,
        ssh_key: str,
        port: int = 22,
    ):
        """
        Initialize file transfer for a specific host.

        Args:
            host: Remote hostname.
            user: SSH username.
            ssh_key: Path to SSH private key.
            port: SSH port.
        """
        self.host = host
        self.user = user
        self.ssh_key = Path(ssh_key).expanduser()
        self.port = port

    def _scp_base_args(self) -> list[str]:
        """Build base SCP command arguments."""
        return [
            "scp",
            "-i", str(self.ssh_key),
            "-P", str(self.port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
        ]

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        timeout: int = 60,
    ) -> bool:
        """
        Upload a file to the remote host.

        Args:
            local_path: Local file path.
            remote_path: Destination path on remote host.
            timeout: Transfer timeout in seconds.

        Returns:
            True if upload succeeded.
        """
        if not local_path.exists():
            logger.error(f"Local file not found: {local_path}")
            return False

        remote_target = f"{self.user}@{self.host}:{remote_path}"
        args = self._scp_base_args() + [str(local_path), remote_target]

        logger.info(f"Uploading {local_path} → {self.host}:{remote_path}")

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                logger.info(f"Upload complete: {local_path.name}")
                return True
            else:
                logger.error(f"Upload failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"Upload timed out: {local_path}")
            return False

    def upload_directory(
        self,
        local_dir: Path,
        remote_dir: str,
        timeout: int = 120,
    ) -> bool:
        """
        Upload a directory recursively to the remote host.

        Args:
            local_dir: Local directory path.
            remote_dir: Destination directory on remote host.
            timeout: Transfer timeout in seconds.

        Returns:
            True if upload succeeded.
        """
        if not local_dir.is_dir():
            logger.error(f"Local directory not found: {local_dir}")
            return False

        remote_target = f"{self.user}@{self.host}:{remote_dir}"
        args = self._scp_base_args() + ["-r", str(local_dir), remote_target]

        logger.info(f"Uploading directory {local_dir} → {self.host}:{remote_dir}")

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                logger.info(f"Directory upload complete: {local_dir.name}")
                return True
            else:
                logger.error(f"Directory upload failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"Directory upload timed out: {local_dir}")
            return False

    def download(
        self,
        remote_path: str,
        local_path: Path,
        timeout: int = 60,
    ) -> bool:
        """
        Download a file from the remote host.

        Args:
            remote_path: File path on remote host.
            local_path: Local destination path.
            timeout: Transfer timeout in seconds.

        Returns:
            True if download succeeded.
        """
        local_path.parent.mkdir(parents=True, exist_ok=True)

        remote_source = f"{self.user}@{self.host}:{remote_path}"
        args = self._scp_base_args() + [remote_source, str(local_path)]

        logger.info(f"Downloading {self.host}:{remote_path} → {local_path}")

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                logger.info(f"Download complete: {local_path.name}")
                return True
            else:
                logger.error(f"Download failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"Download timed out: {remote_path}")
            return False

    def upload_content(
        self,
        content: str,
        remote_path: str,
        timeout: int = 30,
    ) -> bool:
        """
        Upload string content directly to a remote file.

        Writes content to a temp file locally, uploads via SCP,
        then cleans up. Useful for deploying generated code.

        Args:
            content: File content as string.
            remote_path: Destination path on remote host.
            timeout: Transfer timeout in seconds.

        Returns:
            True if upload succeeded.
        """
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tmp", delete=True, encoding="utf-8"
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            return self.upload(Path(tmp.name), remote_path, timeout)
