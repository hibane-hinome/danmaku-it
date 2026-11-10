"""账号目录：一个 cookie 文件就是一个账号。

弹幕机不再只认单个 ``cookie_file``，而是把一个目录里的 cookie 文件全扫一遍，
合法的（含 ``SESSDATA``）就成为一个可选账号。这样切号不用改配置、不用重启，
把 csv 丢进目录即可。

文件放哪、叫什么名字都不重要，只要求能解析出 cookie；文件名（去掉扩展名）
当作默认称呼，联网验证成功后再换成 B 站昵称和头像。

昵称/头像写进 ``cache/accounts.json``，键是 SESSDATA 的摘要而不是明文，
免得把凭证又抄一份到缓存里。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from .sender import load_cookies

# 只扫 csv：目录里往往还躺着 requirements.txt 之类的文本，别去猜它们是不是 cookie
COOKIE_EXTS = (".csv",)
# 昵称缓存的有效期：过期后重新问一次 B 站，昵称改了也能跟上
META_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class Account:
    """一个 cookie 文件对应一个账号。"""

    path: str
    label: str
    cookies: dict[str, str] = field(default_factory=dict)
    uname: str = ""
    face: str = ""
    uid: int = 0
    # 非空表示这个文件不能当账号用（缺 SESSDATA、读不了等）
    error: str = ""

    @property
    def sessdata(self) -> str:
        return self.cookies.get("SESSDATA", "")

    @property
    def jct(self) -> str:
        return self.cookies.get("bili_jct", "")

    @property
    def has_login(self) -> bool:
        """有 SESSDATA 才算能登录的账号；只有 bili_jct 发不了弹幕。"""
        return bool(self.sessdata)

    @property
    def display(self) -> str:
        """下拉里显示的名字：优先 B 站昵称，没验证过就用文件名。"""
        return self.uname or self.label

    @property
    def cache_key(self) -> str:
        return hashlib.sha1(self.sessdata.encode("utf-8")).hexdigest()[:16]

    def meta_is_fresh(self, meta: dict) -> bool:
        record = meta.get(self.cache_key) or {}
        if not record.get("uname"):
            return False
        checked = float(record.get("checked") or 0)
        return (time.time() - checked) < META_TTL_SECONDS

    def apply_meta(self, meta: dict) -> None:
        record = meta.get(self.cache_key) or {}
        self.uname = str(record.get("uname") or "")
        self.face = str(record.get("face") or "")
        self.uid = int(record.get("uid") or 0)

    def store_meta(self, meta: dict) -> None:
        if not self.uname:
            return
        meta[self.cache_key] = {
            "uname": self.uname,
            "face": self.face,
            "uid": self.uid,
            "checked": int(time.time()),
        }


def account_meta_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "accounts.json")


def load_meta(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_meta(path: str, meta: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = path + ".part"
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except OSError:
        try:
            os.remove(temp)
        except OSError:
            pass


def scan_accounts(directory: str, meta: dict | None = None) -> list[Account]:
    """扫目录里的 cookie 文件（不递归），按文件名排序返回。

    缺 SESSDATA 的文件也会返回，但 ``error`` 非空——界面据此提示「哪个文件
    没被当成账号」，比默默跳过好排查。
    """
    if not directory or not os.path.isdir(directory):
        return []

    meta = meta or {}
    accounts: list[Account] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []

    for name in names:
        if name.startswith(".") or not name.lower().endswith(COOKIE_EXTS):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue

        label = os.path.splitext(name)[0]
        account = Account(path=path, label=label, cookies=load_cookies(path))
        if not account.cookies:
            account.error = "读不出 cookie（空文件或格式不对）"
        elif not account.has_login:
            account.error = "没有 SESSDATA，发不了弹幕"
        account.apply_meta(meta)
        accounts.append(account)

    # 能用的排前面，同一个文件不重复出现
    accounts.sort(key=lambda item: (bool(item.error), item.label.lower()))
    return accounts


def usable_accounts(accounts: list[Account]) -> list[Account]:
    return [item for item in accounts if not item.error]
