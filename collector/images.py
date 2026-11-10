"""表情图缓存：后台下载、解码、缩放，主线程取用。

QTextBrowser 不会自己去请求 http 图片，``<img src="https://...">`` 只会画出裂图。
所以这里的做法是：后台线程把图下下来，主线程用 ``QTextDocument.addResource()``
把 QImage 挂到**原始 URL** 上，再插那段 HTML，图片就能显示。

同一个 URL 只下一次：内存里留一份（LRU 上限 ``capacity``），磁盘上也留一份，
下次开播直接用，不必再打网络。表情图都是几十像素的小图，单张约 5–20 KB；
弹幕里的表情种类有限、重复率高，缓存命中率好（头像是一人一图，不做）。
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

# 不带 UA 会被 hdslb 的 CDN 直接回 403
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ImageLoader:
    """按 URL 加载小图。主线程只读结果，不在主线程做任何网络或解码。"""

    def __init__(
        self,
        cache_dir: str,
        *,
        max_px: int = 40,
        workers: int = 4,
        capacity: int = 1500,
        timeout: float = 6.0,
    ) -> None:
        self.cache_dir = cache_dir
        self.max_px = max_px
        self.timeout = timeout
        self._capacity = capacity
        self._lock = threading.Lock()
        # URL -> QImage（成功）或 None（失败）。None 也占位，避免反复重试。
        self._results: OrderedDict[str, QImage | None] = OrderedDict()
        self._loading: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            self.cache_dir = ""

    # ----- 主线程 -----

    def want(self, url: str | None) -> None:
        """登记一个待显示图片；已加载过或正在加载则什么都不做。"""
        if not url:
            return
        with self._lock:
            if url in self._results or url in self._loading:
                return
            self._loading.add(url)
        self._pool.submit(self._load, url)

    def settled(self, url: str | None) -> bool:
        """结果是否已定（拿到图，或确定失败）。排队中返回 False。"""
        if not url:
            return True
        with self._lock:
            return url in self._results

    def image(self, url: str | None) -> QImage | None:
        if not url:
            return None
        with self._lock:
            if url not in self._results:
                return None
            image = self._results[url]
            if image is not None:
                self._results.move_to_end(url)
            return image

    def stop(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ----- 工作线程 -----

    def _load(self, url: str) -> None:
        image: QImage | None = None
        try:
            data = self._read_disk(url)
            if data is None:
                data = self._download(url)
                if data:
                    self._write_disk(url, data)
            if data:
                image = self._decode(data)
        except Exception:  # noqa: BLE001 - 显示层不能因为一张图崩掉
            image = None
        finally:
            with self._lock:
                self._loading.discard(url)
                self._results[url] = image
                while len(self._results) > self._capacity:
                    self._results.popitem(last=False)

    def _decode(self, data: bytes) -> QImage | None:
        image = QImage.fromData(data)
        if image.isNull():
            return None
        if max(image.width(), image.height()) > self.max_px:
            image = image.scaled(
                self.max_px,
                self.max_px,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return image

    def _download(self, url: str) -> bytes | None:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com/"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    # ----- 磁盘缓存 -----

    def _cache_path(self, url: str) -> str:
        if not self.cache_dir:
            return ""
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, digest + ".img")

    def _read_disk(self, url: str) -> bytes | None:
        path = self._cache_path(url)
        if not path:
            return None
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            return data or None
        except OSError:
            return None

    def _write_disk(self, url: str, data: bytes) -> None:
        path = self._cache_path(url)
        if not path:
            return
        temp = path + ".part"
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
            os.replace(temp, path)
        except OSError:
            try:
                os.remove(temp)
            except OSError:
                pass
