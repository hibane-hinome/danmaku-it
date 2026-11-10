"""Configuration loading."""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "rooms": [],
    "database": "data/sc.db",
    "log_file": "logs/collector.log",
    "log_level": "INFO",
    "heartbeat_seconds": 30,
    "stale_timeout_seconds": 120,
    "backoff_base_seconds": 1.0,
    "backoff_max_seconds": 60.0,
    "stats_interval_seconds": 60,
    "cookie_file": "",
    "sessdata": "",
    "dede_user_id": "",
    "record_enter": True,
    "record_events": False,
    "theme": "dark",
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}


def _resolve(base_dir: str, path: str) -> str:
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


def load_config(path: str | None) -> dict:
    config = dict(DEFAULTS)
    base_dir = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()

    if path:
        with open(path, "r", encoding="utf-8") as fh:
            user_config = json.load(fh)
        if not isinstance(user_config, dict):
            raise ValueError("config root must be a JSON object")
        config.update(user_config)

    rooms = config.get("rooms") or []
    if not isinstance(rooms, list) or not rooms:
        raise ValueError("config 'rooms' must be a non-empty list of room ids")
    try:
        config["rooms"] = [int(room) for room in rooms]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid room id in config: {exc}") from exc

    config["database"] = _resolve(base_dir, config["database"])
    config["log_file"] = _resolve(base_dir, config["log_file"])
    config["config_dir"] = base_dir
    return config


def apply_cli_overrides(config: dict, rooms: list[int] | None, database: str | None) -> dict:
    if rooms:
        config["rooms"] = list(rooms)
    if database:
        config["database"] = os.path.abspath(database)
    return config
