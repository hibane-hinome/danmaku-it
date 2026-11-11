"""在弹幕机上直接发弹幕。

匿名连接只能收，发弹幕必须登录。两个 cookie 决定成败：

- ``SESSDATA``   —— 身份凭证
- ``bili_jct``   —— CSRF token，发弹幕时同时作为 ``csrf`` 与 ``csrf_token`` 传

凭证来自账号目录里的某个 cookie 文件（界面挑哪个就用哪个），构造时把
``Account`` 传进来即可。cookie 里没有 ``bili_jct`` 时用 SESSDATA 找 passport
换一次（B 站会随 ``Set-Cookie`` 下发），所以只导出了 SESSDATA 也能发。

除了发弹幕，这个模块还包了「按账号拉表情包 / 可用颜色位置 / 粉丝牌 / 佩戴
粉丝牌」这些发一条弹幕真正需要的东西。

这个模块**不碰 Qt**，界面自己在后台线程里调用，避免网络卡住画面。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from .bili_api import BiliApi

SEND_URL = "https://api.live.bilibili.com/msg/send"
# 直播间点赞。参数要 wbi 签名（wts + w_rid），csrf 传 bili_jct；
# 老的 /xlive/web-ucenter/v2/heartbeat/Click 已经不认了
LIKE_URL = (
    "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3"
)
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
CSRF_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info?csrf="
EMOTICON_URL = (
    "https://api.live.bilibili.com/xlive/web-ucenter/v2/emoticon/GetEmoticons"
    "?platform=pc&room_id={room}"
)
# 匿名领一对 buvid3/buvid4（设备指纹）。**没有它会掉表情包**：实测同一个账号
# 缺 buvid3 时 GetEmoticons 只回「通用表情 + 房间专属」，补上之后才多出
# emoji(pkg_type=3) 和账号自己的装扮表情(pkg_type=5)。cookie 文件是浏览器导出
# 的，不一定带 buvid（HttpOnly 的那个常被扩展漏掉），所以这里自己领一份补进去。
BUVID_URL = "https://api.bilibili.com/x/frontend/finger/spi"
# 当前账号在这个房间**真正**能用的颜色和位置。房间可以限制彩色/顶部弹幕，
# 此时发弹幕接口不报错，只是悄悄按白色滚动下发，所以必须先问这个接口
DM_CONFIG_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/GetDMConfigByGroup?room_id={room}"
# 账号拥有的全部粉丝牌。page_size 上限 10，传大了直接回 -1002002 参数异常
MEDAL_LIST_URL = (
    "https://api.live.bilibili.com/xlive/app-ucenter/v1/user/GetMyMedals"
    "?page={page}&page_size=10"
)
# 佩戴粉丝牌（全局状态，戴哪个所有直播间都显示哪个）
WEAR_MEDAL_URL = "https://api.live.bilibili.com/xlive/web-room/v1/fansMedal/wear"

# 单条弹幕上限，B 站按字符计（不是字节）：实测 80 个汉字或 80 个 ASCII 都能发，
# 81 个回 1003212。房间的 dm_config 里也写着 80。
MAX_LEN = 80
# 同一账号两次发送的最小间隔。实测服务端只要求约 1 秒：间隔 1.0 秒连发 4 条全部
# code=0，压到 0.5 秒才会回 10030「发送弹幕的频率过快」。本地按 1 秒挡一层。
MIN_INTERVAL = 1.0

# 弹幕位置，对应发弹幕接口的 mode 字段
MODE_SCROLL = 1
MODE_BOTTOM = 4
MODE_TOP = 5
MODES = ((MODE_SCROLL, "滚动"), (MODE_BOTTOM, "底部"), (MODE_TOP, "顶部"))
# 默认白色，十进制 RGB
DEFAULT_COLOR = 16777215
# 常用色板，值是十进制（B 站接口就收这个）
COLOR_PRESETS = (
    ("白", 16777215),
    ("红", 16711680),
    ("橙", 16750592),
    ("黄", 16776960),
    ("绿", 65280),
    ("青", 65535),
    ("蓝", 255),
    ("紫", 8388863),
    ("粉", 16741248),
    ("灰", 11184810),
)

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
    10030: "发太快了，B 站要求间隔约 1 秒",
    1003212: "弹幕太长，上限 80 个字符",
}


class SendError(Exception):
    """发送失败。``code`` 是 B 站返回码，网络层错误时为 None。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# 装扮表情的 unique 形如 upower_[<装扮名>_<表情名>]，靠它把一堆同名的
# 「装扮表情」拆成能认出来的分组名；upower_[笑哭] 这种没有装扮名的就退回默认
_UPOWER_RE = re.compile(r"^upower_\[(.+?)_[^\]]*\]$")


def _package_label(pkg: dict, pkg_type: int) -> str:
    """给表情包起个能在分组条上认出来的名字。"""
    name = str(pkg.get("pkg_name") or "表情").strip() or "表情"
    if pkg_type != 5:
        return name
    # 装扮表情的 pkg_name 全都叫「装扮表情」，用第一枚表情的 unique 反推装扮名
    for raw in pkg.get("emoticons") or []:
        match = _UPOWER_RE.match(str(raw.get("emoticon_unique") or "").strip())
        if match:
            return match.group(1)
    return name


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
    """用某个账号的 cookie 往指定直播间发弹幕。线程安全，可反复调用。"""

    def __init__(self, config: dict, account=None) -> None:
        self._ua = config.get("user_agent") or DEFAULT_UA
        self._cookies: dict[str, str] = {}
        self._jct = ""
        self._buvid_tried = False
        self._lock = threading.Lock()
        self._last_sent = 0.0
        self.uname = ""
        self.face = ""
        self.uid = 0
        # 已经佩戴的牌子，避免每条弹幕都去 wear 一次
        self.worn_medal = 0
        # wbi 签名（点赞要用）借 BiliApi 的实现，实例缓存着别每次拉 key
        self._wbi_api: BiliApi | None = None
        if account is not None:
            self._cookies = dict(account.cookies)
            self.uname = account.uname
            self.face = account.face
            self.uid = account.uid
            self._jct = self._cookies.get("bili_jct") or ""
        else:
            self._load(config)

    # ----- 凭证 -----

    def _load(self, config: dict) -> None:
        path = config.get("cookie_file") or ""
        if path and not os.path.isabs(path):
            # config.py 只帮忙解析 database 与 log_file，path 类字段要自己补
            path = os.path.normpath(os.path.join(config.get("config_dir") or ".", path))
        self._cookies = load_cookies(path)
        self._jct = self._cookies.get("bili_jct") or ""
        self._wbi_api = None

    @property
    def has_login(self) -> bool:
        return bool(self._cookies.get("SESSDATA"))

    @property
    def cache_key(self) -> str:
        """按账号分缓存用的指纹：SESSDATA 摘要，不落明文。

        表情包是"账号能用的那一份"，不同账号差别很大（大号有整套装扮表情），
        只按房间缓存会把小号的列表喂给大号。
        """
        sess = self._cookies.get("SESSDATA") or ""
        if not sess:
            return "anon"
        return hashlib.sha1(sess.encode("utf-8")).hexdigest()[:10]

    @property
    def cookie_source(self) -> str:
        return "cookie 文件" if self._cookies else "未配置"

    def cookies(self) -> dict:
        """这份账号 cookie 的副本，用来把采集的连接也做成登录态。"""
        items = dict(self._cookies)
        if self._jct:
            items["bili_jct"] = self._jct
        return items

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

    def _ensure_buvid(self) -> None:
        """缺 buvid3 就领一对补上；只试一次，失败也不重试。

        见 ``BUVID_URL`` 的注释：没有它，GetEmoticons 会少给 emoji 和账号
        自己的装扮表情。顺带发弹幕时也带上，行为更接近真实浏览器。
        """
        if self._buvid_tried:
            return
        self._buvid_tried = True
        if self._cookies.get("buvid3"):
            return
        try:
            payload = self._get_json(BUVID_URL, "https://www.bilibili.com/")
        except Exception:  # noqa: BLE001 - 领不到不影响收弹幕
            return
        data = payload.get("data") or {}
        for key, name in (("buvid3", "b_3"), ("buvid4", "b_4")):
            value = str(data.get(name) or "").strip()
            if value:
                self._cookies[key] = value

    def _get_json(self, url: str, referer: str) -> dict:
        # _ensure_buvid 用同一个方法取 spi，靠 _buvid_tried 提前置位来终止递归
        self._ensure_buvid()
        req = urllib.request.Request(url, headers=self._headers(referer))
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _post_json(self, url: str, body: bytes, referer: str) -> dict:
        self._ensure_buvid()
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
        self.face = str(data.get("face") or "")
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

    def send(
        self,
        room_id: int,
        text: str,
        *,
        color: int = DEFAULT_COLOR,
        mode: int = MODE_SCROLL,
        dm_type: int = 0,
    ) -> dict:
        """发一条弹幕。

        ``dm_type=0`` 是普通文本（``text`` 就是看到的字）；
        ``dm_type=1`` 是表情，此时 ``text`` 必须是表情的唯一 ID
        （``room_42062_121079`` 这种），发文字名会回 10203 表情发送失败。
        """
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

        mode = mode if mode in (MODE_SCROLL, MODE_BOTTOM, MODE_TOP) else MODE_SCROLL
        fields = {
            "color": str(max(0, min(int(color), 16777215))),
            "fontsize": "25",
            "mode": str(mode),
            "msg": text,
            "rnd": str(int(time.time() * 1000)),
            "roomid": str(int(room_id)),
            "bubble": "0",
            "csrf": self._jct,
            "csrf_token": self._jct,
        }
        if dm_type:
            fields["dm_type"] = str(int(dm_type))
            fields["room_type"] = "0"
        body = urllib.parse.urlencode(fields).encode("utf-8")

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

    # ----- 点赞 -----

    def _wbi(self) -> BiliApi:
        """点赞接口要 wbi 签名，key 的取法和主站一致，直接借 BiliApi 那套。"""
        if self._wbi_api is None:
            self._wbi_api = BiliApi(cookies=self.cookies())
        return self._wbi_api

    def like(self, room_id: int, anchor_id: int = 0, count: int = 1) -> dict:
        """给直播间点赞，``count`` 是一次上报的个数（1–10）。

        参数走 query（wts + w_rid 签名），csrf 传 bili_jct；body 是空的。
        B 站对这个接口有每日上限，超了会回非 0 码，原样抛给界面显示。
        """
        if not room_id:
            raise SendError("还没连上直播间，先点「连接」或填房间号")
        if not self.has_login:
            raise SendError("未配置 cookie，无法点赞")
        if not self._jct and not self.refresh_csrf():
            raise SendError("缺少 CSRF token，请重新导出 cookie（需要 bili_jct）")
        if not self.uid:
            ok, info = self.verify()
            if not ok:
                raise SendError(info)

        params = {
            "click_time": max(1, min(int(count), 10)),
            "room_id": int(room_id),
            "uid": int(self.uid or 0),
            "anchor_id": int(anchor_id or 0),
            "web_location": "444.8",
            "csrf": self._jct,
        }
        try:
            query = self._wbi().sign(params)
            payload = self._post_json(
                f"{LIKE_URL}?{query}",
                b"",
                f"https://live.bilibili.com/{int(room_id)}",
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装成 SendError
            raise SendError(f"网络错误：{exc}") from exc

        code = payload.get("code")
        if code == 0:
            return payload.get("data") or {}
        message = payload.get("message") or payload.get("msg") or ""
        raise SendError(message or f"点赞失败（code={code}）", code)

    # ----- 表情 -----

    def fetch_emoticons(self, room_id: int) -> list[dict]:
        """拉这个房间能用的表情包，含账号自己解锁的（装扮 / 房间专属 / emoji）。

        返回 ``[{"name", "type", "items": [{"text", "url", "unique", "msg", "dm_type"}]}]``。
        ``text`` 是显示用的名字，``msg`` 才是发出去的内容：图片表情填唯一 ID，
        文本表情填 ``[dog]`` 这种带方括号的文字（前端自己渲染成图）。

        接口按 ``perm`` 标「有没有解锁」，``perm <= 0`` 的发出去服务端会拦，直接过滤。

        **必须带 buvid3**，否则接口只回通用 + 房间表情，账号自己的装扮表情
        （``pkg_type=5``）和 emoji 包（``pkg_type=3``）整套都不给，见 ``BUVID_URL``。
        """
        url = EMOTICON_URL.format(room=int(room_id or 0))
        try:
            payload = self._get_json(url, f"https://live.bilibili.com/{int(room_id or 0)}")
        except Exception as exc:  # noqa: BLE001 - 界面只关心成不成
            raise SendError(f"拉表情失败：{exc}") from exc

        if payload.get("code") not in (0, None):
            raise SendError(
                payload.get("message") or f"拉表情失败（code={payload.get('code')}）",
                payload.get("code"),
            )

        packages: list[dict] = []
        for pkg in (payload.get("data") or {}).get("data") or []:
            pkg_type = int(pkg.get("pkg_type") or 0)
            # 官方表情(1)、房间专属/UP主(2)、装扮表情(5) 都是"图片表情"，必须走
            # dm_type=1 且 msg 填唯一 ID；emoji 包(3) 是文本表情，发 [dog] 这种
            # 文字，前端自己会渲染成图。
            as_image = pkg_type in (1, 2, 5)
            items: list[dict] = []
            for raw in pkg.get("emoticons") or []:
                if not raw.get("perm", 1):
                    continue
                text = str(raw.get("emoji") or "").strip()
                src = str(raw.get("url") or "").strip()
                unique = str(raw.get("emoticon_unique") or "").strip()
                if not text or not src:
                    continue
                # hdslb 走 http 也能下，但 https 免掉一次 302
                if src.startswith("http://"):
                    src = "https://" + src[len("http://"):]
                image_send = as_image and bool(unique)
                items.append(
                    {
                        "text": text,
                        "url": src,
                        "unique": unique,
                        "msg": unique if image_send else text,
                        "dm_type": 1 if image_send else 0,
                    }
                )
            if items:
                packages.append(
                    {
                        "name": _package_label(pkg, pkg_type),
                        "type": pkg_type,
                        "items": items,
                    }
                )

        # 顺序：房间专属 / UP主 → 通用 → 装扮 → emoji。
        # 房间专属最常用，emoji 是文字表情，放最后省的误点。
        order = {2: 0, 1: 1, 5: 2, 3: 3}
        packages.sort(key=lambda pkg: order.get(pkg["type"], 9))
        # 同名分组加序号（几套装扮撞名、两个 emoji 包之类），分组条上才分得清
        seen: dict[str, int] = {}
        for pkg in packages:
            name = pkg["name"]
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                pkg["name"] = f"{name} {seen[name]}"
        return packages

    # ----- 粉丝牌 -----

    def fetch_medals(self) -> list[dict]:
        """账号拥有的全部粉丝牌（翻页拿全）。

        返回 ``[{"medal_id", "name", "level", "anchor", "room_id", "wearing"}]``，
        按等级从高到低排。``wearing`` 表示当前全局佩戴的那枚（接口的
        ``status == 1``），界面拿它做默认值。

        发弹幕时佩戴哪个牌子是**全局**状态：戴 `锦依卫` 之后
        在别的房间发弹幕也带这个牌子，所以界面要让人自己选。
        """
        medals: list[dict] = []
        page = 1
        while page <= 20:  # 正常账号不会超过 20 页，防接口异常时死循环
            try:
                payload = self._get_json(
                    MEDAL_LIST_URL.format(page=page), "https://live.bilibili.com/"
                )
            except Exception as exc:  # noqa: BLE001 - 界面只关心成不成
                raise SendError(f"拉粉丝牌失败：{exc}") from exc

            code = payload.get("code")
            if code not in (0, None):
                raise SendError(
                    payload.get("message") or f"拉粉丝牌失败（code={code}）", code
                )

            data = payload.get("data") or {}
            items = data.get("items") or []
            for raw in items:
                try:
                    medal_id = int(raw.get("medal_id") or 0)
                except (TypeError, ValueError):
                    continue
                if not medal_id:
                    continue
                medals.append(
                    {
                        "medal_id": medal_id,
                        "name": str(raw.get("medal_name") or ""),
                        "level": int(raw.get("level") or 0),
                        "anchor": str(raw.get("target_name") or ""),
                        "room_id": int(raw.get("roomid") or 0),
                        # status=1 就是账号现在戴着的这枚
                        "wearing": int(raw.get("status") or 0) == 1,
                    }
                )

            total = int((data.get("page_info") or {}).get("total_page") or 0)
            if not items or page >= total:
                break
            page += 1

        medals.sort(key=lambda item: item["level"], reverse=True)
        return medals

    def wear_medal(self, medal_id: int) -> None:
        """佩戴指定粉丝牌。``medal_id`` 传 0 表示不佩戴。"""
        if not self.has_login:
            raise SendError("未配置 cookie，无法佩戴粉丝牌")
        if not self._jct and not self.refresh_csrf():
            raise SendError("缺少 CSRF token，请重新导出 cookie（需要 bili_jct）")

        body = urllib.parse.urlencode(
            {
                "medal_id": str(int(medal_id)),
                "csrf": self._jct,
                "csrf_token": self._jct,
            }
        ).encode("utf-8")
        try:
            payload = self._post_json(
                WEAR_MEDAL_URL, body, "https://live.bilibili.com/"
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装成 SendError 给界面
            raise SendError(f"切换粉丝牌失败：{exc}") from exc

        code = payload.get("code")
        if code not in (0, None):
            raise SendError(
                payload.get("message") or f"切换粉丝牌失败（code={code}）", code
            )
        self.worn_medal = int(medal_id)

    # ----- 颜色 / 位置可用性 -----

    def fetch_send_config(self, room_id: int) -> dict:
        """问服务端：这个账号在这个房间能用哪些颜色和位置。

        返回 ``{"colors": [...], "modes": [...]}``，每项带 ``status``：
        1 可用，0 不可用。**不可用的选了也白发**——发弹幕接口照样回 code=0，
        但服务端会把颜色改回白色、位置改回滚动。所以界面只该把可用的给人选。
        """
        url = DM_CONFIG_URL.format(room=int(room_id or 0))
        try:
            payload = self._get_json(url, f"https://live.bilibili.com/{int(room_id or 0)}")
        except Exception as exc:  # noqa: BLE001 - 拿不到就退回默认色板
            raise SendError(f"拉弹幕配置失败：{exc}") from exc
        if payload.get("code") not in (0, None):
            raise SendError(
                payload.get("message") or f"拉弹幕配置失败（code={payload.get('code')}）",
                payload.get("code"),
            )

        data = payload.get("data") or {}
        colors: list[dict] = []
        seen: set[int] = set()
        for group in data.get("group") or []:
            for raw in group.get("color") or []:
                try:
                    value = int(raw.get("color"))
                except (TypeError, ValueError):
                    continue
                if value in seen:
                    continue
                seen.add(value)
                colors.append(
                    {
                        "name": str(raw.get("name") or ""),
                        "color": value,
                        "status": 1 if int(raw.get("status") or 0) else 0,
                        "group": str(group.get("name") or ""),
                    }
                )

        modes: list[dict] = []
        for raw in data.get("mode") or []:
            try:
                value = int(raw.get("mode"))
            except (TypeError, ValueError):
                continue
            modes.append(
                {
                    "name": str(raw.get("name") or ""),
                    "mode": value,
                    "status": 1 if int(raw.get("status") or 0) else 0,
                }
            )

        # 颜色排在可用的前面，界面直接取前几个就是能点的
        colors.sort(key=lambda item: 0 if item["status"] else 1)
        return {"colors": colors, "modes": modes}


