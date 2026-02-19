# MCP SSH Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Secure SSH access via the [Model Context Protocol](https://modelcontextprotocol.io/).

## Features

- **Host allowlisting** — only pre-configured hosts are accessible
- **Key-based auth by default** — password auth is opt-in per host
- **Host key verification** — uses `known_hosts` by default (auto-accept is opt-in)
- **Command allowlisting** — optionally restrict which commands can run per host
- **SFTP file transfer** — upload/download files without subprocess
- **Audit logging** — every action logged with timestamps
- **Persistent connections** — reuses SSH sessions across tool calls
- **Health monitoring** — detects and reconnects broken connections

## Quick Start

### 1. Install

```bash
cd mcp-ssh-server
pip install -e .
```

### 2. Configure hosts

Copy and edit the example config:

```bash
cp hosts.example.json hosts.json
```

Edit `hosts.json` with your SSH hosts. Minimum required fields:

```json
{
  "hosts": [
    {
      "name": "my-server",
      "hostname": "192.168.1.100",
      "username": "user",
      "identity_file": "~/.ssh/id_rsa"
    }
  ]
}
```

> **Note:** JSON does not support comments. All fields in the config are parsed;
> unknown fields are silently ignored. Use the field reference below as your guide.

> **Security:** If your config contains passwords, restrict file permissions:
> `chmod 600 hosts.json`

### 3. Add to your MCP client

Add to your MCP client settings (e.g. `settings.json`):

```json
{
  "mcpServers": {
    "ssh": {
      "command": "python",
      "args": ["-m", "mcp_ssh", "--config", "/path/to/hosts.json"]
    }
  }
}
```

Or if installed globally:

```json
{
  "mcpServers": {
    "ssh": {
      "command": "mcp-ssh",
      "args": ["--config", "/path/to/hosts.json"]
    }
  }
}
```

> **Important:** Always use `--config` with an absolute path. Without it, the
> server looks for `hosts.json` in the current working directory (which may not
> be where you expect when launched by an MCP client).

### 4. Use it

Your MCP client will automatically discover the SSH tools. Example prompts:

- "List my SSH hosts"
- "Run `nvidia-smi` on runpod"
- "Upload `train.py` to runpod at `/workspace/train.py`"
- "Check the disk space on my production server"

## Tools

| Tool | Description |
|------|-------------|
| `list_hosts` | List all configured hosts and their connection status |
| `ssh_execute` | Run a single command on a remote host |
| `ssh_execute_batch` | Run multiple commands sequentially (stops on error by default) |
| `ssh_upload` | Upload a file via SFTP |
| `ssh_download` | Download a file via SFTP (cleans up partial files on failure) |
| `ssh_status` | Check connectivity for a host (connects if needed) |
| `ssh_disconnect` | Close a connection |

## Configuration

### Host Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Unique identifier for the host |
| `hostname` | string | required | IP address or domain |
| `username` | string | required | SSH username |
| `port` | int | 22 | SSH port |
| `identity_file` | string | null | Path to SSH private key |
| `password` | string | null | SSH password (less secure) |
| `auto_accept_host_key` | bool | false | Accept unknown host keys (**MITM risk** — only for dynamic-IP cloud instances) |
| `command_timeout` | int | 30 | Per-read inactivity timeout in seconds for command I/O (see note below) |
| `transfer_timeout` | int | 120 | Per-read inactivity timeout in seconds for SFTP transfers (see note below) |
| `allowed_commands` | list | null | Restrict to these command prefixes (must be non-empty if set) |

> **Timeout behavior:** Both `command_timeout` and `transfer_timeout` are
> *inactivity* timeouts — they trigger if no data arrives for that many seconds
> in a single read. A slow but steady stream of data will not trigger the
> timeout. They are NOT wall-clock deadlines on total operation duration.

### Server Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `log_level` | string | "INFO" | One of: DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `audit_log_file` | string | null | Path to append audit logs (JSONL format, grows unbounded — set up log rotation externally) |

## Security Model

1. **Allowlisted hosts only** — the LLM cannot connect to arbitrary machines
2. **Remote shell execution** — commands are sent via SSH `exec` channel; most SSH servers (OpenSSH) wrap them in `/bin/sh -c`, so shell features like pipes work on unrestricted hosts
3. **Shell metacharacter blocking** — for hosts with `allowed_commands`, shell metacharacters (`;`, `&`, `|`, `` ` ``, `$`, `()`, `<>`, quotes, newlines) are rejected to prevent command chaining and subcommand injection. **Note:** the allowlist checks the first word of the command only. Commands like `docker`, `kubectl`, or `systemctl` that accept arbitrary subcommands as positional arguments are not safe to allowlist alone — they can still invoke arbitrary code through their own subcommand mechanisms even without shell metacharacters
4. **Host key verification** — `RejectPolicy` by default; requires hosts in `known_hosts`
5. **Audit trail** — every action (status check, connect, execute, upload, download, disconnect) is logged
6. **No credential leakage** — passwords/keys are never returned in tool responses; paramiko errors are logged to stderr only, not exposed to the LLM
7. **Partial file cleanup** — failed downloads are cleaned up from the local filesystem

## Architecture

```
MCP Client  <--STDIO-->  FastMCP Server  <--Paramiko-->  Remote Hosts
                              |
                         hosts.json (allowlist)
```

Built on:
- [FastMCP](https://github.com/jlowin/fastmcp) — Python MCP framework
- [Paramiko](https://www.paramiko.org/) — Python SSH library
