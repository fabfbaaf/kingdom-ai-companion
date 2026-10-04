"""Run only the local companion service. This never launches the game."""

import argparse
from pathlib import Path

import uvicorn

from companion.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-config", type=Path,
                        help="游戏 UserData/KingdomAI/bridge.local.json 的绝对路径")
    parser.add_argument("--port", type=int, default=48860)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为 1–65535")
    if args.bridge_config and not args.bridge_config.is_absolute():
        parser.error("--bridge-config 请提供绝对路径")
    app = create_app(bridge_config=args.bridge_config)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
