"""
Client context management for Datacore's multi-client operations.

Loads per-client YAML configs that map business context, infrastructure,
and communication channels to enable the FDA agent to route tasks correctly.
"""

import yaml
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field


@dataclass
class VMConfig:
    """Azure VM connection details for a client."""
    host: str
    ssh_user: str
    ssh_key: str
    port: int = 22

    def ssh_key_path(self) -> Path:
        """Return expanded SSH key path."""
        return Path(self.ssh_key).expanduser()


@dataclass
class DatabaseConfig:
    """PostgreSQL database details for a client."""
    type: str  # "postgresql"
    host: str  # usually "localhost" when accessed via SSH tunnel
    port: int
    name: str
    user: Optional[str] = None
    password_env: Optional[str] = None  # env var name, never store passwords in config


@dataclass
class ProjectConfig:
    """Project/repository details on the VM."""
    repo_path: str          # absolute path on the VM
    local_mirror: str       # local path on Mac Mini for fast code reading
    tech_stack: str          # e.g. "Python/FastAPI/PostgreSQL"
    deploy_command: str      # command to restart the service after deployment
    pre_deploy_command: Optional[str] = None  # e.g. "pip install -r requirements.txt"
    health_check_command: Optional[str] = None  # e.g. "curl -s localhost:8000/health"
    backup_dir: Optional[str] = None  # where to backup files before deploy
    extra_repo_paths: list[str] = field(default_factory=list)  # additional paths to scan (e.g. ["~/airflow"])


@dataclass
class ClientConfig:
    """
    Complete client configuration.

    Maps a client to their KakaoTalk chat room, Azure VM, database,
    codebase, and business context.
    """
    # Identity
    client_id: str
    name: str
    company: str

    # Communication
    kakaotalk_room: str  # chat room name used for routing messages

    # Infrastructure
    vm: VMConfig
    database: DatabaseConfig
    project: ProjectConfig

    # Business context - this is what makes the agent useful
    business_context: str  # free-form description of the client's business
    key_contacts: dict[str, str] = field(default_factory=dict)  # name -> role
    common_requests: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_yaml(cls, path: Path) -> "ClientConfig":
        """
        Load client config from a YAML file.

        Args:
            path: Path to the YAML config file.

        Returns:
            ClientConfig instance.

        Raises:
            FileNotFoundError: If the config file doesn't exist.
            ValueError: If required fields are missing.
        """
        if not path.exists():
            raise FileNotFoundError(f"Client config not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty client config: {path}")

        client = data.get("client", {})
        infra = data.get("infrastructure", {})
        project = data.get("project", {})

        # Build VM config
        vm_data = infra.get("vm", {})
        vm = VMConfig(
            host=vm_data["host"],
            ssh_user=vm_data.get("ssh_user", "deploy"),
            ssh_key=vm_data.get("ssh_key", "~/.ssh/id_rsa"),
            port=vm_data.get("port", 22),
        )

        # Build database config
        db_data = infra.get("database", {})
        database = DatabaseConfig(
            type=db_data.get("type", "postgresql"),
            host=db_data.get("host", "localhost"),
            port=db_data.get("port", 5432),
            name=db_data["name"],
            user=db_data.get("user"),
            password_env=db_data.get("password_env"),
        )

        # Build project config
        proj = ProjectConfig(
            repo_path=project["repo_path"],
            local_mirror=project.get("local_mirror", ""),
            tech_stack=project.get("tech_stack", ""),
            deploy_command=project["deploy_command"],
            pre_deploy_command=project.get("pre_deploy_command"),
            health_check_command=project.get("health_check_command"),
            backup_dir=project.get("backup_dir"),
            extra_repo_paths=project.get("extra_repo_paths", []),
        )

        return cls(
            client_id=client.get("id", path.stem),
            name=client["name"],
            company=client.get("company", ""),
            kakaotalk_room=client["kakaotalk_room"],
            vm=vm,
            database=database,
            project=proj,
            business_context=data.get("business_context", ""),
            key_contacts=data.get("key_contacts", {}),
            common_requests=data.get("common_requests", []),
            notes=data.get("notes", ""),
        )

    def get_context_for_prompt(self) -> str:
        """
        Format client context for inclusion in a Claude prompt.

        Returns a structured string that gives the agent full business
        and technical context for this client.
        """
        contacts_str = "\n".join(
            f"  - {name}: {role}" for name, role in self.key_contacts.items()
        ) if self.key_contacts else "  None specified"

        requests_str = "\n".join(
            f"  - {req}" for req in self.common_requests
        ) if self.common_requests else "  None specified"

        return f"""=== Client: {self.name} ({self.company}) ===

Business Context:
{self.business_context}

Key Contacts:
{contacts_str}

Common Request Types:
{requests_str}

Technical Setup:
  - VM: {self.vm.host} (user: {self.vm.ssh_user})
  - Database: {self.database.type} ({self.database.name})
  - Tech Stack: {self.project.tech_stack}
  - Repo Path (on VM): {self.project.repo_path}{f"{chr(10)}  - Additional paths: {', '.join(self.project.extra_repo_paths)}" if self.project.extra_repo_paths else ""}
  - Deploy Command: {self.project.deploy_command}

Notes:
{self.notes or '  None'}
"""


class ClientManager:
    """
    Manages multiple client configurations.

    Loads all client YAML files from the configs directory and provides
    lookup by client ID, name, or KakaoTalk room name.
    """

    def __init__(self, configs_dir: Optional[Path] = None):
        """
        Initialize the client manager.

        Args:
            configs_dir: Directory containing client YAML files.
                         Defaults to fda/clients/configs/
        """
        if configs_dir is None:
            configs_dir = Path(__file__).parent / "configs"

        self.configs_dir = configs_dir
        self.clients: dict[str, ClientConfig] = {}
        self._room_to_client: dict[str, str] = {}

        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _load_all(self) -> None:
        """Load all client configs from the configs directory."""
        self.clients.clear()
        self._room_to_client.clear()

        for yaml_file in self.configs_dir.glob("*.yaml"):
            try:
                config = ClientConfig.from_yaml(yaml_file)
                self.clients[config.client_id] = config
                self._room_to_client[config.kakaotalk_room] = config.client_id
            except Exception as e:
                print(f"Warning: Failed to load client config {yaml_file}: {e}")

        for yml_file in self.configs_dir.glob("*.yml"):
            try:
                config = ClientConfig.from_yaml(yml_file)
                if config.client_id not in self.clients:
                    self.clients[config.client_id] = config
                    self._room_to_client[config.kakaotalk_room] = config.client_id
            except Exception as e:
                print(f"Warning: Failed to load client config {yml_file}: {e}")

    def reload(self) -> None:
        """Reload all client configs from disk."""
        self._load_all()

    def get_client(self, client_id: str) -> Optional[ClientConfig]:
        """Get a client config by ID."""
        return self.clients.get(client_id)

    def get_client_by_room(self, room_name: str) -> Optional[ClientConfig]:
        """
        Get a client config by KakaoTalk room name.

        This is the primary lookup used when processing incoming messages.
        """
        client_id = self._room_to_client.get(room_name)
        if client_id:
            return self.clients.get(client_id)
        return None

    def get_client_by_name(self, name: str) -> Optional[ClientConfig]:
        """Get a client config by client name (case-insensitive)."""
        name_lower = name.lower()
        for config in self.clients.values():
            if config.name.lower() == name_lower:
                return config
        return None

    def list_clients(self) -> list[ClientConfig]:
        """Get all client configs."""
        return list(self.clients.values())

    def get_all_room_names(self) -> list[str]:
        """Get all registered KakaoTalk room names."""
        return list(self._room_to_client.keys())
