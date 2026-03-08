"""
Daemon installation for FDA — launchd (macOS) and systemd (Linux).

Provides install/start/stop/uninstall/status operations for running FDA
as a persistent background service.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fda.config import FDA_DAEMON_LABEL, FDA_SYSTEMD_NAME

logger = logging.getLogger(__name__)


def get_fda_executable() -> str:
    """
    Find the fda CLI executable path.

    Returns:
        Path to the fda executable, or a ``python -m fda.cli`` fallback.
    """
    import shutil

    fda_path = shutil.which("fda")
    if fda_path:
        return fda_path
    # Fallback: use python -m fda.cli
    return f"{sys.executable} -m fda.cli"


def install_daemon(verbose: bool = False) -> bool:
    """
    Install FDA as a background service for the current platform.

    Args:
        verbose: Print file paths and details.

    Returns:
        True if installation succeeded.
    """
    if sys.platform == "darwin":
        return _install_launchd(verbose)
    elif sys.platform.startswith("linux"):
        return _install_systemd(verbose)
    else:
        print(f"  Daemon installation not supported on {sys.platform}")
        return False


# ------------------------------------------------------------------
# macOS — launchd
# ------------------------------------------------------------------


def _install_launchd(verbose: bool) -> bool:
    """Install a launchd plist on macOS."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{FDA_DAEMON_LABEL}.plist"

    fda_exec = get_fda_executable()
    log_dir = Path.home() / "Library" / "Logs" / "fda"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Build ProgramArguments array
    if " -m " in fda_exec:
        parts = fda_exec.split()
        program = parts[0]
        args_list = parts[1:] + ["start"]
    else:
        program = fda_exec
        args_list = ["start"]

    all_args = [program] + args_list
    program_args = "".join(
        f"\n        <string>{a}</string>" for a in all_args
    )

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{FDA_DAEMON_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir / "fda.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / "fda.err.log"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}</string>
    </dict>
</dict>
</plist>"""

    plist_path.write_text(plist_content)
    if verbose:
        print(f"  Wrote plist to {plist_path}")
    logger.info(f"Installed launchd plist at {plist_path}")
    return True


# ------------------------------------------------------------------
# Linux — systemd
# ------------------------------------------------------------------


def _install_systemd(verbose: bool) -> bool:
    """Install a systemd user service on Linux."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / f"{FDA_SYSTEMD_NAME}.service"

    fda_exec = get_fda_executable()

    unit_content = f"""[Unit]
Description=FDA Multi-Agent System
After=network.target

[Service]
Type=simple
ExecStart={fda_exec} start
Restart=on-failure
RestartSec=10
Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

[Install]
WantedBy=default.target
"""

    unit_path.write_text(unit_content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if verbose:
        print(f"  Wrote systemd unit to {unit_path}")
    logger.info(f"Installed systemd unit at {unit_path}")
    return True


# ------------------------------------------------------------------
# Start / Stop / Uninstall / Status
# ------------------------------------------------------------------


def start_daemon() -> bool:
    """Start the FDA daemon service."""
    if sys.platform == "darwin":
        plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{FDA_DAEMON_LABEL}.plist"
        )
        if not plist_path.exists():
            print("Daemon not installed. Run: fda onboard")
            return False
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
        )
        return result.returncode == 0
    elif sys.platform.startswith("linux"):
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", FDA_SYSTEMD_NAME],
            capture_output=True,
        )
        return result.returncode == 0
    else:
        print(f"Daemon not supported on {sys.platform}")
        return False


def stop_daemon() -> bool:
    """Stop the FDA daemon service."""
    if sys.platform == "darwin":
        plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{FDA_DAEMON_LABEL}.plist"
        )
        if not plist_path.exists():
            return False
        result = subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
        )
        return result.returncode == 0
    elif sys.platform.startswith("linux"):
        result = subprocess.run(
            ["systemctl", "--user", "stop", FDA_SYSTEMD_NAME],
            capture_output=True,
        )
        return result.returncode == 0
    else:
        return False


def uninstall_daemon() -> bool:
    """Remove the FDA daemon service entirely."""
    stop_daemon()

    if sys.platform == "darwin":
        plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{FDA_DAEMON_LABEL}.plist"
        )
        if plist_path.exists():
            plist_path.unlink()
            logger.info(f"Removed launchd plist: {plist_path}")
            return True
    elif sys.platform.startswith("linux"):
        unit_path = (
            Path.home()
            / ".config"
            / "systemd"
            / "user"
            / f"{FDA_SYSTEMD_NAME}.service"
        )
        if unit_path.exists():
            unit_path.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            logger.info(f"Removed systemd unit: {unit_path}")
            return True

    return False


def daemon_status() -> dict[str, Any]:
    """
    Check whether the FDA daemon is installed and running.

    Returns:
        Dict with keys ``installed`` (bool) and ``running`` (bool).
    """
    if sys.platform == "darwin":
        plist_path = (
            Path.home() / "Library" / "LaunchAgents" / f"{FDA_DAEMON_LABEL}.plist"
        )
        if not plist_path.exists():
            return {"installed": False, "running": False}
        result = subprocess.run(
            ["launchctl", "list", FDA_DAEMON_LABEL],
            capture_output=True,
            text=True,
        )
        return {"installed": True, "running": result.returncode == 0}
    elif sys.platform.startswith("linux"):
        unit_path = (
            Path.home()
            / ".config"
            / "systemd"
            / "user"
            / f"{FDA_SYSTEMD_NAME}.service"
        )
        result = subprocess.run(
            ["systemctl", "--user", "is-active", FDA_SYSTEMD_NAME],
            capture_output=True,
            text=True,
        )
        return {
            "installed": unit_path.exists(),
            "running": result.stdout.strip() == "active",
        }
    else:
        return {"installed": False, "running": False}
