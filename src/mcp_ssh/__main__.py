"""Entry point: python -m mcp_ssh"""

import argparse
import sys

from .server import run


def main():
    parser = argparse.ArgumentParser(
        description="MCP SSH Server — Secure SSH access via Model Context Protocol"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to hosts.json config file (default: ./hosts.json or ~/.ssh/mcp-hosts.json)",
    )
    args = parser.parse_args()

    try:
        run(config_path=args.config)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
