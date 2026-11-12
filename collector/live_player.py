"""看直播：不用浏览器，直接播直播间画面。

为什么要自己取流：打开直播间页面就等于对着全世界的第三方爬虫广播「我在看谁」，
那些观众统计、盯播脚本都能把在场的人抓下来。这里改成直接调用官方的取流接口
（getRoomPlayInfo），拿 HLS 地址自己解，全程不开任何网页。

两个关键点：

* 取流接口要 Cookie（高清晰度、部分房间甚至是必须登录），走的是和发弹幕
  同一份账号凭证——所以直接用 ``DanmakuSender`` 的请求头和 cookie。
* Qt 的 QMediaPlayer 没法定制请求头，而 B 站 CDN 要 Referer。于是本地起一个
  只监听 127.0.0.1 的小代理：把播放列表和分片都转一手，顺手补上 Referer/UA，
  播放器只跟 127.0.0.1 打交道。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode, urljoin, urlsplit

from PySide6.QtCore import QObject, QSize, Qt, QThreadPool, QTimer, QUrl, QRunnable, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

PLAY_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"

# 清晰度档位：数字是 B 站的 qn，高的排前面，取流时按「用户想要 → 实际能给」降级
QN_CHOICES = [(10000, "原画"), (400, "蓝光"), (250, "超清"), (150, "高清"), (80, "流畅")]
QN_LABELS = dict(QN_CHOICES)

# 断流后自动重连的次数上限，用完就退回「未连接」，等人再点
MAX_RETRIES = 5

# 播放列表里要改写的地方：裸 URI 行，和 #EXT-X-MAP / #EXT-X-KEY 里的 URI="..."
_URI_ATTR = re.compile(r'URI="([^"]+)"')


class PlayerError(Exception):
    """取流/播放失败，消息直接给界面看。"""


# ----- 取流 -----


def fetch_stream(sender, room_id: int, qn: int = 10000) -> dict:
    """问 B 站要直播地址。返回 {url, qn, qn_label, kind, title}。

    优先 HLS（Qt 的 ffmpeg 后端对 HLS 支持最稳，分片还能代理重写），
    没有 HLS 再退回 HTTP-FLV。``sender`` 只用来借 cookie / UA / Referer。
    """
    query = urlencode(
        {
            "room_id": room_id,
            "protocol": "0,1",       # 0=http_stream(flv) 1=http_hls
            "format": "0,1,2",       # 0=flv 1=ts 2=fmp4
            "codec": "0,1",          # 0=avc 1=hevc
            "qn": qn,
            "platform": "web",
            "ptype": 8,
            "dolby": 5,
            "panorama": 1,
        }
    )
    url = f"{PLAY_URL}?{query}"
    try:
        payload = sender._get_json(url, f"https://live.bilibili.com/{room_id}")
    except urllib.error.HTTPError as exc:
        raise PlayerError(f"取流接口返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlayerError(f"连不上取流接口：{exc}") from exc

    code = payload.get("code")
    if code not in (0, None):
        raise PlayerError(payload.get("message") or payload.get("msg") or f"取流失败（code={code}）")

    data = payload.get("data") or {}
    status = data.get("live_status")
    if status != 1:
        raise PlayerError("主播没在播" if status == 0 else f"直播间状态异常（live_status={status}）")

    playurl = ((data.get("playurl_info") or {}).get("playurl") or {})
    streams = playurl.get("stream") or []
    if not streams:
        raise PlayerError("这个直播间没有可用的流（可能需要登录，或者只有付费画质）")

    # 先把所有候选摊平：HLS 排前面，同一协议里 fmp4 优先（avc 比 hevc 兼容性好）
    candidates: list[tuple[int, int, dict, str]] = []
    for stream in streams:
        proto = stream.get("protocol_name") or ""
        for fmt in stream.get("format") or []:
            name = fmt.get("format_name") or ""
            for codec in fmt.get("codec") or []:
                full = _join_url(codec)
                if not full:
                    continue
                rank = 0 if proto == "http_hls" else 2
                if name == "fmp4":
                    rank -= 1
                if (codec.get("codec_name") or "") == "avc":
                    rank -= 0.5
                candidates.append((rank, int(codec.get("current_qn") or 0), codec, full))
    if not candidates:
        raise PlayerError("取到了直播信息但里面没有可用地址")

    candidates.sort(key=lambda item: (item[0], -item[1]))
    _, got_qn, _, src = candidates[0]
    return {
        "url": src,
        "qn": got_qn,
        "qn_label": QN_LABELS.get(got_qn, str(got_qn)),
        "kind": "hls" if src.split("?")[0].endswith(".m3u8") else "flv",
        "title": data.get("room_id") and (data.get("title") or ""),
    }


def _join_url(codec: dict) -> str:
    """把 base_url + host + extra 拼成完整地址（B 站这套结构就是这么绕）。"""
    base = codec.get("base_url") or ""
    if not base:
        return ""
    infos = codec.get("url_info") or []
    if not infos:
        return base
    info = infos[0]
    host = (info.get("host") or "").rstrip("/")
    extra = info.get("extra") or ""
    return f"{host}{base}{('?' + extra) if extra else ''}"


# ----- 本地代理 -----


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(text: str) -> str:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad).decode("utf-8")


def _local_path(upstream: str) -> str:
    """上游地址 → 本地代理路径，保留原文件名（扩展名要给 ffmpeg 看）。"""
    name = urlsplit(upstream).path.rsplit("/", 1)[-1] or "seg.bin"
    return f"/f/{_b64(upstream)}/{quote(name)}"


class _ProxyHandler(BaseHTTPRequestHandler):
    """把上游的播放列表/分片转一手，补上 Referer 和 UA。

    只挂在 127.0.0.1 上，路径里的目标地址是 base64 过的，所以外人即使发现端口
    也拿不到「代理任意 URL」的能力——只有本进程拼出来的地址能被打开。
    """

    protocol_version = "HTTP/1.0"
    upstream_headers: dict[str, str] = {}

    def log_message(self, fmt, *args):  # noqa: D401 - 静默，别把日志刷满
        return

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的接口
        # 地址长这样：/f/<base64 的目标地址>/<原文件名>。
        # 尾巴上那截文件名不是装饰：ffmpeg 的 HLS 分片走扩展名白名单，
        # 路径没有 .ts/.m4s/.mp4 就会被当成非法分片直接丢掉。
        parts = self.path.lstrip("/").split("/", 2)
        if len(parts) < 2 or parts[0] != "f":
            self.send_error(400, "bad request")
            return
        try:
            target = _unb64(parts[1])
        except Exception:  # noqa: BLE001 - 乱传的东西直接拒
            self.send_error(400, "bad request")
            return

        if urlsplit(target).path.endswith(".m3u8"):
            self._serve_playlist(target)
        else:
            self._serve_stream(target)

    # -- 播放列表：下载后把里面的地址全换成指向自己的 --

    def _serve_playlist(self, target: str) -> None:
        try:
            body = self._fetch(target)
        except PlayerError as exc:
            self.send_error(502, str(exc))
            return
        text = body.decode("utf-8", "replace")
        base = target
        out = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                out.append(line)
                continue
            if stripped.startswith("#"):
                # #EXT-X-MAP:URI="init.mp4" 这类内嵌地址也要换掉
                out.append(
                    _URI_ATTR.sub(
                        lambda m: f'URI="{_local_path(urljoin(base, m.group(1)))}"',
                        line,
                    )
                )
                continue
            out.append(_local_path(urljoin(base, stripped)))
        data = ("\n".join(out) + "\n").encode("utf-8")
        self._send_bytes(data, "application/vnd.apple.mpegurl")

    def _send_bytes(self, data: bytes, ctype: str) -> None:
        """按 Range 切一段再回。

        ffmpeg 取完播放列表后可能带着 ``Range: bytes=N-`` 再开一次（接着上次读完的
        位置继续），这时候要是回整段，它会把重复的 #EXTM3U 当成坏数据。
        """
        rng = self.headers.get("Range") or ""
        start, end = 0, len(data) - 1
        partial = False
        match = re.match(r"bytes=(\d*)-(\d*)", rng)
        if match and (match.group(1) or match.group(2)):
            partial = True
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), len(data) - 1)
            else:  # bytes=-N 表示最后 N 字节
                start = max(0, len(data) - int(match.group(2)))
        chunk = data[start : end + 1] if partial else data

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- 分片 / FLV：边读边写，不攒内存 --

    def _serve_stream(self, target: str) -> None:
        # fmp4 的初始化段是「文件里的一段字节」（#EXT-X-MAP 带 BYTERANGE），
        # ffmpeg 会用 Range 去取。这里不接管 Range 就会把整个文件回过去，
        # 解析器拿到错位的字节直接报 invalid data。所以 Range 一律原样转给上游。
        headers = dict(self.upstream_headers)
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            req = urllib.request.Request(target, headers=headers)
            upstream = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as exc:
            self.send_error(exc.code, "upstream error")
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            self.send_error(502, "upstream unreachable")
            return

        status = upstream.status if hasattr(upstream, "status") else 200
        ctype = upstream.headers.get("Content-Type") or "application/octet-stream"
        self.send_response(206 if status == 206 else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        # 有长度就报（分片），没有就别报（FLV 直播流是一直到不完的）
        length = upstream.headers.get("Content-Length")
        if length:
            self.send_header("Content-Length", length)
        content_range = upstream.headers.get("Content-Range")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            upstream.close()

    def _fetch(self, target: str) -> bytes:
        try:
            req = urllib.request.Request(target, headers=dict(self.upstream_headers))
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise PlayerError(f"上游返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PlayerError(f"上游取不到：{exc}") from exc


class StreamProxy:
    """127.0.0.1 上的小代理，活到窗口关掉为止。"""

    def __init__(self, headers: dict[str, str]) -> None:
        handler = type("_Handler", (_ProxyHandler,), {"upstream_headers": headers})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url_for(self, upstream: str) -> str:
        """给播放器用的本地地址。

        一律走 /s：是不是播放列表由目标地址的后缀判断。HLS 有主列表→子列表
        两层，子列表也得改写（里面的分片是相对路径），所以不能只看入口。
        """
        return f"http://127.0.0.1:{self.port}{_local_path(upstream)}"

    def close(self) -> None:
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001 - 关掉时出什么错都无所谓
            pass
        try:
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass


# ----- 取流任务（放线程池，别卡界面） -----


class _FetchSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _FetchTask(QRunnable):
    def __init__(self, sender, room_id: int, qn: int) -> None:
        super().__init__()
        self.sender = sender
        self.room_id = room_id
        self.qn = qn
        self.signals = _FetchSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(fetch_stream(self.sender, self.room_id, self.qn))
        except PlayerError as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回到界面
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")


# ----- 直播画面栏 -----


def _repolish(widget) -> None:
    """改过 property 之后要让 QSS 重新算一遍，否则样式不跟着变。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class LivePlayerPane(QWidget):
    """直播画面栏：一块画面 + 一行控制条，嵌在弹幕机左边。

    「连接 / 断开」是同一个按钮的两种状态：连接 = 取流并播放，断开 = 停掉，
    只停画面，不动弹幕那一路。断流、出错会自己重连几次，次数用完就退回
    「未连接」，等人再点一次，不会一直空转。

    单独开窗的版本已经没有了：画面和弹幕放在一个窗口里，看的时候不用来回切。
    """

    state_changed = Signal(bool)  # True=正在播（含连接中），False=已断开

    def __init__(
        self,
        sender,
        cfg: dict,
        room_provider=None,
        save_config=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.sender = sender
        self.cfg = cfg
        self._room_provider = room_provider
        self._save_config = save_config
        self.room_id = 0
        self._pool = QThreadPool.globalInstance()
        self._proxy: StreamProxy | None = None
        self._stream: dict | None = None
        self._task: _FetchTask | None = None
        self._active = False          # 用户想不想看：连接/断开的唯一真值
        self._retries = 0
        self._stalled = 0
        self._full_host: QWidget | None = None
        self._home: tuple | None = None
        self._home_sizes: list[int] | None = None

        self.setObjectName("playerpane")
        self.setMinimumWidth(300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._build()

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._reconnect)

        self._sync_connect()

    # ----- 界面 -----

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.video = QVideoWidget(self)
        self.video.setMinimumSize(QSize(200, 120))
        outer.addWidget(self.video, 1)

        bar = QWidget(self)
        bar.setObjectName("playerbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(6)

        # 连接 / 断开：一个按钮两种状态，和主控那排的连接按钮同一套配色
        self.connect_btn = QPushButton("连接")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setProperty("active", "false")
        self.connect_btn.setCursor(Qt.PointingHandCursor)
        self.connect_btn.clicked.connect(self._toggle_connect)
        row.addWidget(self.connect_btn)

        self.play_btn = QPushButton("暂停")
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setToolTip("直播不能倒回去，再播从最新画面接上")
        self.play_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self.play_btn)

        state = self._saved()
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(66)
        self.vol_slider.setValue(max(0, min(100, int(state.get("vol", 80) or 0))))
        self.vol_slider.setToolTip("音量")
        self.vol_slider.valueChanged.connect(self._on_volume)
        self.vol_slider.sliderReleased.connect(self._remember)
        row.addWidget(self.vol_slider)

        self.qn_box = QComboBox()
        for _, label in QN_CHOICES:
            self.qn_box.addItem(label)
        self.qn_box.setFixedWidth(62)
        self.qn_box.setToolTip("清晰度，切换会重新取一次流")
        try:
            qn_index = int(state.get("qn", 0) or 0)
        except (TypeError, ValueError):
            qn_index = 0
        self.qn_box.setCurrentIndex(max(0, min(qn_index, len(QN_CHOICES) - 1)))
        self.qn_box.currentIndexChanged.connect(self._on_quality)
        row.addWidget(self.qn_box)

        self.status = QLabel("未连接")
        self.status.setObjectName("playerstatus")
        # 状态字只占零头：窄的时候先让位给按钮
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setMinimumWidth(0)
        row.addWidget(self.status, 1)

        self.full_btn = QPushButton("全屏")
        self.full_btn.setCursor(Qt.PointingHandCursor)
        self.full_btn.setToolTip("全屏看画面（Esc / F 退出）")
        self.full_btn.clicked.connect(self._toggle_full)
        row.addWidget(self.full_btn)

        outer.addWidget(bar, 0)
        self._bar = bar

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(self.vol_slider.value() / 100)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)

    def _saved(self) -> dict:
        state = self.cfg.get("player")
        return state if isinstance(state, dict) else {}

    def _remember(self) -> None:
        """清晰度、音量落盘（嵌进主窗口后不再记窗口位置）。"""
        state = dict(self._saved())
        state.update({"qn": self.qn_box.currentIndex(), "vol": self.vol_slider.value()})
        self.cfg["player"] = state
        if self._save_config:
            self._save_config()

    # ----- 连接 / 断开 -----

    def _toggle_connect(self) -> None:
        if self._active:
            self.disconnect_stream()
        else:
            self.connect_stream()

    def connect_stream(self, room_id: int | None = None) -> None:
        """连上（或者重连）这个房间的画面。"""
        room = int(room_id or self.room_id or 0)
        if not room and self._room_provider is not None:
            try:
                room = int(self._room_provider() or 0)
            except Exception:  # noqa: BLE001 - 取不到就当没房间
                room = 0
        if not room:
            self._set_status("没有房间号")
            return
        self.room_id = room
        self._active = True
        self._retries = 0
        self._stalled = 0
        self._sync_connect()
        self._fetch()

    def disconnect_stream(self, reason: str = "已断开") -> None:
        """停掉画面。弹幕那一路完全不受影响。"""
        self._active = False
        self._retry_timer.stop()
        self.player.stop()
        self.player.setSource(QUrl())
        self._stream = None
        self._set_status(reason)
        self._sync_connect()

    def is_streaming(self) -> bool:
        return self._active

    def show_room(self, room_id: int, autoplay: bool = True) -> None:
        """画面栏被拉开时叫一次：换房间就重取，没在播就顺手连上。"""
        room = int(room_id or 0)
        if room and room != self.room_id:
            self.room_id = room
            if self._active:
                self.player.stop()
                self._fetch()
                return
        if autoplay and not self._active:
            self.connect_stream(room)

    def set_sender(self, sender) -> None:
        """换账号：代理里存着 cookie，请求头得跟着换，所以代理整个重建。"""
        if sender is self.sender:
            return
        self.sender = sender
        if self._proxy is not None:
            self._proxy.close()
            self._proxy = None
        if self._active:
            self.player.stop()
            self._fetch()
        else:
            self._set_status("未连接")

    def _sync_connect(self) -> None:
        on = self._active
        self.connect_btn.setText("断开" if on else "连接")
        self.connect_btn.setProperty("active", "true" if on else "false")
        _repolish(self.connect_btn)
        self.connect_btn.setToolTip(
            "停掉画面（弹幕照常收）" if on else "取这个房间的直播流并播放，不打开直播间网页"
        )
        self.play_btn.setEnabled(on)
        self.qn_box.setEnabled(on)
        self.state_changed.emit(on)

    # ----- 取流 / 播放 -----

    def _current_qn(self) -> int:
        index = max(0, min(self.qn_box.currentIndex(), len(QN_CHOICES) - 1))
        return QN_CHOICES[index][0]

    def _fetch(self) -> None:
        self._set_status("取流中…")
        task = _FetchTask(self.sender, self.room_id, self._current_qn())
        task.signals.done.connect(self._on_fetched)
        task.signals.failed.connect(self._on_fetch_failed)
        self._task = task
        self._pool.start(task)

    def _on_fetched(self, stream: dict) -> None:
        if not self._active:  # 取流途中被断开了
            return
        self._stream = stream
        if self._proxy is None:
            self._proxy = StreamProxy(
                self.sender._headers(f"https://live.bilibili.com/{self.room_id}")
            )
        self.player.setSource(QUrl(self._proxy.url_for(stream["url"])))
        self.player.play()
        self.play_btn.setText("暂停")
        self._set_status(f"{stream['qn_label']} · 连接中…")

    def _on_fetch_failed(self, message: str) -> None:
        if not self._active:
            return
        self._set_status(message)
        # 没开播、没有可用流这类问题重试也没用，直接退回「未连接」
        if "没在播" in message or "没有可用" in message:
            self.disconnect_stream(message)
            return
        self._schedule_retry()

    def _on_status(self, status) -> None:
        if not self._active:
            return
        if status == QMediaPlayer.MediaStatus.BufferedMedia:
            self._retries = 0
            self._stalled = 0
            qn = (self._stream or {}).get("qn_label", "")
            self._set_status(f"播放中 · {qn}" if qn else "播放中")
        elif status == QMediaPlayer.MediaStatus.Stalled:
            self._set_status("缓冲中…")
            self._stalled += 1
            # 直播流卡住超过几次就整条重来，等它自己好基本等不到
            if self._stalled >= 6:
                self._stalled = 0
                self._schedule_retry()
        elif status in (
            QMediaPlayer.MediaStatus.EndOfMedia,
            QMediaPlayer.MediaStatus.InvalidMedia,
        ):
            self._schedule_retry()

    def _on_error(self, error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError or not self._active:
            return
        self._set_status(f"播放出错：{message or '未知原因'}")
        self._schedule_retry()

    def _schedule_retry(self) -> None:
        if not self._active or self._retry_timer.isActive():
            return
        if self._retries >= MAX_RETRIES:
            self.disconnect_stream("连接失败，点「连接」再试")
            return
        self._retries += 1
        delay = min(2 + self._retries * 2, 10)
        self._set_status(f"{delay} 秒后重连（第 {self._retries} 次）")
        self._retry_timer.start(delay * 1000)

    def _reconnect(self) -> None:
        if not self._active:
            return
        self.player.stop()
        # 上游地址有时效（一般几小时），重连一律重新取一遍
        self._fetch()

    # ----- 控制 -----

    def _toggle_pause(self) -> None:
        if not self._active:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("播放")
            self._set_status("已暂停")
        else:
            # 直播不能「继续播」太久之前的画面，暂停后再播直接从最新处接上
            self.player.play()
            self.play_btn.setText("暂停")

    def _on_volume(self, value: int) -> None:
        self.audio.setVolume(value / 100)

    def _on_quality(self, _index: int) -> None:
        self._remember()
        if self._active:
            self._reconnect()

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setToolTip(text)

    # ----- 全屏：把整条画面栏临时搬到一个全屏窗口里 -----

    def _toggle_full(self) -> None:
        if self._full_host is not None:
            self._leave_full()
        else:
            self._enter_full()

    def _enter_full(self) -> None:
        parent = self.parentWidget()
        index = parent.indexOf(self) if hasattr(parent, "indexOf") else 0
        sizes = parent.sizes() if hasattr(parent, "sizes") else None
        self._home = (parent, index)
        self._home_sizes = list(sizes) if sizes else None

        host = QWidget(None, Qt.Window)
        host.setWindowTitle("直播画面")
        host.setAttribute(Qt.WA_DeleteOnClose, False)
        box = QVBoxLayout(host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self.setParent(host)
        box.addWidget(self)          # 画面栏带着控制条一起进全屏
        self._full_host = host
        self.full_btn.setText("退出全屏")

        QShortcut(QKeySequence(Qt.Key_Escape), host, activated=self._leave_full)
        QShortcut(QKeySequence("F"), host, activated=self._leave_full)
        host.showFullScreen()
        self.play_btn.setFocus()

    def _leave_full(self) -> None:
        host, self._full_host = self._full_host, None
        if host is None:
            return
        parent, index = self._home or (None, 0)
        host.layout().removeWidget(self)
        self.setParent(parent)
        if hasattr(parent, "insertWidget"):
            parent.insertWidget(index, self)
        if self._home_sizes and hasattr(parent, "setSizes"):
            parent.setSizes(self._home_sizes)
        self.show()
        self.full_btn.setText("全屏")
        host.close()
        host.deleteLater()

    # ----- 收尾 -----

    def shutdown(self) -> None:
        self.disconnect_stream()
        if self._proxy is not None:
            self._proxy.close()
            self._proxy = None
