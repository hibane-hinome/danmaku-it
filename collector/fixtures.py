"""0 号直播间的回放素材：直接使用采集库里存下来的原始 payload。

采集时每条 SC / 礼物的原始 JSON 都整条存进 ``raw_json`` 列（见 storage.py），
这里把真实记录读出来，按原顺序交给界面，用来核对排版、译文显示和金额配色。
只读，不联网，不写库。

SC 的三种到达方式都会覆盖到：

``BODY``        本体自带 ``message_trans``（B 站现在的常态，占大多数）
``JPN_AFTER``   本体没译文，随后一条 ``SUPER_CHAT_MESSAGE_JPN`` 补上
``JPN_BEFORE``  译文比本体先到，需要按 sc_id 缓冲后合并
"""

from __future__ import annotations

import json
import os
import sqlite3

BODY = "body"
JPN_AFTER = "jpn_after"
JPN_BEFORE = "jpn_before"
NONE = "none"

# 弹幕时间轴上一段超过 30 分钟的空档，基本可以认定是换了一场，回放时到此截断
SESSION_GAP = 1800
# 一段至少要这么多条弹幕才拿来当回放素材，避免挑到末尾那种几十秒的零星短场
MIN_DANMAKU_BLOCK = 120

MODE_LABEL = {
    BODY: "本体带译文",
    JPN_AFTER: "译文后到",
    JPN_BEFORE: "译文先到",
    NONE: "无译文",
}


def _connect(db_path: str) -> sqlite3.Connection | None:
    """只读打开采集库；路径不存在或不是本项目的库时返回 None。"""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        conn.execute("SELECT 1 FROM sc_message LIMIT 1")
        conn.execute("SELECT 1 FROM danmaku LIMIT 1")
        return conn
    except sqlite3.Error:
        try:
            conn.close()
        except Exception:
            pass
        return None


def _spread(items: list, limit: int) -> list:
    """在整段时间里均匀取样，避免只拿到最早的一段。"""
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    step = len(items) / float(limit)
    return [items[int(i * step)] for i in range(limit)]


def _parse_sc_row(row: sqlite3.Row) -> dict | None:
    try:
        raw = json.loads(row["raw_json"] or "")
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    trans = (raw.get("message_trans") or "").strip()
    return {"sc_id": str(row["sc_id"]), "raw": raw, "trans": trans}


def _interleave(a: list, b: list) -> list:
    out: list = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def load_replay(
    db_path: str,
    room_id: int | None = None,
    *,
    sc_limit: int = 24,
    danmaku_limit: int = 600,
    event_limit: int = 40,
) -> dict | None:
    """从采集库拼一份回放素材；没有可用数据时返回 None。"""
    conn = _connect(db_path)
    if conn is None:
        return None

    try:
        where = "raw_json IS NOT NULL AND raw_json <> ''"
        args: list = []
        if room_id:
            where += " AND room_id = ?"
            args.append(int(room_id))

        sc_rows = conn.execute(
            f"SELECT sc_id, raw_json FROM sc_message WHERE {where} ORDER BY start_time",
            args,
        ).fetchall()
        parsed = [item for item in (_parse_sc_row(r) for r in sc_rows) if item]
        translated = [item for item in parsed if item["trans"]]
        plain = [item for item in parsed if not item["trans"]]

        half = max(1, sc_limit // 2) if translated else 0
        picked_tr = _spread(translated, min(half, len(translated)))
        picked_pl = _spread(plain, max(0, sc_limit - len(picked_tr)))

        # 两种都展示：一半有译文、一半没有，方便看出界面差异。
        # 有译文的再轮换三种到达方式，确保每条路径都被跑到。
        sc_items: list[dict] = []
        modes = (BODY, JPN_AFTER, JPN_BEFORE)
        translated_seen = 0
        for item in _interleave(picked_tr, picked_pl):
            entry = dict(item)
            if item["trans"]:
                entry["mode"] = modes[translated_seen % len(modes)]
                translated_seen += 1
            else:
                entry["mode"] = NONE
            sc_items.append(entry)

        dm_where = ""
        dm_args: list = []
        if room_id:
            dm_where = " WHERE room_id = ?"
            dm_args.append(int(room_id))
        # 多取一些，再从后往前找最后一场的起点，避免回放里时间突然跳几个小时
        dm_rows = conn.execute(
            "SELECT room_id, uid, uname, text, event_time, received_at, medal_name,"
            " medal_level, guard_level, is_admin, is_emoticon"
            f" FROM danmaku{dm_where} ORDER BY event_time DESC LIMIT ?",
            dm_args + [max(1, danmaku_limit) * 3],
        ).fetchall()
        ordered = [dict(r) for r in reversed(dm_rows)]
        # 按 30 分钟空档切段，取最近一段够长的（末尾常有零星的短场，跳过）
        cuts = [
            i
            for i in range(1, len(ordered))
            if ordered[i]["event_time"] - ordered[i - 1]["event_time"] > SESSION_GAP
        ]
        bounds = [0] + cuts + [len(ordered)]
        blocks = [ordered[bounds[i]: bounds[i + 1]] for i in range(len(bounds) - 1)]
        enough = [b for b in blocks if len(b) >= min(MIN_DANMAKU_BLOCK, danmaku_limit)]
        chosen = (enough or blocks)[-1] if blocks else []
        danmaku = sorted(
            chosen[-danmaku_limit:], key=lambda r: r["event_time"] or 0
        )

        ev_rows = conn.execute(
            "SELECT event_type, uid, uname, gift_name, gift_num, price, event_time"
            " FROM room_event WHERE event_type IN ('gift', 'guard', 'enter')"
            " ORDER BY seq DESC LIMIT ?",
            [max(1, event_limit)],
        ).fetchall()
        events = [
            {"event_type": r["event_type"], "row": dict(r)} for r in reversed(ev_rows)
        ]
    finally:
        conn.close()

    if not (sc_items or danmaku or events):
        return None

    mode_counts = {m: 0 for m in MODE_LABEL}
    for item in sc_items:
        mode_counts[item["mode"]] += 1

    notes = [
        f"0 号直播间：真实记录回放 · SC {len(sc_items)} 条（带译文 {len(picked_tr)} / "
        f"无译文 {mode_counts[NONE]}）· 弹幕 {len(danmaku)} 条 · 礼物进场 {len(events)} 条",
        f"译文路径：{MODE_LABEL[BODY]} {mode_counts[BODY]} / {MODE_LABEL[JPN_AFTER]} "
        f"{mode_counts[JPN_AFTER]} / {MODE_LABEL[JPN_BEFORE]} {mode_counts[JPN_BEFORE]}"
        f" · 来源 {os.path.basename(db_path)} · 只回放不写库",
    ]

    return {
        "title": "真实数据回放 · 0 号直播间",
        "notes": notes,
        "sc": sc_items,
        "danmaku": danmaku,
        "events": events,
        "source": db_path,
    }
