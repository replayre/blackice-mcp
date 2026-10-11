from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .apidocs import DEFAULT_BASE_URL, ApiDocs
from .hub import Hub
from .paths import default_profiles_path
from .protocol import DEFAULT_PORT
from .server import McpSurface
from .supervisor import Supervisor, load_profiles, load_setup_type


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="blackice-mcp", description="MCP hub for BlackICE / REServer development")
    parser.add_argument("--host", default="127.0.0.1", help="Interface to accept agent dial-ins on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to accept agent dial-ins on")
    parser.add_argument(
        "--token",
        default="",
        help=(
            "Require agents to present this token. Off by default: the hub binds loopback, and "
            "a process that can reach it can already run code as you. Set it on a shared machine."
        ),
    )
    parser.add_argument("--print-endpoint", action="store_true", help="Print the endpoint and token, then exit")
    parser.add_argument(
        "--no-console",
        action="store_true",
        help=(
            "Do not give a spawned process its own console window on Windows. By default a "
            "spawned REServer opens one, so its output is visible the way running it by hand is; "
            "pass this when the window would be in the way."
        ),
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Write an example profiles file, then exit. Never overwrites an existing one.",
    )
    parser.add_argument(
        "--profiles",
        default=None,
        help="Path to a process profile file. Defaults to the per-user config directory.",
    )
    parser.add_argument(
        "--api-docs-url",
        default=DEFAULT_BASE_URL,
        help="Base URL the generated API index is served from",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profiles_path = Path(args.profiles) if args.profiles else default_profiles_path()

    flags = f"--mcp-addr {args.host}:{args.port}"
    if args.token:
        flags += f" --mcp-token {args.token}"

    if args.init:
        if profiles_path.exists():
            print(f"{profiles_path} already exists; leaving it alone", file=sys.stderr)
            return 1

        profiles_path.parent.mkdir(parents=True, exist_ok=True)
        profiles_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "local-server": {
                            "kind": "server",
                            "install_dir": "/path/to/server",
                            "bindaddr": "[::]:20770",
                        },
                        "local-client": {
                            "kind": "client",
                            "install_dir": "C:/path/to/blackice",
                        },
                    }
                },
                indent=2,
            )
            + "\n"
        )
        print(f"wrote {profiles_path}", file=sys.stderr)
        return 0

    if args.print_endpoint:
        print(flags)
        return 0

    hub = Hub(host=args.host, port=args.port, token=args.token)
    hub.start()

    supervisor = Supervisor(hub.endpoint, args.token, child_console=not args.no_console)
    surface = McpSurface(
        hub,
        supervisor,
        load_profiles(profiles_path),
        ApiDocs(args.api_docs_url),
        profiles_path,
        load_setup_type(profiles_path),
    )

    print(
        f"blackice-mcp listening on {args.host}:{args.port}\n  start processes with: --mcp {flags}",
        file=sys.stderr,
    )

    try:
        asyncio.run(surface.run_stdio())
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.shutdown()
        hub.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
