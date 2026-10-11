from __future__ import annotations

import sys
from pathlib import Path

import platformdirs

APPAUTHOR = "replay"
APPNAME = "blackice-mcp"

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APPNAME, appauthor=APPAUTHOR))


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APPNAME, appauthor=APPAUTHOR))


def default_profiles_path() -> Path:
    return config_dir() / "profiles.json"


def default_client_install() -> Path | None:
    if not IS_WINDOWS:
        return None

    return Path(platformdirs.user_data_dir("blackice", appauthor=APPAUTHOR))
