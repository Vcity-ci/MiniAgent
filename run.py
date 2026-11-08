#!/usr/bin/env python3
"""Start the MiniAgent web app."""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from agent import config
from agent.web.app import create_app
from agent.web.http import make_server
from agent.web.lifecycle import AutoShutdown


def main() -> int:
    config.load_dotenv()
    parser = argparse.ArgumentParser(description="MiniAgent - minimal from-scratch agent")
    parser.add_argument("--host", default=config.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open a browser")
    parser.add_argument(
        "--no-auto-shutdown",
        action="store_true",
        help="keep the backend alive after the last browser page closes",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    data_dir = Path(args.data_dir) if args.data_dir else config.data_dir()
    log_dir = Path(args.log_dir) if args.log_dir else config.log_dir()
    app = create_app(data_dir=data_dir, log_dir=log_dir)
    server = make_server(app, args.host, args.port)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}"

    watchdog: AutoShutdown | None = None
    if not args.no_auto_shutdown:
        watchdog = AutoShutdown(
            server,
            app,
            app.clients,
            grace=config.CLIENT_SHUTDOWN_GRACE,
            interval=config.AUTO_SHUTDOWN_INTERVAL,
        )
        watchdog.start()

    print(f"MiniAgent running at {url}", flush=True)
    print("Settings and API keys live only in this process; sessions/profile persist.", flush=True)
    if watchdog:
        print("Close the last browser page and the backend will stop automatically.", flush=True)

    if not args.no_browser:
        def _open_browser() -> None:
            try:
                opened = webbrowser.open(url, new=2)
                if not opened:
                    print(f"Could not auto-open a browser; open {url} manually.", flush=True)
            except Exception as exc:
                print(f"Could not auto-open a browser ({exc}); open {url} manually.", flush=True)

        opener = threading.Timer(0.8, _open_browser)
        opener.daemon = True
        opener.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        if watchdog:
            watchdog.stop()
        try:
            server.server_close()
        finally:
            app.shutdown()
    if watchdog and watchdog.triggered:
        print("Browser page closed; MiniAgent stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
