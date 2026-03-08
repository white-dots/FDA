"""SSH connection manager for remote VM operations.

Manages SSH connections to Azure VMs, executes commands remotely,
and reads files from the remote filesystem.

Uses subprocess + ssh CLI rather than paramiko for simplicity and
because macOS ships with a good SSH client.

Connection pooling: Uses SSH ControlMaster to keep a persistent
multiplexed connection. First command opens the master connection;
subsequent commands reuse it (~50ms vs ~1.5s per command).
"""

import atexit
import hashlib
import subprocess
import logging
from pathlib import Path
from typing import Any, Optional
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

    Connection pooling via ControlMaster:
    - First SSH command opens a persistent master connection
    - Subsequent commands multiplex over it (no new TCP/SSH handshake)
    - Master auto-closes after 10 minutes of inactivity
    - Reduces per-command latency from ~1.5s to ~50ms
    """

    # Shared directory for all control sockets
    _control_dir: Optional[Path] = None

    @classmethod
    def _get_control_dir(cls) -> Path:
        """Get or create the shared control socket directory.

        Uses /tmp/fda_ssh/ instead of tempfile.mkdtemp() because macOS
        Unix domain sockets have a 104-char path limit, and the default
        temp dir (/var/folders/…) is too long.
        """
        if cls._control_dir is None:
            cls._control_dir = Path("/tmp/fda_ssh")
            cls._control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            logger.debug(f"SSH control socket dir: {cls._control_dir}")
        return cls._control_dir

    def __init__(
        self,
        host: str,
        user: str,
        ssh_key: str = "",
        port: int = 22,
        connect_timeout: int = 10,
    ):
        """
        Initialize SSH manager for a specific host.

        Args:
            host: Remote hostname or IP.
            user: SSH username.
            ssh_key: Path to SSH private key file. Empty string for password auth.
            port: SSH port (default 22).
            connect_timeout: Connection timeout in seconds.
        """
        self.host = host
        self.user = user
        self.ssh_key = Path(ssh_key).expanduser() if ssh_key else None
        self.port = port
        self.connect_timeout = connect_timeout

        # ControlMaster socket path — must be short for macOS 104-char limit
        # Use a short hash of user@host:port to keep the path under ~30 chars
        control_dir = self._get_control_dir()
        sock_id = hashlib.md5(f"{user}@{host}:{port}".encode()).hexdigest()[:12]
        self._control_path = control_dir / sock_id
        self._master_started = False

    def _ssh_base_args(self) -> list[str]:
        """Build base SSH command arguments with ControlMaster support."""
        args = [
            "ssh",
            "-p", str(self.port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            # ControlMaster: reuse existing connection or start one
            "-o", f"ControlPath={self._control_path}",
        ]

        # If master is already running, just attach to it
        if self._master_started and self._control_path.exists():
            args.extend(["-o", "ControlMaster=no"])
        else:
            # Auto-start master if not running; persist for 10 min idle
            args.extend(["-o", "ControlMaster=auto"])
            args.extend(["-o", "ControlPersist=600"])

        if self.ssh_key:
            args.extend(["-i", str(self.ssh_key)])
            args.extend(["-o", "BatchMode=yes"])
        # When no key is provided, SSH will use the default agent/keychain
        # or prompt for password (interactive). For automated use, ensure
        # ssh-agent has the key loaded or use ~/.ssh/config PasswordAuthentication.
        args.append(f"{self.user}@{self.host}")
        return args

    def test_connection(self) -> bool:
        """
        Test if the SSH connection is working.

        Returns:
            True if connection is good, False if there are issues.
        """
        try:
            result = self.execute("echo 'connection_test'", timeout=10)
            return result.success and "connection_test" in result.stdout
        except Exception as e:
            logger.warning(f"SSH connection test failed for {self.user}@{self.host}: {e}")
            return False

    def close_master(self) -> None:
        """Explicitly close the ControlMaster connection."""
        if self._control_path.exists():
            try:
                subprocess.run(
                    [
                        "ssh",
                        "-o", f"ControlPath={self._control_path}",
                        "-O", "exit",
                        f"{self.user}@{self.host}",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                logger.debug(f"Closed SSH master for {self.user}@{self.host}")
            except Exception as e:
                logger.debug(f"Error closing SSH master: {e}")
            self._master_started = False

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

            # Track that master is now running (first successful command starts it)
            if result.returncode == 0 and not self._master_started:
                self._master_started = True
                logger.debug(f"SSH ControlMaster established for {self.user}@{self.host}")

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
            pattern: File pattern to match (default: all files).
            recursive: Whether to search recursively.

        Returns:
            List of file paths.
        """
        if recursive:
            command = f"find {directory} -name '{pattern}' -type f"
        else:
            command = f"ls -1 {directory}/{pattern} 2>/dev/null || true"

        result = self.execute(command, timeout=30)
        if result.success and result.stdout:
            return [line.strip() for line in result.stdout.split("\n") if line.strip()]
        return []

    def warmup(self) -> bool:
        """
        Warm up the SSH connection by establishing the ControlMaster.

        Returns:
            True if warmup succeeded.
        """
        try:
            result = self.execute("echo warmup", timeout=15)
            return result.success
        except Exception as e:
            logger.warning(f"SSH warmup failed for {self.user}@{self.host}: {e}")
            return False

    def get_git_status(self, repo_path: str) -> Optional[dict[str, Any]]:
        """
        Get git status information from a remote repository.

        Args:
            repo_path: Path to the git repository on remote host.

        Returns:
            Dict with git status info, or None if not a git repo.
        """
        # Check if it's a git repo
        result = self.execute(f"cd {repo_path} && git rev-parse --git-dir", timeout=10)
        if not result.success:
            return None

        status_info = {}

        # Get current branch
        result = self.execute(f"cd {repo_path} && git branch --show-current", timeout=10)
        if result.success:
            status_info["branch"] = result.stdout.strip()

        # Get last commit
        result = self.execute(f"cd {repo_path} && git log -1 --oneline", timeout=10)
        if result.success:
            status_info["last_commit"] = result.stdout.strip()

        # Get status
        result = self.execute(f"cd {repo_path} && git status --porcelain", timeout=10)
        if result.success:
            changes = result.stdout.strip().split("\n") if result.stdout.strip() else []
            status_info["modified_files"] = len(changes)
            status_info["has_changes"] = len(changes) > 0

        return status_info

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close the master connection."""
        self.close_master()
