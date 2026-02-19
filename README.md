# MCP SSH Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Lets your AI assistant run commands on remote servers over SSH.

Instead of manually SSH-ing into your servers, you can just ask your AI things like *"check the GPU usage on my server"* or *"upload this file to my cloud machine"* — and it handles the connection for you, securely.

Built on the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), so it works with any MCP-compatible client (Claude Code, Cursor, Windsurf, etc.).

## What does this do?

This tool gives your AI assistant the ability to:

- **Run commands** on your remote servers (e.g. `nvidia-smi`, `df -h`, `ls`)
- **Upload and download files** between your computer and a remote server
- **Monitor server health** and connection status

All of this happens through a secure SSH connection, and you control exactly which servers the AI can access.

## Prerequisites

Before you start, make sure you have:

- **Python 3.10 or newer** — [Download here](https://www.python.org/downloads/) if you don't have it. To check, open a terminal and type `python --version`.
- **An SSH key** — This is how your computer proves its identity to a remote server without a password. If you've never set one up, see [Creating an SSH key](#creating-an-ssh-key) below.
- **A remote server to connect to** — This could be a cloud GPU (RunPod, Vast.ai, Lambda), a VPS (DigitalOcean, Linode), or any machine you can SSH into.

## Quick Start

### 1. Install

Open a terminal and run:

```bash
cd mcp-ssh-server
pip install -e .
```

> **What does `-e` mean?** It installs in "editable" mode — if you update the code later, you don't need to reinstall.

### 2. Configure your servers

Copy the example config file and rename it:

```bash
cp hosts.example.json hosts.json
```

Open `hosts.json` in any text editor and replace the example values with your real server details:

```json
{
  "hosts": [
    {
      "name": "my-server",
      "hostname": "192.168.1.100",
      "username": "root",
      "identity_file": "~/.ssh/id_ed25519"
    }
  ]
}
```

Here's what each field means:

| Field | What to put here |
|-------|-----------------|
| `name` | A short nickname for the server (you'll use this in prompts) |
| `hostname` | The server's IP address or domain name |
| `username` | The username you SSH in as (often `root` for cloud GPUs) |
| `identity_file` | Path to your SSH private key (usually `~/.ssh/id_ed25519` or `~/.ssh/id_rsa`) |

> **Where is my SSH key?** On Mac/Linux, check `~/.ssh/`. On Windows, check `C:\Users\YourName\.ssh\`. You're looking for a file like `id_ed25519` or `id_rsa` (the one *without* `.pub`).

> **Security tip:** If your config contains passwords instead of keys, restrict who can read the file: `chmod 600 hosts.json`

### 3. Add to your AI tool

This step tells your AI assistant where to find the SSH server. Where you add it depends on which tool you use:

**For Claude Code** — create or edit `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "ssh": {
      "command": "python",
      "args": ["-m", "mcp_ssh", "--config", "/full/path/to/hosts.json"]
    }
  }
}
```

**For Cursor** — go to Settings > MCP Servers and add a new server with the same command and args.

**For other MCP clients** — consult your client's docs for where to register MCP servers. The command is always `python -m mcp_ssh --config /full/path/to/hosts.json`.

> **Important:** Use the full (absolute) path to your `hosts.json`. Relative paths like `./hosts.json` may not work because the AI tool might launch the server from a different directory.

### 4. Start using it

Restart your AI tool to pick up the new server. Then just ask it things in plain language:

- *"List my SSH hosts"*
- *"Run `nvidia-smi` on my-server"*
- *"Upload `train.py` to my-server at `/workspace/train.py`"*
- *"Check the disk space on my-server"*
- *"Download `/workspace/results.csv` from my-server"*

The AI will handle connecting, running the command, and showing you the results.

## Using with Cloud GPU Providers

If you're using a cloud GPU service like **RunPod**, **Vast.ai**, or **Lambda**, keep these things in mind:

**IP addresses change.** Most cloud GPU providers assign a new IP address every time you start or restart your machine. When that happens, update the `hostname` in your `hosts.json` with the new IP.

**Ports may not be 22.** Some providers use a non-standard SSH port (e.g. `22100`, `2222`). Check your provider's dashboard for the correct port and set it in your config:

```json
{
  "name": "gpu-box",
  "hostname": "203.0.113.50",
  "port": 22100,
  "username": "root",
  "identity_file": "~/.ssh/id_ed25519",
  "auto_accept_host_key": true
}
```

**Host key verification.** Normally, SSH remembers a server's identity so it can warn you if something changes (which could mean someone is intercepting your connection). But cloud instances get a new identity every time they restart, which would cause connection failures. Setting `auto_accept_host_key` to `true` skips this check — this is **expected and safe** for ephemeral cloud instances, but should be `false` for permanent servers.

## Available Tools

These are the actions your AI assistant can perform:

| Tool | What it does |
|------|-------------|
| `list_hosts` | Shows all your configured servers and whether they're connected |
| `ssh_execute` | Runs a single command on a server |
| `ssh_execute_batch` | Runs multiple commands in sequence (stops if one fails) |
| `ssh_upload` | Sends a file from your computer to a server |
| `ssh_download` | Gets a file from a server to your computer (cleans up if the transfer fails) |
| `ssh_status` | Checks if a server is reachable (connects if needed) |
| `ssh_disconnect` | Closes the connection to a server |

## Configuration Reference

### Host Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Nickname for the server |
| `hostname` | string | required | IP address or domain name |
| `username` | string | required | SSH username |
| `port` | int | 22 | SSH port number |
| `identity_file` | string | null | Path to your SSH private key |
| `password` | string | null | SSH password (less secure than a key) |
| `auto_accept_host_key` | bool | false | Skip host key verification (only for cloud instances with changing IPs) |
| `command_timeout` | int | 30 | Seconds to wait for output before timing out |
| `transfer_timeout` | int | 120 | Seconds to wait during file transfers before timing out |
| `allowed_commands` | list | null | Only allow these specific commands (see [Security](#security-model)) |

> **How timeouts work:** These are *inactivity* timeouts — they trigger if the server stops sending data for that many seconds. A long-running command that continuously produces output will not time out.

### Server Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `log_level` | string | "INFO" | How much detail to log: DEBUG, INFO, WARNING, ERROR, or CRITICAL |
| `audit_log_file` | string | null | File path to record all actions (grows over time — set up log rotation if needed) |

## Security Model

This server is designed to give your AI assistant *controlled* access to your servers, not unlimited access.

1. **Only servers you configure** — the AI cannot connect to any server that isn't in your `hosts.json`
2. **SSH key auth by default** — passwords are supported but keys are more secure
3. **Host key verification** — by default, connections are rejected if the server isn't in your `known_hosts` file (prevents man-in-the-middle attacks)
4. **Command restrictions** — you can optionally limit which commands the AI can run per server using `allowed_commands`
5. **Shell injection protection** — for servers with `allowed_commands`, dangerous characters (`;`, `&`, `|`, `` ` ``, `$`, quotes, etc.) are blocked to prevent the AI from chaining commands. **Note:** commands that accept subcommands (like `docker`, `kubectl`, or `systemctl`) can still execute arbitrary code through their own mechanisms — the allowlist only checks the first word
6. **Audit trail** — every action is logged with timestamps
7. **No credential leakage** — passwords and keys are never shown to the AI
8. **Failed transfer cleanup** — if a download fails partway through, the partial file is automatically deleted

## Creating an SSH Key

If you don't have an SSH key yet, here's how to create one:

**Mac/Linux:**
```bash
ssh-keygen -t ed25519
```

**Windows (PowerShell):**
```powershell
ssh-keygen -t ed25519
```

Press Enter to accept the default file location. You can optionally set a passphrase for extra security.

This creates two files:
- `~/.ssh/id_ed25519` — your **private key** (keep this secret, this goes in `identity_file`)
- `~/.ssh/id_ed25519.pub` — your **public key** (this goes on the remote server)

To add the public key to your server, copy the contents of `id_ed25519.pub` and add it to `~/.ssh/authorized_keys` on the server. Most cloud GPU providers have a dashboard where you can paste your public key instead.

## Architecture

```
Your AI Tool  <--STDIO-->  MCP SSH Server  <--SSH-->  Your Remote Servers
                                |
                          hosts.json (server list)
```

Built on:
- [FastMCP](https://github.com/jlowin/fastmcp) — Python MCP framework
- [Paramiko](https://www.paramiko.org/) — SSH library for Python

## Troubleshooting

**"Config file not found"** — Make sure you're using the full path to `hosts.json` in your MCP settings, not a relative path.

**"Host key verification failed"** — The server isn't in your `known_hosts` file. Either SSH into the server manually once first (`ssh user@hostname`) to add it, or set `auto_accept_host_key: true` in your config (only for cloud instances).

**"Identity file not found"** — Double-check the `identity_file` path in your config. On Windows, use forward slashes: `C:/Users/YourName/.ssh/id_ed25519`.

**"Connection refused"** — The server might be down, the IP might have changed (common with cloud GPUs), or the port might be wrong. Check your provider's dashboard.

**AI says the tool isn't available** — Restart your AI tool after adding the MCP config. Some tools need a full restart to detect new MCP servers.
