"""SSH client wrapper with security controls.

Uses paramiko for persistent connections and SFTP for file transfers.
All operations are guarded by the host allowlist from config.

Threading model: Each SSHConnection uses a single RLock that is held during
all operations (connect, execute, upload, download). This is intentional —
paramiko's SSHClient is not thread-safe for concurrent operations on the
same transport. The lock is held for the full duration of each operation
(not just a single read), so concurrent tool calls targeting the same host
will queue behind the running operation.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import paramiko

from .config import HostConfig, ServerConfig

logger = logging.getLogger("mcp-ssh")

# Shell metacharacters that can chain commands, substitute, or redirect.
# These are checked ONLY for hosts with an allowed_commands list.
# Note: OpenSSH wraps exec_command in /bin/sh -c "...", so all these
# characters have shell significance.
# Excluded: ! (only in interactive bash), {} (brace expansion, not execution)
# Includes quotes to prevent subcommand injection (e.g. docker exec ... sh -c '...')
_SHELL_META_RE = re.compile(r"[;&|`$()<>\n\"']")


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class CommandResult:
    """Result of a remote command execution."""
    command: str
    exit_code: int
    stdout: str
    stderr: str
    host: str
    started_at: str
    ended_at: str
    duration_ms: int


class SSHConnection:
    """Manages a single SSH connection with health monitoring.

    Thread safety: uses a single RLock (reentrant) so that methods like
    _ensure_connected_locked can call connect() without deadlocking.
    The lock is held during all blocking I/O to prevent concurrent paramiko
    operations on the same transport (which is not thread-safe).
    """

    def __init__(self, host_config: HostConfig):
        self.config = host_config
        self.state = ConnectionState.DISCONNECTED
        self._client: Optional[paramiko.SSHClient] = None
        self._lock = threading.RLock()  # Reentrant to avoid self-deadlock
        self._last_used: Optional[float] = None

    def connect(self) -> None:
        with self._lock:
            if self.state == ConnectionState.CONNECTED and self._is_alive():
                return

            logger.info(f"Connecting to {self.config.name} ({self.config.hostname}:{self.config.port})")
            self.state = ConnectionState.CONNECTING

            client = paramiko.SSHClient()
            try:
                # Host key policy: secure by default
                if self.config.auto_accept_host_key:
                    logger.warning(f"Auto-accepting host keys for {self.config.name} (MITM risk)")
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                else:
                    client.load_system_host_keys()
                    client.set_missing_host_key_policy(paramiko.RejectPolicy())

                connect_kwargs = {
                    "hostname": self.config.hostname,
                    "port": self.config.port,
                    "username": self.config.username,
                    "timeout": 15,
                    "banner_timeout": 15,
                    "auth_timeout": 15,
                }

                if self.config.identity_file:
                    connect_kwargs["key_filename"] = self.config.identity_file
                    connect_kwargs["look_for_keys"] = False
                elif self.config.password:
                    connect_kwargs["password"] = self.config.password
                    connect_kwargs["look_for_keys"] = False

                client.connect(**connect_kwargs)

                # Close any previous client to prevent leaks
                if self._client:
                    try:
                        self._client.close()
                    except Exception:
                        pass

                self._client = client
                self.state = ConnectionState.CONNECTED
                self._last_used = time.monotonic()
                logger.info(f"Connected to {self.config.name}")

            except Exception as e:
                # Defensive close — paramiko usually cleans up internally on
                # connect failure, but explicit close ensures no edge cases.
                try:
                    client.close()
                except Exception:
                    pass
                self.state = ConnectionState.ERROR
                logger.error(f"Failed to connect to {self.config.name}: {e}")
                raise

    def disconnect(self) -> None:
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self.state = ConnectionState.DISCONNECTED
            logger.info(f"Disconnected from {self.config.name}")

    def execute(self, command: str) -> CommandResult:
        """Execute a command on the remote host.

        The command allowlist (if configured) rejects commands containing
        shell metacharacters to prevent chaining bypasses like 'ls; rm -rf /'.
        OpenSSH wraps exec_command in /bin/sh -c, so these characters are
        genuinely dangerous.
        """
        # Check command allowlist — done outside lock (read-only config)
        if self.config.allowed_commands is not None:
            cmd_base = command.split()[0] if command.strip() else ""
            # Reject shell metacharacters that could chain/redirect commands
            if _SHELL_META_RE.search(command):
                raise PermissionError(
                    f"Command contains shell metacharacters (rejected for host "
                    f"'{self.config.name}' which has an allowlist). "
                    f"Send each command separately without pipes or chaining."
                )
            if cmd_base not in self.config.allowed_commands:
                allowed = ", ".join(self.config.allowed_commands)
                raise PermissionError(
                    f"Command '{cmd_base}' not in allowlist for {self.config.name}. "
                    f"Allowed: {allowed}"
                )

        started_at = datetime.now(timezone.utc)

        with self._lock:
            self._ensure_connected_locked()
            try:
                _, stdout, stderr = self._client.exec_command(
                    command,
                    timeout=self.config.command_timeout,
                )
                # Read stdout and stderr CONCURRENTLY to prevent deadlock.
                # SSH channels have ~64KB flow-control windows. If a command
                # fills stderr before closing stdout (or vice versa), sequential
                # reads deadlock: stdout.read() blocks waiting for EOF, while
                # the remote process blocks trying to write more stderr.
                # Sub-threads only touch channel-local objects (no lock needed).
                # Exceptions are captured and re-raised on the main thread.
                out_buf: list[bytes] = []
                err_buf: list[bytes] = []
                out_exc: list[BaseException] = []
                err_exc: list[BaseException] = []

                def _read_stdout():
                    try:
                        out_buf.append(stdout.read())
                    except Exception as exc:
                        out_exc.append(exc)

                def _read_stderr():
                    try:
                        err_buf.append(stderr.read())
                    except Exception as exc:
                        err_exc.append(exc)

                t_out = threading.Thread(target=_read_stdout, daemon=True)
                t_err = threading.Thread(target=_read_stderr, daemon=True)
                t_out.start()
                t_err.start()

                # Bounded join: if the command produces no output for
                # command_timeout seconds, the per-recv timeout in the
                # reader thread will fire and unblock the join.  The
                # extra buffer here covers the gap between the last
                # recv timeout and thread teardown.
                join_deadline = self.config.command_timeout + 5
                t_out.join(timeout=join_deadline)
                t_err.join(timeout=join_deadline)

                if t_out.is_alive() or t_err.is_alive():
                    # Reader threads stuck — kill the channel so they unblock
                    stdout.channel.close()
                    raise TimeoutError(
                        f"Command timed out on '{self.config.name}' "
                        f"(no output for {self.config.command_timeout}s)"
                    )

                # Propagate thread exceptions with the real root cause
                if out_exc:
                    raise out_exc[0]
                if err_exc:
                    raise err_exc[0]

                out = out_buf[0].decode("utf-8", errors="replace")
                err = err_buf[0].decode("utf-8", errors="replace")
                exit_code = stdout.channel.recv_exit_status()
                self._last_used = time.monotonic()
            except paramiko.SSHException as e:
                self.state = ConnectionState.ERROR
                logger.error(f"Transport error on {self.config.name}: {e}")
                raise RuntimeError(
                    f"Command execution failed on '{self.config.name}'"
                )
            except Exception as e:
                # Non-transport error (timeout, decode, etc.) — connection
                # may still be alive, don't force a reconnect.
                logger.error(f"Command failed on {self.config.name}: {e}")
                raise RuntimeError(
                    f"Command execution failed on '{self.config.name}'"
                )

        ended_at = datetime.now(timezone.utc)
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)

        result = CommandResult(
            command=command,
            exit_code=exit_code,
            stdout=out,
            stderr=err,
            host=self.config.name,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            duration_ms=duration_ms,
        )

        logger.info(
            f"[{self.config.name}] exit={exit_code} duration={duration_ms}ms cmd={command[:80]}"
        )
        return result

    def upload(self, local_path: str, remote_path: str) -> dict:
        """Upload a file via SFTP."""
        local_path = os.path.expanduser(local_path)
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Local file not found: {local_path}")

        file_size = os.path.getsize(local_path)

        with self._lock:
            self._ensure_connected_locked()
            try:
                sftp = self._client.open_sftp()
                try:
                    sftp.get_channel().settimeout(self.config.transfer_timeout)
                    sftp.put(local_path, remote_path)
                finally:
                    sftp.close()
                self._last_used = time.monotonic()
            except paramiko.SSHException as e:
                self.state = ConnectionState.ERROR
                logger.error(f"Upload transport error on {self.config.name}: {e}")
                raise RuntimeError(
                    f"Upload failed to '{self.config.name}': {remote_path}"
                )
            except Exception as e:
                # SFTP-level error (permissions, disk full, etc.) —
                # connection itself may still be alive.
                logger.error(f"Upload failed to {self.config.name}: {e}")
                raise RuntimeError(
                    f"Upload failed to '{self.config.name}': {remote_path}"
                )

        logger.info(f"[{self.config.name}] uploaded {local_path} -> {remote_path} ({file_size} bytes)")
        return {
            "success": True,
            "host": self.config.name,
            "local_path": local_path,
            "remote_path": remote_path,
            "bytes": file_size,
        }

    def download(self, remote_path: str, local_path: str) -> dict:
        """Download a file via SFTP."""
        local_path = os.path.expanduser(local_path)
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.isdir(local_dir):
            os.makedirs(local_dir, exist_ok=True)

        with self._lock:
            self._ensure_connected_locked()
            try:
                sftp = self._client.open_sftp()
                try:
                    sftp.get_channel().settimeout(self.config.transfer_timeout)
                    sftp.get(remote_path, local_path)
                finally:
                    sftp.close()
                self._last_used = time.monotonic()
            except paramiko.SSHException as e:
                # Clean up partial download file
                try:
                    if os.path.exists(local_path):
                        os.unlink(local_path)
                        logger.warning(f"Cleaned up partial download: {local_path}")
                except OSError:
                    pass
                self.state = ConnectionState.ERROR
                logger.error(f"Download transport error from {self.config.name}: {e}")
                raise RuntimeError(
                    f"Download failed from '{self.config.name}': {remote_path}"
                )
            except Exception as e:
                # SFTP-level error — clean up but don't mark connection as dead
                try:
                    if os.path.exists(local_path):
                        os.unlink(local_path)
                        logger.warning(f"Cleaned up partial download: {local_path}")
                except OSError:
                    pass
                logger.error(f"Download failed from {self.config.name}: {e}")
                raise RuntimeError(
                    f"Download failed from '{self.config.name}': {remote_path}"
                )

        file_size = os.path.getsize(local_path)
        logger.info(f"[{self.config.name}] downloaded {remote_path} -> {local_path} ({file_size} bytes)")
        return {
            "success": True,
            "host": self.config.name,
            "remote_path": remote_path,
            "local_path": local_path,
            "bytes": file_size,
        }

    def is_connected(self) -> bool:
        with self._lock:
            return self._check_alive_and_sync_state()

    def status(self) -> dict:
        with self._lock:
            connected = self._check_alive_and_sync_state()
            idle_seconds = None
            if self._last_used is not None:
                idle_seconds = round(time.monotonic() - self._last_used, 1)
            return {
                "name": self.config.name,
                "hostname": self.config.hostname,
                "port": self.config.port,
                "username": self.config.username,
                "state": self.state.value,
                "connected": connected,
                "idle_seconds": idle_seconds,
            }

    def _check_alive_and_sync_state(self) -> bool:
        """Check if alive and sync state accordingly. Must hold _lock."""
        alive = self._is_alive()
        if self.state == ConnectionState.CONNECTED and not alive:
            self.state = ConnectionState.ERROR
            logger.warning(f"Transport died for {self.config.name}, state -> ERROR")
        return self.state == ConnectionState.CONNECTED and alive

    def _is_alive(self) -> bool:
        """Check if the underlying SSH transport is active. Must hold _lock."""
        if not self._client:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def _ensure_connected_locked(self) -> None:
        """Check connection and reconnect if needed. Must be called with _lock held."""
        if not self._check_alive_and_sync_state():
            self.connect()


class SSHManager:
    """Manages all SSH connections according to server config."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self._connections: dict[str, SSHConnection] = {}
        self._lock = threading.Lock()

    def get_connection(self, host_name: str) -> SSHConnection:
        """Get or create a connection for a configured host."""
        host_config = self.config.get_host(host_name)

        with self._lock:
            if host_name not in self._connections:
                self._connections[host_name] = SSHConnection(host_config)
            return self._connections[host_name]

    def list_hosts(self) -> list[dict]:
        """List all configured hosts with their connection status."""
        # Snapshot connections under manager lock
        with self._lock:
            conn_snapshot = dict(self._connections)

        hosts = []
        for name, host_config in self.config.hosts.items():
            info = {
                "name": name,
                "hostname": host_config.hostname,
                "port": host_config.port,
                "username": host_config.username,
                "has_key": host_config.identity_file is not None,
                "command_timeout": host_config.command_timeout,
            }
            conn = conn_snapshot.get(name)
            if conn:
                # Use status() which holds conn._lock for both reads,
                # avoiding a race between state and connected values
                st = conn.status()
                info.update({"state": st["state"], "connected": st["connected"]})
            else:
                info.update({"state": "disconnected", "connected": False})

            if host_config.allowed_commands:
                info["allowed_commands"] = host_config.allowed_commands

            hosts.append(info)
        return hosts

    def disconnect_all(self) -> None:
        """Disconnect all active connections."""
        # Snapshot and clear under manager lock, then disconnect outside it
        # to avoid holding both manager lock and connection lock simultaneously
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()

        for conn in connections:
            try:
                conn.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting {conn.config.name}: {e}")

        logger.info("All connections closed")
