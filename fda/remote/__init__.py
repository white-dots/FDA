"""Remote execution: SSH, SCP, and deployment to Azure VMs."""

from fda.remote.ssh_manager import SSHManager
from fda.remote.file_transfer import FileTransfer
from fda.remote.deploy import Deployer

__all__ = ["SSHManager", "FileTransfer", "Deployer"]
