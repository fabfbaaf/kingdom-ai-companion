"""Remember only an explicitly selected Kingdom directory, never game/save data."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from companion.config import default_config_path
from companion.game_installation import installed_bridge_config, validate_game_directory


class InstallationStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path().with_name("installation.local.json")
        self.game: Path | None = None
        try:
            if self.path.is_file() and self.path.stat().st_size <= 16_000:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                self.game = validate_game_directory(Path(value["game_directory"]))
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def public(self) -> dict:
        return {"game_directory": str(self.game) if self.game else None,
                "bridge_installed": bool(self.game and installed_bridge_config(self.game))}

    def select(self, value: str) -> Path:
        game = validate_game_directory(Path(value))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".installation-{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(json.dumps({"game_directory": str(game)}, ensure_ascii=False),
                                 encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.game = game
        return game
