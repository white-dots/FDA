"""
SSH connection manager for remote VM operations.

Manages SSH connections to Azure VMs, executes commands remotely,
and reads files from the remote filesystem.

Uses subprocess + ssh CLI rather than paramiko for simplicity and
because macOS ships with a good SSH client. Falls back to paramiko
if installed.
"""

import subprocess
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SSHResult:
    """Result of a remote SSH command execution."""
    stdout: str
    stderr: str
    return_code: int
    command: str
    host: str

    @property
    def success(self) -> bool:
        return self.return_code == 0

    @property
    def output(self) -> str:
        """Combined stdout, preferring stdout over stderr."""
        return self.stdout if self.stdout else self.stderr


class SSHManager:
    """
    Manages SSH connections and remote command execution.

    Uses the system's SSH client for reliability on macOS.
    Connections use key-based auth (no passwords).
    """

    def __init__(
        self,
        host: str,
        user: str,
        ssh_key: str,
        port: int = 22,
        connect_timeout: int = 10,
    ):
        """
        Initialize SSH manager for a specific host.

        Args:
            host: Remote hostname or IP.
            user: SSH username.
            ssh_key: Path to SSH private key file.
            port: SSH port (default 22).
            connect_timeout: Connection timeout in seconds.
        """
        self.host = host
        self.user = user
        self.ssh_key = Path(ssh_key).expanduser()
        self.port = port
        self.connect_timeout = connect_timeout

    def _ssh_base_args(self) -> list[str]:
        """Build base SSH command arguments."""
        return [
            "ssh",
            "-i", str(self.ssh_key),
            "-p", str(self.port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "BatchMode=yes",  # Never prompt for password
            f"{self.user}@{self.host}",
        ]

    def execute(
        self,
        command: str,
        timeout: int = 60,
        cwd: Optional[str] = None,
    ) -> SSHResult:
        """
        Execute a command on the remote host.

        Args:
            command: Shell command to execute.
            timeout: Command timeout in seconds.
            cwd: Working directory on remote host.

        Returns:
            SSHResult with stdout, stderr, and return code.
        """
        if cwd:
            command = f"cd {cwd} && {command}"

        args = self._ssh_base_args() + [command]

        logger.debug(f"SSH exec on {self.host}: {command}")

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            ssh_result = SSHResult(
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
                command=command,
                host=self.host,
            )

            if not ssh_result.success:
                logger.warning(
                    f"SSH command failed on {self.host}: {command}\n"
                    f"stderr: {result.stderr[:500]}"
                )

            return ssh_result

        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out on {self.host}: {command}")
            return SSHResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                return_code=-1,
                command=command,
                host=self.host,
            )
        except Exception as e:
            logger.error(f"SSH error on {self.host}: {e}")
            return SSHResult(
                stdout="",
                stderr=str(e),
                return_code=-1,
                command=command,
                host=self.host,
            )

    def read_file(self, remote_path: str) -> Optional[str]:
        """
        Read a file from the remote host.

        Args:
            remote_path: Absolute path on the remote host.

        Returns:
            File contents as string, or None on failure.
        """
        result = self.execute(f"cat {remote_path}", timeout=30)
        if result.success:
            return result.stdout
        logger.error(f"Failed to read {remote_path} on {self.host}: {result.stderr}")
        return None

    def read_files(self, remote_paths: list[str]) -> dict[str, Optional[str]]:
        """
        Read multiple files from the remote host in one SSH session.

        Args:
            remote_paths: List of absolute paths on the remote host.

        Returns:
            Dict mapping path -> contents (None if read failed).
        """
        if not remote_paths:
            return {}

        # Build a single command that cats all files with delimiters
        delimiter = "===FDA_FILE_DELIMITER==="
        parts = []
        for path in remote_paths:
            parts.append(f'echo "{delimiter}{path}"; cat "{path}" 2>/dev/null || echo "FDA_READ_ERROR"')

        combined = " ; ".join(parts)
        result = self.execute(combined, timeout=60)

        if not result.success:
            return {path: None for path in remote_paths}

        # Parse the output
        files: dict[str, Optional[str]] = {}
        current_path = None
        current_content: list[str] = []

        for line in result.stdout.split("\n"):
            if line.startswith(delimiter):
                # Save previous file
                if current_path is not None:
                    content = "\n".join(current_content)
                    files[current_path] = None if "FDA_READ_ERROR" in content else content

                current_path = line[len(delimiter):]
                current_content = []
            else:
                current_content.append(line)

        # Save last file
        if current_path is not None:
            content = "\n".join(current_content)
            files[current_path] = None if "FDA_READ_ERROR" in content else content

        # Fill in any missing paths
        for path in remote_paths:
            if path not in files:
                files[path] = None

        return files

    def list_files(
        self,
        directory: str,
        pattern: str = "*",
        recursive: bool = False,
    ) -> list[str]:
        """
        List files in a remote directory.

        Args:
            directory: Remote directory path.
            pattern: Glob pattern to match.
            recursive: Whether to search recursively.

        Returns:
            List of file paths.
        """
        if recursive:
            cmd = f'find {directory} -name "{pattern}" -type f 2>/dev/null'
        else:
            cmd = f'ls -1 {directory}/{pattern} 2>/dev/null'

        result = self.execute(cmd, timeout=30)
        if result.success:
            return [line for line in result.stdout.strip().split("\n") if line]
        return []

    def file_exists(self, remote_path: str) -> bool:
        """Check if a file exists on the remote host."""
        result = self.execute(f"test -f {remote_path} && echo 'yes' || echo 'no'")
        return result.success and "yes" in result.stdout

    def get_git_status(self, repo_path: str) -> Optional[dict]:
        """
        Get git status of a repository on the remote host.

        Args:
            repo_path: Path to the git repository.

        Returns:
            Dict with branch, commit hash, and dirty status.
        """
        result = self.execute(
            'echo "BRANCH:$(git rev-parse --abbrev-ref HEAD)"; '
            'echo "COMMIT:$(git rev-parse --short HEAD)"; '
            'echo "DIRTY:$(git status --porcelain | wc -l)"',
            cwd=repo_path,
        )

        if not result.success:
            return None

        info = {}
        for line in result.stdout.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                info[key.strip().lower()] = value.strip()

        return info

    def test_connection(self) -> bool:
        """
        Test SSH connectivity to the remote host.

        Returns:
            True if the connection succeeds.
        """
        result = self.execute("echo 'FDA_CONNECTION_OK'", timeout=15)
        return result.success and "FDA_CONNECTION_OK" in result.stdout
