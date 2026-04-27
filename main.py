#!/usr/bin/env python3
"""Entry point for the packaged vcnet-test binary.

Usage (web UI):
    vcnet-test [--host HOST] [--port PORT]

Usage (internal runner, invoked by the web server as a subprocess):
    vcnet-test --_runner [nettest_runner args ...]

The binary is built with PyInstaller (see vcnet-test.spec).
External files that must sit beside the binary:
    nettest.config.json   — vCenter credentials and test settings
    ovf/                  — Alpine OVF template
    artifacts/            — created automatically; stores run output
"""

from __future__ import annotations

import sys


def main() -> None:
    # Internal dispatch: the web server spawns itself with --_runner to run
    # the test runner in a subprocess without needing a separate Python script.
    if "--_runner" in sys.argv:
        sys.argv.remove("--_runner")
        import nettest_runner
        raise SystemExit(nettest_runner.run())

    # Web UI mode
    import argparse
    parser = argparse.ArgumentParser(
        description="vCenter Network Policy Test — web UI",
        add_help=True,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    args, _ = parser.parse_known_args()

    import threading
    import webbrowser
    import uvicorn
    # Import the app object directly — passing a string import path fails
    # in frozen mode because uvicorn can't find the module on disk.
    from web_app import app

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"
    # Open browser after a short delay so uvicorn has time to bind the port.
    threading.Timer(1.2, webbrowser.open, args=[url]).start()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
