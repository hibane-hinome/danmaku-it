"""Floating danmaku machine built on Qt (PySide6) for high-volume rooms."""

from __future__ import annotations

import asyncio
import html
import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QGuiApplication, QImage, QTextBlockUserData,
    QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSizeGrip, QSpinBox, QSplitter, QTextBrowser, QVBoxLayout,
    QWidget,
)

from .bili_api import BiliApi
from .collector import RoomCollector
from .config import DEFAULTS
from .fixtures import JPN_AFTER, JPN_BEFORE, load_replay
from .images import ImageLoader
from .storage import Storage, export_csv, export_ndjson, extract_sc

TITLE = "SC Collector · 外挂弹幕机"
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
    for key in ("database", "log_file"):
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

    def start(self, config: dict, room: int) -> None:
        if self.running:
            return
        self.running = True
        config = dict(config, rooms=[room])
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

    def stop(self, timeout: float = 8.0) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)


# ---------------- floating window ----------------

DARK_STYLE = """
QFrame#panel {
    background-color: rgba(24, 26, 30, 242);
    border: 1px solid rgba(255, 255, 255, 32);
    border-radius: 14px;
}
QLabel { color: #d6d8dd; font-size: 12px; }
QLabel#title { color: #f0f2f5; font-size: 13px; font-weight: 600; }
QLabel#stat { color: #8b93a1; font-size: 11px; }
QLabel#section { color: #f0a020; font-size: 12px; font-weight: 600; }
QLabel#live  { color: #4ade80; font-size: 11px; }
QLabel#dead  { color: #f87171; font-size: 11px; }
QPlainTextEdit, QTextBrowser {
    background: rgba(14, 15, 18, 200);
    color: #e8eaee;
    border: none;
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
QPushButton#ghost { background: transparent; border: none; color: #9aa3b2; padding: 4px 8px; }
QPushButton#ghost:hover { color: #ffffff; }
QCheckBox { color: #c3c8d1; font-size: 11px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #3a414d; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #4b5462; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
"""

LIGHT_STYLE = """
QFrame#panel {
    background-color: #f7f8fa;
    border: 1px solid #d8dee6;
    border-radius: 10px;
}
QLabel { color: #374151; font-size: 12px; }
QLabel#title { color: #111827; font-size: 13px; font-weight: 600; }
QLabel#stat { color: #6b7280; font-size: 11px; }
QLabel#section { color: #b45309; font-size: 12px; font-weight: 600; }
QLabel#live  { color: #16a34a; font-size: 11px; }
QLabel#dead  { color: #dc2626; font-size: 11px; }
QPlainTextEdit, QTextBrowser {
    background: #ffffff;
    color: #1f2328;
    border: 1px solid #e3e7ee;
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
QPushButton#ghost { background: transparent; border: none; color: #6b7280; padding: 4px 8px; }
QPushButton#ghost:hover { color: #111827; background: #eef1f5; border-radius: 6px; }
QCheckBox { color: #374151; font-size: 11px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #c7ced8; border-radius: 4px; min-height: 24px; }
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
        self.service = CollectorService()
        self.state = RoomState(input_room=0)
        self._images = ImageLoader(
            os.path.join(os.path.dirname(self.config_path), "cache", "images")
        )
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

        self.setWindowTitle(TITLE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 640)
        self.setMinimumSize(540, 320)

        self._build()
        self._apply_theme(clear=False)
        self._restore_pane_order()
        self._restore_layout()
        self._restore_panes()
        self._restore_geometry()

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

        self._style_hints = QGuiApplication.styleHints()
        self._style_hints.colorSchemeChanged.connect(self._on_system_theme_changed)

    # ----- theme -----

    def _apply_theme(self, theme: str | None = None, clear: bool = True) -> None:
        if theme is not None and theme != self.theme:
            self.theme = theme
        self._c = THEME_COLORS[self.theme]
        colors = self._c

        self.setStyleSheet(DARK_STYLE if self.theme == "dark" else LIGHT_STYLE)
        self.splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {colors['split']}; }}"
        )
        self._shadow.setColor(QColor(*colors["shadow"]))
        if hasattr(self, "room_arrow"):
            self.room_arrow.setStyleSheet(
                f"color:{colors['arrow']}; background:transparent; font-size:13px;"
            )
        for handle in self._drag_handles.values():
            handle.setStyleSheet(
                f"QLabel#grip {{ color: {colors['grip']}; font-size: 12px; }}"
            )

        self._fmt_user.setForeground(QColor(colors["user"]))
        self._fmt_time.setForeground(QColor(colors["time"]))
        self._fmt_sc.setForeground(QColor(colors["price"]))
        self._fmt_sys.setForeground(QColor(colors["time"]))

        if clear:
            for view in (self.danmaku, self.event_view, self.sc_view):
                view.clear()

    def _on_system_theme_changed(self, scheme) -> None:
        if str(self.cfg.get("theme", "dark")) != "system":
            return
        self._apply_theme("dark" if scheme == Qt.ColorScheme.Dark else "light")

    # ----- construction -----

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

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
        header.addWidget(self.title_label, 1)

        self.pin_btn = QPushButton("置顶")
        self.pin_btn.setObjectName("ghost")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(True)
        self.pin_btn.clicked.connect(self._toggle_topmost)
        header.addWidget(self.pin_btn)

        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.clicked.connect(self._open_settings)
        header.addWidget(self.settings_btn)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("ghost")
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)
        return header

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
        row.addWidget(self._section_label("事件"))
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
        self.room_input.setFixedWidth(148)
        self.room_input.setToolTip("可直接输入房间号；下拉列表可右键删除历史记录")
        self.room_input.lineEdit().setPlaceholderText("房间号")
        self.room_input.lineEdit().returnPressed.connect(self._toggle_connect)
        self.room_input.view().viewport().installEventFilter(self)
        # QSS 无法稳定绘制下拉三角，用透明子标签叠在按钮区上（点击穿透到下拉区域）。
        self.room_arrow = QLabel("▾", self.room_input)
        self.room_arrow.setAlignment(Qt.AlignCenter)
        self.room_arrow.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._fill_room_history()
        footer.addWidget(self.room_input)

        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setProperty("active", "false")
        self.connect_btn.clicked.connect(self._toggle_connect)
        footer.addWidget(self.connect_btn)

        self.export_btn = QPushButton("导出")
        self.export_btn.setObjectName("ghost")
        self.export_btn.clicked.connect(self._export_menu)
        footer.addWidget(self.export_btn)

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
        copy_action = menu.addAction("复制", view.copy)
        copy_action.setEnabled(view.textCursor().hasSelection())
        menu.addSeparator()
        menu.addAction("清空本栏", handler)
        menu.exec(view.mapToGlobal(pos))

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
            try:
                self.move(int(geo["x"]), int(geo["y"]))
            except (KeyError, TypeError, ValueError):
                pass

    def _toggle_topmost(self) -> None:
        on = self.pin_btn.isChecked()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()

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
            self.room_input.addItem(str(room), room)
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
        raw = self.room_input.currentText().strip()
        if not raw.isdigit():
            QMessageBox.warning(self, TITLE, "请输入数字房间号")
            return
        room = int(raw)
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
        self.service.start(self.cfg, room)

    def _disconnect(self) -> None:
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("停止中")
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

    # ----- event pump -----

    def _tick(self) -> None:
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
                self.state.real_room = int(event.get("room_id") or 0)
                self.state.title = event.get("title") or ""
                self.title_label.setText(self.state.title or TITLE)
                self._append_sys(f"已进入 {self.state.real_room} · {self.state.title}")
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

    @staticmethod
    def _row_image_urls(row: dict) -> list[str]:
        """只下载表情图。头像是一人一图、几乎不命中缓存，量大且没多少人看，不取。"""
        if row.get("is_emoticon"):
            url = row.get("emote_url") or ""
            return [url] if url else []
        return []

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

    def _danmaku_html(self, row: dict) -> str:
        event_time = row.get("event_time") or 0
        stamp = (
            datetime.fromtimestamp(event_time).strftime("%H:%M:%S")
            if event_time
            else datetime.now().strftime("%H:%M:%S")
        )
        parts = [f'<span style="color:{self._c["time"]}">{stamp}</span>']

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

        text = html.escape(row.get("text") or "")
        if row.get("is_emoticon"):
            emote_url = row.get("emote_url") or ""
            emote = self._images.image(emote_url)
            if emote is not None:
                width, height = self._image_size(emote, EMOTE_PX)
                parts.append(
                    f'<img src="{html.escape(emote_url)}" width="{width}" height="{height}">'
                )
            else:
                # 图还没到或下载失败，先按原来的青色 [名字] 显示
                parts.append(f'<span style="color:{self._c["emoticon"]}">[{text}]</span>')
        else:
            parts.append(f'<span style="color:{self._c["text"]}">{text}</span>')
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
        cursor.insertHtml(
            f'<span style="color:{self._c["time"]}">{stamp}</span> '
            f'<span style="color:{color}">[{html.escape(tag)}]</span> '
            f'<span style="color:{self._c["text"]}">{html.escape(text)}</span>'
        )
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

    # ----- settings / export -----

    def _open_settings(self) -> None:
        dialog = Dialog(self)
        dialog.adjustSize()
        center = self.frameGeometry().center()
        dialog.move(center - dialog.rect().center())
        dialog.exec()

    def _export_menu(self) -> None:
        path, selected = QFileDialog.getSaveFileName(
            self, "导出", f"sc_{datetime.now():%Y%m%d_%H%M%S}.ndjson",
            "NDJSON (*.ndjson);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            storage = Storage(self.cfg["database"])
            try:
                if path.lower().endswith(".csv") or "CSV" in selected:
                    count = export_csv(storage.iter_rows(), path)
                else:
                    count = export_ndjson(storage.iter_rows(with_raw=True), path, with_raw=True)
            finally:
                storage.close()
            self._append_sys(f"已导出 {count} 条 → {path}")
        except Exception as exc:  # noqa: BLE001 - report to user
            QMessageBox.critical(self, "导出失败", str(exc))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        height = self.room_input.height()
        self.room_arrow.setGeometry(self.room_input.width() - 18, 0, 16, height)

    def closeEvent(self, event) -> None:
        if self.service.running:
            self.service.stop()
        if self._mock_stop is not None:
            self._mock_stop.set()
        self.timer.stop()
        self._image_timer.stop()
        self._images.stop()
        self.cfg["split"] = self._layout_sizes()
        self.cfg["window"] = {"x": self.x(), "y": self.y()}
        try:
            save_gui_config(self.config_path, self.cfg)
        except OSError:
            pass
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()


class Dialog(QDialog):
    def __init__(self, owner: FloatingWindow) -> None:
        super().__init__(owner)
        self.owner = owner
        # The main window is a frameless always-on-top Qt.Tool, so the dialog needs its own
        # top-most flag, otherwise it is rendered underneath the floating panel.
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        c = owner._c
        self.setStyleSheet(f"""
            QDialog {{ background: {c['dialog_bg']}; }}
            QLabel {{ color: {c['dialog_fg']}; font-size: 12px; }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
                background: {c['dialog_input_bg']}; color: {c['dialog_input_fg']};
                border: 1px solid {c['dialog_border']};
                border-radius: 6px; padding: 4px 6px; font-size: 12px;
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
        """)
        cfg = owner.cfg

        form = QFormLayout(self)
        form.setContentsMargins(16, 16, 16, 12)
        form.setSpacing(10)

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

        self.sessdata = QLineEdit(str(cfg.get("sessdata", "")))
        self.sessdata.setEchoMode(QLineEdit.Password)
        self.sessdata.setPlaceholderText("留空则沿用 Cookie 文件；两者都空为匿名")
        form.addRow("SESSDATA", self.sessdata)

        self.dede_user_id = QLineEdit(str(cfg.get("dede_user_id", "")))
        self.dede_user_id.setPlaceholderText("必须与 SESSDATA 属于同一账号")
        form.addRow("DedeUserID", self.dede_user_id)

        self.cookie_file = QLineEdit(str(cfg.get("cookie_file", "")))
        self.cookie_file.setPlaceholderText("可选：浏览器导出的 cookie 文件路径")
        form.addRow("Cookie 文件", self.cookie_file)

        self.topmost = QCheckBox("窗口置顶")
        self.topmost.setChecked(owner.pin_btn.isChecked())
        form.addRow("", self.topmost)

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

    def _with_browse(self, entry: QLineEdit, save: bool) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(entry, 1)
        button = QPushButton("浏览")
        button.setObjectName("plain")

        def browse() -> None:
            if save:
                path, _ = QFileDialog.getSaveFileName(self, "选择数据库", entry.text(), "SQLite (*.db)")
            else:
                path, _ = QFileDialog.getSaveFileName(self, "选择日志", entry.text(), "Log (*.log)")
            if path:
                entry.setText(path)

        button.clicked.connect(browse)
        row.addWidget(button)
        return holder

    def _save(self) -> None:
        owner = self.owner
        theme = str(self.theme_box.currentData())
        owner.cfg.update({
            "database": self.db.text().strip(),
            "log_file": self.log.text().strip(),
            "heartbeat_seconds": self.heartbeat.value(),
            "backoff_max_seconds": self.backoff.value(),
            "log_level": self.level.currentText(),
            "sessdata": self.sessdata.text().strip(),
            "dede_user_id": self.dede_user_id.text().strip(),
            "cookie_file": self.cookie_file.text().strip(),
            "theme": theme,
        })
        owner.danmaku.document().setMaximumBlockCount(self.max_blocks.value())
        owner._apply_theme(resolve_theme(theme))
        save_gui_config(owner.config_path, owner.cfg)
        if self.topmost.isChecked() != owner.pin_btn.isChecked():
            owner.pin_btn.setChecked(self.topmost.isChecked())
            owner._toggle_topmost()
        owner._append_sys("设置已保存" + ("，连接中修改数据库/日志需重连生效" if owner.service.running else ""))
        self.accept()


def run_gui(config_path: str) -> int:
    app = QApplication.instance() or QApplication([])
    # The main window is a Qt.Tool, which does not count as a "primary window".
    # Without this, closing any child dialog (e.g. settings) quits the whole app.
    app.setQuitOnLastWindowClosed(False)
    window = FloatingWindow(config_path)
    window.show()
    return app.exec()
