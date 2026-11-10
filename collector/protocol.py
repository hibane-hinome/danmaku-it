"""Packed header codec and payload decoding for the Bilibili live danmaku protocol."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from dataclasses import dataclass
from typing import Iterator

import brotli

HEADER = struct.Struct(">IHHII")
HEADER_SIZE = 16

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

PROTO_PLAIN = 0
PROTO_POPULARITY = 1
PROTO_ZLIB = 2
PROTO_BROTLI = 3

CLIENT_PROTO_VER = 1


@dataclass(frozen=True)
class Packet:
    operation: int
    proto_ver: int
    body: bytes


def pack_packet(operation: int, body: bytes = b"", header_ver: int = CLIENT_PROTO_VER, sequence: int = 1) -> bytes:
    header = HEADER.pack(HEADER_SIZE + len(body), HEADER_SIZE, header_ver, operation, sequence)
    return header + body


def pack_auth(room_id: int, token: str, uid: int = 0, protover: int = 3) -> bytes:
    payload = {
        "uid": uid,
        "roomid": room_id,
        "protover": protover,
        "platform": "web",
        "type": 2,
        "key": token,
    }
    return pack_packet(OP_AUTH, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def pack_heartbeat() -> bytes:
    return pack_packet(OP_HEARTBEAT, b"[object Object]")


def iter_packets(data: bytes) -> Iterator[Packet]:
    """Split one websocket frame into protocol packets."""
    offset = 0
    total = len(data)
    while offset + HEADER_SIZE <= total:
        packet_len, header_len, proto_ver, operation, _seq = HEADER.unpack_from(data, offset)
        if packet_len < HEADER_SIZE or offset + packet_len > total:
            # Truncated or bogus length: stop instead of producing garbage.
            return
        body = data[offset + header_len: offset + packet_len]
        yield Packet(operation=operation, proto_ver=proto_ver, body=body)
        offset += packet_len


def _decompress(proto_ver: int, body: bytes) -> bytes:
    if proto_ver == PROTO_ZLIB:
        return zlib.decompress(body)
    if proto_ver == PROTO_BROTLI:
        return brotli.decompress(body)
    return body


def _json_objects(body: bytes) -> Iterator[dict]:
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return
    if isinstance(obj, dict):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item


def load_messages(packet: Packet) -> Iterator[dict]:
    """Expand a message packet (plain / zlib / brotli, possibly nested) into JSON objects."""
    if packet.proto_ver in (PROTO_ZLIB, PROTO_BROTLI):
        try:
            inflated = _decompress(packet.proto_ver, packet.body)
        except Exception:
            return
        nested = list(iter_packets(inflated))
        if nested:
            for sub in nested:
                if sub.proto_ver in (PROTO_ZLIB, PROTO_BROTLI):
                    yield from load_messages(sub)
                else:
                    yield from _json_objects(sub.body)
        else:
            yield from _json_objects(inflated)
        return

    yield from _json_objects(packet.body)


def parse_auth_reply(packet: Packet) -> dict | None:
    try:
        obj = json.loads(packet.body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_popularity(packet: Packet) -> int | None:
    if len(packet.body) == 4:
        return struct.unpack(">I", packet.body)[0]
    return None


def _read_varint(buf: bytes, index: int):
    shift = 0
    value = 0
    while index < len(buf):
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 63:
            return None, index
    return None, index


def parse_interact_word_v2(pb_base64: str) -> dict | None:
    """Decode the protobuf body of INTERACT_WORD_V2.

    Field map observed on live traffic: 1=uid, 2=uname, 5=msg_type, 6=room_id, 7=timestamp.
    msg_type 1 means the user entered the room.
    """
    if not pb_base64:
        return None
    try:
        raw = base64.b64decode(pb_base64)
    except (TypeError, ValueError):
        return None

    fields: dict[int, object] = {}
    index = 0
    total = len(raw)
    while index < total:
        key, index = _read_varint(raw, index)
        if key is None:
            return None
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _read_varint(raw, index)
            if value is None:
                return None
            fields[field] = value
        elif wire == 2:
            size, index = _read_varint(raw, index)
            if size is None or index + size > total:
                return None
            chunk = raw[index:index + size]
            index += size
            if field == 2:
                fields[2] = chunk.decode("utf-8", "replace")
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            return None

    uid = fields.get(1)
    if uid is None:
        return None
    return {
        "uid": uid,
        "uname": fields.get(2, ""),
        "msg_type": fields.get(5, 0),
        "room_id": fields.get(6),
        "timestamp": fields.get(7),
    }


def _decode_fields(raw: bytes) -> dict[int, object] | None:
    """Shallow protobuf decode: varints as int, length-delimited as raw bytes."""
    fields: dict[int, object] = {}
    index = 0
    total = len(raw)
    while index < total:
        key, index = _read_varint(raw, index)
        if key is None:
            return None
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _read_varint(raw, index)
            if value is None:
                return None
            fields[field] = value
        elif wire == 2:
            size, index = _read_varint(raw, index)
            if size is None or index + size > total:
                return None
            fields[field] = raw[index:index + size]
            index += size
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            return None
    return fields


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _as_text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


def parse_send_gift_v2(pb_base64: str) -> dict | None:
    """Decode the protobuf body of SEND_GIFT_V2.

    Field map observed on live traffic:
      1=uid, 10=gift{1 id, 2 name, 3 num, 5 price, 8 coin_type, 9 tid, 10 timestamp},
      15=user{1 uid, 2 base{1 uname, 2 face}, 3 medal{1 inner{11 name}, 2 level}}
    """
    if not pb_base64:
        return None
    try:
        raw = base64.b64decode(pb_base64)
    except (TypeError, ValueError):
        return None

    top = _decode_fields(raw)
    if not top:
        return None
    gift = _decode_fields(top.get(10) or b"")
    if not gift:
        return None

    user = _decode_fields(top.get(15) or b"") or {}
    base = _decode_fields(user.get(2) or b"") or {}
    medal = _decode_fields(user.get(3) or b"") or {}
    medal_inner = _decode_fields(medal.get(1) or b"") or {}

    gift_name = _as_text(gift.get(2))
    if not gift_name:
        return None

    return {
        "uid": _as_int(top.get(1)) or _as_int(user.get(1)),
        "uname": _as_text(base.get(1)),
        "giftId": _as_int(gift.get(1)),
        "giftName": gift_name,
        "num": _as_int(gift.get(3)) or 1,
        "price": _as_int(gift.get(5)),
        "coin_type": _as_text(gift.get(8)) or "gold",
        "tid": _as_text(gift.get(9)),
        "timestamp": _as_int(gift.get(10)),
        "medal_name": _as_text(medal_inner.get(11)) or _as_text(medal.get(3)),
        "medal_level": _as_int(medal.get(2)),
    }
