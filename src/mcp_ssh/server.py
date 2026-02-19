"""MCP SSH Server — exposes SSH tools via Model Context Protocol.

Usage:
    python -m mcp_ssh --config hosts.json
    python -m mcp_ssh                       # looks for ./hosts.json or ~/.ssh/mcp-hosts.json
"""

import atexit
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from fastmcp import FastMCP

from .config import ConfigError, load_config
from .ssh_client import SSHManager

logger = logging.getLogger("mcp-ssh")

# ---------------------------------------------------------------------------
# Globals set during startup
# ---------------------------------------------------------------------------
_manager: Optional[SSHManager] = None
_audit_file: Optional[str] = None
_audit_lock = threading.Lock()


def _get_manager() -> SSHManager:
    """Get the initialized manager or raise a clear error."""
    if _manager is None:
        raise RuntimeError("SSH server not initialized — call run() first")
    return _manager


def _audit(action: str, host: str, detail: str = "") -> None:
    """Write a structured audit log entry (thread-safe)."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "host": host,
        "detail": detail[:500],
    }
    logger.info(f"AUDIT: {json.dumps(entry)}")
    if _audit_file:
        with _audit_lock:
            try:
                with open(_audit_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                logger.warning(f"Failed to write audit log: {e}")


# ---------------------------------------------------------------------------
# MCP Server and Tools
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="mcp-ssh",
    instructions=(
        "SSH server for remote command execution and file transfer. "
        "Use list_hosts to see available hosts before connecting. "
        "All hosts must be pre-configured in the hosts.json config file."
    ),
)


@mcp.tool
def list_hosts() -> list[dict]:
    """List all configured SSH hosts and their connection status.

    Returns a list of hosts with name, hostname, port, username,
    connection state, and any command restrictions.
    Call this first to discover available hosts.
    """
    return _get_manager().list_hosts()


@mcp.tool
def ssh_execute(host: str, command: str) -> dict:
    """Execute a shell command on a remote SSH host.

    Args:
        host: Name of the configured SSH host (from list_hosts).
        command: The shell command to execute on the remote host.

    Returns:
        Dict with stdout, stderr, exit_code, duration_ms, and timing info.
    """
    _audit("execute", host, command)
    conn = _get_manager().get_connection(host)
    result = conn.execute(command)
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "host": result.host,
        "duration_ms": result.duration_ms,
    }


@mcp.tool
def ssh_execute_batch(host: str, commands: list[str], stop_on_error: bool = True) -> dict:
    """Execute multiple commands sequentially on a remote SSH host.

    Args:
        host: Name of the configured SSH host.
        commands: List of shell commands to execute in order.
        stop_on_error: If True (default), stop executing after the first
            command that fails (non-zero exit code or exception).

    Returns:
        Dict with results list and overall success boolean.
    """
    _audit("execute_batch", host, f"{len(commands)} commands")
    conn = _get_manager().get_connection(host)
    results = []
    success = True

    for cmd in commands:
        _audit("execute", host, cmd)
        try:
            result = conn.execute(cmd)
            results.append({
                "command": cmd,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
            })
            if result.exit_code != 0:
                success = False
                if stop_on_error:
                    break
        except Exception as e:
            logger.error(f"Batch command failed on {host}: {e}")
            results.append({
                "command": cmd,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command execution failed on '{host}'",
                "duration_ms": 0,
            })
            success = False
            if stop_on_error:
                break

    return {"results": results, "success": success}


@mcp.tool
def ssh_upload(host: str, local_path: str, remote_path: str) -> dict:
    """Upload a local file to a remote SSH host via SFTP.

    Args:
        host: Name of the configured SSH host.
        local_path: Path to the local file to upload.
        remote_path: Destination path on the remote host.

    Returns:
        Dict with success status, paths, and byte count.
    """
    _audit("upload", host, f"{local_path} -> {remote_path}")
    conn = _get_manager().get_connection(host)
    return conn.upload(local_path, remote_path)


@mcp.tool
def ssh_download(host: str, remote_path: str, local_path: str) -> dict:
    """Download a file from a remote SSH host via SFTP.

    Args:
        host: Name of the configured SSH host.
        remote_path: Path to the file on the remote host.
        local_path: Local destination path.

    Returns:
        Dict with success status, paths, and byte count.
    """
    _audit("download", host, f"{remote_path} -> {local_path}")
    conn = _get_manager().get_connection(host)
    return conn.download(remote_path, local_path)


@mcp.tool
def ssh_status(host: str) -> dict:
    """Check connectivity and status for a specific SSH host.

    Args:
        host: Name of the configured SSH host.

    Returns:
        Dict with connection state, host details, and connectivity test result.
    """
    _audit("status", host)
    conn = _get_manager().get_connection(host)
    status = conn.status()

    if conn.is_connected():
        try:
            result = conn.execute("echo ok")
            status["connectivity"] = "ok" if result.exit_code == 0 else "degraded"
        except Exception:
            # Re-fetch status to reflect updated state (likely ERROR)
            status = conn.status()
            status["connectivity"] = "failed"
    else:
        _audit("connect", host, "triggered by status check")
        try:
            conn.connect()
            # Re-fetch status after successful connect
            status = conn.status()
            status["connectivity"] = "ok"
        except Exception:
            status["connectivity"] = "failed"

    return status


@mcp.tool
def ssh_disconnect(host: str) -> dict:
    """Disconnect from a specific SSH host.

    Args:
        host: Name of the configured SSH host.

    Returns:
        Dict confirming disconnection.
    """
    _audit("disconnect", host)
    conn = _get_manager().get_connection(host)
    conn.disconnect()
    return {"host": host, "state": "disconnected"}


# ---------------------------------------------------------------------------
# Server Lifecycle
# ---------------------------------------------------------------------------

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _find_config() -> str:
    """Search for config in standard locations."""
    candidates = [
        os.path.abspath("hosts.json"),
        os.path.expanduser("~/.ssh/mcp-hosts.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    searched = "\n  ".join(candidates)
    raise ConfigError(
        f"No config file found. Searched:\n  {searched}\n"
        f"Create one of these files, or pass --config <path>"
    )


def run(config_path: Optional[str] = None) -> None:
    """Initialize and run the MCP SSH server."""
    global _manager, _audit_file

    # Find config
    if not config_path:
        config_path = _find_config()

    # Load config
    config = load_config(config_path)

    # Validate and setup logging
    log_level = config.log_level.upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            f"Invalid log_level '{config.log_level}'. "
            f"Must be one of: {', '.join(sorted(_VALID_LOG_LEVELS))}"
        )

    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    _audit_file = config.audit_log_file
    _manager = SSHManager(config)

    logger.info(f"MCP SSH Server starting with {len(config.hosts)} host(s)")

    # Graceful shutdown via atexit (works regardless of how FastMCP handles signals)
    def shutdown():
        logger.info("Shutting down — closing all connections...")
        if _manager:
            _manager.disconnect_all()

    atexit.register(shutdown)

    # Run MCP server over STDIO
    mcp.run(transport="stdio")
