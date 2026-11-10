"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import sys
import tempfile
import time
import zlib

import brotli

from . import protocol
from .bili_api import BiliApi
from .collector import RoomCollector
from .config import apply_cli_overrides, load_config
from .storage import Storage, export_csv, export_ndjson, extract_sc

log = logging.getLogger("sc_collector")


def setup_logging(level: str, log_file: str | None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        parent = os.path.dirname(os.path.abspath(log_file))
        if parent:
            os.makedirs(parent, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


# ---------------- commands ----------------


def cmd_run(args: argparse.Namespace) -> int:
    config = apply_cli_overrides(load_config(args.config), args.room, args.database)
    setup_logging(args.log_level or config["log_level"], config["log_file"])
    log.info("starting collector rooms=%s db=%s", config["rooms"], config["database"])
    return asyncio.run(_run_async(config))


async def _run_async(config: dict) -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(*_args) -> None:
        if not stop_event.is_set():
            log.info("shutdown requested")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, _request_stop)
            except (ValueError, OSError):
                pass

    storage = Storage(config["database"])
    api = BiliApi(
        user_agent=config["user_agent"],
        cookie_file=config.get("cookie_file", ""),
        sessdata=config.get("sessdata", ""),
        dede_user_id=config.get("dede_user_id", ""),
    )
    collectors = [
        RoomCollector(
            room_id=room,
            api=api,
            storage=storage,
            stop_event=stop_event,
            heartbeat_seconds=int(config["heartbeat_seconds"]),
            stale_timeout_seconds=int(config["stale_timeout_seconds"]),
            backoff_base_seconds=float(config["backoff_base_seconds"]),
            backoff_max_seconds=float(config["backoff_max_seconds"]),
            user_agent=config["user_agent"],
            record_enter=bool(config.get("record_enter", True)),
            record_events=bool(config.get("record_events", False)),
            event_filter=config.get("event_filter"),
        )
        for room in config["rooms"]
    ]

    tasks = [asyncio.create_task(c.run(), name=f"room-{c.input_room_id}") for c in collectors]
    tasks += [
        asyncio.create_task(c.report(float(config["stats_interval_seconds"])), name=f"report-{c.input_room_id}")
        for c in collectors
    ]

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_event.set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        total = {key: sum(c.stats[key] for c in collectors) for key in collectors[0].stats} if collectors else {}
        log.info("stopped. stats=%s", total)
        storage.close()
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    setup_logging("WARNING", None)
    api = BiliApi()
    code = 0
    for room in args.room:
        try:
            info = api.resolve_room(room)
            token, hosts = api.get_danmu_info(info["real_room_id"])
            info["token_length"] = len(token)
            info["hosts"] = [f"{h.get('host')}:{h.get('wss_port')}" for h in hosts]
            print(json.dumps(info, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001 - CLI reporting
            print(f"room {room}: ERROR {exc}")
            code = 1
    return code


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(config["database"])
    try:
        rows = storage.iter_rows(room_id=args.room, since=args.since, until=args.until, with_raw=args.with_raw)
        if args.format == "ndjson":
            count = export_ndjson(rows, args.out, with_raw=args.with_raw)
        else:
            count = export_csv(rows, args.out)
        print(f"exported {count} rows -> {args.out}")
    finally:
        storage.close()
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    return run_gui(args.config)


def cmd_stats(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    storage = Storage(config["database"])
    try:
        print(json.dumps(storage.stats(), ensure_ascii=False, indent=2))
    finally:
        storage.close()
    return 0


SAMPLE_SC = {
    "cmd": "SUPER_CHAT_MESSAGE",
    "data": {
        "id": "9007199254740993",
        "uid": 123456,
        "price": 2000,
        "message": "测试留言\n多行内容",
        "start_time": 1700000000,
        "end_time": 1700003600,
        "background_color": "#EDF5FF",
        "user_info": {"uname": "测试用户", "face": "https://example/face.jpg", "guard_level": 3},
        "medal_info": {"medal_name": "牌子", "medal_level": 21, "medal_color": 6067854, "anchor_roomid": 6},
    },
}


def cmd_selftest(args: argparse.Namespace) -> int:
    """Offline check: pv0/zlib/brotli decoding, dedupe by primary key, NDJSON/CSV export."""
    setup_logging("WARNING", None)
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        print(f"[{'PASS' if condition else 'FAIL'}] {name}")
        if not condition:
            failures.append(name)

    plain = protocol.pack_packet(protocol.OP_MESSAGE, json.dumps(SAMPLE_SC).encode(), header_ver=0)
    two_plain = plain + plain
    zlib_pkt = protocol.pack_packet(protocol.OP_MESSAGE, zlib.compress(two_plain), header_ver=protocol.PROTO_ZLIB)
    br_pkt = protocol.pack_packet(protocol.OP_MESSAGE, brotli.compress(two_plain), header_ver=protocol.PROTO_BROTLI)

    for label, frame, expected in (("pv0", plain, 1), ("zlib", zlib_pkt, 2), ("brotli", br_pkt, 2)):
        messages = []
        for packet in protocol.iter_packets(frame):
            messages.extend(protocol.load_messages(packet))
        check(f"decode {label} -> {expected} message(s)", len(messages) == expected)
        if messages:
            check(f"{label} keeps unicode payload", messages[0]["data"]["message"] == SAMPLE_SC["data"]["message"])

    mixed = protocol.pack_packet(protocol.OP_HEARTBEAT_REPLY, (1234).to_bytes(4, "big"))
    packets = list(protocol.iter_packets(mixed))
    check("heartbeat reply parsed", protocol.parse_popularity(packets[0]) == 1234)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "selftest.db")
        storage = Storage(db_path)
        data = SAMPLE_SC["data"]
        row = extract_sc(7734200, data, 1700000000123)
        check("extract_sc returns row", row is not None and row["sc_id"] == "9007199254740993")
        first = storage.insert_sc(row)
        second = storage.insert_sc(row)
        check("first insert accepted", first is True)
        check("duplicate sc_id ignored", second is False)
        check("row count == 1", storage.stats()["sc_total"] == 1)

        deletion = storage.insert_deletion(7734200, {"ids": [9007199254740993]}, 1700000100000)
        check("deletion recorded", deletion == 1)
        check("deletion row count == 1", storage.stats()["deletion_total"] == 1)

        ndjson_path = os.path.join(tmp, "out.ndjson")
        csv_path = os.path.join(tmp, "out.csv")
        n1 = export_ndjson(storage.iter_rows(with_raw=True), ndjson_path, with_raw=True)
        n2 = export_csv(storage.iter_rows(), csv_path)
        check("ndjson export row count", n1 == 1)
        check("csv export row count", n2 == 1)
        with open(ndjson_path, encoding="utf-8") as fh:
            line = json.loads(fh.readline())
        check("ndjson keeps start_time", line["start_time"] == 1700000000)
        check("ndjson keeps raw json", isinstance(line.get("raw"), dict))
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            header = fh.readline().strip().split(",")
        check("csv header has start_time_iso", "start_time_iso" in header)
        storage.close()

    print()
    if failures:
        print(f"SELFTEST FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("SELFTEST PASSED")
    return 0


# ---------------- parser ----------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sc_collector",
        description="Standalone Bilibili super-chat collector (anonymous websocket, SQLite).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the long-running collector")
    run.add_argument("-c", "--config", default="config.json")
    run.add_argument("--room", type=int, action="append", help="override rooms (repeatable)")
    run.add_argument("--database", help="override database path")
    run.add_argument("--log-level", help="override log level")
    run.set_defaults(func=cmd_run)

    resolve = sub.add_parser("resolve", help="resolve room id and verify danmu token")
    resolve.add_argument("--room", type=int, action="append", required=True)
    resolve.set_defaults(func=cmd_resolve)

    export = sub.add_parser("export", help="export collected super chats")
    export.add_argument("-c", "--config", default="config.json")
    export.add_argument("--format", choices=["ndjson", "csv"], default="ndjson")
    export.add_argument("--out", required=True)
    export.add_argument("--room", type=int)
    export.add_argument("--since", type=int, help="start_time lower bound (unix seconds)")
    export.add_argument("--until", type=int, help="start_time upper bound (unix seconds)")
    export.add_argument("--with-raw", action="store_true", help="include raw payload (ndjson only)")
    export.set_defaults(func=cmd_export)

    stats = sub.add_parser("stats", help="show collected row counts")
    stats.add_argument("-c", "--config", default="config.json")
    stats.set_defaults(func=cmd_stats)

    selftest = sub.add_parser("selftest", help="offline protocol/dedupe/export check")
    selftest.set_defaults(func=cmd_selftest)

    gui = sub.add_parser("gui", help="open the graphical danmaku machine")
    gui.add_argument("-c", "--config", default="config.json")
    gui.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - top level guard
        logging.getLogger("sc_collector").exception("fatal: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
