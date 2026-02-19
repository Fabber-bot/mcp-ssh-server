"""Configuration loader for SSH hosts.

Security model:
- Only hosts declared in the config file are accessible
- Key-based authentication is the default (password auth must be explicitly enabled per host)
- Host key verification uses known_hosts by default (auto-accept is opt-in per host)
"""

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mcp-ssh")


class ConfigError(Exception):
    """Raised when host configuration is invalid."""
    pass


@dataclass
class HostConfig:
    """Configuration for a single SSH host."""
    name: str
    hostname: str
    username: str
    port: int = 22
    identity_file: Optional[str] = None
    password: Optional[str] = None
    auto_accept_host_key: bool = False
    command_timeout: int = 30
    transfer_timeout: int = 120
    allowed_commands: Optional[list[str]] = None  # None = all allowed

    def __post_init__(self):
        if not self.identity_file and not self.password:
            raise ConfigError(
                f"Host '{self.name}': must specify either 'identity_file' or 'password'"
            )
        if self.identity_file:
            expanded = os.path.expanduser(self.identity_file)
            if not os.path.isfile(expanded):
                logger.warning(f"Identity file not found: {expanded} (host: {self.name})")
            self.identity_file = expanded
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"Host '{self.name}': invalid port {self.port}")
        if self.command_timeout < 1:
            raise ConfigError(f"Host '{self.name}': command_timeout must be >= 1")
        if self.transfer_timeout < 1:
            raise ConfigError(f"Host '{self.name}': transfer_timeout must be >= 1")
        if self.allowed_commands is not None:
            if not isinstance(self.allowed_commands, list):
                raise ConfigError(
                    f"Host '{self.name}': 'allowed_commands' must be a list, "
                    f"got {type(self.allowed_commands).__name__}"
                )
            if len(self.allowed_commands) == 0:
                raise ConfigError(
                    f"Host '{self.name}': allowed_commands is empty (blocks all commands). "
                    f"Use null/omit to allow all commands, or list specific commands."
                )
            for j, cmd in enumerate(self.allowed_commands):
                if not isinstance(cmd, str) or not cmd:
                    raise ConfigError(
                        f"Host '{self.name}': allowed_commands[{j}] must be a non-empty string"
                    )


@dataclass
class ServerConfig:
    """Top-level server configuration."""
    hosts: dict[str, HostConfig] = field(default_factory=dict)
    log_level: str = "INFO"
    audit_log_file: Optional[str] = None

    def get_host(self, name: str) -> HostConfig:
        if name not in self.hosts:
            available = ", ".join(self.hosts.keys()) or "(none)"
            raise ConfigError(
                f"Host '{name}' is not in the allowlist. Available: {available}"
            )
        return self.hosts[name]


def _check_file_permissions(path: Path) -> None:
    """Warn if config file is readable by group or others (Unix only)."""
    try:
        file_stat = path.stat()
        mode = file_stat.st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(
                f"Config file {path} is readable by group/others. "
                f"This is a security risk if it contains passwords. "
                f"Run: chmod 600 {path}"
            )
    except (OSError, AttributeError):
        # Windows doesn't have the same permission model — skip check
        pass


def load_config(config_path: str) -> ServerConfig:
    """Load and validate server configuration from a JSON file."""
    path = Path(config_path).expanduser().resolve()

    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    _check_file_permissions(path)

    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file: {e}")

    # Parse top-level settings
    log_level = data.get("log_level", "INFO")
    if not isinstance(log_level, str):
        raise ConfigError(f"'log_level' must be a string, got {type(log_level).__name__}")

    audit_log_file = data.get("audit_log_file")
    if audit_log_file is not None and not isinstance(audit_log_file, str):
        raise ConfigError(
            f"'audit_log_file' must be a string path or null, "
            f"got {type(audit_log_file).__name__}"
        )

    # Parse hosts
    raw_hosts = data.get("hosts", [])
    if not isinstance(raw_hosts, list):
        raise ConfigError("'hosts' must be a list")

    hosts = {}
    seen_names = set()

    for i, raw in enumerate(raw_hosts):
        ctx = f"hosts[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{ctx}: each host entry must be a JSON object, got {type(raw).__name__}")

        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ConfigError(f"{ctx}: missing or invalid 'name'")
        if name in seen_names:
            raise ConfigError(f"{ctx}: duplicate host name '{name}'")
        seen_names.add(name)

        hostname = raw.get("hostname")
        if not hostname or not isinstance(hostname, str):
            raise ConfigError(f"{ctx}: missing or invalid 'hostname'")

        username = raw.get("username")
        if not username or not isinstance(username, str):
            raise ConfigError(f"{ctx}: missing or invalid 'username'")

        try:
            host = HostConfig(
                name=name,
                hostname=hostname,
                username=username,
                port=raw.get("port", 22),
                identity_file=raw.get("identity_file"),
                password=raw.get("password"),
                auto_accept_host_key=raw.get("auto_accept_host_key", False),
                command_timeout=raw.get("command_timeout", 30),
                transfer_timeout=raw.get("transfer_timeout", 120),
                allowed_commands=raw.get("allowed_commands"),
            )
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(f"{ctx}: {e}")

        hosts[name] = host

    config = ServerConfig(
        hosts=hosts,
        log_level=log_level,
        audit_log_file=audit_log_file,
    )
    logger.info(f"Loaded {len(hosts)} host(s) from {path}")
    return config
