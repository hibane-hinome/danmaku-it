"""在弹幕机上直接发弹幕。

匿名连接只能收，发弹幕必须登录。两个 cookie 决定成败：

- ``SESSDATA``   —— 身份凭证
- ``bili_jct``   —— CSRF token，发弹幕时同时作为 ``csrf`` 与 ``csrf_token`` 传

两者都从 config 的 ``cookie_file``（浏览器导出的 cookie 文件）或 ``sessdata``
字段取。cookie 文件里没有 ``bili_jct`` 时，用 SESSDATA 找 passport 换一次
（B 站会随 ``Set-Cookie`` 下发），所以只导出了 SESSDATA 也能发。

这个模块**不碰 Qt**，界面自己在后台线程里调用，避免网络卡住画面。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

SEND_URL = "https://api.live.bilibili.com/msg/send"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
CSRF_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info?csrf="

# 单条弹幕上限（B 站按字符计）
MAX_LEN = 20
# 同一账号两次发送的最小间隔，服务端约 5 秒，本地先挡一层避免无谓报错
MIN_INTERVAL = 5.0

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 常见错误码的人话解释；服务端给了 message 时优先用 message
_CODE_HINTS = {
    -101: "登录已失效，请重新导出 cookie",
    -111: "CSRF 校验失败，cookie 不完整",
    -403: "没有发言权限（可能被禁言、等级不足或需要粉丝牌）",
    -400: "请求被服务端拒绝",
    1003212: "弹幕太长，服务端限制 20 字",
}


class SendError(Exception):
    """发送失败。``code`` 是 B 站返回码，网络层错误时为 None。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def load_cookies(path: str) -> dict[str, str]:
    """读 cookie 文件，返回 ``{name: value}``；文件不存在返回空 dict。

    兼容两种布局：

    - 标准 Netscape（7 列）：``domain, flag, path, secure, expiry, name, value``
    - Chrome 扩展导出（13 列）：``name, value, domain, path, expiry, ...``

    两者靠首列区分：标准格式首列一定是域名（含 ``.``），另一格式首列是
    cookie 名（``SESSDATA`` 之类，不含 ``.``）。
    """
    out: dict[str, str] = {}
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return out

    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        # Netscape 里 HttpOnly cookie 以 #HttpOnly_ 开头，内容仍要解析
        if line.startswith(b"#HttpOnly_"):
            line = line[len(b"#HttpOnly_"):]
        elif line.lstrip().startswith(b"#"):
            continue

        parts = line.split(b"\t")
        if len(parts) < 2:
            continue
        if len(parts) >= 7 and b"." in parts[0]:
            name, value = parts[5], parts[6]
        else:
            name, value = parts[0], parts[1]

        key = name.decode("utf-8", "replace").strip()
        if key:
            out[key] = value.decode("utf-8", "replace").strip()
    return out


class DanmakuSender:
    """用配置里的 cookie 往指定直播间发弹幕。线程安全，可反复调用。"""

    def __init__(self, config: dict) -> None:
        self._ua = config.get("user_agent") or DEFAULT_UA
        self._cookies: dict[str, str] = {}
        self._jct = ""
        self._lock = threading.Lock()
        self._last_sent = 0.0
        self.uname = ""
        self.uid = 0
        self._load(config)

    # ----- 凭证 -----

    def _load(self, config: dict) -> None:
        path = config.get("cookie_file") or ""
        if path and not os.path.isabs(path):
            # config.py 只帮忙解析 database 与 log_file，path 类字段要自己补
            path = os.path.normpath(os.path.join(config.get("config_dir") or ".", path))
        self._cookies = load_cookies(path)

        if config.get("sessdata"):
            self._cookies.setdefault("SESSDATA", str(config["sessdata"]).strip())
        if config.get("dede_user_id"):
            self._cookies.setdefault("DedeUserID", str(config["dede_user_id"]).strip())

        self._jct = self._cookies.get("bili_jct") or ""

    @property
    def has_login(self) -> bool:
        return bool(self._cookies.get("SESSDATA"))

    @property
    def cookie_source(self) -> str:
        return "cookie 文件" if self._cookies else "未配置"

    def _cookie_header(self) -> str:
        items = dict(self._cookies)
        if self._jct:
            items["bili_jct"] = self._jct
        return "; ".join(f"{k}={v}" for k, v in items.items() if v)

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "User-Agent": self._ua,
            "Cookie": self._cookie_header(),
            "Referer": referer,
            "Origin": "https://live.bilibili.com",
        }

    def refresh_csrf(self) -> bool:
        """cookie 里没有 bili_jct 时，用 SESSDATA 找 passport 换一个。

        实测 ``/x/passport-login/web/cookie/info`` 会在 Set-Cookie 里回
        ``bili_jct`` 与 ``DedeUserID``，所以只导 SESSDATA 也能补全。
        """
        if not self.has_login:
            return False
        try:
            req = urllib.request.Request(
                CSRF_URL, headers=self._headers("https://www.bilibili.com/")
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                for raw in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = raw.partition("=")
                    if name.strip() == "bili_jct":
                        token = rest.split(";")[0].strip()
                        if token:
                            self._jct = token
                            self._cookies["bili_jct"] = token
                            return True
        except OSError:
            return False
        return False

    # ----- 请求 -----

    def _get_json(self, url: str, referer: str) -> dict:
        req = urllib.request.Request(url, headers=self._headers(referer))
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _post_json(self, url: str, body: bytes, referer: str) -> dict:
        headers = dict(self._headers(referer))
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def verify(self) -> tuple[bool, str]:
        """确认 cookie 还有效，顺便取昵称。返回 ``(可用, 说明)``。"""
        if not self.has_login:
            return False, "未配置 cookie，只能在弹幕机里看，不能发"
        try:
            payload = self._get_json(NAV_URL, "https://www.bilibili.com/")
        except Exception as exc:  # noqa: BLE001 - 网络异常一律按不可用处理
            return False, f"连不上 B 站：{exc}"

        data = payload.get("data") or {}
        if not data.get("isLogin"):
            return False, "cookie 已失效，请重新导出"
        self.uname = str(data.get("uname") or "")
        self.uid = int(data.get("mid") or 0)
        if not self._jct:
            self.refresh_csrf()
        if not self._jct:
            return False, "拿不到 CSRF token，请重新导出含 bili_jct 的 cookie"
        return True, self.uname

    # ----- 发送 -----

    def cooldown(self) -> float:
        """还要等几秒才能发下一条（0 表示可以发）。"""
        with self._lock:
            return max(0.0, MIN_INTERVAL - (time.monotonic() - self._last_sent))

    def send(self, room_id: int, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise SendError("不能发空弹幕")
        if len(text) > MAX_LEN:
            raise SendError(f"最多 {MAX_LEN} 个字，当前 {len(text)} 个")
        if not room_id:
            raise SendError("还没连上直播间，先点「连接」或填房间号")
        if not self.has_login:
            raise SendError("未配置 cookie，无法发送")
        if not self._jct and not self.refresh_csrf():
            raise SendError("缺少 CSRF token，请重新导出 cookie（需要 bili_jct）")

        wait = self.cooldown()
        if wait > 0:
            raise SendError(f"发太快了，{wait:.0f} 秒后再发")

        body = urllib.parse.urlencode(
            {
                "color": "16777215",
                "fontsize": "25",
                "mode": "1",
                "msg": text,
                "rnd": str(int(time.time() * 1000)),
                "roomid": str(int(room_id)),
                "bubble": "0",
                "csrf": self._jct,
                "csrf_token": self._jct,
            }
        ).encode("utf-8")

        try:
            payload = self._post_json(
                SEND_URL, body, f"https://live.bilibili.com/{int(room_id)}"
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装成 SendError 给界面
            raise SendError(f"网络错误：{exc}") from exc

        code = payload.get("code")
        if code == 0:
            with self._lock:
                self._last_sent = time.monotonic()
            return payload.get("data") or {}

        message = payload.get("message") or payload.get("msg") or ""
        hint = _CODE_HINTS.get(code)
        raise SendError(hint or message or f"发送失败（code={code}）", code)
