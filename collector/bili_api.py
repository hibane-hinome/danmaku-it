"""Anonymous Bilibili live HTTP API access (buvid cookies + WBI signing)."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

WBI_CACHE_SECONDS = 30 * 60


class BiliApiError(RuntimeError):
    def __init__(self, code: int, message: str, url: str = "") -> None:
        super().__init__(f"bilibili api code={code} message={message} url={url}")
        self.code = code
        self.message = message
        self.url = url


class BiliApi:
    """Minimal anonymous client: cookie seeding, WBI signing, room + danmu info."""

    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        timeout: float = 10.0,
        cookie_file: str = "",
        sessdata: str = "",
        dede_user_id: str = "",
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._mixin_key: str | None = None
        self._mixin_key_at = 0.0
        self._cookies_ready = False
        self.load_cookie_file(cookie_file)
        # Explicit values win over the cookie file so the GUI can override them.
        if sessdata:
            self._set_cookie("SESSDATA", sessdata)
        if dede_user_id:
            self._set_cookie("DedeUserID", dede_user_id)

    # ---------------- low level ----------------

    def _request(self, url: str, referer: str = "https://live.bilibili.com/") -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
            },
        )
        with self._opener.open(req, timeout=self.timeout) as resp:
            return resp.read()

    def _get_json(self, url: str, referer: str = "https://live.bilibili.com/") -> dict:
        body = self._request(url, referer=referer)
        return json.loads(body.decode("utf-8"))

    def _set_cookie(self, name: str, value: str, domain: str = ".bilibili.com") -> None:
        self._jar.set_cookie(
            http.cookiejar.Cookie(
                0, name, value, None, False, domain, False, False, "/", True,
                False, None, True, None, None, {},
            )
        )

    def cookies(self) -> dict:
        return {c.name: c.value for c in self._jar}

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self._jar)

    def load_cookie_file(self, path: str) -> int:
        """Load a browser cookie export (tab or comma separated) into the jar.

        Returns the number of cookies imported. Lines whose first field is "name"
        are treated as a header row and skipped.
        """
        if not path:
            return 0
        imported = 0
        try:
            with open(path, encoding="utf-8-sig") as handle:
                for line in handle:
                    text = line.rstrip("\r\n")
                    if not text:
                        continue
                    parts = text.split("\t")
                    if len(parts) < 2:
                        parts = text.split(",")
                    if len(parts) < 2:
                        continue
                    name, value = parts[0].strip(), parts[1].strip()
                    if not name or not value or name.lower() == "name":
                        continue
                    self._set_cookie(name, value)
                    imported += 1
        except OSError:
            return 0
        if imported:
            self._cookies_ready = False
        return imported

    # ---------------- auth / wbi ----------------

    def ensure_cookies(self) -> None:
        """Seed buvid3/buvid4 without login; several live endpoints reject requests without them."""
        if self._cookies_ready:
            return
        if "buvid3" not in self.cookies():
            try:
                self._request("https://www.bilibili.com/", referer="https://www.bilibili.com/")
            except Exception:
                pass
        try:
            spi = self._get_json(
                "https://api.bilibili.com/x/frontend/finger/spi",
                referer="https://www.bilibili.com/",
            )
            data = spi.get("data") or {}
            if data.get("b_3"):
                self._set_cookie("buvid3", data["b_3"])
            if data.get("b_4"):
                self._set_cookie("buvid4", data["b_4"])
        except Exception:
            pass
        self._cookies_ready = True

    def _refresh_mixin_key(self) -> str:
        nav = self._get_json(
            "https://api.bilibili.com/x/web-interface/nav",
            referer="https://www.bilibili.com/",
        )
        wbi = ((nav.get("data") or {}).get("wbi_img") or {})
        img_url = wbi.get("img_url") or ""
        sub_url = wbi.get("sub_url") or ""
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise BiliApiError(-1, "wbi keys missing", "https://api.bilibili.com/x/web-interface/nav")
        raw = img_key + sub_key
        self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._mixin_key_at = time.time()
        return self._mixin_key

    def mixin_key(self) -> str:
        if self._mixin_key is None or time.time() - self._mixin_key_at > WBI_CACHE_SECONDS:
            return self._refresh_mixin_key()
        return self._mixin_key

    def sign(self, params: dict) -> str:
        key = self.mixin_key()
        signed = dict(params)
        signed["wts"] = int(time.time())
        filtered = {
            k: "".join(ch for ch in str(v) if ch not in "!'()*")
            for k, v in signed.items()
        }
        query = urllib.parse.urlencode(sorted(filtered.items()))
        filtered["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
        return urllib.parse.urlencode(sorted(filtered.items()))

    # ---------------- endpoints ----------------

    def resolve_room(self, room_id: int) -> dict:
        """Return real_room_id / title / live_status / anchor uid for a short or real room id."""
        self.ensure_cookies()
        query = self.sign({"room_id": room_id, "web_location": "444.8"})
        url = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?" + query
        data = self._get_json(url)
        if data.get("code") != 0:
            raise BiliApiError(data.get("code", -1), data.get("message", ""), url)
        payload = data.get("data") or {}
        room_info = payload.get("room_info") or {}
        anchor = (payload.get("anchor_info") or {}).get("base_info") or {}
        guard_info = payload.get("guard_info") or {}
        watched_show = payload.get("watched_show") or {}
        return {
            "input_room_id": room_id,
            "real_room_id": int(room_info.get("room_id") or room_id),
            "title": room_info.get("title") or "",
            "uname": anchor.get("uname") or "",
            "live_status": int(room_info.get("live_status") or 0),
            "anchor_uid": int(room_info.get("uid") or 0),
            "online": int(room_info.get("online") or 0),
            "watched": int(watched_show.get("num") or 0),
            "guard_count": int(guard_info.get("count") or 0),
        }

    def get_danmu_info(self, room_id: int) -> tuple[str, list[dict]]:
        """Return (token, host_list). The token is only valid for this exact room_id."""
        self.ensure_cookies()
        query = self.sign({"id": room_id, "type": 0, "web_location": "444.8"})
        url = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?" + query
        data = self._get_json(url)
        if data.get("code") != 0:
            raise BiliApiError(data.get("code", -1), data.get("message", ""), url)
        payload = data.get("data") or {}
        token = payload.get("token") or ""
        host_list = payload.get("host_list") or []
        if not token or not host_list:
            raise BiliApiError(-1, "danmu info empty", url)
        return token, host_list

    def websocket_urls(self, host_list: list[dict]) -> list[str]:
        urls: list[str] = []
        for host in host_list:
            name = host.get("host")
            port = host.get("wss_port")
            if name and port:
                urls.append(f"wss://{name}:{port}/sub")
        # Classic fallback endpoint.
        fallback = "wss://broadcastlv.chat.bilibili.com:443/sub"
        if fallback not in urls:
            urls.append(fallback)
        return urls
