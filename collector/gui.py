"""Floating danmaku machine built on Qt (PySide6) for high-volume rooms."""

from __future__ import annotations

import asyncio
import html
import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from string import Template

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QGuiApplication, QIcon,
    QImage, QPainter, QPixmap,
    QTextBlockUserData, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizeGrip,
    QSizePolicy, QSpinBox, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
    QWidgetAction,
)

from .accounts import account_meta_path, load_meta, save_meta, scan_accounts
from .bili_api import BiliApi
from .collector import RoomCollector
from .config import DEFAULTS
from .fixtures import JPN_AFTER, JPN_BEFORE, load_replay
from .images import ImageLoader
from .sender import COLOR_PRESETS, MAX_LEN as SEND_MAX_LEN
from .sender import DEFAULT_COLOR, MODES, DanmakuSender, SendError
from .storage import Storage, extract_sc, read_room_names

TITLE = "弹幕机 · danmaku-it"
# 图标由 tools/make_icon.py 生成，改图案后重跑那个脚本即可
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
ICON_PATH = os.path.join(ASSETS_DIR, "icon.ico")
MAX_DANMAKU_BLOCKS = 600
MAX_EVENT_BLOCKS = 200
MAX_SC_BLOCKS = 600
MAX_EVENTS_PER_TICK = 400
MAX_DANMAKU_PER_TICK = 150
TICK_MS = 30
# 表情图最多等这么久再上屏；超时先按 [名字] 发出去，图下完再补画那一行
IMAGE_WAIT_SECONDS = 0.4
MAX_PENDING_DANMAKU = 200
EMOTE_PX = 20
# 房间大表情 / 收藏集的表情原图比小表情大得多，照 20px 画就糊成一团
EMOTE_BIG_PX = 40
# 正文里的表情占位：[名字]（老式小表情、收藏集表情都写成这样）
EMOTE_TOKEN = re.compile(r"\[([^\[\]\s][^\[\]]{0,31})\]")
# 窗口整体不透明度 0–100（设置里可调）。整个窗口一起变淡，底板、文字、
# 输入框都跟着走，不再有「哪一块自己半透明」的区别
WINDOW_OPACITY = 100
# 下限：再淡就别想看清弹幕了，而且会找不到窗口
WINDOW_OPACITY_MIN = 30
# 账号下拉里的头像边长（取 2 倍再缩，缩放后更干净）
AVATAR_PX = 18
# 表情面板：按钮尺寸、每行几个、面板宽高
EMOTICON_BTN = 40
EMOTICON_COLS = 8
EMOTICON_GAP = 2   # 格子间距
EMOTICON_PAD = 2   # 网格容器内边距
EMOTICON_BAR = 10  # 竖向滚动条占位（QSS 里 8px 宽 + 1px 边距）
# 宽度必须按内容算：写死一个数，最右一列就会被裁掉半截
EMOTICON_PANEL_W = (
    8 + 8                                    # root 左右内边距
    + EMOTICON_PAD * 2                       # 网格容器内边距
    + EMOTICON_COLS * EMOTICON_BTN
    + (EMOTICON_COLS - 1) * EMOTICON_GAP
    + EMOTICON_BAR
    + 2                                      # 取整余量
)
EMOTICON_PANEL_H = 268


# ---------------- config helpers ----------------

def load_gui_config(path: str) -> dict:
    """Tolerant loader: an empty room list is allowed while the user edits."""
    config = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                config.update(data)
        except (OSError, json.JSONDecodeError):
            pass

    rooms: list[int] = []
    for room in config.get("rooms") or []:
        try:
            rooms.append(int(room))
        except (TypeError, ValueError):
            continue
    config["rooms"] = rooms

    base_dir = os.path.dirname(os.path.abspath(path))
    # 老配置只有单个 cookie_file，迁到它所在的目录，省得让人手动再填一次
    if not config.get("cookie_dir"):
        legacy = config.get("cookie_file") or ""
        if legacy:
            legacy = legacy if os.path.isabs(legacy) else os.path.join(base_dir, legacy)
            config["cookie_dir"] = os.path.dirname(os.path.normpath(legacy))
    if not config.get("cookie_dir"):
        config["cookie_dir"] = DEFAULTS["cookie_dir"]

    for key in ("database", "log_file", "cookie_dir"):
        value = config.get(key) or DEFAULTS[key]
        config[key] = value if os.path.isabs(value) else os.path.normpath(os.path.join(base_dir, value))
    config["config_dir"] = base_dir
    return config


def save_gui_config(path: str, config: dict) -> None:
    # 模板不落盘：config.example.json 是要入库的样板，混进窗口坐标和绝对路径就废了
    if os.path.basename(path).endswith(".example.json"):
        return
    payload = {key: value for key, value in config.items() if key != "config_dir"}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


# ---------------- background collector ----------------

@dataclass
class RoomState:
    input_room: int
    real_room: int = 0
    title: str = ""
    state: str = "未连接"
    messages: int = 0
    sc: int = 0
    gifts: int = 0
    enters: int = 0
    online: int = 0
    watched: int = 0
    guards: int = 0


class ScBlockData(QTextBlockUserData):
    """Tags an SC log block with its sc id so a late translation can locate it."""

    def __init__(self, sc_id: str) -> None:
        super().__init__()
        self.sc_id = sc_id


class DanmakuBlockData(QTextBlockUserData):
    """挂在一行弹幕上：图片还没下完，等到了再补画这一行。"""

    def __init__(self, row: dict, pending: list[str]) -> None:
        super().__init__()
        self.row = row
        self.pending = pending


class CollectorService:
    """Runs one asyncio collector on a worker thread and streams events into a queue."""

    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        # Kept in sync with the event pane: whatever is on screen is also stored.
        self.event_filter: dict[str, bool] = {
            "enter": True, "gift": True, "guard": True, "delete": True,
        }

    def start(self, config: dict, room: int, cookies: dict | None = None) -> None:
        if self.running:
            return
        self.running = True
        # 采集也走选中的账号：匿名连接会让 B 站把用户名打成 ***，而且下发的
        # 弹幕明显更少（实测同一房间 10 秒：匿名 13 条 / 登录 40 条）
        config = dict(config, rooms=[room], collector_cookies=dict(cookies or {}))
        self._thread = threading.Thread(target=self._thread_main, args=(config,), daemon=True)
        self._thread.start()

    def _thread_main(self, config: dict) -> None:
        try:
            asyncio.run(self._async_main(config))
        except Exception as exc:  # noqa: BLE001 - surfaces fatal errors to the UI
            self.events.put({"type": "fatal", "error": str(exc)})
        finally:
            self.running = False
            self.events.put({"type": "stopped"})

    async def _async_main(self, config: dict) -> None:
        self._loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        self._stop = stop

        storage = Storage(config["database"])
        api = BiliApi(
            user_agent=config["user_agent"],
            cookies=config.get("collector_cookies") or None,
            cookie_file=config.get("cookie_file", ""),
            sessdata=config.get("sessdata", ""),
            dede_user_id=config.get("dede_user_id", ""),
        )
        collector = RoomCollector(
            room_id=config["rooms"][0],
            api=api,
            storage=storage,
            stop_event=stop,
            heartbeat_seconds=int(config["heartbeat_seconds"]),
            stale_timeout_seconds=int(config["stale_timeout_seconds"]),
            backoff_base_seconds=float(config["backoff_base_seconds"]),
            backoff_max_seconds=float(config["backoff_max_seconds"]),
            user_agent=config["user_agent"],
            record_enter=True,
            record_events=True,
            event_filter=self.event_filter,
            event_sink=self.events.put,
        )
        task = asyncio.create_task(collector.run(), name=f"room-{collector.input_room_id}")
        try:
            await stop.wait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            storage.close()
            self._loop = None
            self._stop = None

    def stop(self, timeout: float = 8.0) -> None:
        # 线程已经退出时 loop 可能已经关掉了，再 call_soon_threadsafe 会抛
        # "Event loop is closed"——按一下断开、线程刚好自己停完就会撞上
        loop, stop_event = self._loop, self._stop
        if loop is not None and stop_event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)


# ---------------- floating window ----------------

DARK_STYLE = """
QFrame#panel {
    background-color: $panel_bg;
    border: $panel_border;
    border-radius: $panel_radius;
}
QLabel { color: #d6d8dd; font-size: 12px; }
QLabel#title { color: #f0f2f5; font-size: 13px; font-weight: 600; }
QLabel#stat { color: #8b93a1; font-size: 11px; }
QLabel#section { color: #f0a020; font-size: 12px; font-weight: 600; }
QLabel#live  { color: #4ade80; font-size: 11px; }
QLabel#dead  { color: #f87171; font-size: 11px; }
QPlainTextEdit, QTextBrowser {
    background: $view_bg;
    color: #e8eaee;
    border: $view_border;
    border-radius: 8px;
    padding: 6px;
    font-size: 12px;
}
QPlainTextEdit#enter, QTextBrowser#enter { color: #7d8797; }
QPlainTextEdit#gift, QTextBrowser#gift { color: #f0a020; }
QLineEdit {
    background: rgba(255, 255, 255, 16);
    color: #e8eaee;
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 7px;
    padding: 5px 8px;
    font-size: 12px;
}
QComboBox {
    background: rgba(255, 255, 255, 16);
    color: #e8eaee;
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 7px;
    padding: 4px 6px;
    font-size: 12px;
}
QComboBox QLineEdit {
    background: transparent;
    border: none;
    padding: 0;
}
QComboBox::drop-down {
    border: none;
    border-left: 1px solid rgba(255, 255, 255, 22);
    width: 18px;
}
QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
}
QComboBox QAbstractItemView {
    background: #1b1f26;
    color: #e8eaee;
    border: 1px solid #2f3540;
    selection-background-color: #2f6fed;
    selection-color: #ffffff;
    outline: none;
}
QPushButton {
    background: rgba(255, 255, 255, 18);
    color: #dfe3e9;
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: 7px;
    padding: 5px 12px;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 32); }
QPushButton#primary { background: #2f6fed; border: none; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #3b7bf5; }
QPushButton#primary[active="true"] { background: #b03a3a; }
/* 发送按钮右侧的「▾」：跟发送同一个底色，拼成一个按钮，只多一条竖线 */
QPushButton#primary[joined="left"] {
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
}
QPushButton#sendmore {
    background: #2f6fed;
    border: none;
    border-left: 1px solid rgba(255, 255, 255, 78);
    color: white;
    padding: 0 3px;
    font-size: 10px;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
}
QPushButton#sendmore:hover { background: #3b7bf5; }
QPushButton#sendmore:disabled {
    background: #2b3240;
    color: #7d8797;
    border-left-color: rgba(255, 255, 255, 22);
}
QPushButton#ghost {
    background: $ghost_bg;
    border: none;
    border-radius: 7px;
    color: #9aa3b2;
    padding: 4px 8px;
}
QPushButton#ghost:hover { color: #ffffff; }
QPushButton#ghost:checked {
    background: rgba(47, 111, 237, 58);
    color: #a9c8ff;
    border: 1px solid rgba(94, 148, 255, 96);
}
QPushButton#ghost:checked:hover { background: rgba(47, 111, 237, 84); }
QPushButton:disabled {
    color: #5d6878;
    background: rgba(255, 255, 255, 8);
    border-color: rgba(255, 255, 255, 14);
}
QPushButton#primary:disabled { background: #2b3240; color: #7d8797; }
QPushButton#ghost:disabled { background: transparent; color: #4f5764; }
QLineEdit:disabled, QComboBox:disabled {
    color: #5d6878;
    background: rgba(255, 255, 255, 8);
    border-color: rgba(255, 255, 255, 14);
}
QComboBox QLineEdit:disabled { color: #5d6878; background: transparent; border: none; }
QCheckBox { color: #c3c8d1; font-size: 11px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: $scroll_handle_hi; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""

LIGHT_STYLE = """
QFrame#panel {
    background-color: $panel_bg;
    border: $panel_border;
    border-radius: $panel_radius;
}
QLabel { color: #374151; font-size: 12px; }
QLabel#title { color: #111827; font-size: 13px; font-weight: 600; }
QLabel#stat { color: #6b7280; font-size: 11px; }
QLabel#section { color: #b45309; font-size: 12px; font-weight: 600; }
QLabel#live  { color: #16a34a; font-size: 11px; }
QLabel#dead  { color: #dc2626; font-size: 11px; }
QPlainTextEdit, QTextBrowser {
    background: $view_bg;
    color: #1f2328;
    border: $view_border;
    border-radius: 6px;
    padding: 6px;
    font-size: 12px;
}
QPlainTextEdit#enter, QTextBrowser#enter { color: #6b7280; }
QPlainTextEdit#gift, QTextBrowser#gift { color: #b45309; }
QLineEdit {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d8dee6;
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 12px;
}
QComboBox {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d8dee6;
    border-radius: 6px;
    padding: 4px 6px;
    font-size: 12px;
}
QComboBox QLineEdit {
    background: transparent;
    border: none;
    padding: 0;
}
QComboBox::drop-down {
    border: none;
    border-left: 1px solid #e3e7ee;
    width: 18px;
}
QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d8dee6;
    selection-background-color: #2f6fed;
    selection-color: #ffffff;
    outline: none;
}
QPushButton {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 12px;
}
QPushButton:hover { background: #f0f3f7; }
QPushButton:pressed { background: #e6ebf2; }
QPushButton#primary { background: #2f6fed; border: none; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #3b7bf5; }
QPushButton#primary[active="true"] { background: #c0392b; }
QPushButton#primary[joined="left"] {
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
}
QPushButton#sendmore {
    background: #2f6fed;
    border: none;
    border-left: 1px solid rgba(255, 255, 255, 150);
    color: white;
    padding: 0 3px;
    font-size: 10px;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
}
QPushButton#sendmore:hover { background: #3b7bf5; }
QPushButton#sendmore:disabled {
    background: #b9cdf3;
    color: #ffffff;
    border-left-color: rgba(255, 255, 255, 190);
}
QPushButton#ghost {
    background: $ghost_bg;
    border: none;
    border-radius: 7px;
    color: #6b7280;
    padding: 4px 8px;
}
QPushButton#ghost:hover { color: #111827; background: #eef1f5; border-radius: 6px; }
QPushButton#ghost:checked {
    background: #dfe9ff;
    color: #2f6fed;
    border: 1px solid #b9cdf3;
    border-radius: 6px;
    font-weight: 600;
}
QPushButton#ghost:checked:hover { background: #d2e0ff; }
QPushButton:disabled {
    color: #a8b0bd;
    background: #f5f7fa;
    border-color: #e5e9ef;
}
QPushButton#primary:disabled { background: #b9cdf3; color: #ffffff; }
QPushButton#ghost:disabled { background: transparent; color: #b3bac6; }
QLineEdit:disabled, QComboBox:disabled {
    color: #a8b0bd;
    background: #f7f9fb;
    border-color: #e5e9ef;
}
QComboBox QLineEdit:disabled { color: #a8b0bd; background: transparent; border: none; }
QCheckBox { color: #374151; font-size: 11px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll_handle; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #aab3c0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""

# Theme-dependent colours that end up in rich text / widget-level stylesheets,
# because QSS alone cannot reach them (inline HTML styles, splitter handle, ...).
THEME_COLORS = {
    "dark": {
        "text": "#e8eaee",
        "time": "#7d8797",
        "user": "#6fb3f2",
        "medal": "#c792ea",
        "guard": "#f0a020",
        "admin": "#5ec97a",
        "emoticon": "#7ee0c8",
        "enter": "#7d8797",
        "delete": "#ff6b6b",
        "sep": "#5d6878",
        "price": "#f0a020",
        "price_hi": "#ff7b3d",
        # 自己发的弹幕：整行底色 + 左侧竖条
        "self_bg": "#243247",
        "self_mark": "#5c9dff",
        "split": "#2a2f38",
        "arrow": "#b6bdc9",
        "grip": "#5b6472",
        "shadow": (0, 0, 0, 150),
        "dialog_bg": "#1b1d22",
        "dialog_fg": "#d6d8dd",
        "dialog_input_bg": "#262a31",
        "dialog_input_fg": "#e8eaee",
        "dialog_border": "#363b45",
        "dialog_plain_bg": "#2a2e36",
        "dialog_plain_fg": "#e8eaee",
        "dialog_plain_border": "#3a414d",
    },
    "light": {
        "text": "#1f2328",
        "time": "#6b7280",
        "user": "#1a73e8",
        "medal": "#7c3aed",
        "guard": "#b45309",
        "admin": "#15803d",
        "emoticon": "#0f766e",
        "enter": "#6b7280",
        "delete": "#dc2626",
        "sep": "#c7ced8",
        "price": "#b45309",
        "price_hi": "#c2410c",
        # 自己发的弹幕：整行底色 + 左侧竖条
        "self_bg": "#e2ecfd",
        "self_mark": "#1a73e8",
        "split": "#e5e7eb",
        "arrow": "#6b7280",
        "grip": "#9aa3b2",
        "shadow": (15, 23, 42, 55),
        "dialog_bg": "#f7f8fa",
        "dialog_fg": "#374151",
        "dialog_input_bg": "#ffffff",
        "dialog_input_fg": "#1f2328",
        "dialog_border": "#d8dee6",
        "dialog_plain_bg": "#ffffff",
        "dialog_plain_fg": "#1f2328",
        "dialog_plain_border": "#d0d7de",
    },
}

THEME_LABELS = {"dark": "暗色", "light": "浅色", "system": "跟随系统"}


def resolve_theme(name: str) -> str:
    """Map the stored preference to the palette that is actually rendered."""
    if name == "system":
        scheme = QGuiApplication.styleHints().colorScheme()
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"
    return name if name in THEME_COLORS else "dark"


class EmoticonPanel(QWidget):
    """点「表情」弹出的选择面板。

    用 ``Qt.Popup`` 独立窗口，点别处自动收起，行为和原生下拉一致。图片交给
    ``ImageLoader``（和弹幕列表共享磁盘缓存），谁先下完谁先画，没到的先留空位，
    不会为了等图把面板卡住。
    """

    picked = Signal(object)

    def __init__(self, loader: ImageLoader, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.Popup)
        self._loader = loader
        self._packages: list[dict] = []
        self._buttons: list[tuple[QPushButton, str]] = []
        self._chips: list[QPushButton] = []
        self._active = 0
        self._ticks = 0
        self.setFixedSize(EMOTICON_PANEL_W, EMOTICON_PANEL_H)
        self.setAttribute(Qt.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 7, 8, 7)
        root.setSpacing(5)

        # 分组条装得下的账号只有两三个包，大号能拉出十来个（房间 + 通用 + 各套装扮
        # + emoji），一行放不下，所以套一层横向滚动，滚轮在条上也能横着滚
        self._chip_scroll = QScrollArea()
        self._chip_scroll.setWidgetResizable(True)
        self._chip_scroll.setFrameShape(QFrame.NoFrame)
        self._chip_scroll.setFixedHeight(26)
        self._chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._chip_host = QWidget()
        self._chip_host.setObjectName("emoteChipHost")
        self._chip_row = QHBoxLayout(self._chip_host)
        self._chip_row.setContentsMargins(0, 0, 0, 0)
        self._chip_row.setSpacing(4)
        self._chip_row.addStretch(1)
        self._chip_scroll.setWidget(self._chip_host)
        root.addWidget(self._chip_scroll)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._grid = QGridLayout(self._body)
        self._grid.setContentsMargins(
            EMOTICON_PAD, EMOTICON_PAD, EMOTICON_PAD, EMOTICON_PAD
        )
        self._grid.setSpacing(EMOTICON_GAP)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll, 1)

        self._hint = QLabel("正在拉取表情…")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setWordWrap(True)
        root.addWidget(self._hint, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._on_tick)

        self.apply_theme(THEME_COLORS["dark"])

    # ----- 外部 -----

    def apply_theme(self, colors: dict) -> None:
        self.setStyleSheet(
            f"""
            EmoticonPanel {{ background: {colors['dialog_bg']};
                             border: 1px solid {colors['dialog_border']}; border-radius: 10px; }}
            QLabel {{ color: {colors['dialog_fg']}; font-size: 11px; background: transparent; }}
            QScrollArea, QWidget#emoteBody, QWidget#emoteChipHost {{ background: transparent; border: none; }}
            QPushButton#emoteChip {{
                background: transparent; border: none; color: {colors['time']};
                padding: 2px 7px; font-size: 11px; border-radius: 5px;
            }}
            QPushButton#emoteChip:hover {{ color: {colors['dialog_fg']}; }}
            QPushButton#emoteChip[on="true"] {{
                background: rgba(47, 111, 237, 55); color: {colors['text']};
            }}
            QPushButton#emoteItem {{
                background: transparent; border: 1px solid transparent; border-radius: 6px;
            }}
            QPushButton#emoteItem:hover {{ background: rgba(47, 111, 237, 60); }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 1px; }}
            QScrollBar::handle:vertical {{ background: {colors['grip']}; border-radius: 4px; min-height: 20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            """
        )
        self._body.setObjectName("emoteBody")

    def set_packages(self, packages: list[dict], message: str = "") -> None:
        """喂入表情包；``packages`` 为空时把 ``message`` 当提示显示。"""
        self._packages = list(packages or [])
        if not self._packages:
            self._clear_chips()
            self._clear_buttons()
            self._scroll.hide()
            self._hint.setText(message or "没有可用表情")
            self._hint.show()
            return
        self._hint.hide()
        self._scroll.show()
        self._rebuild_chips()
        self._select(min(self._active, len(self._packages) - 1))

    def popup_at(self, anchor: QWidget) -> None:
        """贴在按钮上方弹出；上面放不下就翻到下面。"""
        corner = anchor.mapToGlobal(QPoint(0, 0))
        screen = anchor.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = corner.x() + anchor.width() - self.width()
        y = corner.y() - self.height() - 6
        if y < area.top() + 4:
            y = corner.y() + anchor.height() + 6
        x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
        self.move(x, y)
        self.show()
        self.raise_()
        self._on_tick()

    # ----- 内部 -----

    def _clear_chips(self) -> None:
        for chip in self._chips:
            self._chip_row.removeWidget(chip)
            chip.hide()
            chip.setParent(None)
            chip.deleteLater()
        self._chips = []
        self._active = 0

    def _clear_buttons(self) -> None:
        self._timer.stop()
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 只 removeWidget 不够：deleteLater 是延时的，旧按钮会盖在新按钮上
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._buttons = []

    def _rebuild_chips(self) -> None:
        self._clear_chips()
        for index, pkg in enumerate(self._packages):
            name = pkg["name"]
            if len(name) > 7:
                name = name[:7] + "…"
            chip = QPushButton(f"{name} {len(pkg['items'])}")
            chip.setObjectName("emoteChip")
            chip.setProperty("on", "false")
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(f"{pkg['name']} · {len(pkg['items'])} 个")
            chip.clicked.connect(lambda _=False, i=index: self._select(i))
            # 插在 stretch 之前，让分组靠左
            self._chip_row.insertWidget(len(self._chips), chip)
            self._chips.append(chip)

    def _select(self, index: int) -> None:
        if not self._packages:
            return
        self._active = max(0, min(index, len(self._packages) - 1))
        for i, chip in enumerate(self._chips):
            chip.setProperty("on", "true" if i == self._active else "false")
            chip.style().unpolish(chip)
            chip.style().polish(chip)
        if 0 <= self._active < len(self._chips):
            # 组多的时候选中的按钮可能在可视区外
            self._chip_scroll.ensureWidgetVisible(self._chips[self._active], 4, 0)
        self._fill(self._packages[self._active])

    def wheelEvent(self, event) -> None:
        # 指针在分组条上时滚轮横着滚分组，在下面网格里就正常滚表情
        if self._chip_scroll.geometry().contains(event.position().toPoint()):
            bar = self._chip_scroll.horizontalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def _fill(self, pkg: dict) -> None:
        self._clear_buttons()
        for i, item in enumerate(pkg["items"]):
            btn = QPushButton()
            btn.setObjectName("emoteItem")
            btn.setFixedSize(EMOTICON_BTN, EMOTICON_BTN)
            btn.setIconSize(QSize(EMOTICON_BTN - 8, EMOTICON_BTN - 8))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(item["text"])
            btn.clicked.connect(lambda _=False, data=item: self.picked.emit(data))
            self._grid.addWidget(btn, i // EMOTICON_COLS, i % EMOTICON_COLS)
            self._buttons.append((btn, item["url"]))
            self._loader.want(item["url"])
        self._ticks = 0
        self._on_tick()
        self._scroll.verticalScrollBar().setValue(0)

    def _on_tick(self) -> None:
        pending = False
        for btn, url in self._buttons:
            if not btn.icon().isNull():
                continue
            image = self._loader.image(url)
            if image is not None and not image.isNull():
                btn.setIcon(QIcon(QPixmap.fromImage(image)))
            elif self._loader.settled(url):
                # 下不来就留空白，tooltip 里有名字，不反复重试
                btn.setIcon(QIcon())
            else:
                pending = True
        self._ticks += 1
        if not pending or self._ticks > 40:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)


class ComboArrowFollower(QObject):
    """下拉框右边的 ▾ 跟着走。

    箭头是盖在 combo 上的独立 label，位置按 combo 宽度算。以前 combo 宽度
    写死，摆一次就够；现在会随窗口挤压，得在 combo 自己 resize 时重摆。
    """

    def __init__(self, arrow: QLabel) -> None:
        super().__init__(arrow)
        self.arrow = arrow

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Resize:
            self.arrow.setGeometry(obj.width() - 18, 0, 16, obj.height())
        return False


class PaneDragHandle(QObject):
    """Drag handle that moves a whole pane up or down inside the splitter."""

    def __init__(self, window: "FloatingWindow", key: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.window = window
        self.key = key
        self._dragging = False
        self._moved = False

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._dragging = True
            self._moved = False
            obj.setCursor(Qt.ClosedHandCursor)
            return True
        if kind == QEvent.MouseMove and self._dragging:
            if self.window._drag_pane_step(self.key, event.globalPosition().y()):
                self._moved = True
            return True
        if kind == QEvent.MouseButtonRelease and self._dragging:
            self._dragging = False
            obj.setCursor(Qt.OpenHandCursor)
            if self._moved:
                self.window._finish_pane_drag()
            return True
        return False


class FloatingWindow(QWidget):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.config_path = os.path.abspath(config_path)
        self.cfg = load_gui_config(self.config_path)
        self.theme = resolve_theme(str(self.cfg.get("theme", "dark")))
        self._c = THEME_COLORS[self.theme]
        # 房间下拉要显示 UP 主名字：历史里连过的房间，collect_room 表里就有
        self._backfill_room_names()
        self.service = CollectorService()
        self.state = RoomState(input_room=0)
        self._cache_dir = os.path.join(os.path.dirname(self.config_path), "cache")
        self._images = ImageLoader(os.path.join(self._cache_dir, "images"))
        # 头像走同一份磁盘缓存，只是尺寸更小
        self._avatar_images = ImageLoader(
            os.path.join(self._cache_dir, "images"), max_px=AVATAR_PX * 2
        )
        # 账号：目录里一个 cookie 文件一个账号，启动先扫一遍（不联网）
        self._meta_path = account_meta_path(self._cache_dir)
        self._meta = load_meta(self._meta_path)
        self.accounts = scan_accounts(self.cfg.get("cookie_dir") or "", self._meta)
        self.usable = [item for item in self.accounts if not item.error]
        self._avatar_done: set[str] = set()
        self._account_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account")
        self._account_future: Future | None = None
        # 当前账号的粉丝牌（账号级，不随房间变）
        self._medals: list[dict] = []
        self._medal_future: Future | None = None
        # 换牌子是账号级操作，单独一路，别和拉列表抢同一个 worker
        self._wear_future: Future | None = None
        self._wear_target = 0
        # 发弹幕：只读 cookie 不联网，首次发送时再在后台线程 verify
        preferred = self._preferred_account()
        self.sender = DanmakuSender(self.cfg, preferred)
        self._sender_path = preferred.path if preferred is not None else ""
        self._send_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="send")
        self._send_future: Future | None = None
        self._mode_locked = False  # 房间只开放一种位置时锁死选择
        # 位置以前挂在下拉的 currentData 上，现在收进发送按钮的下拉，值自己存
        self._send_mode_value = int(self.cfg.get("send_mode", 1))
        self.send_color = int(self.cfg.get("send_color", DEFAULT_COLOR)) & 0xFFFFFF
        # 表情面板单独一份图（要更大的尺寸），磁盘缓存和弹幕列表共用
        self._emote_images = ImageLoader(
            os.path.join(self._cache_dir, "images"),
            max_px=EMOTICON_BTN * 2,
        )
        self._emote_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="emote")
        self._context_future: Future | None = None
        self._emote_room = 0
        self._emote_packages: list[dict] = []
        self._emote_message = ""
        self._send_options: dict | None = None
        self._emoticon_panel: EmoticonPanel | None = None
        self._settings: SettingsPopup | None = None
        self._danmaku_pending: list[dict] = []
        self._pending_images = 0
        self._drag_origin: QPoint | None = None
        self._dropped = 0
        self._panes: dict[str, QWidget] = {}
        self._pane_visible: dict[str, bool] = {"danmaku": True, "event": True, "sc": True}
        self._pane_actions: dict[str, QAction] = {}
        self._drag_handles: dict[str, QLabel] = {}
        self._pane_sizes: dict[str, int] = {}
        self._mock_thread: threading.Thread | None = None
        self._mock_stop: threading.Event | None = None
        # 0 号直播间：本地回放 + 本地回显发送，用来调试发送行
        self._test_mode = False
        # 表情名 → (图地址, 高度)：本房间表情包的那一份，包列表换了才重建
        self._emote_index_src: list[dict] | None = None
        self._emote_index: dict[str, tuple[str, int]] = {}
        # 窗口尺寸落盘：还原完成前不许写，免得启动时的 resize 把旧值冲掉
        self._geo_ready = False
        self._geo_timer: QTimer | None = None

        self.setWindowTitle(TITLE)
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 640)
        # 拖窄的下限：再窄布局就摆不开了（316 是各行的硬底，留一点余量）
        self.setMinimumSize(320, 300)

        self._build()
        self._apply_theme(clear=False)
        self._restore_pane_order()
        self._restore_layout()
        self._restore_panes()
        self._restore_geometry()
        # 窗口不透明度是持久化的，启动就按上次那个值来
        self._apply_opacity()
        # 昵称/头像走后台，别让启动等网络
        self._request_account_meta()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

        # 表情到齐的弹幕不会立刻上屏，由这个定时器接力补发，顺便补画迟到的表情
        self._image_timer = QTimer(self)
        self._image_timer.timeout.connect(self._on_image_timer)
        self._image_timer.start(120)

        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.setInterval(800)
        self._split_timer.timeout.connect(self._save_config_quiet)

        # 拖宽高/挪位置也记，别只靠关窗那一下（进程被杀就丢了）
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(600)
        self._geo_timer.timeout.connect(self._store_geometry)
        self._geo_ready = True

        self._style_hints = QGuiApplication.styleHints()
        self._style_hints.colorSchemeChanged.connect(self._on_system_theme_changed)

        # 先把上次那个房间的表情和可用颜色取回来，免得第一次点开才发现不能用
        QTimer.singleShot(0, self._refresh_room_context)

    # ----- theme -----

    def _apply_theme(self, theme: str | None = None, clear: bool = True) -> None:
        if theme is not None and theme != self.theme:
            self.theme = theme
        self._c = THEME_COLORS[self.theme]
        colors = self._c

        self.setStyleSheet(self._stylesheet())
        self.splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {colors['split']}; }}"
        )
        self._shadow.setColor(QColor(*colors["shadow"]))
        for arrow in (
            getattr(self, "room_arrow", None),
            getattr(self, "account_arrow", None),
            getattr(self, "medal_arrow", None),
        ):
            if arrow is not None:
                arrow.setStyleSheet(
                    f"color:{colors['arrow']}; background:transparent; font-size:11px;"
                )
        for handle in self._drag_handles.values():
            handle.setStyleSheet(
                f"QLabel#grip {{ color: {colors['grip']}; font-size: 12px; }}"
            )

        self._fmt_user.setForeground(QColor(colors["user"]))
        self._fmt_time.setForeground(QColor(colors["time"]))
        self._fmt_sc.setForeground(QColor(colors["price"]))
        self._fmt_sys.setForeground(QColor(colors["time"]))

        panel = getattr(self, "_emoticon_panel", None)
        if panel is not None:
            panel.apply_theme(colors)

        if clear:
            for view in (self.danmaku, self.event_view, self.sc_view):
                view.clear()

        # 换主题会把箭头颜色改回「可用」色，这里按连接状态重新压一压
        if hasattr(self, "send_btn"):
            self._refresh_send_state()


    def _on_system_theme_changed(self, scheme) -> None:
        if str(self.cfg.get("theme", "dark")) != "system":
            return
        self._apply_theme("dark" if scheme == Qt.ColorScheme.Dark else "light")

    # ----- 外观 -----

    def _panel_palette(self) -> dict[str, str]:
        """底板、内容区、滚动条这几个颜色按主题填。

        QSS 是一整块字符串，没法在运行时改单条规则，所以做成模板再填。
        底板和内容区都用实色：淡不淡交给窗口整体的不透明度管，各控件
        再自己半透明就会叠出一层灰，SC 栏、底栏看着就跟别处不一样。
        """
        dark = self.theme == "dark"
        return {
            "panel_bg": "#181a1e" if dark else "#f7f8fa",
            "panel_border": (
                "1px solid rgba(255, 255, 255, 32)" if dark else "1px solid #d8dee6"
            ),
            "panel_radius": "14px" if dark else "10px",
            "view_bg": "#0e0f12" if dark else "#ffffff",
            "view_border": "none" if dark else "1px solid #e3e7ee",
            "ghost_bg": "transparent",
            "scroll_handle": "#3a414d" if dark else "#c7ced8",
            "scroll_handle_hi": "#4b5462" if dark else "#d5dbe4",
        }

    def _stylesheet(self) -> str:
        template = DARK_STYLE if self.theme == "dark" else LIGHT_STYLE
        return Template(template).substitute(self._panel_palette())

    def _opacity(self) -> int:
        """窗口整体不透明度，30–100。"""
        try:
            value = int(self.cfg.get("window_opacity", WINDOW_OPACITY))
        except (TypeError, ValueError):
            value = WINDOW_OPACITY
        return max(WINDOW_OPACITY_MIN, min(100, value))

    def _apply_opacity(self) -> None:
        """整个窗口一起变淡：底板、文字、输入框都跟着走。

        比在 QSS 里逐块调 rgba 省事，也不会出现「这几栏比那几栏透」。
        """
        self.setWindowOpacity(self._opacity() / 100)


    # ----- construction -----

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self._outer = outer

        panel = QFrame(self)
        panel.setObjectName("panel")
        outer.addWidget(panel)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        panel.setGraphicsEffect(shadow)
        self._shadow = shadow

        root = QVBoxLayout(panel)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        root.addLayout(self._build_header())
        root.addLayout(self._build_control_row())
        # 账号和粉丝牌放在列表上方：先定「谁在发」，再看内容
        root.addLayout(self._build_identity_row())

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        self.splitter = splitter

        danmaku_pane = QWidget()
        danmaku_layout = QVBoxLayout(danmaku_pane)
        danmaku_layout.setContentsMargins(0, 0, 0, 0)
        danmaku_layout.setSpacing(4)
        danmaku_layout.addLayout(self._build_danmaku_header())
        danmaku_layout.addWidget(self._build_danmaku())
        splitter.addWidget(danmaku_pane)

        event_pane = QWidget()
        event_layout = QVBoxLayout(event_pane)
        event_layout.setContentsMargins(0, 0, 0, 0)
        event_layout.setSpacing(4)
        event_layout.addLayout(self._build_event_header())
        self.event_view = self._build_event_view("event", "进场 / 礼物 / 上舰会显示在这里")
        event_layout.addWidget(self.event_view)
        splitter.addWidget(event_pane)

        sc_pane = QWidget()
        sc_layout = QVBoxLayout(sc_pane)
        sc_layout.setContentsMargins(0, 0, 0, 0)
        sc_layout.setSpacing(4)
        sc_header = QHBoxLayout()
        sc_header.setContentsMargins(0, 0, 0, 0)
        sc_header.setSpacing(6)
        sc_header.addWidget(self._make_drag_handle("sc"))
        self.sc_title = self._section_label("醒目留言 0")
        sc_header.addWidget(self.sc_title)
        sc_header.addStretch(1)
        sc_layout.addLayout(sc_header)
        sc_layout.addWidget(self._build_sc_list())
        splitter.addWidget(sc_pane)

        self._panes = {"danmaku": danmaku_pane, "event": event_pane, "sc": sc_pane}
        self._attach_clear_menu()

        splitter.setSizes([340, 170, 150])
        root.addWidget(splitter, 1)

        root.addLayout(self._build_send_row())

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self.grip = QSizeGrip(panel)
        self.grip.setFixedSize(12, 12)
        grip_row.addWidget(self.grip)
        root.addLayout(grip_row)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(6)

        self.dot = QLabel("●")
        self.dot.setObjectName("dead")
        header.addWidget(self.dot)

        self.title_label = QLabel(TITLE)
        self.title_label.setObjectName("title")
        # 标题只是装饰：窗口拖窄时让它先让位，别把整行撑住（窄了会被藏起来）
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.title_label.setMinimumWidth(0)
        header.addWidget(self.title_label)
        header.addStretch(1)

        self.pin_btn = QPushButton()
        self.pin_btn.setObjectName("ghost")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(True)
        # 固定宽度：切换文案时按钮不应带着整行抖一下
        self.pin_btn.setFixedWidth(58)
        self.pin_btn.clicked.connect(self._toggle_topmost)
        header.addWidget(self.pin_btn)
        self._sync_pin_button()

        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("ghost")
        self.close_btn.clicked.connect(self.close)
        header.addWidget(self.close_btn)
        return header

    def _build_identity_row(self) -> QHBoxLayout:
        """发送身份：用哪个账号发、戴哪个粉丝牌。"""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)

        self.account_box = QComboBox()
        # 账号 / 粉丝牌两个下拉原来写死宽度，直接把窗口宽度卡死；
        # 改成「窄能缩、宽到原来的尺寸为止」
        self.account_box.setMinimumWidth(92)
        self.account_box.setMaximumWidth(178)
        self.account_box.setToolTip("发弹幕用的账号（来自设置里的账号目录）")
        self.account_box.currentIndexChanged.connect(self._on_account_changed)
        # 两个下拉各占一半可用宽度：窗口宽时回到原来的尺寸（有 max 卡着），
        # 拖窄时按指数一起缩，不会把整行撑住
        self.account_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.account_box, 1)
        self.account_arrow = self._combo_arrow(self.account_box)

        self.medal_box = QComboBox()
        self.medal_box.setMinimumWidth(104)
        self.medal_box.setMaximumWidth(186)
        self.medal_box.setToolTip(
            "默认是账号当前戴着的牌子；换一个会立刻改掉账号上的佩戴（弹幕上显示的就是它）"
        )
        self.medal_box.currentIndexChanged.connect(self._on_medal_changed)
        self.medal_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.medal_box, 1)
        self.medal_arrow = self._combo_arrow(self.medal_box)

        # 账号和牌子的状态都在构造完控件后才能填，这里先压一次，避免闪空下拉
        self._populate_accounts()
        self._fill_medal_box()
        return row

    def _combo_arrow(self, combo: QComboBox) -> QLabel:
        """给下拉框贴一个自绘的 ▾。

        QSS 里 down-arrow 被置空（系统画的那个三角在暗色底上几乎看不见），
        所以统一盖一个透明 label，颜色跟着主题走。
        """
        arrow = QLabel("▾", combo)
        arrow.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        arrow.setAlignment(Qt.AlignCenter)
        arrow.setGeometry(combo.width() - 18, 0, 16, combo.height())
        combo.installEventFilter(ComboArrowFollower(arrow))
        return arrow

    def _build_send_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)

        self.send_input = QLineEdit()
        self.send_input.setPlaceholderText(f"在这发弹幕，Enter 发送（最多 {SEND_MAX_LEN} 字）")
        self.send_input.setMaxLength(SEND_MAX_LEN)
        self.send_input.returnPressed.connect(self._on_send)
        row.addWidget(self.send_input, 1)

        self.emote_btn = QPushButton("表情")
        self.emote_btn.setObjectName("ghost")
        self.emote_btn.setFixedWidth(38)
        self.emote_btn.setToolTip("B 站表情（含本房间专属和账号已解锁的表情包）")
        self.emote_btn.clicked.connect(self._open_emoticons)
        row.addWidget(self.emote_btn)

        # 发送和它右边的「▾」拼成一个按钮：颜色、位置都收进这个下拉，
        # 窄窗口下能省出原来色块 + 位置下拉占的两格
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.setProperty("joined", "left")
        self.send_btn.setProperty("active", "false")
        self.send_btn.clicked.connect(self._on_send)
        self.send_btn.setToolTip(self._send_hint())
        row.addWidget(self.send_btn)

        self.send_more = QPushButton("▾")
        self.send_more.setObjectName("sendmore")
        self.send_more.setFixedWidth(18)
        self.send_more.setCursor(Qt.PointingHandCursor)
        self.send_more.setToolTip("弹幕颜色与位置")
        self.send_more.clicked.connect(self._open_send_options)
        row.addWidget(self.send_more)
        return row

    # ----- 账号 -----

    def _preferred_account(self):
        """优先用上次选的那个账号，否则用目录里第一个可用的。"""
        path = str(self.cfg.get("account_path") or "")
        for account in self.usable:
            if account.path == path:
                return account
        return self.usable[0] if self.usable else None

    def _current_account(self):
        path = self.account_box.currentData()
        for account in self.usable:
            if account.path == path:
                return account
        return self._preferred_account()

    def _populate_accounts(self) -> None:
        """把扫到的账号填进下拉；昵称还没验证出来时先显示文件名。"""
        preferred = str(self.cfg.get("account_path") or "")
        self.account_box.blockSignals(True)
        self.account_box.clear()
        for account in self.usable:
            self.account_box.addItem(account.display, account.path)
            index = self.account_box.count() - 1
            tip = account.path if account.display == account.label else f"{account.label}\n{account.path}"
            self.account_box.setItemData(index, tip, Qt.ToolTipRole)

        if not self.usable:
            self.account_box.addItem("未找到账号", "")
            self.account_box.setEnabled(False)
            self.account_box.setToolTip(
                f"账号目录里没有可用的 cookie 文件：{self.cfg.get('cookie_dir') or '（未设置）'}"
            )
        else:
            self.account_box.setEnabled(True)
            self.account_box.setToolTip("发弹幕用的账号（来自设置里的账号目录）")
            index = self.account_box.findData(preferred)
            self.account_box.setCurrentIndex(index if index >= 0 else 0)
        self.account_box.blockSignals(False)

    def _on_account_changed(self, _index: int = -1) -> None:
        account = self._current_account()
        if account is not None and account.path != str(self.cfg.get("account_path") or ""):
            self.cfg["account_path"] = account.path
            self._save_config_quiet()
        self._use_account(account)

    def _use_account(self, account) -> None:
        """换成这个账号：重建发送器，表情包、可用颜色、粉丝牌全部作废重拉。"""
        was_connected = self._room_ready()
        self.sender = DanmakuSender(self.cfg, account)
        self._sender_path = account.path if account is not None else ""
        self._emote_room = 0
        self._emote_packages = []
        self._emote_message = ""
        self._send_options = None
        self._mode_locked = False
        self._medals = []
        self._fill_medal_box()
        self._refresh_send_state()
        if self._room_ready():
            self._refresh_room_context(force=True)
            self._request_medals()
        self._request_account_meta()
        # 采集用的身份是连接时定下的：正在收的时候换账号，得重连才会生效
        if was_connected and (self.service.running or self._mock_thread is not None):
            self._append_sys("已切换账号：点一次「断开」再「连接」，采集才会用这个身份")

    def _request_account_meta(self) -> None:
        """后台补昵称和头像；缓存还新鲜的账号不问 B 站。"""
        if self._account_future is not None or not self.usable:
            return
        pending = [item for item in self.usable if not item.meta_is_fresh(self._meta)]
        if not pending:
            return
        self._account_future = self._account_pool.submit(self._verify_accounts, pending)

    def _verify_accounts(self, accounts: list) -> list[tuple[str, str, str, int, bool]]:
        """后台线程：逐个账号问一次 nav，绝不碰控件。"""
        out: list[tuple[str, str, str, int, bool]] = []
        for account in accounts:
            sender = DanmakuSender(self.cfg, account)
            ok, _info = sender.verify()
            out.append((account.path, sender.uname, sender.face, sender.uid, ok))
        return out

    def _poll_account_meta(self) -> None:
        if self._account_future is None or not self._account_future.done():
            return
        future, self._account_future = self._account_future, None
        try:
            results = future.result()
        except Exception:  # noqa: BLE001 - 补资料失败不该影响发弹幕
            return

        by_path = {item.path: item for item in self.usable}
        changed = False
        for path, uname, face, uid, ok in results:
            account = by_path.get(path)
            if account is None or not ok or not uname:
                continue
            account.uname, account.face, account.uid = uname, face, uid
            account.store_meta(self._meta)
            changed = True
        if not changed:
            return

        save_meta(self._meta_path, self._meta)
        # 补资料的是当前账号时，顺手把发送器上的名字同步过去，tooltip 才不会一直显示空
        current = self._current_account()
        if current is not None and current.path == self._sender_path:
            self.sender.uname = current.uname
            self.sender.face = current.face
            self.sender.uid = current.uid
        self.account_box.blockSignals(True)
        for index, account in enumerate(self.usable):
            if index < self.account_box.count():
                self.account_box.setItemText(index, account.display)
        self.account_box.blockSignals(False)
        self._avatar_done.clear()
        self._refresh_avatars()
        self._refresh_send_state()

    def _refresh_avatars(self) -> None:
        """头像下完就贴到下拉项上；没下完的先登记，下一轮再看。"""
        for index, account in enumerate(self.usable):
            url = account.face
            if not url or url in self._avatar_done or index >= self.account_box.count():
                continue
            if not self._avatar_images.settled(url):
                self._avatar_images.want(url)
                continue
            image = self._avatar_images.image(url)
            if image is not None and not image.isNull():
                self.account_box.setItemIcon(index, QIcon(QPixmap.fromImage(image)))
            self._avatar_done.add(url)

    # ----- 粉丝牌 -----

    def _request_medals(self) -> None:
        if self._medal_future is not None:
            return
        if not self.sender.has_login:
            self._medals = []
            self._fill_medal_box()
            return
        if self._medals:
            # 牌子是账号级的，换房间只会影响「本房间」标注，重填就行
            self._fill_medal_box()
            return
        self._fill_medal_box(loading=True)
        self._medal_future = self._account_pool.submit(self.sender.fetch_medals)

    def _poll_medals(self) -> None:
        if self._medal_future is None or not self._medal_future.done():
            return
        future, self._medal_future = self._medal_future, None
        try:
            self._medals = future.result()
            # 记住服务端当前戴着哪枚：默认选中它时就不用再 wear 一次
            for medal in self._medals:
                if medal.get("wearing"):
                    self.sender.worn_medal = int(medal["medal_id"])
                    break
        except SendError as exc:
            self._medals = []
            self._append_sys(f"粉丝牌列表拉取失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 拉不到牌子不影响发弹幕
            self._medals = []
            self._append_sys(f"粉丝牌列表拉取失败：{type(exc).__name__}: {exc}")
        self._fill_medal_box()

    def _fill_medal_box(self, *, loading: bool = False) -> None:
        keep = int(self.cfg.get("send_medal") or 0)
        self.medal_box.blockSignals(True)
        self.medal_box.clear()
        if loading:
            self.medal_box.addItem("拉取粉丝牌…", 0)
            self.medal_box.setEnabled(False)
            self.medal_box.setToolTip("正在拉取该账号的粉丝牌")
        elif not self._room_ready():
            self.medal_box.addItem("连接后可选牌子", 0)
            self.medal_box.setEnabled(False)
            self.medal_box.setToolTip("粉丝牌按账号查询，连上直播间再选")
        elif not self._medals:
            self.medal_box.addItem("无粉丝牌", 0)
            self.medal_box.setEnabled(False)
            self.medal_box.setToolTip("该账号没有粉丝牌，发的弹幕不会带牌子")
        else:
            room = self.state.real_room
            worn = 0
            for medal in self._medals:
                label = f"{medal['name']} Lv{medal['level']}"
                if medal.get("wearing"):
                    worn = medal["medal_id"]
                # 两个标记都挂上会撑出下拉框，本房间的优先显示
                if room and medal["room_id"] == room:
                    label += "（本房间）"
                elif medal.get("wearing"):
                    label += "（佩戴中）"
                self.medal_box.addItem(label, medal["medal_id"])
                self.medal_box.setItemData(
                    self.medal_box.count() - 1,
                    f"{medal['anchor']} · 房间 {medal['room_id']}",
                    Qt.ToolTipRole,
                )
            # 默认落在账号当前戴着的那枚（没记过选择时），不要凭空改成别的
            index = self.medal_box.findData(keep or worn)
            self.medal_box.setCurrentIndex(index if index >= 0 else 0)
            self.medal_box.setEnabled(True)
            self.medal_box.setToolTip(
                f"账号共 {len(self._medals)} 枚粉丝牌，默认是你现在戴着的那枚；"
                "选了别的会在发送前先切换。\n"
                "注意：佩戴是全局状态，网页端也会跟着变；而在这个房间发弹幕时，"
                "B 站固定显示你在这个主播的牌子，切换改不了它"
            )
        self.medal_box.blockSignals(False)

    def _on_medal_changed(self) -> None:
        medal_id = int(self.medal_box.currentData() or 0)
        if int(self.cfg.get("send_medal") or 0) == medal_id:
            return
        self.cfg["send_medal"] = medal_id
        self._save_config_quiet()
        # 选了就立刻戴：弹幕显示哪枚牌子完全由佩戴状态决定，早点换过来，
        # 免得要等到发下一条才生效（发完再换回来这种"临时佩戴"B站不支持）。
        if medal_id and self.medal_box.isEnabled():
            self._wear_medal_now(medal_id)

    def _medal_label(self, medal_id: int) -> str:
        for medal in self._medals:
            if int(medal["medal_id"]) == int(medal_id):
                return f"{medal['name']} Lv{medal['level']}"
        return f"粉丝牌 {medal_id}"

    def _wear_medal_now(self, medal_id: int) -> None:
        """把账号全局佩戴换成这枚牌子（后台线程，不卡界面）。"""
        if self._wear_future is not None or not self.sender.has_login:
            return
        if not self._room_ready():
            return
        if self.sender.worn_medal == medal_id:
            return
        self._wear_target = int(medal_id)
        sender = self.sender
        self._wear_future = self._account_pool.submit(self._wear_worker, sender, int(medal_id))

    def _wear_worker(self, sender: DanmakuSender, medal_id: int) -> tuple[bool, str]:
        try:
            sender.wear_medal(medal_id)
            return True, ""
        except SendError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反馈到界面
            return False, f"{type(exc).__name__}: {exc}"

    def _poll_wear(self) -> None:
        if self._wear_future is None or not self._wear_future.done():
            return
        future, self._wear_future = self._wear_future, None
        try:
            ok, payload = future.result()
        except Exception as exc:  # noqa: BLE001
            ok, payload = False, f"{type(exc).__name__}: {exc}"
        target = self._wear_target
        if ok:
            # 「（佩戴中）」「（本房间）」这些标注得跟着换一枚，不然读起来自相矛盾
            for medal in self._medals:
                medal["wearing"] = int(medal["medal_id"]) == target
            self._fill_medal_box()
            self._append_sys(
                f"已换成 {self._medal_label(target)}，账号上佩戴的粉丝牌跟着改了"
            )
        else:
            # 没戴上不改下拉：这条还会在下次发送时再试一次
            self._append_sys(f"切换粉丝牌失败：{payload}（发送时会再试一次）")

    def _selected_medal(self) -> int:
        if not self.medal_box.isEnabled():
            return 0
        return int(self.medal_box.currentData() or 0)

    # ----- 发送选项：颜色 / 位置（都收在发送按钮的「▾」里） -----

    def _send_mode(self) -> int:
        return int(self._send_mode_value)

    def _set_send_mode(self, mode: int, menu: QMenu | None = None) -> None:
        self._send_mode_value = int(mode)
        self.cfg["send_mode"] = self._send_mode_value
        self._save_config_quiet()
        self._sync_send_hint()
        if menu is not None:
            menu.close()

    def _mode_choices(self) -> list[tuple[str, int]]:
        """这个房间允许的位置；没取到配置就用内置三档。"""
        options = self._send_options or {}
        modes = [
            (str(item.get("name") or item["mode"]), int(item["mode"]))
            for item in options.get("modes") or []
            if item.get("status")
        ]
        return modes or [(label, mode) for mode, label in MODES]

    def _color_choices(self) -> list[tuple[str, int]]:
        """本房间这个账号能用的颜色；拿不到配置就退回内置色板。"""
        options = self._send_options or {}
        colors = [
            (item["name"] or f"#{item['color']:06x}", item["color"])
            for item in options.get("colors") or []
            if item.get("status")
        ]
        return colors or list(COLOR_PRESETS)

    def _set_send_color(self, value: int, save: bool = True, menu: QMenu | None = None) -> None:
        self.send_color = int(value) & 0xFFFFFF
        self._sync_send_hint()
        if save:
            self.cfg["send_color"] = self.send_color
            self._save_config_quiet()
        if menu is not None:
            menu.close()

    def _sync_send_hint(self) -> None:
        """颜色和位置都收进下拉了，至少让按钮的悬停提示能看见当前值。"""
        mode = {m: label for label, m in self._mode_choices()}.get(self._send_mode(), "")
        extra = f"颜色 #{self.send_color:06x}" + (f" · 位置 {mode}" if mode else "")
        self.send_btn.setToolTip(f"{self._send_hint()}\n{extra}")
        self.send_more.setToolTip(f"弹幕颜色与位置\n{extra}")

    @staticmethod
    def _menu_label(text: str, name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        return label

    @staticmethod
    def _menu_line() -> QFrame:
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: rgba(128, 128, 128, 72); border: none;")
        return line

    def _menu_skin(self) -> dict[str, str]:
        """下拉面板的配色。

        右键菜单一直是系统样式（跟着系统深浅色走），这张面板里自己画了
        色块和按钮，得按窗口当前主题上色，不然暗色主题下会有一块亮底。
        """
        if self.theme == "dark":
            return {
                "menu": "#1b1f26",
                "border": "rgba(255, 255, 255, 46)",
                "section": "#9aa3b2",
                "value": "#7d8797",
                "note": "#6b7280",
                "choice_bg": "rgba(255, 255, 255, 18)",
                "choice_fg": "#c3c8d1",
                "choice_border": "rgba(255, 255, 255, 30)",
                "choice_hi": "rgba(255, 255, 255, 36)",
                "choice_on_bg": "rgba(47, 111, 237, 88)",
                "choice_on_fg": "#ffffff",
                "choice_on_border": "rgba(120, 166, 255, 150)",
            }
        return {
            "menu": "#ffffff",
            "border": "#dbe0e8",
            "section": "#4b5563",
            "value": "#6b7280",
            "note": "#8a93a3",
            "choice_bg": "#eef1f6",
            "choice_fg": "#3b4250",
            "choice_border": "#dbe0e8",
            "choice_hi": "#e3e9f2",
            "choice_on_bg": "#d7e4ff",
            "choice_on_fg": "#1d4ed8",
            "choice_on_border": "#8fb4f8",
        }

    def _build_send_menu(self) -> QMenu:
        """颜色 + 位置合成的一张面板，点「▾」时弹出来。"""
        menu = QMenu(self)
        skin = self._menu_skin()
        menu.setStyleSheet(
            f"QMenu {{ background: {skin['menu']}; border: 1px solid {skin['border']};"
            " border-radius: 8px; padding: 2px; }"
        )
        holder = QWidget()
        holder.setStyleSheet(
            f"QLabel#menuSection {{ color: {skin['section']}; font-size: 11px;"
            " font-weight: 600; }"
            f"QLabel#menuValue {{ color: {skin['value']}; font-size: 10px; }}"
            f"QLabel#menuNote {{ color: {skin['note']}; font-size: 10px; }}"
            f"QPushButton#choice {{ background: {skin['choice_bg']};"
            f" color: {skin['choice_fg']}; border: 1px solid {skin['choice_border']};"
            " border-radius: 6px; padding: 3px 10px; font-size: 11px; }"
            f"QPushButton#choice:hover {{ background: {skin['choice_hi']}; }}"
            f"QPushButton#choice:checked {{ background: {skin['choice_on_bg']};"
            f" color: {skin['choice_on_fg']}; border-color: {skin['choice_on_border']}; }}"
            f"QPushButton#choice:disabled {{ color: {skin['note']}; }}"
        )
        box = QVBoxLayout(holder)
        box.setContentsMargins(10, 9, 10, 9)
        box.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(self._menu_label("弹幕颜色", "menuSection"))
        head.addStretch(1)
        head.addWidget(self._menu_label(f"#{self.send_color:06x}", "menuValue"))
        box.addLayout(head)

        choices = self._color_choices()
        total = len((self._send_options or {}).get("colors") or [])
        grid = QGridLayout()
        grid.setSpacing(4)
        for i, (name, value) in enumerate(choices):
            # 选中的那一格加一圈亮边：20px 的小块光看颜色不知道选没选
            picked = value == self.send_color
            swatch = QPushButton()
            swatch.setFixedSize(20, 20)
            swatch.setToolTip(f"{name} #{value:06x}")
            swatch.setCursor(Qt.PointingHandCursor)
            swatch.setStyleSheet(
                f"background:#{value:06x}; padding:0; border-radius:4px; border:2px solid "
                + ("#ffffff" if picked else "rgba(128,128,128,110)")
            )
            swatch.clicked.connect(
                lambda _=False, v=value: self._set_send_color(v, menu=menu)
            )
            grid.addWidget(swatch, i // 5, i % 5)
        box.addLayout(grid)

        if total == 1:
            note = "该房间只允许这一种颜色"
        elif total:
            note = f"本房间可用 {len(choices)} 种颜色"
        else:
            note = "未取到房间配置，先给默认色板"
        box.addWidget(self._menu_label(note, "menuNote"))

        custom = QPushButton("自定义颜色…")
        custom.setObjectName("ghost")
        custom.clicked.connect(lambda _=False: self._pick_custom_color(menu))
        box.addWidget(custom)

        box.addWidget(self._menu_line())

        box.addWidget(self._menu_label("弹幕位置", "menuSection"))
        modes = QHBoxLayout()
        modes.setSpacing(4)
        for name, mode in self._mode_choices():
            btn = QPushButton(name)
            btn.setObjectName("choice")
            btn.setCheckable(True)
            btn.setChecked(mode == self._send_mode())
            btn.setEnabled(not self._mode_locked)
            btn.clicked.connect(lambda _=False, m=mode: self._set_send_mode(m, menu))
            modes.addWidget(btn)
        modes.addStretch(1)
        box.addLayout(modes)
        if self._mode_locked:
            box.addWidget(self._menu_label("该房间只开放这一种位置", "menuNote"))

        action = QWidgetAction(menu)
        action.setDefaultWidget(holder)
        menu.addAction(action)
        menu.adjustSize()
        return menu

    def _open_send_options(self) -> None:
        menu = self._build_send_menu()
        # 贴在「▾」正下方、右侧对齐：面板比按钮宽，往左伸才不会跑出屏幕
        menu.exec(
            self.send_more.mapToGlobal(
                QPoint(self.send_more.width() - menu.width(), self.send_more.height() + 4)
            )
        )

    def _pick_custom_color(self, menu: QMenu | None = None) -> None:
        # 先关菜单，否则取色对话框会盖在菜单上、关掉后菜单又冒出来
        if menu is not None:
            menu.close()
        picked = QColorDialog.getColor(
            QColor(f"#{self.send_color:06x}"), self, "弹幕颜色"
        )
        if picked.isValid():
            self._set_send_color(picked.rgb() & 0xFFFFFF)

    # ----- 表情 -----

    def _emoticon_cache_path(self, room: int) -> str:
        base = os.path.join(os.path.dirname(self.config_path), "cache")
        # 带上账号指纹：大号和小号能用的表情包不是一个集合
        return os.path.join(base, f"emoticons_{int(room)}_{self.sender.cache_key}.json")

    def _emoticon_room(self) -> int:
        room = self._send_room()
        if room:
            return room
        rooms = self.cfg.get("rooms") or []
        return int(rooms[0]) if rooms else 0

    def _open_emoticons(self) -> None:
        if self._emoticon_panel is None:
            self._emoticon_panel = EmoticonPanel(self._emote_images, self)
            self._emoticon_panel.picked.connect(self._on_emoticon_picked)
        self._emoticon_panel.apply_theme(self._c)

        self._refresh_room_context()
        if not self._emoticon_room():
            self._emoticon_panel.set_packages(
                [], "没有房间配置：先连一次真实房间，表情包会记下来"
            )
            self._emoticon_panel.popup_at(self.emote_btn)
            return
        self._emoticon_panel.set_packages(
            self._emote_packages, self._emote_message or "正在拉取表情…"
        )
        self._emoticon_panel.popup_at(self.emote_btn)

    def _refresh_room_context(self, force: bool = False) -> None:
        """房间变了就重拉表情包和可用颜色/位置。"""
        room = self._emoticon_room()
        if not room:
            return
        if not force and room == self._emote_room:
            return
        self._request_room_context(room)

    def _request_room_context(self, room: int) -> None:
        self._emote_room = room
        self._emote_message = ""
        self._emote_packages = self._read_emoticon_cache(room)
        self._send_options = None
        if self._context_future is not None:
            return
        if not self.sender.has_login:
            self._emote_message = "未配置 cookie，拉不到表情包"
            return
        if not self._emote_packages:
            self._emote_message = "正在拉取表情…"
        self._context_future = self._emote_pool.submit(self._fetch_room_context, room)

    def _fetch_room_context(self, room: int) -> tuple[list[dict], dict | None, str]:
        """后台线程：表情包 + 可用颜色/位置。两者互不影响，缺一个另一个照用。"""
        packages: list[dict] = []
        error = ""
        try:
            packages = self.sender.fetch_emoticons(room)
        except SendError as exc:
            error = str(exc)
        try:
            options: dict | None = self.sender.fetch_send_config(room)
        except SendError:
            options = None
        return packages, options, error

    def _apply_send_options(self) -> None:
        """按服务端给的可用性收窄位置选项和颜色面板。"""
        options = self._send_options or {}

        modes = [item for item in options.get("modes") or [] if item.get("status")]
        if modes:
            allowed = [int(item["mode"]) for item in modes]
            # 房间换了、允许的位置跟着变：当前那个不给用就落到第一个可用的
            if self._send_mode() not in allowed:
                self._send_mode_value = allowed[0]
            self._mode_locked = len(modes) == 1
            self.cfg["send_mode"] = self._send_mode()

        colors = [item for item in options.get("colors") or [] if item.get("status")]
        if colors and not any(item["color"] == self.send_color for item in colors):
            # 当前选的颜色房间里不给用，服务端会悄悄改白色，不如直接切成可用的
            self._set_send_color(colors[0]["color"])

        self._sync_send_hint()

    def _on_emoticon_picked(self, item: dict) -> None:
        if self._emoticon_panel is not None:
            self._emoticon_panel.hide()
        text = str(item.get("text") or "")
        msg = str(item.get("msg") or text)
        dm_type = int(item.get("dm_type") or 0)
        current = self.send_input.text().strip()
        if current:
            if dm_type:
                # 图片表情的 msg 是唯一 ID，混在文字里发出去只会变成 ID 或被拒
                self._append_sys("图片表情只能单独发：先把输入框里的字发掉或清空")
                return
            # 已经在写别的：插到光标处，让人接着写完再一起发
            if len(current) + len(text) > SEND_MAX_LEN:
                self._append_sys(
                    f"一条最多 {SEND_MAX_LEN} 字，先把输入框里的发掉或删几个字再插表情"
                )
                return
            self.send_input.insert(text)
            self.send_input.setFocus()
            return
        # 输入框空着就是要发表情，直接发，绕开输入框的长度截断
        self._submit_send(msg, display=text, dm_type=dm_type, emote_url=str(item.get("url") or ""))

    def _poll_room_context(self) -> None:
        if self._context_future is None or not self._context_future.done():
            return
        future, self._context_future = self._context_future, None
        room = self._emote_room
        try:
            packages, options, error = future.result()
        except Exception as exc:  # noqa: BLE001 - 界面不能因为拉不到表情挂掉
            packages, options, error = [], None, f"{type(exc).__name__}: {exc}"

        if room != self._emote_room:
            # 拉的过程中又换了房间，这批结果作废，按新房间重来
            self._request_room_context(self._emote_room)
            return

        if options:
            self._send_options = options
            self._apply_send_options()
        if packages:
            self._emote_packages = packages
            self._emote_message = ""
            self._write_emoticon_cache(room, packages)
        else:
            self._emote_message = error or "没有可用表情"
            self._emote_room = 0  # 下次点表情可以重试

        if self._emoticon_panel is not None and self._emoticon_panel.isVisible():
            self._emoticon_panel.set_packages(
                self._emote_packages, self._emote_message or "没有可用表情"
            )

    def _read_emoticon_cache(self, room: int) -> list[dict]:
        if not room:
            return []
        try:
            with open(self._emoticon_cache_path(room), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        packages = data.get("packages") if isinstance(data, dict) else None
        if not isinstance(packages, list):
            return []
        for pkg in packages:
            if not isinstance(pkg, dict) or not isinstance(pkg.get("items"), list):
                return []
        return packages

    def _write_emoticon_cache(self, room: int, packages: list[dict]) -> None:
        if not room or not packages:
            return
        path = self._emoticon_cache_path(room)
        temp = path + ".part"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"fetched": int(time.time()), "packages": packages},
                    fh,
                    ensure_ascii=False,
                )
            os.replace(temp, path)
        except OSError:
            pass

    def _send_hint(self) -> str:
        if self._test_mode:
            return "0 号直播间是本地测试：发送只在本机回显，不会发到 B 站"
        if not self.state.real_room:
            return "先连接直播间才能发弹幕（避免发错房间）"
        if not self.sender.has_login:
            return "账号目录里没有可用账号：设置里指定目录，放入导出的 cookie 文件"
        who = f"以 {self.sender.uname} 的身份发送" if self.sender.uname else "用当前账号发送"
        return f"{who}；B 站限制 {SEND_MAX_LEN} 字、约 1 秒一条"

    def _reload_sender(self) -> None:
        """设置里改过账号目录后重扫一遍，不必重启。"""
        self.accounts = scan_accounts(self.cfg.get("cookie_dir") or "", self._meta)
        self.usable = [item for item in self.accounts if not item.error]
        self._avatar_done.clear()
        self._populate_accounts()
        self._use_account(self._current_account())

        broken = [item for item in self.accounts if item.error]
        if broken:
            self._append_sys(
                "这些文件没被当成账号："
                + "、".join(f"{item.label}（{item.error}）" for item in broken)
            )
        if self.usable:
            self._append_sys(f"账号目录已加载 {len(self.usable)} 个账号")
        else:
            self._append_sys(
                f"账号目录里没找到可用的 cookie 文件：{self.cfg.get('cookie_dir')}"
            )
        self._request_account_meta()

    def _room_ready(self) -> bool:
        """已经连进真实房间，或者正在 0 号直播间做本地测试。"""
        return bool(self.state.real_room) or self._test_mode

    def _send_room(self) -> int:
        """发送目标：只认已经连上的真实房间号。

        以前没连接时拿输入框里填的数字当目标，等于让界面自己猜发到哪：
        少按一位就可能发进别人房间，陌生房间的发言还容易触发风控。
        现在必须真正连进某个房间才允许发送。

        0 号直播间不连网络，所以这里返回 0；真正的目标由 ``_submit_send``
        按 ``_test_mode`` 分叉，测试模式下只在本地回显。
        """
        return self.state.real_room

    def _refresh_send_state(self) -> None:
        """按「已连上房间」和「有可用账号」两个条件开关整条发送行。"""
        room_ready = self._room_ready()
        # 测试模式不会真的发出去，没有 cookie 也允许点，方便调界面
        ready = room_ready and (self._test_mode or self.sender.has_login)
        busy = self._send_future is not None
        if self._test_mode:
            placeholder = f"0 号直播间测试：发送只在本机回显（最多 {SEND_MAX_LEN} 字）"
        elif not room_ready:
            placeholder = "先连接直播间，才能发弹幕"
        elif not self.sender.has_login:
            placeholder = "账号目录里没有可用账号"
        else:
            placeholder = f"在这发弹幕，Enter 发送（最多 {SEND_MAX_LEN} 字）"
        self.send_input.setPlaceholderText(placeholder)
        for widget in (self.send_input, self.emote_btn, self.send_more):
            widget.setEnabled(ready)
        self.send_btn.setEnabled(ready and not busy)
        self._sync_send_hint()
        self.send_input.setToolTip(self._send_hint())

    def _on_send(self) -> None:
        text = self.send_input.text().strip()
        if text:
            self._submit_send(text)

    def _submit_send(
        self, msg: str, *, display: str = "", dm_type: int = 0, emote_url: str = ""
    ) -> None:
        if self._send_future is not None:
            return
        if self._test_mode:
            # 测试模式：一步都不出网，直接在弹幕流里回显，方便核对颜色/位置/表情
            self._echo_local(msg, display or msg, dm_type, emote_url)
            return
        room = self._send_room()
        if not room:
            self._append_sys("发送失败：还没有连上任何直播间，发送行已锁住")
            return
        self.send_btn.setEnabled(False)
        self.send_btn.setText("发送中…")
        self._send_future = self._send_pool.submit(
            self._send_worker, room, msg, self.send_color, self._send_mode(), dm_type,
            display or msg, self._selected_medal(),
        )

    def _echo_local(self, msg: str, display: str, dm_type: int, emote_url: str) -> None:
        """测试模式下把自己「发」的弹幕补进列表，标记成本人 + 测试。"""
        row = {
            "event_time": int(time.time()),
            "uname": self.sender.uname or "我",
            "uid": int(self.sender.uid or 0),
            "text": display,
            "is_emoticon": bool(dm_type and emote_url),
            "emote_url": emote_url if dm_type else "",
            "is_admin": False,
            "guard_level": 0,
            "medal_name": "",
            "medal_level": 0,
            "_self": True,
            "_test": True,
            "_color": self.send_color,
            "_mode": self._send_mode(),
        }
        # 选了牌子就把牌子也画上，测试时能看清带不带牌子
        medal_id = self._selected_medal()
        if medal_id:
            for medal in self._medals:
                if int(medal.get("medal_id") or 0) == medal_id:
                    row["medal_name"] = medal.get("name") or ""
                    row["medal_level"] = int(medal.get("level") or 0)
                    break
        self.state.messages += 1
        self.danmaku_title.setText(f"弹幕 {self.state.messages}")
        for url in self._row_image_urls(row):
            self._images.want(url)
        self._insert_danmaku([row])

        if self.send_input.text().strip() == msg:
            self.send_input.clear()
        self.send_btn.setText("已发送")
        QTimer.singleShot(1200, lambda: self.send_btn.setText("发送"))
        self._append_sys(f"测试发送（未出网）：{display}")

    def _send_worker(
        self,
        room: int,
        msg: str,
        color: int,
        mode: int,
        dm_type: int,
        display: str,
        medal_id: int,
    ) -> tuple[bool, str, str]:
        """在后台线程发，绝不碰界面；返回 ``(成功?, 提示文本/错误, 附注)``。"""
        note = ""
        try:
            if not self.sender.uname:
                ok, info = self.sender.verify()
                if not ok:
                    return False, info, ""
            # 选了牌子就先戴：佩戴是全局状态，戴不上不该拦住这条弹幕，只记一笔
            if medal_id and self.sender.worn_medal != medal_id:
                try:
                    self.sender.wear_medal(medal_id)
                except SendError as exc:
                    note = f"（粉丝牌没戴上：{exc}）"
            self.sender.send(room, msg, color=color, mode=mode, dm_type=dm_type)
            return True, display, note
        except SendError as exc:
            return False, str(exc), ""
        except Exception as exc:  # noqa: BLE001 - 任何异常都要反馈到界面
            return False, f"{type(exc).__name__}: {exc}", ""

    def _poll_send(self) -> None:
        if self._send_future is None or not self._send_future.done():
            return
        future, self._send_future = self._send_future, None
        try:
            ok, payload, note = future.result()
        except Exception as exc:  # noqa: BLE001 - worker 之外的意外，也要回到可发送状态
            ok, payload, note = False, f"{type(exc).__name__}: {exc}", ""

        self.send_btn.setText("发送")
        # 恢复可用性：是否连上、是否还在发，都以真实状态为准
        self._refresh_send_state()
        if ok:
            # 只有内容没被改过才清空，避免把用户新打的字冲掉
            if self.send_input.text().strip() == payload:
                self.send_input.clear()
            self.send_btn.setText("已发送")
            QTimer.singleShot(1200, lambda: self.send_btn.setText("发送"))
            if note:
                self._append_sys(f"已发送{note}：{payload}")
        else:
            self._append_sys(f"发送失败：{payload}")

    def _build_danmaku_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._make_drag_handle("danmaku"))
        self.danmaku_title = self._section_label("弹幕 0")
        row.addWidget(self.danmaku_title)
        row.addStretch(1)
        return row

    def _build_danmaku(self) -> QTextBrowser:
        self.danmaku = QTextBrowser()
        self.danmaku.setOpenLinks(False)
        self.danmaku.setOpenExternalLinks(False)
        self.danmaku.setFrameShape(QFrame.NoFrame)
        self.danmaku.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.danmaku.document().setMaximumBlockCount(MAX_DANMAKU_BLOCKS)
        self.danmaku.setPlaceholderText("弹幕会显示在这里")
        self.danmaku.anchorClicked.connect(self._open_profile)

        # Colours are applied by _apply_theme() so they follow the active palette.
        self._fmt_user = QTextCharFormat()
        self._fmt_time = QTextCharFormat()
        self._fmt_sc = QTextCharFormat()
        self._fmt_sys = QTextCharFormat()
        return self.danmaku

    def _build_event_view(self, name: str, placeholder: str) -> QTextBrowser:
        view = QTextBrowser()
        view.setObjectName(name)
        view.setOpenLinks(False)
        view.setOpenExternalLinks(False)
        view.setFrameShape(QFrame.NoFrame)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        view.document().setMaximumBlockCount(MAX_EVENT_BLOCKS)
        view.setPlaceholderText(placeholder)
        return view

    def _build_event_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self._make_drag_handle("event"))
        self.event_title = self._section_label("事件")
        row.addWidget(self.event_title)
        row.addStretch(1)
        self.event_filters: dict[str, QCheckBox] = {}
        # Enter events are the noisiest, so they start hidden.
        saved_filters = self.cfg.get("event_filter") or {}
        for key, label in (("enter", "进场"), ("gift", "礼物"), ("guard", "上舰"), ("delete", "删除")):
            box = QCheckBox(label)
            box.setChecked(bool(saved_filters.get(key, key != "enter")))
            box.setToolTip("勾选后才会显示并写入数据库")
            box.toggled.connect(lambda on, k=key: self._on_event_filter_changed(k, on))
            self.event_filters[key] = box
            row.addWidget(box)
        self._sync_event_filter()
        return row

    def _on_event_filter_changed(self, kind: str, checked: bool) -> None:
        self.cfg.setdefault("event_filter", {})[kind] = checked
        self._sync_event_filter()
        self._save_config_quiet()

    def _sync_event_filter(self) -> None:
        """Push what is actually visible into the collector (which also stores it)."""
        pane_on = self._pane_visible.get("event", True)
        for kind in ("enter", "gift", "guard", "delete"):
            box = self.event_filters.get(kind)
            self.service.event_filter[kind] = bool(
                pane_on and box is not None and box.isChecked()
            )

    def _event_visible(self, kind: str) -> bool:
        box = self.event_filters.get(kind)
        return box is None or box.isChecked()

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _build_sc_list(self) -> QTextBrowser:
        self.sc_view = QTextBrowser()
        self.sc_view.setOpenLinks(False)
        self.sc_view.setOpenExternalLinks(False)
        self.sc_view.setFrameShape(QFrame.NoFrame)
        self.sc_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.sc_view.document().setMaximumBlockCount(MAX_SC_BLOCKS)
        self.sc_view.setPlaceholderText("醒目留言会显示在这里")
        self.sc_view.anchorClicked.connect(self._open_profile)
        return self.sc_view

    def _build_control_row(self) -> QHBoxLayout:
        footer = QHBoxLayout()
        footer.setSpacing(6)

        self.room_input = QComboBox()
        self.room_input.setEditable(True)
        # 可压缩：窗口拖窄时它跟着缩，宽的时候仍是 148
        self.room_input.setMinimumWidth(96)
        self.room_input.setMaximumWidth(148)
        self.room_input.setToolTip(
            "可直接输入房间号；下拉列表可右键删除历史记录。填 0 是本地测试（回放 + 试发）"
        )
        self.room_input.lineEdit().setPlaceholderText("房间号")
        self.room_input.lineEdit().returnPressed.connect(self._toggle_connect)
        # 选中历史项后只把数字留在输入框，名字只在展开的列表里显示
        self.room_input.activated.connect(self._on_room_activated)
        self.room_input.view().viewport().installEventFilter(self)
        # QSS 无法稳定绘制下拉三角，用透明子标签叠在按钮区上（点击穿透到下拉区域）。
        self.room_arrow = QLabel("▾", self.room_input)
        self.room_arrow.setAlignment(Qt.AlignCenter)
        self.room_arrow.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.room_input.installEventFilter(ComboArrowFollower(self.room_arrow))
        self._fill_room_history()
        footer.addWidget(self.room_input)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setProperty("active", "false")
        self.connect_btn.clicked.connect(self._toggle_connect)
        footer.addWidget(self.connect_btn)

        self.pane_btn = QPushButton("栏目")
        self.pane_btn.setObjectName("ghost")
        menu = QMenu(self.pane_btn)
        for key, label in (("danmaku", "弹幕"), ("event", "事件"), ("sc", "醒目留言")):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(lambda on, k=key: self._set_pane_visible(k, on))
            menu.addAction(action)
            self._pane_actions[key] = action
        self.pane_btn.setMenu(menu)
        footer.addWidget(self.pane_btn)

        self.stat_label = QLabel("在线 0 · 舰长 0")
        self.stat_label.setObjectName("stat")
        self.stat_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # 同理：状态字是锦上添花，窄窗口下先收起来，别跟房间号抢地方
        self.stat_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.stat_label.setMinimumWidth(0)
        footer.addWidget(self.stat_label, 1)
        return footer

    def _attach_clear_menu(self) -> None:
        views = (
            (self.danmaku, self._clear_danmaku),
            (self.event_view, self._clear_events),
            (self.sc_view, self._clear_sc),
        )
        for view, handler in views:
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda pos, v=view, h=handler: self._show_clear_menu(v, pos, h)
            )

    def _show_clear_menu(self, view: QTextBrowser, pos: QPoint, handler) -> None:
        menu = QMenu(view)
        menu.addAction("设置…", self._open_settings)
        menu.addSeparator()
        copy_action = menu.addAction("复制", view.copy)
        copy_action.setEnabled(view.textCursor().hasSelection())
        menu.addSeparator()
        menu.addAction("清空本栏", handler)
        menu.exec(view.mapToGlobal(pos))

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction(
            "取消置顶" if self.pin_btn.isChecked() else "置顶",
            lambda: self._set_topmost(not self.pin_btn.isChecked()),
        )
        panes = menu.addMenu("栏目")
        for key in ("danmaku", "event", "sc"):
            action = self._pane_actions.get(key)
            if action is not None:
                panes.addAction(action)
        menu.addSeparator()
        menu.addAction("清空弹幕", self._clear_danmaku)
        menu.addAction("设置…", self._open_settings)
        menu.exec(event.globalPos())

    def _clear_danmaku(self) -> None:
        self.danmaku.clear()
        self._danmaku_pending.clear()
        self._pending_images = 0
        self.state.messages = 0
        self._dropped = 0
        self.danmaku_title.setText("弹幕 0")

    def _clear_events(self) -> None:
        self.event_view.clear()
        self.state.gifts = 0
        self.state.enters = 0

    def _clear_sc(self) -> None:
        self.sc_view.clear()
        self.state.sc = 0
        self.sc_title.setText("醒目留言 0")

    def _set_pane_visible(self, key: str, visible: bool) -> None:
        pane = self._panes.get(key)
        if pane is None or visible == self._pane_visible.get(key, True):
            return
        if not visible:
            self._remember_pane_sizes()
        pane.setVisible(visible)
        self._pane_visible[key] = visible
        if key == "event":
            self._sync_event_filter()
        if visible:
            self.splitter.setSizes(self._layout_sizes())
        self.cfg["panes"] = dict(self._pane_visible)
        try:
            save_gui_config(self.config_path, self.cfg)
        except OSError:
            pass

    def _restore_panes(self) -> None:
        saved = self.cfg.get("panes") or {}
        for key, action in self._pane_actions.items():
            if key in saved and not saved[key]:
                action.blockSignals(True)
                action.setChecked(False)
                action.blockSignals(False)
                self._set_pane_visible(key, False)

    def _make_drag_handle(self, key: str) -> QLabel:
        handle = QLabel("⠿")
        handle.setObjectName("grip")
        handle.setFixedWidth(12)
        handle.setToolTip("按住拖动，调整栏目顺序")
        handle.setCursor(Qt.OpenHandCursor)
        handle.installEventFilter(PaneDragHandle(self, key, handle))
        self._drag_handles[key] = handle
        return handle

    def _drag_pane_step(self, key: str, global_y: float) -> bool:
        """Swap one position when the pointer crosses a neighbour's centre."""
        pane = self._panes.get(key)
        if pane is None:
            return False
        index = self.splitter.indexOf(pane)
        if index < 0:
            return False
        self._remember_pane_sizes()
        keep = dict(self._pane_sizes)
        for neighbour in range(self.splitter.count()):
            if neighbour == index:
                continue
            widget = self.splitter.widget(neighbour)
            if not widget.isVisible() or widget.height() <= 0:
                continue
            center = widget.mapToGlobal(QPoint(0, widget.height() // 2)).y()
            crossed = global_y > center if neighbour > index else global_y < center
            if not crossed:
                continue
            self.splitter.insertWidget(neighbour, pane)
            self.splitter.setSizes(
                [keep.get(self._key_at(position), 0) for position in range(self.splitter.count())]
            )
            return True
        return False

    def _pane_order_keys(self) -> list[str]:
        by_id = {id(pane): key for key, pane in self._panes.items()}
        order = []
        for index in range(self.splitter.count()):
            key = by_id.get(id(self.splitter.widget(index)))
            if key is not None:
                order.append(key)
        return order

    def _finish_pane_drag(self) -> None:
        self._remember_pane_sizes()
        self.cfg["pane_order"] = self._pane_order_keys()
        self.cfg["split"] = self._layout_sizes()
        self._save_config_quiet()

    def _restore_pane_order(self) -> None:
        saved = self.cfg.get("pane_order")
        if not isinstance(saved, list):
            return
        keys = [key for key in saved if key in self._panes]
        if len(keys) != len(self._panes):
            return
        for index, key in enumerate(keys):
            self.splitter.insertWidget(index, self._panes[key])

    def _restore_layout(self) -> None:
        sizes = self.cfg.get("split")
        if isinstance(sizes, list) and len(sizes) == self.splitter.count():
            try:
                values = [int(value) for value in sizes]
            except (TypeError, ValueError):
                values = None
            if values:
                for index, value in enumerate(values):
                    key = self._key_at(index)
                    if key and value > 0:
                        self._pane_sizes[key] = value
                self.splitter.setSizes(values)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)

    def _key_at(self, index: int) -> str | None:
        widget = self.splitter.widget(index)
        for key, pane in self._panes.items():
            if pane is widget:
                return key
        return None

    def _remember_pane_sizes(self) -> None:
        for index, value in enumerate(self.splitter.sizes()):
            key = self._key_at(index)
            if key and value > 0:
                self._pane_sizes[key] = value

    def _layout_sizes(self) -> list[int]:
        """Full three-pane sizes, keeping each pane's last value (hidden reads 0)."""
        sizes = self.splitter.sizes()
        result = []
        for index, value in enumerate(sizes):
            key = self._key_at(index)
            if value <= 0 and key in self._pane_sizes:
                value = self._pane_sizes[key]
            result.append(value)
        return result

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        self._remember_pane_sizes()
        self.cfg["split"] = self._layout_sizes()
        self._split_timer.start()

    def _save_config_quiet(self) -> None:
        try:
            save_gui_config(self.config_path, self.cfg)
        except OSError:
            pass

    # ----- window behaviour -----

    def _restore_geometry(self) -> None:
        geo = self.cfg.get("window")
        if isinstance(geo, dict):
            def _read(key: str) -> int | None:
                try:
                    return int(geo[key])
                except (KeyError, TypeError, ValueError):
                    return None

            # 宽高和位置一起还原；老配置只有 x/y，那就只挪位置
            width, height = _read("w"), _read("h")
            if width and height:
                self.resize(
                    max(width, self.minimumWidth()), max(height, self.minimumHeight())
                )
            x, y = _read("x"), _read("y")
            if x is not None and y is not None:
                self.move(x, y)
            self._clamp_to_screen()
        self._geo_ready = True

    def _clamp_to_screen(self) -> None:
        """上次的位置/尺寸可能落在已经拔掉的显示器上，收回到有画面的区域里。"""
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        width = min(self.width(), max(self.minimumWidth(), area.width()))
        height = min(self.height(), max(self.minimumHeight(), area.height()))
        self.resize(width, height)
        x = min(max(self.x(), area.left()), area.right() - width + 1)
        y = min(max(self.y(), area.top()), area.bottom() - height + 1)
        self.move(x, y)

    def _store_geometry(self) -> None:
        self.cfg["window"] = {
            "x": self.x(),
            "y": self.y(),
            "w": self.width(),
            "h": self.height(),
        }
        self._save_config_quiet()

    def _queue_geometry_save(self) -> None:
        # 启动时的 resize 事件不能盖掉刚读出来的旧尺寸，等还原完再开始记
        if self._geo_ready and self._geo_timer is not None:
            self._geo_timer.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 拖窄时标题/状态字先让位，别让它们撑着整行
        self._sync_compact_chrome()
        self._queue_geometry_save()

    def _sync_compact_chrome(self) -> None:
        """窄窗口下收起「标题」「在线/舰长」这两个纯装饰文字。

        它们都有 sizePolicy=Ignored，本来就不会卡住布局；这里再按阈值藏掉，
        是为了避免出现被裁掉一半的半截字。
        """
        width = self.width()
        for name, limit in (("title_label", 470), ("stat_label", 430)):
            widget = getattr(self, name, None)
            if widget is None:
                continue
            want = width >= limit
            if widget.isHidden() == want:
                widget.setVisible(want)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._queue_geometry_save()

    def _toggle_topmost(self) -> None:
        self._set_topmost(not self.pin_btn.isChecked())

    def _set_topmost(self, on: bool) -> None:
        if self.pin_btn.isChecked() != on:
            self.pin_btn.blockSignals(True)
            self.pin_btn.setChecked(on)
            self.pin_btn.blockSignals(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()
        self._sync_pin_button()

    def _sync_pin_button(self) -> None:
        """置顶按钮要能一眼看出状态：文案 + 高亮底（见 QSS 的 :checked）。"""
        pinned = self.pin_btn.isChecked()
        self.pin_btn.setText("已置顶" if pinned else "置顶")
        self.pin_btn.setToolTip(
            "当前窗口固定在所有窗口之上，点击取消" if pinned
            else "点击把窗口固定在最前面，不被其他窗口盖住"
        )
        self.pin_btn.style().unpolish(self.pin_btn)
        self.pin_btn.style().polish(self.pin_btn)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and event.position().y() < 60:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_origin = None

    # ----- collection -----

    def _toggle_connect(self) -> None:
        if self.service.running or self._mock_thread is not None:
            self._disconnect()
        else:
            self._connect()

    def _fill_room_history(self, keep_text: str = "") -> None:
        history = [int(room) for room in (self.cfg.get("room_history") or [])]
        self.room_input.blockSignals(True)
        self.room_input.clear()
        for room in history:
            self.room_input.addItem(self._room_label(room), room)
        if keep_text:
            current = keep_text
        elif self.cfg["rooms"]:
            current = str(self.cfg["rooms"][0])
        elif history:
            current = str(history[0])
        else:
            current = ""
        self.room_input.setEditText(current)
        self.room_input.blockSignals(False)
        self._fit_room_popup()
        self._sync_room_tooltip()

    def _fit_room_popup(self) -> None:
        """下拉弹窗按最长的一项撑开。

        房间框固定 148px，而「22748536 · 火羽ひのめ」比它长，不撑开就被省略号吃掉，
        名字等于白加。
        """
        metrics = self.room_input.fontMetrics()
        widest = max(
            (metrics.horizontalAdvance(self.room_input.itemText(i))
             for i in range(self.room_input.count())),
            default=0,
        )
        self.room_input.view().setMinimumWidth(max(self.room_input.width(), widest + 36))

    def _room_name(self, room: int) -> str:
        """房间号 → UP 主昵称，没记录过就是空串。"""
        names = self.cfg.get("room_names") or {}
        return str(names.get(str(int(room))) or names.get(int(room)) or "")

    def _room_label(self, room: int) -> str:
        """下拉里显示的文字：号码 · 名字。名字未知时只留号码，别的什么都别编。"""
        room = int(room)
        if room == 0:
            return "0 · 本地测试"
        name = self._room_name(room)
        return f"{room} · {name}" if name else str(room)

    def _set_room_name(self, room: int, name: str) -> None:
        """记住某个房间的 UP 主名字，并刷新下拉里的文字。"""
        name = (name or "").strip()
        if not room or not name:
            return
        names = dict(self.cfg.get("room_names") or {})
        if names.get(str(room)) == name:
            return
        names[str(room)] = name
        self.cfg["room_names"] = names
        self._refresh_room_labels()
        self._save_config_quiet()

    def _backfill_room_names(self) -> None:
        """启动时从库里补名字：历史记录里大部分房间都连过，collect_room 有 uname。

        读不到库（路径空、文件不存在、表还没建）就当没有，不影响启动；也不在这里
        写盘，等下次正常保存配置时一起落下。
        """
        try:
            stored = read_room_names(str(self.cfg.get("database") or ""))
        except Exception:  # noqa: BLE001 - 只是显示用，不能拖垮启动
            return
        if not stored:
            return
        merged = {str(room): name for room, name in stored.items()}
        merged.update({str(k): v for k, v in (self.cfg.get("room_names") or {}).items()})
        self.cfg["room_names"] = merged

    def _refresh_room_labels(self) -> None:
        """只改下拉项的文字，不动输入框内容（可能正在手打房间号）。"""
        kept = self.room_input.currentText()
        self.room_input.blockSignals(True)
        for index in range(self.room_input.count()):
            room = self.room_input.itemData(index)
            if room is None:
                continue
            self.room_input.setItemText(index, self._room_label(int(room)))
        self.room_input.setEditText(kept)
        self.room_input.blockSignals(False)
        self._fit_room_popup()
        self._sync_room_tooltip()

    def _sync_room_tooltip(self) -> None:
        base = "可直接输入房间号；下拉列表可右键删除历史记录。填 0 是本地测试（回放 + 试发）"
        room = self._parse_room(self.room_input.currentText())
        name = self._room_name(room) if room else ""
        self.room_input.setToolTip(f"{name}\n{base}" if name else base)

    def _parse_room(self, text: str) -> int | None:
        """从输入框取房间号，允许 `22748536 · 火羽ひのめ` 这种带名字的写法。"""
        digits = ""
        for char in (text or "").strip():
            if char.isdigit():
                digits += char
            elif digits:
                break
            elif char not in " ·-—":
                return None
        return int(digits) if digits else None

    def _on_room_activated(self, index: int) -> None:
        room = self.room_input.itemData(index)
        if room is None:
            return
        self.room_input.setEditText(str(int(room)))
        self._sync_room_tooltip()

    def _remember_room(self, room: int) -> None:
        history = [int(value) for value in (self.cfg.get("room_history") or [])]
        history = [room] + [value for value in history if value != room]
        self.cfg["room_history"] = history[:10]
        self._fill_room_history(keep_text=str(room))
        self._save_config_quiet()

    def eventFilter(self, obj, event) -> bool:
        # 右键只弹菜单，不交给列表处理，避免选中项；左键仍是正常的选中并收起。
        try:
            viewport = self.room_input.view().viewport()
        except RuntimeError:  # 退出时 C++ 对象已释放
            return super().eventFilter(obj, event)
        if obj is viewport:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.RightButton:
                self._room_history_menu(event.position().toPoint())
                return True
        return super().eventFilter(obj, event)

    def _room_history_menu(self, pos: QPoint) -> None:
        view = self.room_input.view()
        index = view.indexAt(pos)
        text_before = self.room_input.currentText()
        menu = QMenu(view)
        remove_action = menu.addAction("删除此条")
        remove_action.setEnabled(index.isValid())
        menu.addAction("清空历史", self._clear_room_history)
        chosen = menu.exec(view.viewport().mapToGlobal(pos))
        if chosen is remove_action and index.isValid():
            room = self.room_input.itemData(index.row())
            self.room_input.removeItem(index.row())
            self.cfg["room_history"] = [
                value for value in (self.cfg.get("room_history") or []) if value != room
            ]
            self._save_config_quiet()
        self.room_input.setEditText(text_before)
        # 菜单弹出会让下拉列表收起，操作完重新展开，避免看起来被关掉。
        if self.room_input.count():
            QTimer.singleShot(0, self.room_input.showPopup)

    def _clear_room_history(self) -> None:
        text = self.room_input.currentText()
        self.room_input.clear()
        self.room_input.setEditText(text)
        self.cfg["room_history"] = []
        self._save_config_quiet()

    def _connect(self) -> None:
        room = self._parse_room(self.room_input.currentText())
        if room is None:
            QMessageBox.warning(self, TITLE, "请输入数字房间号")
            return
        # 0 号直播间是本地回放 + 本地回显，用来调试发送行
        self._test_mode = room == 0
        self.state = RoomState(input_room=room, state="连接中")
        self.danmaku.clear()
        self.sc_view.clear()
        self._dropped = 0
        self.danmaku_title.setText("弹幕 0")
        self.sc_title.setText("醒目留言 0")
        self.stat_label.setText("在线 0 · 舰长 0")
        self._set_connected_ui(True)
        self.title_label.setText(f"{TITLE}")

        # Room 0 is the offline fixture: no network, synthetic danmaku/SC/gift stream.
        if room == 0:
            self._start_mock()
            return

        self.cfg["rooms"] = [room]
        self._remember_room(room)
        # 换房间后可用颜色/位置和表情包都可能不同，重拉一次
        self._refresh_room_context(force=True)
        # 有账号就带着它的 cookie 连：匿名连接用户名会是 ***，弹幕也明显少
        cookies = self.sender.cookies() if self.sender.has_login else None
        self.service.start(self.cfg, room, cookies=cookies)

    def _disconnect(self) -> None:
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("停止中")
        # 一按断开就锁住发送行，别让停止的这几秒里还能往旧房间发
        self.state.real_room = 0
        self._test_mode = False
        self._refresh_send_state()
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        self.service.stop()
        thread = self._mock_thread
        if thread is not None:
            if self._mock_stop is not None:
                self._mock_stop.set()
            thread.join(timeout=3.0)
            self._mock_thread = None
        self.service.events.put({"type": "ui_stopped"})

    def _start_mock(self) -> None:
        self._mock_stop = threading.Event()
        self._mock_thread = threading.Thread(
            target=self._mock_worker, args=(self._mock_stop,), daemon=True
        )
        self._mock_thread.start()
        self.state.state = "测试数据"
        self.dot.setObjectName("live")
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.title_label.setText("0 号直播间 · 正在载入回放")
        self._append_sys("0 号直播间：读取采集库里的真实记录，点「断开」停止")
        self._append_sys(
            "测试模式：这里发出的弹幕只在本机回显，不会发到 B 站，"
            "可用来核对颜色、位置和表情"
        )

    def _mock_worker(self, stop: threading.Event) -> None:
        """0 号直播间优先回放真实原始 JSON，取不到数据再退回合成流。"""
        fixture = None
        try:
            fixture = load_replay(str(self.cfg.get("database") or ""), sc_limit=24)
        except Exception as exc:  # 库被占用或损坏都不该让界面卡住
            self.service.events.put(
                {"type": "sys", "text": f"读取真实记录失败：{exc}，改用合成数据"}
            )
        if fixture:
            self._replay_fixture(fixture, stop)
        else:
            self._mock_synthetic(stop)

    def _replay_fixture(self, fixture: dict, stop: threading.Event) -> None:
        put = self.service.events.put
        put({"type": "room", "room_id": 0, "title": fixture["title"], "uname": ""})
        for note in fixture["notes"]:
            put({"type": "sys", "text": note})

        danmaku = fixture["danmaku"]
        sc_items = fixture["sc"]
        events = fixture["events"]
        delayed: list[tuple[float, dict]] = []
        i_dm = i_ev = 0
        round_no = 0
        sc_index = 0
        tick = 0

        while not stop.is_set():
            tick += 1

            # 弹幕放慢到约 33 条/秒，方便肉眼核对每一条的排版
            for _ in range(4):
                if danmaku:
                    put({"type": "danmaku", "row": dict(danmaku[i_dm % len(danmaku)])})
                    i_dm += 1

            now = time.time()
            while delayed and delayed[0][0] <= now:
                put(delayed.pop(0)[1])

            # 约每 1.5 秒一条 SC
            if sc_items and tick % 12 == 0:
                item = sc_items[sc_index]
                sc_index += 1
                if sc_index >= len(sc_items):
                    sc_index = 0
                    round_no += 1
                self._replay_sc(put, item, round_no, delayed)

            # 约每 3 秒一条礼物 / 进场
            if events and tick % 24 == 0:
                event = events[i_ev % len(events)]
                i_ev += 1
                put({"type": event["event_type"], "row": dict(event["row"])})

            stop.wait(0.12)

    def _replay_sc(self, put, item: dict, round_no: int, delayed: list) -> None:
        """把一条真实 SC 按它对应的到达方式推给界面。"""
        sc_id = f"{item['sc_id']}-r{round_no}"
        raw = dict(item["raw"])
        mode = item["mode"]

        if mode == JPN_BEFORE:
            # B 站先发译文、后发本体：界面此时还没有这条记录，译文会被丢弃，
            # 真实采集侧靠 _pending_sc_trans 缓冲后合并进本体，这里照做。
            put({"type": "sc_trans", "sc_id": sc_id, "text": item["trans"]})
            raw["message_trans"] = ""
            row = extract_sc(0, raw, int(time.time() * 1000))
            if row is None:
                return
            row["sc_id"] = sc_id
            row["message_trans"] = item["trans"]
            put({"type": "sc", "row": row})
            return

        if mode == JPN_AFTER:
            raw["message_trans"] = ""
            row = extract_sc(0, raw, int(time.time() * 1000))
            if row is None:
                return
            row["sc_id"] = sc_id
            put({"type": "sc", "row": row})
            delayed.append(
                (time.time() + 0.6, {"type": "sc_trans", "sc_id": sc_id, "text": item["trans"]})
            )
            delayed.sort(key=lambda pair: pair[0])
            return

        row = extract_sc(0, raw, int(time.time() * 1000))
        if row is None:
            return
        row["sc_id"] = sc_id
        put({"type": "sc", "row": row})

    def _mock_synthetic(self, stop: threading.Event) -> None:
        names = ["海猫开门", "梓有蛇栖", "脆猫c_c", "纸蓝针_zlz", "爱吃大白米饭的肥鱼", "沐雨i"]
        texts = ["好家伙", "笑死", "这个怎么打", "主播加油", "前方高能", "233333",
                 "这波操作可以", "别玩了快睡觉", "有无大佬带带", "这 bug 太真实"]
        self.service.events.put(
            {"type": "room", "room_id": 0, "title": "本地测试数据 · 模拟直播间", "uname": ""}
        )
        self.service.events.put(
            {"type": "room_stats", "online": 21357, "watched": 272158, "guards": 272}
        )

        seq = 0
        next_burst = time.time() + 10
        while not stop.is_set():
            seq += 1
            now = int(time.time())

            def push_danmaku(index: int) -> None:
                emoticon = index % 40 == 0
                self.service.events.put({"type": "danmaku", "row": {
                    "room_id": 0,
                    "uid": 100000 + index % 5000,
                    "uname": f"{names[index % len(names)]}{index % 97}",
                    "text": "敢耍俺" if emoticon else texts[index % len(texts)],
                    "event_time": now,
                    "medal_name": "粉丝团" if index % 3 == 0 else None,
                    "medal_level": index % 40 if index % 3 == 0 else 0,
                    "guard_level": 3 if index % 400 == 0 else 0,
                    "is_admin": index % 900 == 0,
                    "is_emoticon": 1 if emoticon else 0,
                }})

            push_danmaku(seq)

            if seq % 75 == 0:
                sc_id = f"mock-{seq}"
                self.service.events.put({"type": "sc", "row": {
                    "sc_id": sc_id,
                    "room_id": 0,
                    "uid": 900000 + seq,
                    "uname": f"测试老板{seq % 50}",
                    "price": (30, 50, 100, 500, 1000, 2000)[seq % 6],
                    "message": f"这是第 {seq} 条模拟醒目留言，用于检查日志流排版与分隔符号",
                    "start_time": now,
                    "guard_level": 3 if seq % 2 == 0 else 0,
                }})
                if seq % 150 == 0:
                    self.service.events.put(
                        {"type": "sc_trans", "sc_id": sc_id, "text": "mock translation"}
                    )

            if seq % 25 == 0:
                kind = "gift" if (seq // 25) % 2 else "enter"
                if self.service.event_filter.get(kind):
                    if kind == "gift":
                        self.service.events.put({"type": "gift", "row": {
                            "event_time": now, "uname": f"投喂者{seq % 30}",
                            "gift_name": "粉丝团灯牌" if seq % 3 else "辣条", "gift_num": 1,
                        }})
                    else:
                        self.service.events.put({"type": "enter", "row": {
                            "event_time": now, "uname": f"观众{seq % 60}"}})

            if seq % 100 == 0:
                self.service.events.put({"type": "room_stats", "online": 21000 + seq % 900})

            # Every 10s a 300-message burst, to check that spikes do not freeze the UI.
            if time.time() >= next_burst:
                next_burst = time.time() + 10
                for i in range(300):
                    push_danmaku(seq * 1000 + i)

            stop.wait(0.04)

    def _set_connected_ui(self, running: bool) -> None:
        self.connect_btn.setText("断开" if running else "连接")
        self.connect_btn.setProperty("active", "true" if running else "false")
        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)
        self.connect_btn.setEnabled(True)
        self.room_input.setEnabled(not running)
        self._refresh_send_state()
        self._fill_medal_box()

    # ----- event pump -----

    def _tick(self) -> None:
        self._poll_send()
        self._poll_room_context()
        self._poll_account_meta()
        self._poll_medals()
        self._poll_wear()
        self._refresh_avatars()

        batch: list[dict] = []
        for _ in range(MAX_EVENTS_PER_TICK):
            try:
                batch.append(self.service.events.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return

        danmaku_rows: list[dict] = []
        for event in batch:
            kind = event.get("type")
            if kind == "danmaku":
                row = event.get("row")
                if row:
                    danmaku_rows.append(row)
            elif kind == "room_stats":
                if "online" in event:
                    self.state.online = int(event.get("online") or 0)
                if "watched" in event:
                    self.state.watched = int(event.get("watched") or 0)
                if "guards" in event:
                    self.state.guards = int(event.get("guards") or 0)
            elif kind == "room":
                room_no = int(event.get("room_id") or 0)
                self.state.real_room = room_no
                self.state.title = event.get("title") or ""
                self.title_label.setText(
                    self.state.title or TITLE
                    if room_no
                    else f"0 号直播间 · {self.state.title or '本地测试'}"
                )
                if room_no:
                    # 采集线程把 UP 主昵称一起带回来了，存下来给房间下拉用
                    self._set_room_name(room_no, event.get("uname") or "")
                    self._append_sys(f"已进入 {room_no} · {self.state.title}")
                    if self.sender.has_login:
                        who = self.sender.uname or "账号"
                        if self.sender.uname:
                            self._append_sys(f"采集身份：{self.sender.uname}")
                        self.dot.setToolTip(f"以 {who} 的身份采集")
                    else:
                        self._append_sys(
                            "匿名连接：部分房间会把用户名显示成 ***，"
                            "在账号目录里放 cookie、选上账号后重连即可"
                        )
                        self.dot.setToolTip("匿名连接：部分房间的用户名会显示为 ***")
                self._refresh_send_state()
                self._request_medals()
            elif kind == "sys":
                self._append_sys(str(event.get("text") or ""))
            elif kind == "status":
                if event.get("state") == "connected":
                    self.state.state = "已连接"
                    self.dot.setObjectName("live")
                    if event.get("title"):
                        self.title_label.setText(event["title"])
                else:
                    self.state.state = "重连中"
                    self.dot.setObjectName("dead")
                    self._append_sys(f"断线：{event.get('error')}，{event.get('delay')}s 后重试")
                self.dot.style().unpolish(self.dot)
                self.dot.style().polish(self.dot)
            elif kind == "sc":
                row = event.get("row") or {}
                self.state.sc += 1
                self._append_sc(row)
            elif kind == "sc_trans":
                self._set_sc_translation(event.get("sc_id"), event.get("text") or "")
            elif kind == "gift":
                row = event.get("row") or {}
                self.state.gifts += 1
                if self._event_visible("gift"):
                    self._append_event_line(
                        row.get("event_time"),
                        "礼物",
                        self._c["guard"],
                        f"{row.get('uname') or ''} 送出 {row.get('gift_name') or ''}"
                        f" ×{row.get('gift_num') or 1}",
                    )
            elif kind == "guard":
                row = event.get("row") or {}
                self.state.guards += 1
                if self._event_visible("guard"):
                    self._append_event_line(
                        row.get("event_time"),
                        "上舰",
                        self._c["guard"],
                        f"{row.get('uname') or ''} 开通了 {row.get('gift_name') or '舰长'}",
                    )
            elif kind == "enter":
                self.state.enters += 1
                row = event.get("row") or {}
                if self._event_visible("enter"):
                    self._append_event_line(
                        row.get("event_time"), "进场", self._c["enter"], f"{row.get('uname') or ''} 进入"
                    )
            elif kind == "delete":
                if self._event_visible("delete"):
                    self._append_event_line(0, "SC删除", self._c["delete"], str(event.get("ids")))
            elif kind == "fatal":
                self._append_sys(f"采集异常：{event.get('error')}")
                self._set_connected_ui(False)
            elif kind == "ui_stopped":
                # 断开后必须把房间号清掉，否则发送行会以为还连着，能发到上一个房间
                self.state.real_room = 0
                self._test_mode = False
                self._set_connected_ui(False)
                self.dot.setObjectName("dead")
                self._append_sys("已断开")

        if danmaku_rows:
            self.state.messages += len(danmaku_rows)
            if len(danmaku_rows) > MAX_DANMAKU_PER_TICK:
                self._dropped += len(danmaku_rows) - MAX_DANMAKU_PER_TICK
                danmaku_rows = danmaku_rows[-MAX_DANMAKU_PER_TICK:]
            self._append_danmaku_batch(danmaku_rows)

        popularity = (
            f"{self.state.online / 10000:.1f}万" if self.state.online >= 10000 else str(self.state.online)
        )
        drop = f"（丢 {self._dropped}）" if self._dropped else ""
        self.danmaku_title.setText(f"弹幕 {self.state.messages}{drop}")
        self.sc_title.setText(f"醒目留言 {self.state.sc}")
        self.stat_label.setText(f"在线 {popularity} · 舰长 {self.state.guards}")

    # ----- rendering -----

    def _append_danmaku_batch(self, rows: list[dict]) -> None:
        now = time.monotonic()
        for row in rows:
            row["_queued_at"] = now
            for url in self._row_image_urls(row):
                self._images.want(url)
            self._danmaku_pending.append(row)
        if len(self._danmaku_pending) > MAX_PENDING_DANMAKU:
            # 积压说明图片一直在超时，别让画面跟着等，全按文字先发出去
            for row in self._danmaku_pending:
                row["_queued_at"] = 0.0
        self._flush_danmaku()

    def _emote_index_map(self) -> dict[str, tuple[str, int]]:
        """表情名 → (图地址, 高度)，取自账号在当前房间能用的表情包。

        老式小表情（``[偷笑]`` 这类）payload 里只有名字没有图，靠这份表兜底；
        表情包换了才重建，平时就是一次字典查找。
        """
        packages = self._emote_packages
        if packages is self._emote_index_src:
            return self._emote_index
        index: dict[str, tuple[str, int]] = {}
        for package in packages or []:
            for item in package.get("items") or []:
                name = str(item.get("text") or "").strip().strip("[]")
                url = str(item.get("url") or "")
                # dm_type=0 的是纯文本表情包（发 [dog] 这种），没有图
                if name and url and item.get("dm_type"):
                    index.setdefault(name, (url, EMOTE_PX))
        self._emote_index_src = packages
        self._emote_index = index
        return index

    def _row_emotes(self, row: dict) -> dict[str, tuple[str, int]]:
        """这一行里能画成图的表情：``{名字: (图地址, 显示高度)}``。

        优先级：弹幕报文自带的表情表 → 本房间表情包。前者含房间大表情和
        收藏集，后者只用来补老式小表情。
        """
        text = str(row.get("text") or "")
        found: dict[str, tuple[str, int]] = {}
        for name, info in (row.get("emote_map") or {}).items():
            try:
                url, px = str(info[0] or ""), int(info[1] or EMOTE_PX)
            except (IndexError, TypeError, ValueError):
                continue
            if url:
                found[str(name)] = (url, px)

        if row.get("is_emoticon") and not found:
            # 整条就是一个表情：正文写的是表情名，不带中括号
            url = str(row.get("emote_url") or "")
            if url:
                try:
                    px = int(row.get("emote_px") or EMOTE_PX)
                except (TypeError, ValueError):
                    px = EMOTE_PX
                found[text] = (url, px)
                return found
            # 没图（0 号回放的老行只有 is_emoticon 标记）：按文字继续走兜底索引

        index = self._emote_index_map()
        for match in EMOTE_TOKEN.finditer(text):
            name = match.group(1)
            if name in found:
                continue
            hit = index.get(name)
            if hit is not None:
                found[name] = hit
        return found

    def _row_image_urls(self, row: dict) -> list[str]:
        """只下载表情图。头像是一人一图、几乎不命中缓存，量大且没多少人看，不取。"""
        return [url for url, _px in self._row_emotes(row).values()]

    def _flush_danmaku(self) -> None:
        """按到达顺序上屏；队头还在等图就停住，避免后面的弹幕插队。"""
        if not self._danmaku_pending:
            return
        now = time.monotonic()
        ready: list[dict] = []
        while self._danmaku_pending and len(ready) < MAX_DANMAKU_PER_TICK:
            row = self._danmaku_pending[0]
            if now - row.get("_queued_at", now) < IMAGE_WAIT_SECONDS:
                if any(not self._images.settled(url) for url in self._row_image_urls(row)):
                    break
            ready.append(self._danmaku_pending.pop(0))
        if ready:
            self._insert_danmaku(ready)

    def _insert_danmaku(self, rows: list[dict]) -> None:
        document = self.danmaku.document()
        scrollbar = self.danmaku.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.End)
        cursor.beginEditBlock()
        for row in rows:
            urls = self._row_image_urls(row)
            for url in urls:
                image = self._images.image(url)
                if image is not None:
                    # 挂到原始 URL 上，HTML 里的 <img src> 就能直接取到
                    document.addResource(
                        QTextDocument.ResourceType.ImageResource, QUrl(url), image
                    )
            if cursor.position() > 0:
                cursor.insertBlock()
            cursor.insertHtml(self._danmaku_html(row))
            self._highlight_self_block(cursor, row)

            pending = [url for url in urls if not self._images.settled(url)]
            if pending:
                # 图还没到，先按文字显示，等下一轮补画这一行
                cursor.block().setUserData(DanmakuBlockData(row, pending))
                self._pending_images += 1
        cursor.endEditBlock()

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _on_image_timer(self) -> None:
        self._flush_danmaku()
        self._refresh_danmaku_images()

    def _refresh_danmaku_images(self) -> None:
        """迟到的表情图：把对应那一行重画一遍。"""
        if self._pending_images <= 0:
            return
        document = self.danmaku.document()
        ready: list[tuple[int, dict]] = []
        still_waiting = 0
        block = document.begin()
        while block.isValid():
            data = block.userData()
            if isinstance(data, DanmakuBlockData):
                if all(self._images.settled(url) for url in data.pending):
                    ready.append((block.blockNumber(), data.row))
                else:
                    still_waiting += 1
            block = block.next()
        # 被 maximumBlockCount 截掉的行不会再来，计数以这次实际扫描为准
        self._pending_images = still_waiting

        for number, row in ready:
            target = document.findBlockByNumber(number)
            if target.isValid():
                target.setUserData(None)
                self._rebuild_danmaku_block(target, row)

    def _rebuild_danmaku_block(self, block, row: dict) -> None:
        document = self.danmaku.document()
        for url in self._row_image_urls(row):
            image = self._images.image(url)
            if image is not None:
                document.addResource(
                    QTextDocument.ResourceType.ImageResource, QUrl(url), image
                )
        scrollbar = self.danmaku.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.StartOfBlock)
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.beginEditBlock()
        cursor.removeSelectedText()
        cursor.insertHtml(self._danmaku_html(row))
        self._highlight_self_block(cursor, row)
        cursor.endEditBlock()

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    @staticmethod
    def _image_size(image: QImage, height: int) -> tuple[int, int]:
        """按目标高度等比算宽高，QTextDocument 只认固定的 px 尺寸。"""
        if image.height() <= 0:
            return height, height
        width = max(1, round(height * image.width() / image.height()))
        return width, height

    def _is_self_row(self, row: dict) -> bool:
        """这一行是不是自己发的：优先看标记，其次按 uid 认。"""
        if row.get("_self"):
            return True
        uid = int(row.get("uid") or 0)
        mine = int(getattr(self.sender, "uid", 0) or 0)
        return bool(uid) and uid == mine

    def _highlight_self_block(self, cursor: QTextCursor, row: dict) -> None:
        """整行铺一层底色，比只改文字颜色更容易一眼扫到。

        非本人的行必须显式清掉底色：插入新块会继承上一块的格式，
        自己的那行后面要是跟着一条别人的弹幕，别人也会被染上。
        """
        fmt = cursor.blockFormat()
        if self._is_self_row(row):
            fmt.setBackground(QBrush(QColor(self._c["self_bg"])))
        else:
            fmt.clearBackground()
        cursor.setBlockFormat(fmt)

    def _text_html(self, row: dict) -> str:
        """正文：普通文字照原样，``[名字]`` 能对上图就画图、对不上留着字。

        B站把老式小表情写成 ``[大哭]`` 这种占位符，图片地址在报文的表情表里
        （或者本房间表情包里），所以这里要逐段替换，不能整条 escape 了事。
        """
        raw = str(row.get("text") or "")
        emotes = self._row_emotes(row)
        if not emotes:
            return f'<span style="color:{self._c["text"]}">{html.escape(raw)}</span>'

        if row.get("is_emoticon") and not any(f"[{name}]" in raw for name in emotes):
            # 整条就是一个表情（房间大表情 / 通用表情：正文写的是表情名）
            url, px = next(iter(emotes.values()))
            image = self._images.image(url)
            if image is not None:
                width, height = self._image_size(image, px)
                return f'<img src="{html.escape(url)}" width="{width}" height="{height}">'
            return f'<span style="color:{self._c["emoticon"]}">[{html.escape(raw)}]</span>'

        out: list[str] = []
        pos = 0

        def add_text(chunk: str) -> None:
            if chunk:
                out.append(f'<span style="color:{self._c["text"]}">{html.escape(chunk)}</span>')

        for match in EMOTE_TOKEN.finditer(raw):
            hit = emotes.get(match.group(1))
            if hit is None:
                continue
            url, px = hit
            image = self._images.image(url)
            if image is None:
                # 图还没下完或下载失败：这一段原样留着，等补画那一行
                continue
            add_text(raw[pos:match.start()])
            width, height = self._image_size(image, px)
            out.append(f'<img src="{html.escape(url)}" width="{width}" height="{height}">')
            pos = match.end()
        add_text(raw[pos:])
        return "".join(out)

    def _danmaku_html(self, row: dict) -> str:
        event_time = row.get("event_time") or 0
        stamp = (
            datetime.fromtimestamp(event_time).strftime("%H:%M:%S")
            if event_time
            else datetime.now().strftime("%H:%M:%S")
        )
        parts = [f'<span style="color:{self._c["time"]}">{stamp}</span>']
        if self._is_self_row(row):
            parts.insert(0, f'<span style="color:{self._c["self_mark"]}">▌</span>')

        medal = row.get("medal_name") or ""
        if medal:
            parts.append(
                f'<span style="color:{self._c["medal"]}">[{html.escape(medal)} {row.get("medal_level") or 0}]</span>'
            )

        guard = int(row.get("guard_level") or 0)
        if guard:
            label = {1: "总督", 2: "提督", 3: "舰长"}.get(guard, "舰长")
            parts.append(f'<span style="color:{self._c["guard"]}">[{label}]</span>')

        if row.get("is_admin"):
            parts.append(f'<span style="color:{self._c["admin"]}">[房管]</span>')

        uname = html.escape(row.get("uname") or "")
        uid = row.get("uid") or 0
        if uid:
            parts.append(
                f'<a href="https://space.bilibili.com/{uid}" '
                f'style="color:{self._c["user"]};text-decoration:none">{uname}</a>:'
            )
        else:
            parts.append(f'<span style="color:{self._c["user"]}">{uname}</span>:')

        # 正文交给 _text_html：里面的 [名字] 能对上图的会被换成图片
        parts.append(self._text_html(row))
        if row.get("_test"):
            # 测试模式的回显带上这次发送用的颜色和位置，方便对照
            mode = dict(MODES).get(int(row.get("_mode") or 1), "滚动")
            color = int(row.get("_color") or 0)
            parts.append(
                f'<span style="color:{self._c["time"]}">[测试 #{color:06x} · {mode}]</span>'
            )
        return " ".join(parts)

    def _open_profile(self, url: QUrl) -> None:
        QDesktopServices.openUrl(url)

    def _append_sys(self, text: str) -> None:
        if len(text) > 90:
            text = text[:88] + "…"
        scrollbar = self.danmaku.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        cursor = QTextCursor(self.danmaku.document())
        cursor.movePosition(QTextCursor.End)
        cursor.beginEditBlock()
        if cursor.position() > 0:
            cursor.insertText("\n")
        cursor.insertText(f"— {text}", self._fmt_sys)
        # 系统行不跟着上一条弹幕的底色走
        fmt = cursor.blockFormat()
        fmt.clearBackground()
        cursor.setBlockFormat(fmt)
        cursor.endEditBlock()
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _append_event_line(self, event_time, tag: str, color: str, text: str) -> None:
        try:
            stamp = datetime.fromtimestamp(int(event_time)).strftime("%H:%M:%S")
        except (TypeError, ValueError, OSError):
            stamp = datetime.now().strftime("%H:%M:%S")

        view = self.event_view
        scrollbar = view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        cursor = QTextCursor(view.document())
        cursor.movePosition(QTextCursor.End)
        cursor.beginEditBlock()
        if cursor.position() > 0:
            cursor.insertBlock()
        line = (
            f'<span style="color:{self._c["time"]}">{stamp}</span> '
            f'<span style="color:{color}">[{html.escape(tag)}]</span> '
            f'<span style="color:{self._c["text"]}">{html.escape(text)}</span>'
        )
        cursor.insertHtml(line)
        cursor.endEditBlock()

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _sc_html(self, row: dict) -> str:
        price = int(row.get("price") or 0)
        start = int(row.get("start_time") or 0)
        stamp = (
            datetime.fromtimestamp(start).strftime("%H:%M:%S")
            if start
            else datetime.now().strftime("%H:%M:%S")
        )
        color = self._c["price_hi"] if price >= 100 else self._c["price"]

        parts = [
            f'<span style="color:{self._c["sep"]}">━━━━━━━━━━━━━━━━━━━━━━━━━━</span><br>',
            f'<span style="color:{self._c["time"]}">{stamp}</span>',
            f'<span style="color:{color};font-weight:600">¥{price}</span>',
        ]

        guard = int(row.get("guard_level") or 0)
        if guard:
            label = {1: "总督", 2: "提督", 3: "舰长"}.get(guard, "舰长")
            parts.append(f'<span style="color:{self._c["guard"]}">[{label}]</span>')

        uname = html.escape(row.get("uname") or "")
        uid = row.get("uid") or 0
        if uid:
            parts.append(
                f'<a href="https://space.bilibili.com/{uid}" '
                f'style="color:{self._c["user"]};text-decoration:none">{uname}</a>'
            )
        else:
            parts.append(f'<span style="color:{self._c["user"]}">{uname}</span>')

        parts.append(
            f'<br><span style="color:{self._c["text"]}">{html.escape(row.get("message") or "")}</span>'
        )
        trans = row.get("message_trans")
        if trans:
            parts.append(
                f'<br><span style="color:{self._c["time"]}">译：{html.escape(trans)}</span>'
            )
        return " ".join(parts)

    def _append_sc(self, row: dict) -> None:
        # SC 不排队等图：译文可能随后就到，插块顺序不能乱
        scrollbar = self.sc_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

        cursor = QTextCursor(self.sc_view.document())
        cursor.movePosition(QTextCursor.End)
        cursor.beginEditBlock()
        if cursor.position() > 0:
            cursor.insertBlock()
        cursor.insertHtml(self._sc_html(row))
        cursor.endEditBlock()

        self.sc_view.document().lastBlock().setUserData(ScBlockData(str(row.get("sc_id") or "")))

        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _set_sc_translation(self, sc_id: str | None, text: str) -> None:
        if not sc_id or not text:
            return
        target = str(sc_id)
        block = self.sc_view.document().begin()
        while block.isValid():
            data = block.userData()
            if isinstance(data, ScBlockData) and data.sc_id == target:
                scrollbar = self.sc_view.verticalScrollBar()
                at_bottom = scrollbar.value() >= scrollbar.maximum() - 4

                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.EndOfBlock)
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(self._c["time"]))
                cursor.insertBlock()
                cursor.insertText(f"译：{text}", fmt)

                if at_bottom:
                    scrollbar.setValue(scrollbar.maximum())
                return
            block = block.next()

    # ----- settings -----

    def _open_settings(self) -> None:
        # 再点一次就收起（popup 按钮的常见手感），不必先点别处
        if self._settings is not None:
            if self._settings.isVisible():
                self._settings.hide()
                return
            self._settings.deleteLater()
        self._settings = SettingsPopup(self)
        self._settings.adjustSize()
        self._settings.popup_at(self.settings_btn)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 布局跑完才知道控件实际尺寸，箭头在这里对一次位置
        for combo, arrow in (
            (self.room_input, self.room_arrow),
            (self.account_box, self.account_arrow),
            (self.medal_box, self.medal_arrow),
        ):
            arrow.setGeometry(combo.width() - 18, 0, 16, combo.height())

    def closeEvent(self, event) -> None:
        if self.service.running:
            self.service.stop()
        if self._mock_stop is not None:
            self._mock_stop.set()
        self.timer.stop()
        self._image_timer.stop()
        self._images.stop()
        self._emote_images.stop()
        self._send_pool.shutdown(wait=False)
        self._emote_pool.shutdown(wait=False, cancel_futures=True)
        self._account_pool.shutdown(wait=False, cancel_futures=True)
        self._avatar_images.stop()
        if self._emoticon_panel is not None:
            self._emoticon_panel.close()
        self.cfg["split"] = self._layout_sizes()
        self.cfg["window"] = {
            "x": self.x(),
            "y": self.y(),
            "w": self.width(),
            "h": self.height(),
        }
        try:
            save_gui_config(self.config_path, self.cfg)
        except OSError:
            pass
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()


class SettingsPopup(QDialog):
    """设置面板按 popup 弹：无标题栏，点外面或按 Esc 就收起。

    主窗口是 frameless + always-on-top 的 Qt.Tool，普通 QDialog 会被压在下面
    或者抢走焦点不还；用 Popup 和表情面板一个手感，也不会在任务栏留一个独立窗口。
    """

    def __init__(self, owner: FloatingWindow) -> None:
        super().__init__(owner)
        self.owner = owner
        self._popup_geo = None  # 文件对话框收起时的临时隐藏位置
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("设置")
        self.setMinimumWidth(470)
        c = owner._c
        # 下拉箭头：QSS 默认画的是深色小三角，暗色底上完全看不见
        arrow_png = "arrow-light.png" if owner.theme == "light" else "arrow-dark.png"
        arrow_url = os.path.join(ASSETS_DIR, arrow_png).replace("\\", "/")
        self.setStyleSheet(f"""
            QFrame#sheet {{
                background: {c['dialog_bg']};
                border: 1px solid {c['dialog_border']};
                border-radius: 10px;
            }}
            QLabel {{ color: {c['dialog_fg']}; font-size: 12px; }}
            QLabel#title {{ font-size: 13px; font-weight: 600; }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background: {c['dialog_input_bg']}; color: {c['dialog_input_fg']};
                border: 1px solid {c['dialog_border']};
                border-radius: 6px; padding: 4px 6px; font-size: 12px;
            }}
            QComboBox {{ padding-right: 20px; }}
            QComboBox::drop-down {{ border: none; width: 18px; }}
            QComboBox::down-arrow {{
                image: url({arrow_url}); width: 8px; height: 5px;
            }}
            QComboBox QAbstractItemView {{
                background: {c['dialog_input_bg']}; color: {c['dialog_input_fg']};
                border: 1px solid {c['dialog_border']};
                selection-background-color: #2f6fed; selection-color: #ffffff;
            }}
            QCheckBox {{ color: {c['dialog_fg']}; }}
            QPushButton {{
                background: #2f6fed; color: white; border: none; border-radius: 6px;
                padding: 6px 16px; font-size: 12px;
            }}
            QPushButton#plain {{
                background: {c['dialog_plain_bg']}; color: {c['dialog_plain_fg']};
                border: 1px solid {c['dialog_plain_border']};
            }}
            QPushButton#close {{
                background: transparent; color: {c['dialog_fg']};
                border: none; padding: 2px 8px; font-size: 13px;
            }}
            QPushButton#close:hover {{ background: {c['dialog_plain_bg']}; border-radius: 6px; }}
        """)
        cfg = owner.cfg

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.sheet = QFrame()
        self.sheet.setObjectName("sheet")
        outer.addWidget(self.sheet)
        form = QFormLayout(self.sheet)
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(10)

        header = QWidget()
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(0, 0, 0, 2)
        head_row.setSpacing(6)
        title = QLabel("设置")
        title.setObjectName("title")
        head_row.addWidget(title)
        head_row.addStretch(1)
        close = QPushButton("✕")
        close.setObjectName("close")
        close.setFixedSize(26, 22)
        close.setToolTip("关闭（Esc）")
        close.clicked.connect(self.reject)
        head_row.addWidget(close)
        form.addRow(header)

        self.db = QLineEdit(cfg["database"])
        form.addRow("数据库路径", self._with_browse(self.db, save=True))

        self.log = QLineEdit(cfg["log_file"])
        form.addRow("日志路径", self._with_browse(self.log, save=False))

        self.heartbeat = QSpinBox()
        self.heartbeat.setRange(5, 120)
        self.heartbeat.setValue(int(cfg["heartbeat_seconds"]))
        form.addRow("心跳（秒）", self.heartbeat)

        self.backoff = QDoubleSpinBox()
        self.backoff.setRange(5.0, 600.0)
        self.backoff.setDecimals(1)
        self.backoff.setValue(float(cfg["backoff_max_seconds"]))
        form.addRow("最大重连间隔（秒）", self.backoff)

        self.level = QComboBox()
        self.level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.level.setCurrentText(str(cfg.get("log_level", "INFO")))
        form.addRow("日志级别", self.level)

        self.max_blocks = QSpinBox()
        self.max_blocks.setRange(200, 5000)
        self.max_blocks.setSingleStep(100)
        self.max_blocks.setValue(owner.danmaku.document().maximumBlockCount())
        form.addRow("弹幕保留行数", self.max_blocks)

        self.theme_box = QComboBox()
        for key in ("dark", "light", "system"):
            self.theme_box.addItem(THEME_LABELS[key], key)
        index = self.theme_box.findData(str(cfg.get("theme", "dark")))
        self.theme_box.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("主题", self.theme_box)

        self.window_opacity = QSpinBox()
        self.window_opacity.setRange(WINDOW_OPACITY_MIN, 100)
        self.window_opacity.setSingleStep(5)
        self.window_opacity.setSuffix(" %")
        self.window_opacity.setValue(owner._opacity())
        self.window_opacity.setToolTip(
            "整个窗口一起变淡（底板、文字、输入框都跟着走）。\n"
            "想让它叠在直播画面上不那么挡，调到 60–80% 比较好；100% = 完全不透明"
        )
        form.addRow("窗口不透明度", self.window_opacity)

        self.cookie_dir = QLineEdit(str(cfg.get("cookie_dir", "")))
        self.cookie_dir.setPlaceholderText("放 cookie 文件的目录，可以放多个 csv，启动时自动扫描")
        form.addRow("账号目录", self._with_dir_browse(self.cookie_dir))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("plain")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("保存")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        form.addRow("", buttons)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

    def popup_at(self, anchor: QWidget | None = None, at: QPoint | None = None) -> None:
        """贴在按钮下方弹出；下面放不下就翻到上面，横向右对齐。

        ``at`` 是鼠标位置：透明模式下按钮收起来了，只能贴着光标弹。
        """
        if at is not None:
            corner, width, height = at, 0, 0
            screen = QApplication.screenAt(at) or QApplication.primaryScreen()
        else:
            corner = anchor.mapToGlobal(QPoint(0, 0))
            width, height = anchor.width(), anchor.height()
            screen = anchor.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = corner.x() + width - self.width()
        y = corner.y() + height + 6
        if y + self.height() > area.bottom() - 4:
            y = max(area.top() + 4, corner.y() - self.height() - 6)
        x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    # ----- 文件对话框 -----

    def _suspend_popup(self) -> None:
        """开文件对话框时先收起自己。

        Popup 有自己的鼠标/键盘 grab，系统文件对话框一抢焦点它就会自己收起，
        而且那之后 Qt 不允许再把它弹回来。所以先主动 hide，选完再放回去。
        """
        self._popup_geo = self.geometry()
        self.hide()

    def _resume_popup(self) -> None:
        if self._popup_geo is not None:
            self.setGeometry(self._popup_geo)
        self.show()
        # Popup 重新出现时第一次 show() 会被 Qt 的 popup 管理吞掉（立刻隐藏），
        # setVisible(True) 才是真正把面板放回去的那一下
        self.setVisible(True)
        self.raise_()
        QTimer.singleShot(0, self._reshow_popup)

    def _reshow_popup(self) -> None:
        self.setVisible(True)
        self.raise_()

    def _with_browse(self, entry: QLineEdit, save: bool) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(entry, 1)
        button = QPushButton("浏览")
        button.setObjectName("plain")

        def browse() -> None:
            self._suspend_popup()
            try:
                if save:
                    path, _ = QFileDialog.getSaveFileName(
                        self, "选择数据库", entry.text(), "SQLite (*.db)"
                    )
                else:
                    path, _ = QFileDialog.getSaveFileName(
                        self, "选择日志", entry.text(), "Log (*.log)"
                    )
            finally:
                self._resume_popup()
            if path:
                entry.setText(path)

        button.clicked.connect(browse)
        row.addWidget(button)
        return holder

    def _with_dir_browse(self, entry: QLineEdit) -> QWidget:
        """目录选择：账号目录是「一整个文件夹」，不是单个文件。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(entry, 1)
        button = QPushButton("浏览")
        button.setObjectName("plain")

        def browse() -> None:
            self._suspend_popup()
            try:
                path = QFileDialog.getExistingDirectory(
                    self, "选择账号目录", entry.text() or os.path.dirname(self.owner.config_path)
                )
            finally:
                self._resume_popup()
            if path:
                entry.setText(path)

        button.clicked.connect(browse)
        row.addWidget(button)
        return holder

    def _save(self) -> None:
        owner = self.owner
        theme = str(self.theme_box.currentData())
        # 目录也按 config 所在目录解析，和 database / log_file 一致
        cookie_dir = self.cookie_dir.text().strip()
        if cookie_dir and not os.path.isabs(cookie_dir):
            cookie_dir = os.path.normpath(
                os.path.join(os.path.dirname(owner.config_path), cookie_dir)
            )
        owner.cfg.update({
            "database": self.db.text().strip(),
            "log_file": self.log.text().strip(),
            "heartbeat_seconds": self.heartbeat.value(),
            "backoff_max_seconds": self.backoff.value(),
            "log_level": self.level.currentText(),
            "cookie_dir": cookie_dir,
            "theme": theme,
            "window_opacity": self.window_opacity.value(),
        })
        owner.danmaku.document().setMaximumBlockCount(self.max_blocks.value())
        owner._reload_sender()
        owner._apply_theme(resolve_theme(theme))
        owner._apply_opacity()
        save_gui_config(owner.config_path, owner.cfg)
        owner._append_sys("设置已保存" + ("，连接中修改数据库/日志需重连生效" if owner.service.running else ""))
        self.accept()


def _set_app_user_model_id() -> None:
    """让任务栏/Alt-Tab 认这个应用自己的图标，而不是 python.exe 的。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("danmaku-it")
    except Exception:  # noqa: BLE001 - 改造失败不影响运行
        pass


def run_gui(config_path: str) -> int:
    _set_app_user_model_id()
    app = QApplication.instance() or QApplication([])
    icon = QIcon(ICON_PATH) if os.path.exists(ICON_PATH) else QIcon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    # The main window is a Qt.Tool, which does not count as a "primary window".
    # Without this, closing any child dialog (e.g. settings) quits the whole app.
    app.setQuitOnLastWindowClosed(False)
    window = FloatingWindow(config_path)
    window.show()
    return app.exec()
