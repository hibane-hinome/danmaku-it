"""SQLite persistence and NDJSON/CSV export for collected super chats."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sc_message (
    sc_id               TEXT PRIMARY KEY,
    room_id             INTEGER NOT NULL,
    uid                 INTEGER,
    uname               TEXT,
    price               INTEGER,
    message             TEXT,
    start_time          INTEGER NOT NULL,
    end_time            INTEGER,
    received_at         INTEGER NOT NULL,
    guard_level         INTEGER,
    medal_name          TEXT,
    medal_level         INTEGER,
    medal_anchor_roomid INTEGER,
    medal_color         INTEGER,
    raw_json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sc_room_start ON sc_message(room_id, start_time);
CREATE INDEX IF NOT EXISTS idx_sc_start ON sc_message(start_time);
CREATE INDEX IF NOT EXISTS idx_sc_received ON sc_message(received_at);

CREATE TABLE IF NOT EXISTS sc_deletion (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    sc_id       TEXT,
    received_at INTEGER NOT NULL,
    raw_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_del_sc ON sc_deletion(room_id, sc_id);

CREATE TABLE IF NOT EXISTS collect_room (
    room_id    INTEGER PRIMARY KEY,
    title      TEXT,
    uname      TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS room_event (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    event_key   TEXT NOT NULL,
    uid         INTEGER,
    uname       TEXT,
    gift_name   TEXT,
    gift_num    INTEGER,
    price       INTEGER,
    coin_type   TEXT,
    event_time  INTEGER,
    received_at INTEGER NOT NULL,
    raw_json    TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_event_key ON room_event(room_id, event_type, event_key);
CREATE INDEX IF NOT EXISTS idx_event_time ON room_event(room_id, event_time);

CREATE TABLE IF NOT EXISTS danmaku (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER NOT NULL,
    uid         INTEGER,
    uname       TEXT,
    text        TEXT NOT NULL,
    event_time  INTEGER,
    received_at INTEGER NOT NULL,
    medal_name  TEXT,
    medal_level INTEGER,
    guard_level INTEGER,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    is_emoticon INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_danmaku_time ON danmaku(room_id, event_time);
"""

INSERT_DANMAKU = """
INSERT INTO danmaku (
    room_id, uid, uname, text, event_time, received_at,
    medal_name, medal_level, guard_level, is_admin, is_emoticon
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_SC = """
INSERT INTO sc_message (
    sc_id, room_id, uid, uname, price, message, message_trans, start_time, end_time,
    received_at, guard_level, medal_name, medal_level, medal_anchor_roomid,
    medal_color, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(sc_id) DO NOTHING
"""

EXPORT_COLUMNS = [
    "sc_id", "room_id", "uid", "uname", "price", "message", "message_trans",
    "start_time", "start_time_iso", "received_at", "received_at_iso",
    "start_offset_seconds", "guard_level", "medal_name", "medal_level",
]


def _iso(ts_seconds: float) -> str:
    return datetime.fromtimestamp(ts_seconds).astimezone().isoformat(timespec="seconds")


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_sc(room_id: int, data: dict, received_at_ms: int) -> dict | None:
    """Flatten one SUPER_CHAT_MESSAGE payload into a row dict."""
    sc_id = data.get("id")
    if sc_id is None:
        return None
    user_info = data.get("user_info") or {}
    medal_info = data.get("medal_info") or {}
    start_time = _to_int(data.get("start_time"))
    if start_time is None:
        start_time = received_at_ms // 1000
    return {
        "sc_id": str(sc_id),
        "room_id": room_id,
        "uid": _to_int(data.get("uid")),
        "uname": user_info.get("uname"),
        "price": _to_int(data.get("price")),
        "message": data.get("message"),
        # 少数情况下 SUPER_CHAT_MESSAGE 本体就带译文（多数房间靠随后的 _JPN 消息补）
        "message_trans": (data.get("message_trans") or "").strip() or None,
        "start_time": start_time,
        "end_time": _to_int(data.get("end_time")),
        "received_at": received_at_ms,
        "guard_level": _to_int(user_info.get("guard_level")),
        "medal_name": medal_info.get("medal_name"),
        "medal_level": _to_int(medal_info.get("medal_level")),
        "medal_anchor_roomid": _to_int(medal_info.get("anchor_roomid")),
        "medal_color": _to_int(medal_info.get("medal_color")),
        "raw_json": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    }


def extract_event(room_id: int, event_type: str, data: dict, received_at_ms: int) -> dict | None:
    """Flatten a SEND_GIFT / INTERACT_WORD payload into a room_event row dict."""
    if event_type == "enter":
        uid = _to_int(data.get("uid"))
        if uid is None:
            return None
        trigger = _to_int(data.get("trigger_time")) or 0
        event_time = _to_int(data.get("timestamp"))
        if event_time is None:
            event_time = (trigger // 1000) if trigger > 10**11 else (trigger or received_at_ms // 1000)
        return {
            "room_id": room_id,
            "event_type": "enter",
            "event_key": f"{uid}:{trigger or event_time}:{data.get('msg_type')}",
            "uid": uid,
            "uname": data.get("uname"),
            "gift_name": None,
            "gift_num": None,
            "price": None,
            "coin_type": None,
            "event_time": event_time,
            "received_at": received_at_ms,
            "raw_json": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        }

    if event_type == "guard":
        uid = _to_int(data.get("uid"))
        guard_level = _to_int(data.get("guard_level")) or 0
        start = _to_int(data.get("start_time")) or _to_int(data.get("timestamp")) or received_at_ms // 1000
        return {
            "room_id": room_id,
            "event_type": "guard",
            "event_key": f"{uid}:{start}:{guard_level}",
            "uid": uid,
            "uname": data.get("username") or data.get("uname"),
            "gift_name": data.get("gift_name") or {1: "总督", 2: "提督", 3: "舰长"}.get(guard_level, "舰长"),
            "gift_num": _to_int(data.get("num")),
            "price": _to_int(data.get("price")),
            "coin_type": "gold",
            "event_time": _to_int(data.get("timestamp")) or start,
            "received_at": received_at_ms,
            "raw_json": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        }

    uid = _to_int(data.get("uid"))
    event_time = _to_int(data.get("timestamp")) or received_at_ms // 1000
    gift_name = data.get("giftName") or data.get("gift_name")
    gift_id = data.get("giftId") or data.get("gift_id")
    key = data.get("tid") or f"{uid}:{event_time}:{gift_id or gift_name}"
    return {
        "room_id": room_id,
        "event_type": "gift",
        "event_key": str(key),
        "uid": uid,
        "uname": data.get("uname"),
        "gift_name": gift_name,
        "gift_num": _to_int(data.get("num")),
        "price": _to_int(data.get("price")),
        "coin_type": data.get("coin_type"),
        "event_time": event_time,
        "received_at": received_at_ms,
        "raw_json": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    }


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self._danmaku_buffer: list[tuple] = []

    def _migrate(self) -> None:
        """Add columns introduced after the first schema version."""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(sc_message)")}
        if "message_trans" not in columns:
            self._conn.execute("ALTER TABLE sc_message ADD COLUMN message_trans TEXT")
        danmaku_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(danmaku)")}
        if "is_emoticon" not in danmaku_columns:
            self._conn.execute(
                "ALTER TABLE danmaku ADD COLUMN is_emoticon INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        try:
            self.flush_danmaku()
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass

    # ---------------- writes ----------------

    def insert_danmaku(self, row: dict) -> None:
        """Buffer one danmaku; flushed in batches so hot rooms stay cheap."""
        self._danmaku_buffer.append((
            row.get("room_id"), row.get("uid"), row.get("uname"), row.get("text"),
            row.get("event_time"), row.get("received_at"),
            row.get("medal_name"), row.get("medal_level"),
            row.get("guard_level"), row.get("is_admin") or 0,
            row.get("is_emoticon") or 0,
        ))
        if len(self._danmaku_buffer) >= 200:
            self.flush_danmaku()

    def flush_danmaku(self) -> None:
        if not self._danmaku_buffer:
            return
        batch, self._danmaku_buffer = self._danmaku_buffer, []
        try:
            with self._conn:
                self._conn.executemany(INSERT_DANMAKU, batch)
        except sqlite3.Error:
            self._danmaku_buffer = batch + self._danmaku_buffer

    def insert_sc(self, row: dict) -> bool:
        """Insert one SC. Returns False when the sc_id already exists (dedupe by primary key)."""
        cur = self._conn.execute(
            INSERT_SC,
            (
                row["sc_id"], row["room_id"], row["uid"], row["uname"], row["price"],
                row["message"], row.get("message_trans"), row["start_time"], row["end_time"],
                row["received_at"], row["guard_level"], row["medal_name"], row["medal_level"],
                row["medal_anchor_roomid"], row["medal_color"], row["raw_json"],
            ),
        )
        inserted = cur.rowcount > 0
        self._conn.commit()
        return inserted

    def insert_deletion(self, room_id: int, data: dict, received_at_ms: int) -> int:
        """Record a SUPER_CHAT_MESSAGE_DELETE event, one row per deleted sc id."""
        ids = data.get("ids")
        if ids is None and data.get("id") is not None:
            ids = [data.get("id")]
        if not isinstance(ids, list):
            ids = []
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if not ids:
            self._conn.execute(
                "INSERT INTO sc_deletion (room_id, sc_id, received_at, raw_json) VALUES (?, NULL, ?, ?)",
                (room_id, received_at_ms, raw),
            )
            self._conn.commit()
            return 1
        self._conn.executemany(
            "INSERT INTO sc_deletion (room_id, sc_id, received_at, raw_json) VALUES (?, ?, ?, ?)",
            [(room_id, str(sc_id), received_at_ms, raw) for sc_id in ids],
        )
        self._conn.commit()
        return len(ids)

    def insert_event(self, row: dict) -> bool:
        """Insert one room event (gift / enter). Dedupe by (room_id, event_type, event_key)."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO room_event (room_id, event_type, event_key, uid, uname, "
            "gift_name, gift_num, price, coin_type, event_time, received_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["room_id"], row["event_type"], row["event_key"], row["uid"], row["uname"],
                row["gift_name"], row["gift_num"], row["price"], row["coin_type"],
                row["event_time"], row["received_at"], row["raw_json"],
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            self._conn.commit()
        return inserted

    def update_sc_translation(self, sc_id: str, text: str) -> bool:
        """Attach a SUPER_CHAT_MESSAGE_JPN translation to an existing SC row."""
        cur = self._conn.execute(
            "UPDATE sc_message SET message_trans = ? WHERE sc_id = ?", (text, str(sc_id))
        )
        self._conn.commit()
        return cur.rowcount > 0

    def upsert_room(self, room_id: int, title: str, uname: str) -> None:
        self._conn.execute(
            "INSERT INTO collect_room (room_id, title, uname, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(room_id) DO UPDATE SET title=excluded.title, uname=excluded.uname, "
            "updated_at=excluded.updated_at",
            (room_id, title, uname, int(time.time() * 1000)),
        )
        self._conn.commit()

    # ---------------- reads ----------------

    def iter_rows(
        self,
        room_id: int | None = None,
        since: int | None = None,
        until: int | None = None,
        with_raw: bool = False,
    ) -> Iterator[dict]:
        sql = [
            "SELECT sc_id, room_id, uid, uname, price, message, message_trans, start_time, end_time,",
            "received_at, guard_level, medal_name, medal_level, medal_anchor_roomid",
            ", raw_json FROM sc_message WHERE 1=1",
        ]
        params: list = []
        if room_id is not None:
            sql.append(" AND room_id = ?")
            params.append(room_id)
        if since is not None:
            sql.append(" AND start_time >= ?")
            params.append(since)
        if until is not None:
            sql.append(" AND start_time <= ?")
            params.append(until)
        sql.append(" ORDER BY start_time ASC, received_at ASC")
        for row in self._conn.execute("".join(sql), params):
            item = dict(row)
            if not with_raw:
                item.pop("raw_json", None)
            yield item

    def iter_deletions(self) -> Iterator[dict]:
        for row in self._conn.execute("SELECT * FROM sc_deletion ORDER BY received_at ASC"):
            yield dict(row)

    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM sc_message").fetchone()[0]
        deletions = self._conn.execute("SELECT COUNT(*) FROM sc_deletion").fetchone()[0]
        rooms = [
            dict(row)
            for row in self._conn.execute(
                "SELECT room_id, COUNT(*) AS sc_count, MIN(start_time) AS first_start, "
                "MAX(start_time) AS last_start FROM sc_message GROUP BY room_id"
            )
        ]
        return {"sc_total": total, "deletion_total": deletions, "rooms": rooms}


# ---------------- export ----------------


def _row_to_export(row: dict) -> dict:
    start_time = row["start_time"]
    received_at = row["received_at"]
    return {
        "sc_id": row["sc_id"],
        "room_id": row["room_id"],
        "uid": row["uid"],
        "uname": row["uname"],
        "price": row["price"],
        "message": row["message"],
        "message_trans": row.get("message_trans"),
        "start_time": start_time,
        "start_time_iso": _iso(start_time),
        "received_at": received_at,
        "received_at_iso": _iso(received_at / 1000),
        "start_offset_seconds": round(received_at / 1000 - start_time, 3),
        "guard_level": row["guard_level"],
        "medal_name": row["medal_name"],
        "medal_level": row["medal_level"],
    }


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def export_ndjson(rows: Iterable[dict], path: str, with_raw: bool = False) -> int:
    _ensure_parent(path)
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            item = _row_to_export(row)
            if with_raw:
                raw = row.get("raw_json")
                item["raw"] = json.loads(raw) if raw else None
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1
    return count


def export_csv(rows: Iterable[dict], path: str) -> int:
    _ensure_parent(path)
    count = 0
    # utf-8-sig keeps Excel happy; the BOM does not affect RAG pipelines.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_to_export(row))
            count += 1
    return count
