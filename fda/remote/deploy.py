"""
Deployment manager for pushing code changes to Azure VMs.

Handles the full deployment cycle:
1. Backup current files on the VM
2. Upload changed files via SCP
3. Run pre-deploy commands (pip install, migrations, etc.)
4. Restart the service
5. Verify health
6. Rollback on failure
"""

import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime

from fda.remote.ssh_manager import SSHManager
from fda.remote.file_transfer import FileTransfer
from fda.clients.client_config import ClientConfig

logger = logging.getLogger(__name__)


@dataclass
class DeployResult:
    """Result of a deployment attempt."""
    success: bool
    client_id: str
    files_deployed: list[str] = field(default_factory=list)
    backup_path: Optional[str] = None
    deploy_output: str = ""
    health_check_output: str = ""
    error: Optional[str] = None
    rolled_back: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def summary(self) -> str:
        """Human-readable deployment summary."""
        status = "SUCCESS" if self.success else "FAILED"
        lines = [f"Deploy {status} for {self.client_id} at {self.timestamp}"]

        if self.files_deployed:
            lines.append(f"Files: {', '.join(self.files_deployed)}")
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.rolled_back:
            lines.append("Rollback: Applied")
        if self.health_check_output:
            lines.append(f"Health: {self.health_check_output[:200]}")

        return "\n".join(lines)


class Deployer:
    """
    Manages deployments to client VMs.

    Takes a ClientConfig, creates SSH/SCP connections, and handles
    the full backup → upload → deploy → verify → rollback cycle.
    """

    def __init__(self, client_config: ClientConfig):
        """
        Initialize deployer for a specific client.

        Args:
            client_config: Client configuration with VM and project details.
        """
        self.config = client_config
        self.ssh = SSHManager(
            host=client_config.vm.host,
            user=client_config.vm.ssh_user,
            ssh_key=client_config.vm.ssh_key,
            port=client_config.vm.port,
        )
        self.scp = FileTransfer(
            host=client_config.vm.host,
            user=client_config.vm.ssh_user,
            ssh_key=client_config.vm.ssh_key,
            port=client_config.vm.port,
        )

    def deploy_files(
        self,
        file_changes: dict[str, str],
        skip_backup: bool = False,
    ) -> DeployResult:
        """
        Deploy changed files to the client's VM.

        Args:
            file_changes: Dict mapping relative file paths (within repo)
                          to their new content.
            skip_backup: Skip backing up existing files (not recommended).

        Returns:
            DeployResult with status and details.
        """
        result = DeployResult(
            success=False,
            client_id=self.config.client_id,
        )

        repo_path = self.config.project.repo_path

        # Step 1: Test connectivity
        if not self.ssh.test_connection():
            result.error = f"Cannot connect to {self.config.vm.host}"
            return result

        logger.info(f"Deploying {len(file_changes)} files to {self.config.name}")

        # Step 2: Backup current files
        if not skip_backup:
            backup_path = self._backup_files(
                list(file_changes.keys()), repo_path
            )
            if backup_path:
                result.backup_path = backup_path
            else:
                logger.warning("Backup failed, proceeding without backup")

        # Step 3: Upload changed files
        for relative_path, content in file_changes.items():
            remote_path = f"{repo_path}/{relative_path}"

            success = self.scp.upload_content(content, remote_path)
            if success:
                result.files_deployed.append(relative_path)
                logger.info(f"Uploaded: {relative_path}")
            else:
                result.error = f"Failed to upload: {relative_path}"
                # Rollback on upload failure
                if result.backup_path:
                    self._rollback(result.backup_path, repo_path)
                    result.rolled_back = True
                return result

        # Step 4: Run pre-deploy command (if configured)
        if self.config.project.pre_deploy_command:
            pre_result = self.ssh.execute(
                self.config.project.pre_deploy_command,
                cwd=repo_path,
                timeout=120,
            )
            if not pre_result.success:
                result.error = f"Pre-deploy failed: {pre_result.stderr[:300]}"
                if result.backup_path:
                    self._rollback(result.backup_path, repo_path)
                    result.rolled_back = True
                return result

        # Step 5: Restart the service
        deploy_result = self.ssh.execute(
            self.config.project.deploy_command,
            timeout=60,
        )
        result.deploy_output = deploy_result.output

        if not deploy_result.success:
            result.error = f"Deploy command failed: {deploy_result.stderr[:300]}"
            if result.backup_path:
                self._rollback(result.backup_path, repo_path)
                result.rolled_back = True
            return result

        # Step 6: Health check (if configured)
        if self.config.project.health_check_command:
            # Wait a moment for the service to start
            import time
            time.sleep(3)

            health_result = self.ssh.execute(
                self.config.project.health_check_command,
                timeout=15,
            )
            result.health_check_output = health_result.output

            if not health_result.success:
                result.error = f"Health check failed: {health_result.stderr[:300]}"
                if result.backup_path:
                    self._rollback(result.backup_path, repo_path)
                    result.rolled_back = True
                return result

        # Success!
        result.success = True
        logger.info(
            f"Deployment successful for {self.config.name}: "
            f"{len(result.files_deployed)} files"
        )
        return result

    def _backup_files(
        self,
        relative_paths: list[str],
        repo_path: str,
    ) -> Optional[str]:
        """
        Backup existing files before deployment.

        Args:
            relative_paths: Files to backup (relative to repo root).
            repo_path: Repository root on the VM.

        Returns:
            Backup directory path on the VM, or None on failure.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.config.project.backup_dir or f"{repo_path}/../backups"
        backup_path = f"{backup_dir}/{timestamp}"

        # Create backup directory
        result = self.ssh.execute(f"mkdir -p {backup_path}")
        if not result.success:
            logger.error(f"Failed to create backup dir: {result.stderr}")
            return None

        # Copy files to backup
        for rel_path in relative_paths:
            src = f"{repo_path}/{rel_path}"
            # Preserve directory structure in backup
            dest_dir = f"{backup_path}/{Path(rel_path).parent}"
            self.ssh.execute(f"mkdir -p {dest_dir}")
            self.ssh.execute(f"cp {src} {dest_dir}/ 2>/dev/null || true")

        logger.info(f"Backup created: {backup_path}")
        return backup_path

    def _rollback(self, backup_path: str, repo_path: str) -> bool:
        """
        Rollback to backed-up files.

        Args:
            backup_path: Path to backup directory on the VM.
            repo_path: Repository root on the VM.

        Returns:
            True if rollback succeeded.
        """
        logger.warning(f"Rolling back to backup: {backup_path}")

        result = self.ssh.execute(
            f"cp -r {backup_path}/* {repo_path}/",
            timeout=30,
        )

        if result.success:
            # Restart the service after rollback
            self.ssh.execute(self.config.project.deploy_command, timeout=60)
            logger.info("Rollback complete, service restarted")
            return True
        else:
            logger.error(f"Rollback failed: {result.stderr}")
            return False

    def test_connectivity(self) -> dict:
        """
        Test connectivity and gather VM status.

        Returns:
            Dict with connection status, git info, etc.
        """
        status = {
            "host": self.config.vm.host,
            "connected": False,
            "git_status": None,
        }

        if self.ssh.test_connection():
            status["connected"] = True
            status["git_status"] = self.ssh.get_git_status(
                self.config.project.repo_path
            )

        return status
