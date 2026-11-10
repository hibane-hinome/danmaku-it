"""Per-room websocket collector with heartbeat, backoff reconnect and dedupe-safe writes."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import protocol
from .bili_api import BiliApi, BiliApiError
from .storage import Storage, extract_event, extract_sc

log = logging.getLogger("sc_collector")

AUTH_TIMEOUT_SECONDS = 12.0


def build_danmaku(room_id: int, info: list, received_at: int) -> dict | None:
    """Flatten one DANMU_MSG info array into the fields worth keeping.

    The rich user payload lives in info[0][15]["user"]; info[2] stays as a
    fallback for servers that omit it.
    """
    user_node = {}
    if isinstance(info[0], list) and len(info[0]) > 15 and isinstance(info[0][15], dict):
        user_node = info[0][15].get("user") or {}
    base = user_node.get("base") or {}
    medal = user_node.get("medal") or {}
    profile = info[2] if isinstance(info[2], list) else []

    uid = int(user_node.get("uid") or (profile[0] if profile else 0) or 0)
    uname = base.get("name") or (profile[1] if len(profile) > 1 else "") or ""

    try:
        event_time = int(info[0][4]) // 1000 if isinstance(info[0], list) and len(info[0]) > 4 else 0
    except (TypeError, ValueError):
        event_time = 0

    guard_level = int(medal.get("guard_level") or 0)
    if not guard_level:
        guard_level = int((user_node.get("guard") or {}).get("level") or 0)

    return {
        "room_id": room_id,
        "uid": uid,
        "uname": uname,
        "text": info[1],
        "event_time": event_time,
        "received_at": received_at,
        "medal_name": medal.get("name") or "",
        "medal_level": int(medal.get("level") or 0),
        "guard_level": guard_level,
        "is_admin": 1 if len(profile) > 2 and profile[2] else 0,
        "is_emoticon": 1 if _emoticon_node(info[0]) else 0,
    }


def _emoticon_node(head: object) -> dict:
    """Return the emoticon payload of a danmaku, or {} for plain text.

    Bilibili sends emoticons as a dict at info[0][13]; some builds put it at
    info[0][12] instead, so both are checked. The visible text is the
    emoticon's name (e.g. "敢耍俺").
    """
    if not isinstance(head, list):
        return {}
    for index in (13, 12):
        if len(head) > index and isinstance(head[index], dict):
            node = head[index]
            if node.get("url") or node.get("emoticon_unique"):
                return node
    return {}


class RoomCollector:
    def __init__(
        self,
        room_id: int,
        api: BiliApi,
        storage: Storage,
        stop_event: asyncio.Event,
        heartbeat_seconds: int = 30,
        stale_timeout_seconds: int = 120,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        user_agent: str = "",
        record_enter: bool = True,
        record_events: bool = False,
        event_filter: dict[str, bool] | None = None,
        event_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self.input_room_id = room_id
        self.real_room_id = room_id
        self.title = ""
        self.api = api
        self.storage = storage
        self.stop_event = stop_event
        self.event_sink = event_sink
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_timeout_seconds = stale_timeout_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.user_agent = user_agent
        self.record_enter = record_enter
        self.record_events = record_events
        # Shared with the GUI: which event kinds are on screen, and therefore kept.
        self.event_filter = event_filter

        self.stats = {
            "messages": 0, "sc": 0, "duplicates": 0, "deletions": 0, "sessions": 0,
            "gifts": 0, "enters": 0, "sc_trans": 0,
        }
        self.last_received_at = 0.0
        self.last_danmaku_flush = 0.0
        # SC 译文比 SC 本体先到时会 update 不到行，先按 sc_id 暂存，插入时再补上
        self._pending_sc_trans: dict[str, str] = {}

    # ---------------- helpers ----------------

    def _wants(self, kind: str) -> bool:
        """True when this event kind is on screen and should be kept.

        Without an explicit filter (CLI) every kind is allowed.
        """
        if self.event_filter is None:
            return True
        return bool(self.event_filter.get(kind, False))

    def _emit(self, event: dict) -> None:
        if self.event_sink is None:
            return
        event.setdefault("room_id", self.real_room_id)
        event.setdefault("input_room_id", self.input_room_id)
        event.setdefault("ts", int(time.time() * 1000))
        try:
            self.event_sink(event)
        except Exception:  # noqa: BLE001 - a broken sink must not kill collection
            log.debug("event sink failed", exc_info=True)

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_max_seconds, self.backoff_base_seconds * (2 ** attempt))
        return delay * (1 + random.uniform(-0.2, 0.2))

    async def _resolve(self) -> dict:
        info = await asyncio.to_thread(self.api.resolve_room, self.input_room_id)
        self.real_room_id = info["real_room_id"]
        self.title = info.get("title") or ""
        self.storage.upsert_room(self.real_room_id, info["title"], info["uname"])
        self._emit({"type": "room", "title": self.title, "uname": info.get("uname") or ""})
        self._emit({"type": "room_stats", "guards": info.get("guard_count", 0)})
        return info

    async def _connect(self, urls: list[str], room_id: int, token: str):
        """Try every advertised host until one accepts the auth packet."""
        last_error: Exception | None = None
        for url in urls:
            headers = {
                "User-Agent": self.user_agent,
                "Origin": "https://live.bilibili.com",
                "Referer": "https://live.bilibili.com/",
            }
            cookie = self.api.cookie_header()
            if cookie:
                headers["Cookie"] = cookie
            try:
                ws = await connect(
                    url,
                    additional_headers=headers,
                    open_timeout=10,
                    close_timeout=3,
                    ping_interval=None,  # bilibili has its own 30s heartbeat
                    max_size=None,
                    compression=None,
                )
            except Exception as exc:  # noqa: BLE001 - network layer
                last_error = exc
                log.warning("room=%s connect failed %s (%s)", room_id, url, exc)
                continue

            try:
                # When cookies carry a login, the auth packet must declare the same uid.
                try:
                    uid = int(self.api.cookies().get("DedeUserID") or 0)
                except (AttributeError, TypeError, ValueError):
                    uid = 0
                await ws.send(protocol.pack_auth(room_id, token, uid))
                reply = await self._await_auth(ws)
                if reply is None or reply.get("code") != 0:
                    code = None if reply is None else reply.get("code")
                    raise BiliApiError(int(code if code is not None else -1), "auth rejected", url)
                log.info("room=%s connected via %s", room_id, url)
                return ws
            except Exception as exc:  # noqa: BLE001 - try next host
                last_error = exc
                log.warning("room=%s auth failed at %s (%s)", room_id, url, exc)
                try:
                    await ws.close()
                except Exception:
                    pass

        raise ConnectionError(f"all danmaku hosts failed for room {room_id}: {last_error}")

    async def _await_auth(self, ws) -> dict | None:
        deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return None
            if isinstance(raw, str):
                continue
            for packet in protocol.iter_packets(raw):
                if packet.operation == protocol.OP_AUTH_REPLY:
                    return protocol.parse_auth_reply(packet)
        return None

    # ---------------- one session ----------------

    async def _session(self) -> None:
        info = await self._resolve()
        room_id = self.real_room_id
        if not info["live_status"]:
            log.info("room=%s (%s) is offline, still listening", room_id, info["title"])

        token, host_list = await asyncio.to_thread(self.api.get_danmu_info, room_id)
        urls = self.api.websocket_urls(host_list)
        ws = await self._connect(urls, room_id, token)
        self.stats["sessions"] += 1
        self.last_received_at = time.monotonic()
        self._emit({"type": "status", "state": "connected", "title": self.title})

        hb_task = asyncio.create_task(self._heartbeat(ws, room_id))
        try:
            await self._recv_loop(ws, room_id)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await ws.close()
            except Exception:
                pass

    async def _heartbeat(self, ws, room_id: int) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                await ws.send(protocol.pack_heartbeat())
            except Exception as exc:  # noqa: BLE001 - recv loop will notice the drop
                log.debug("room=%s heartbeat send failed: %s", room_id, exc)
                return

    async def _recv_loop(self, ws, room_id: int) -> None:
        while not self.stop_event.is_set():
            idle = time.monotonic() - self.last_received_at
            timeout = max(1.0, self.stale_timeout_seconds - idle)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                raise ConnectionError(
                    f"no packet for {self.stale_timeout_seconds}s, forcing reconnect"
                )
            except ConnectionClosed as exc:
                raise ConnectionError(f"websocket closed: {exc}") from exc

            self.last_received_at = time.monotonic()
            if isinstance(raw, str):
                continue
            self._handle_frame(raw, room_id)
            if time.monotonic() - self.last_danmaku_flush >= 1.0:
                self.storage.flush_danmaku()
                self.last_danmaku_flush = time.monotonic()

    def _handle_frame(self, raw: bytes, room_id: int) -> None:
        for packet in protocol.iter_packets(raw):
            if packet.operation == protocol.OP_HEARTBEAT_REPLY:
                protocol.parse_popularity(packet)
                continue
            if packet.operation == protocol.OP_AUTH_REPLY:
                continue
            if packet.operation != protocol.OP_MESSAGE:
                continue
            for message in protocol.load_messages(packet):
                self._handle_message(message, room_id)

    def _handle_message(self, message: dict, room_id: int) -> None:
        self.stats["messages"] += 1
        cmd = (message.get("cmd") or "").strip()
        # Commands may carry suffixes such as "DANMU_MSG:4:0:2:2:2:0".
        base_cmd = cmd.split(":", 1)[0]
        data = message.get("data")
        received_at = int(time.time() * 1000)

        if base_cmd == "SUPER_CHAT_MESSAGE" and isinstance(data, dict):
            row = extract_sc(room_id, data, received_at)
            if row is None:
                log.warning("room=%s SC without id, skipped", room_id)
                return
            # 译文可能比本体先到（B 站把翻译拆在 _JPN 消息里），取回来一起插入
            pending = self._pending_sc_trans.pop(str(row["sc_id"]), None)
            if pending and not row.get("message_trans"):
                row["message_trans"] = pending
            if self.storage.insert_sc(row):
                self.stats["sc"] += 1
                self._emit({"type": "sc", "row": row, "duplicate": False})
                log.info(
                    "room=%s SC id=%s price=%s user=%s start=%s msg=%r",
                    room_id, row["sc_id"], row["price"], row["uname"], row["start_time"],
                    (row["message"] or "")[:40],
                )
            else:
                self.stats["duplicates"] += 1
                log.debug("room=%s duplicate SC id=%s ignored", room_id, row["sc_id"])
            return

        if base_cmd == "SUPER_CHAT_MESSAGE_JPN":
            # Translation companion of a previous SUPER_CHAT_MESSAGE, linked by SC id.
            payload = data if isinstance(data, dict) else message
            sc_id = payload.get("id")
            if sc_id is None:
                sc_id = payload.get("sc_id")
            text = (payload.get("message_trans") or payload.get("translation") or "").strip()
            if sc_id is None or not text:
                return
            stored = self.storage.update_sc_translation(sc_id, text)
            if not stored:
                # 对应的 SUPER_CHAT_MESSAGE 还没入库，先暂存，插入时补上
                if len(self._pending_sc_trans) > 200:
                    self._pending_sc_trans.clear()
                self._pending_sc_trans[str(sc_id)] = text
                log.debug("room=%s SC translation arrived before SC body, buffered id=%s", room_id, sc_id)
            self.stats["sc_trans"] += 1
            self._emit({"type": "sc_trans", "sc_id": str(sc_id), "text": text, "stored": stored})
            log.info("room=%s SC translation id=%s text=%r", room_id, sc_id, text[:40])
            return

        if base_cmd == "SUPER_CHAT_MESSAGE_DELETE" and isinstance(data, dict):
            if not self._wants("delete"):
                return
            count = 0
            if self.record_events:
                count = self.storage.insert_deletion(room_id, data, received_at)
            self.stats["deletions"] += count
            self._emit({"type": "delete", "ids": data.get("ids") or ([data["id"]] if data.get("id") else [])})
            log.info("room=%s SC delete event, sc_ids=%s", room_id, data.get("ids") or data.get("id"))
            return

        if base_cmd == "GUARD_BUY":
            if not self._wants("guard"):
                return
            payload = data if isinstance(data, dict) else message
            row = extract_event(room_id, "guard", payload, received_at)
            if row is None:
                return
            if self.record_events:
                self.storage.insert_event(row)
            self.stats["guards"] = self.stats.get("guards", 0) + 1
            self._emit({"type": "guard", "row": row})
            return

        if base_cmd in ("SEND_GIFT", "SEND_GIFT_V2"):
            if not self._wants("gift"):
                return
            payload = data if isinstance(data, dict) else message
            if base_cmd == "SEND_GIFT_V2":
                payload = protocol.parse_send_gift_v2(payload.get("pb") or "")
            if payload is None:
                return
            row = extract_event(room_id, "gift", payload, received_at)
            if row is None:
                return
            if self.record_events:
                self.storage.insert_event(row)
            self.stats["gifts"] += 1
            self._emit({"type": "gift", "row": row})
            return

        if base_cmd == "ONLINE_RANK_COUNT":
            payload = data if isinstance(data, dict) else {}
            online = payload.get("online_count") or payload.get("count") or 0
            watchers = payload.get("watched_count") or 0
            self._emit({"type": "room_stats", "online": int(online), "watchers": int(watchers)})
            return

        if base_cmd == "WATCHED_CHANGE":
            payload = data if isinstance(data, dict) else {}
            self._emit({"type": "room_stats", "watched": int(payload.get("num") or 0)})
            return

        if base_cmd == "INTERACT_WORD_V2":
            if not (self.record_enter and self._wants("enter")):
                return
            payload = data if isinstance(data, dict) else {}
            decoded = protocol.parse_interact_word_v2(payload.get("pb") or "")
            if decoded is None or str(decoded.get("msg_type")) != "1":
                return
            row = extract_event(room_id, "enter", decoded, received_at)
            if row is None:
                return
            if self.record_events:
                self.storage.insert_event(row)
            self.stats["enters"] += 1
            self._emit({"type": "enter", "row": row})
            return

        if base_cmd == "INTERACT_WORD":
            # msg_type 1 = enter the room, 2 = follow, 3 = share.
            if not (self.record_enter and self._wants("enter")):
                return
            payload = data if isinstance(data, dict) else message
            if str(payload.get("msg_type")) != "1":
                return
            row = extract_event(room_id, "enter", payload, received_at)
            if row is None:
                return
            if self.record_events:
                self.storage.insert_event(row)
            self.stats["enters"] += 1
            self._emit({"type": "enter", "row": row})
            return

        if base_cmd == "DANMU_MSG":
            # DANMU_MSG is flat: {"cmd": "DANMU_MSG", "info": [...], ...} with no "data" node.
            payload = data if isinstance(data, dict) else message
            info = payload.get("info") if isinstance(payload, dict) else None
            if not (isinstance(info, list) and len(info) > 2):
                return
            if not (isinstance(info[1], str) and info[1]):
                return
            row = build_danmaku(room_id, info, received_at)
            if row is None:
                return
            self.storage.insert_danmaku(row)
            self._emit({"type": "danmaku", "row": row})

    # ---------------- public loop ----------------

    async def run(self) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the collector alive
                if self.stop_event.is_set():
                    break
                delay = self._backoff(attempt)
                attempt += 1
                self._emit({"type": "status", "state": "reconnecting", "error": str(exc), "delay": round(delay, 1)})
                log.warning(
                    "room=%s session ended: %s; reconnecting in %.1fs (attempt %d)",
                    self.input_room_id, exc, delay, attempt,
                )
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            else:
                # Session returned normally (server closed); short pause before retry.
                attempt = 0
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def report(self, interval: float = 60.0) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self.stop_event.is_set():
                return
            log.info(
                "room=%s stats messages=%d sc=%d dup=%d del=%d sessions=%d",
                self.real_room_id, self.stats["messages"], self.stats["sc"],
                self.stats["duplicates"], self.stats["deletions"], self.stats["sessions"],
            )
