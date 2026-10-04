"""Portable Windows entry point; game launch and AI control remain explicit."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from companion.app import create_app
from companion.game_installation import (
    detect_games,
    installed_bridge_config,
    validate_game_directory,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="王国 AI 陪玩 · P1 玩家 / P2 AI")
    parser.add_argument("--bridge-config", type=Path)
    parser.add_argument("--game-dir", type=Path)
    parser.add_argument("--port", type=int, default=48860)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须介于1024和65535")
    if args.bridge_config is not None and not args.bridge_config.is_absolute():
        parser.error("桥接配置需绝对路径")
    if args.game_dir:
        try:
            game = validate_game_directory(args.game_dir)
            args.bridge_config = args.bridge_config or installed_bridge_config(game)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if args.bridge_config is None:
        configs = [config for game in detect_games() if (config := installed_bridge_config(game))]
        if len(configs) == 1:
            args.bridge_config = configs[0]

    log_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "KingdomAICompanion"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = (log_dir / "companion.log").open("a", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    address = f"http://127.0.0.1:{args.port}"
    try:
        with urllib.request.urlopen(address + "/api/session", timeout=1) as response:
            is_running = response.status == 200 and "token" in response.read(2048).decode()
    except (OSError, urllib.error.URLError):
        is_running = False
    if is_running:
        if not args.no_browser:
            webbrowser.open(address)
        return

    app = create_app(bridge_config=args.bridge_config)
    server = uvicorn.Server(uvicorn.Config(app,
                                           host="127.0.0.1", port=args.port, access_log=False))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    if not args.no_browser:
        def show_ui() -> None:
            for _ in range(100):
                if server.started:
                    webbrowser.open(address)
                    return
                if server.should_exit:
                    return
                time.sleep(0.1)
        threading.Thread(target=show_ui, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
