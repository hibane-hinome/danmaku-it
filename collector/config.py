"""Configuration loading."""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "rooms": [],
    # 界面用：最近连过的房间号，以及「房间号 → UP 主昵称」（下拉里显示名字）
    "room_history": [],
    "room_names": {},
    "database": "data/sc.db",
    "log_file": "logs/collector.log",
    "log_level": "INFO",
    "heartbeat_seconds": 30,
    "stale_timeout_seconds": 120,
    "backoff_base_seconds": 1.0,
    "backoff_max_seconds": 60.0,
    "stats_interval_seconds": 60,
    # 账号目录：这里放多个 cookie 文件，界面会扫出所有能用的账号供切换
    "cookie_dir": "accounts",
    "record_enter": True,
    "record_events": False,
    "theme": "dark",
    # 发弹幕用：颜色是十进制 RGB（16777215 = 白色），mode 1 滚动 / 4 底部 / 5 顶部
    "send_color": 16777215,
    "send_mode": 1,
    # 想佩戴的粉丝牌 id，0 = 不切换（按账号当前的佩戴状态发）
    "send_medal": 0,
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
