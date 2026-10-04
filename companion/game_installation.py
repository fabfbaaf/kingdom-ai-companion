"""Discover and explicitly launch this game; never read save contents."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def validate_game_directory(path: Path) -> Path:
    path = path.expanduser().resolve(strict=True)
    for relative in ("KingdomTwoCrowns.exe", "GameAssembly.dll",
                     "KingdomTwoCrowns_Data/il2cpp_data/Metadata/global-metadata.dat"):
        item = path / relative
        if not item.is_file() or not item.resolve().is_relative_to(path):
            raise ValueError("请选择包含 KingdomTwoCrowns.exe 的完整游戏目录")
    return path


def steam_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        import winreg
        for hive, key, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    value, _ = winreg.QueryValueEx(handle, name)
                    roots.append(Path(value))
            except OSError:
                pass
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.getenv(variable):
            roots.append(Path(os.environ[variable]) / "Steam")
    return list(dict.fromkeys(roots))


def detect_games(roots: list[Path] | None = None) -> list[Path]:
    libraries = list(roots if roots is not None else steam_roots())
    for root in list(libraries):
        for vdf in (root / "steamapps/libraryfolders.vdf", root / "config/libraryfolders.vdf"):
            try:
                if vdf.stat().st_size > 2_000_000:
                    continue
                content = vdf.read_text(encoding="utf-8")
                libraries.extend(Path(value.replace("\\\\", "\\")) for value in
                                 re.findall(r'"path"\s*"([^"\r\n]+)"', content))
            except (OSError, UnicodeError):
                pass
    found = []
    for library in dict.fromkeys(libraries):
        candidate = library / "steamapps/common/Kingdom Two Crowns"
        try:
            game = validate_game_directory(candidate)
            if game not in found:
                found.append(game)
        except (OSError, ValueError):
            pass
    return found


def installed_bridge_config(game: Path) -> Path | None:
    path = game / "UserData/KingdomAI/bridge.local.json"
    return path if path.is_file() else None


def launch_game(game: Path) -> subprocess.Popen:
    """Request a normal Steam launch, or run the selected standalone edition."""
    game = validate_game_directory(game)
    library = (game.parent.parent.parent
               if game.parent.name.casefold() == "common"
               and game.parent.parent.name.casefold() == "steamapps" else None)
    steam_api = (
        "steam_api.dll", "steam_api64.dll",
        "KingdomTwoCrowns_Data/Plugins/steam_api.dll",
        "KingdomTwoCrowns_Data/Plugins/steam_api64.dll",
        "KingdomTwoCrowns_Data/Plugins/x86/steam_api.dll",
        "KingdomTwoCrowns_Data/Plugins/x86_64/steam_api64.dll",
    )
    if library is None and not any((game / name).is_file() for name in steam_api):
        return subprocess.Popen([str(game / "KingdomTwoCrowns.exe")], cwd=str(game))
    candidates = steam_roots()
    if library is not None:
        candidates.append(library)
    for root in dict.fromkeys(candidates):
        executable = root / "Steam.exe"
        if executable.is_file():
            executable = executable.resolve(strict=True)
            return subprocess.Popen([str(executable), "-applaunch", "701160"],
                                    cwd=str(executable.parent))
    raise ValueError("这是 Steam 版游戏，但没有找到 Steam 客户端；请先打开 Steam 并从游戏库启动")
