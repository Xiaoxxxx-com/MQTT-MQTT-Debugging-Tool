"""内置 MQTT Broker 服务端。

为什么自己实现而不是依赖现成的 Broker？
    · 上位机要能**独立**跑在一台机器上做联调，不能要求用户另外装 mosquitto
    · 只需要实现调试场景够用的子集：MQTT 3.1 / 3.1.1，QoS 0/1/2，Retain，遗嘱，
      会话保持，通配符订阅
    · 协议报文解析只依赖 struct，零第三方依赖

不支持（有意省略，超出调试需求）：
    MQTT 5.0、共享订阅的负载均衡、持久化存储、$SYS 主题发布、桥接

支持 WebSocket 传输（RFC 6455）
------------------------------
    很多前端（浏览器 / 小程序）和 EMQX 的默认部署只开放 ws://host:8083/mqtt，
    所以 Broker 除了裸 TCP 之外还监听了第二个 WebSocket 端口。
    握手在 `_ws_handshake()`，帧编解码在 `_WebSocketTransport`。
    两条传输路径共用同一份 MQTT 报文处理逻辑（ClientSession._read_loop）。

线程模型
--------
    accept 线程(TCP)  ┐
    accept 线程(WS)   ┼→ 每个客户端一个 ClientSession 线程
    全局 _lock (RLock) → 保护 clients / subscriptions / retained 等共享状态

每连接一线程是最简单可靠的模型：调试场景下并发量很小（几十个连接），
不值得为可扩展性引入事件循环的复杂度。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .config import BrokerConfig
from .core import topic_matches

# --------------------------------------------------------------------------- #
# 报文类型与常量
# --------------------------------------------------------------------------- #

CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
PUBREC = 5
PUBREL = 6
PUBCOMP = 7
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14

PACKET_NAMES = {
    CONNECT: "CONNECT", CONNACK: "CONNACK", PUBLISH: "PUBLISH", PUBACK: "PUBACK",
    PUBREC: "PUBREC", PUBREL: "PUBREL", PUBCOMP: "PUBCOMP", SUBSCRIBE: "SUBSCRIBE",
    SUBACK: "SUBACK", UNSUBSCRIBE: "UNSUBSCRIBE", UNSUBACK: "UNSUBACK",
    PINGREQ: "PINGREQ", PINGRESP: "PINGRESP", DISCONNECT: "DISCONNECT",
}

# CONNACK 返回码
CONNACK_ACCEPTED = 0
CONNACK_BAD_PROTOCOL = 1
CONNACK_ID_REJECTED = 2
CONNACK_SERVER_UNAVAILABLE = 3
CONNACK_BAD_CREDENTIALS = 4
CONNACK_NOT_AUTHORIZED = 5

CONNACK_TEXT = {
    0: "接受连接", 1: "协议版本不支持", 2: "客户端标识符被拒绝",
    3: "服务端不可用", 4: "用户名或密码错误", 5: "未授权",
}

# 订阅返回码里的失败值
SUBACK_FAILURE = 0x80

MAX_REMAINING_LENGTH = 268435455   # 报文最大长度（协议规定）


# --------------------------------------------------------------------------- #
# 事件回调
# --------------------------------------------------------------------------- #

@dataclass
class BrokerEvent:
    """投递给界面的事件。"""

    kind: str                       # log / client_join / client_leave / stats / state
    text: str = ""
    level: str = "info"
    client_id: str = ""
    remote: str = ""
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------- #
# 报文编解码
# --------------------------------------------------------------------------- #

class ProtocolError(Exception):
    """报文格式非法，应断开连接。"""


def encode_remaining_length(length: int) -> bytes:
    """MQTT 的可变长度编码。"""
    if length < 0 or length > MAX_REMAINING_LENGTH:
        raise ProtocolError("报文长度超出协议限制")
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 0x80
        out.append(byte)
        if length == 0:
            break
    return bytes(out)


def decode_remaining_length(read_byte: Callable[[], bytes]) -> int:
    """读取可变长度字段。最多 4 字节。"""
    multiplier = 1
    value = 0
    for _ in range(4):
        raw = read_byte()
        if not raw:
            raise ProtocolError("连接已关闭")
        byte = raw[0]
        value += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            return value
        multiplier *= 128
    raise ProtocolError("可变长度字段格式非法")


def encode_string(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 65535:
        raise ProtocolError("字符串超出 65535 字节")
    return struct.pack("!H", len(data)) + data


def decode_string(data: bytes, offset: int) -> Tuple[str, int]:
    if offset + 2 > len(data):
        raise ProtocolError("字符串长度字段不完整")
    (length,) = struct.unpack_from("!H", data, offset)
    offset += 2
    if offset + length > len(data):
        raise ProtocolError("字符串内容不完整")
    raw = data[offset:offset + length]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    return text, offset + length


def build_packet(packet_type: int, flags: int, payload: bytes) -> bytes:
    header = bytes([(packet_type << 4) | (flags & 0x0F)])
    return header + encode_remaining_length(len(payload)) + payload


# --------------------------------------------------------------------------- #
# WebSocket 传输（RFC 6455 子集）
# --------------------------------------------------------------------------- #

#: WebSocket 握手用的固定 GUID（RFC 6455 §1.3，共 36 个字符，末尾不要多写 D）
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 帧 opcode
WS_OP_CONT = 0x0        # 续帧
WS_OP_TEXT = 0x1
WS_OP_BINARY = 0x2
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA

#: 单帧最大载荷，防御性上限（正常 MQTT over WS 不会这么大）
WS_MAX_FRAME = 16 * 1024 * 1024


class WebSocketError(Exception):
    """WebSocket 握手或帧格式非法。"""


def ws_accept_key(client_key: str) -> str:
    """按 RFC 6455 计算 Sec-WebSocket-Accept。"""
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_ws_frame(payload: bytes, opcode: int = WS_OP_BINARY,
                    fin: bool = True) -> bytes:
    """编码一个服务端→客户端的帧（服务端发送的帧不掩码）。"""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


def decode_ws_frame(recv_exact: Callable[[int], bytes]) -> Tuple[int, bytes, bool]:
    """从连接读一个 WebSocket 帧。

    参数 recv_exact 是"精确读取 n 字节"的函数（同 ClientSession._recv_exact）。
    返回 (opcode, payload, fin)。自动处理客户端掩码（客户端必须掩码）。
    """
    header = recv_exact(2)
    fin = bool(header[0] & 0x80)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        (length,) = struct.unpack("!H", recv_exact(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", recv_exact(8))
        if length >> 63:
            raise WebSocketError("帧长度最高位必须为 0")
    if length > WS_MAX_FRAME:
        raise WebSocketError(f"单帧过大（{length} 字节）")

    mask = recv_exact(4) if masked else b""
    payload = recv_exact(length) if length else b""

    if masked:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return opcode, payload, fin


def parse_ws_handshake(data: bytes) -> Tuple[str, List[str]]:
    """解析 HTTP Upgrade 请求，返回 (Sec-WebSocket-Key, 子协议列表)。

    校验失败抛 WebSocketError；子协议列表可能为空（客户端没要求子协议）。
    """
    try:
        text = data.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover
        raise WebSocketError("握手请求不是合法的 HTTP 文本")

    lines = text.split("\r\n")
    if not lines or not lines[0].upper().startswith("GET "):
        raise WebSocketError("握手请求必须是 GET")

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()

    if "websocket" not in headers.get("upgrade", "").lower():
        raise WebSocketError("缺少 Upgrade: websocket 头")
    if "upgrade" not in headers.get("connection", "").lower():
        raise WebSocketError("缺少 Connection: Upgrade 头")

    key = headers.get("sec-websocket-key", "")
    if not key:
        raise WebSocketError("缺少 Sec-WebSocket-Key 头")
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            raise WebSocketError("Sec-WebSocket-Key 不是 16 字节")
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, WebSocketError):
            raise
        raise WebSocketError("Sec-WebSocket-Key 不是合法的 Base64")

    protocols: List[str] = []
    raw_proto = headers.get("sec-websocket-protocol", "")
    if raw_proto:
        protocols = [p.strip() for p in raw_proto.split(",") if p.strip()]
    return key, protocols


def build_ws_handshake_response(key: str, protocol: Optional[str] = None) -> bytes:
    """构造 101 响应。"""
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {ws_accept_key(key)}",
    ]
    if protocol:
        lines.append(f"Sec-WebSocket-Protocol: {protocol}")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode("latin-1")


def build_ws_http_error(status: int, reason: str, detail: str = "") -> bytes:
    """握手失败时回一个普通 HTTP 响应，便于用户用浏览器/curl 自检。

    reason 会被放进状态行，必须是 ASCII（HTTP 状态行不能出现非 ASCII 字节）；
    中文说明请放 detail，它进 body（UTF-8 编码，没问题）。
    """
    ascii_reason = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in reason) or "Bad Request"
    text = detail or reason
    body = f"MQTT WebSocket handshake failed: {text}\n".encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {ascii_reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    return head + body


# --------------------------------------------------------------------------- #
# 客户端会话
# --------------------------------------------------------------------------- #

class ClientSession(threading.Thread):
    """一个客户端连接的读写线程。

    传输层可以是裸 TCP，也可以是 WebSocket —— 由 `transport` 决定：
    本类只负责 MQTT 报文语义，收发字节这层交给 transport 对象，
    这样 TCP 与 WS 共用同一份协议处理逻辑（少写一半代码，也少一半 bug）。
    """

    def __init__(self, broker: "MQTTBroker", conn: socket.socket, addr: tuple,
                 transport: str = "TCP", ws_path: str = "") -> None:
        super().__init__(daemon=True)
        self.broker = broker
        self.conn = conn
        self.addr = addr
        self.remote = f"{addr[0]}:{addr[1]}"
        self.transport = transport                     # "TCP" / "WebSocket"
        self.ws_path = ws_path

        self.client_id = ""
        self.username = ""
        self.keepalive = 0
        self.clean_session = True
        self.protocol_level = 4
        self.will: Optional[Dict[str, Any]] = None
        self.joined_at = time.time()
        self.last_seen = time.time()

        self.subscriptions: Dict[str, int] = {}      # topic filter -> qos

        # 出站 QoS 1/2 状态
        self._outgoing: Dict[int, int] = {}          # mid -> qos（等待确认）
        # 入站 QoS 2 状态
        self._incoming_qos2: Set[int] = set()

        self._mid = 0
        self._send_lock = threading.Lock()
        self._alive = True

        # WebSocket 分片重组缓冲 + 待处理的控制帧
        self._ws_frag_opcode: Optional[int] = None
        self._ws_frag_buf = bytearray()
        self._ws_pending: List[bytes] = []            # 已解码、待上层消费的 MQTT 字节流
        self._ws_pending_off = 0

        try:
            self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        # 发送超时：防止对端 TCP 窗口打满时 sendall 一直阻塞，
        # 那会把这个线程连同它持有的 _send_lock 一起挂死。
        try:
            self.conn.settimeout(15.0)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def next_mid(self) -> int:
        with self._send_lock:
            self._mid = (self._mid % 65535) + 1
            return self._mid

    def send(self, data: bytes) -> bool:
        with self._send_lock:
            if not self._alive:
                return False
            try:
                if self.transport == "WebSocket":
                    # 一条 MQTT 报文一个 WS 二进制帧（EMQX / paho 都是这个行为）
                    data = encode_ws_frame(data, WS_OP_BINARY)
                self.conn.sendall(data)
                self.broker.stats["bytes_out"] += len(data)
                return True
            except OSError:
                self._alive = False
                return False

    def send_packet(self, packet_type: int, flags: int, payload: bytes) -> bool:
        return self.send(build_packet(packet_type, flags, payload))

    def _ws_send_raw(self, raw: bytes) -> bool:
        """直接发一段已经成帧的字节（控制帧用，不要再包一层 WS 帧）。"""
        with self._send_lock:
            if not self._alive:
                return False
            try:
                self.conn.sendall(raw)
                self.broker.stats["bytes_out"] += len(raw)
                return True
            except OSError:
                self._alive = False
                return False

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            if self.transport == "WebSocket":
                if not self._ws_handshake():
                    return
            if not self._handle_connect():
                return
            self.broker._on_client_ready(self)
            self._read_loop()
        except (ProtocolError, WebSocketError, OSError) as exc:
            self.broker._log(f"客户端 {self.client_id or self.remote} 断开：{exc}", "warn", self)
        except Exception as exc:  # noqa: BLE001 - 单连接异常不能拖垮整个 Broker
            self.broker._log(f"客户端 {self.client_id or self.remote} 异常：{exc}", "error", self)
        finally:
            self.broker._on_client_gone(self)

    # ------------------------------------------------------------------ #
    def _recv_exact(self, size: int) -> bytes:
        buf = bytearray()
        while len(buf) < size:
            try:
                chunk = self.conn.recv(size - len(buf))
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                raise ProtocolError("连接已关闭")
            if not chunk:
                raise ProtocolError("连接已关闭")
            buf.extend(chunk)
        self.last_seen = time.time()
        self.broker.stats["bytes_in"] += len(buf)
        return bytes(buf)

    # ------------------------------------------------------------------ #
    # WebSocket 握手与收帧
    # ------------------------------------------------------------------ #
    def _ws_handshake(self) -> bool:
        """解析 HTTP Upgrade 请求并回 101。失败时回一个 HTTP 错误页方便自检。"""
        # 读到头部结束（\r\n\r\n）为止，最多 16KB
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            if len(raw) > 16384:
                raise WebSocketError("握手请求头过大")
            raw.extend(self._recv_exact(1))

        head, _, rest = bytes(raw).partition(b"\r\n\r\n")
        try:
            key, protocols = parse_ws_handshake(head + b"\r\n\r\n")
        except WebSocketError as exc:
            self.broker._log(f"{self.remote} WebSocket 握手失败：{exc}", "warn", self)
            try:
                self.conn.sendall(build_ws_http_error(400, "Bad Request", str(exc)))
            except OSError:
                pass
            return False

        # 路径校验：只处理配置的路径（默认 /mqtt），其它路径给出明确提示
        first_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first_line.split()
        request_path = parts[1] if len(parts) >= 2 else "/"
        request_path = request_path.split("?", 1)[0]
        want = (self.ws_path or "/mqtt")
        if request_path.rstrip("/") != want.rstrip("/"):
            msg = f"路径 {request_path} 不存在，本 Broker 的 WebSocket 路径是 {want}"
            self.broker._log(f"{self.remote} WS 握手被拒：{msg}", "warn", self)
            try:
                self.conn.sendall(build_ws_http_error(
                    404, "Not Found",
                    f"路径 {request_path} 不存在，本 Broker 的 WebSocket 路径是 {want}"))
            except OSError:
                pass
            return False

        # 子协议协商：客户端要 mqtt 就回 mqtt（不带的话也接受，兼容性更好）
        agreed: Optional[str] = None
        for proto in protocols:
            if proto.lower() in ("mqtt", "mqttv3.1"):
                agreed = proto
                break
        if protocols and agreed is None:
            self.broker._log(
                f"{self.remote} 请求的子协议 {'/'.join(protocols)} 不支持，"
                f"仍按 mqtt 处理", "warn", self,
            )
            agreed = "mqtt"

        try:
            self.conn.sendall(build_ws_handshake_response(key, agreed))
        except OSError:
            return False

        # 握手请求的 body 已被读走，若有多余字节（理论上不该有）先丢掉
        if rest:
            self._ws_pending.append(rest)
        self.broker._log(
            f"{self.remote} WebSocket 握手成功（路径 {request_path}，"
            f"子协议 {agreed or '未指定'}）", "info", self,
        )
        return True

    def _ws_read_payload(self) -> bytes:
        """从 WS 连接读出一个完整的应用层数据块（已处理分片与控制帧）。"""
        while True:
            opcode, payload, fin = decode_ws_frame(self._recv_exact)

            if opcode == WS_OP_CLOSE:
                raise ProtocolError("收到 WebSocket Close 帧")
            if opcode == WS_OP_PING:
                self._ws_send_raw(encode_ws_frame(payload, WS_OP_PONG))
                continue
            if opcode == WS_OP_PONG:
                continue

            if opcode in (WS_OP_TEXT, WS_OP_BINARY):
                if fin:
                    return payload
                self._ws_frag_opcode = opcode
                self._ws_frag_buf = bytearray(payload)
                continue

            if opcode == WS_OP_CONT:
                if self._ws_frag_opcode is None:
                    raise WebSocketError("收到意外的续帧")
                self._ws_frag_buf.extend(payload)
                if len(self._ws_frag_buf) > WS_MAX_FRAME:
                    raise WebSocketError("分片消息过大")
                if fin:
                    data = bytes(self._ws_frag_buf)
                    self._ws_frag_buf = bytearray()
                    self._ws_frag_opcode = None
                    return data
                continue

            raise WebSocketError(f"未知的 WebSocket opcode 0x{opcode:X}")

    def _ws_recv(self, size: int) -> bytes:
        """按字节流语义取 size 字节：跨 WS 帧边界自动拼接。

        MQTT 报文可能被拆到多个 WS 帧里（尤其是大载荷），所以这里要
        维护一个缓冲，不能假设"一帧 = 一个报文"。
        """
        while True:
            available = sum(len(b) for b in self._ws_pending) - self._ws_pending_off
            if available >= size:
                break
            self._ws_pending.append(self._ws_read_payload())

        out = bytearray()
        while len(out) < size:
            if not self._ws_pending:
                break
            chunk = self._ws_pending[0]
            need = size - len(out)
            take = chunk[self._ws_pending_off:self._ws_pending_off + need]
            out.extend(take)
            self._ws_pending_off += len(take)
            if self._ws_pending_off >= len(chunk):
                self._ws_pending.pop(0)
                self._ws_pending_off = 0
        self.last_seen = time.time()
        return bytes(out)

    def _read_bytes(self, size: int) -> bytes:
        """统一的底层读取入口：TCP 直接读，WS 走帧解码。"""
        if self.transport == "WebSocket":
            return self._ws_recv(size)
        return self._recv_exact(size)

    def _handle_connect(self) -> bool:
        """处理首个报文，必须是 CONNECT。"""
        header = self._read_bytes(1)
        if not header:
            raise ProtocolError("连接已关闭")
        packet_type = header[0] >> 4
        if packet_type != CONNECT:
            self.broker._log(
                f"来自 {self.remote} 的首个报文不是 CONNECT（收到 {PACKET_NAMES.get(packet_type, packet_type)}），拒绝",
                "warn", self,
            )
            return False

        length = decode_remaining_length(lambda: self._read_bytes(1))
        if length <= 0 or length > 65535 * 4:
            raise ProtocolError("CONNECT 报文长度异常")
        body = self._read_bytes(length)

        offset = 0
        proto_name, offset = decode_string(body, offset)
        if offset >= len(body):
            raise ProtocolError("CONNECT 缺少协议版本")
        proto_level = body[offset]
        offset += 1
        if offset >= len(body):
            raise ProtocolError("CONNECT 缺少连接标志")
        flags = body[offset]
        offset += 1
        if offset + 2 > len(body):
            raise ProtocolError("CONNECT 缺少 Keep Alive")
        (keepalive,) = struct.unpack_from("!H", body, offset)
        offset += 2

        if proto_name not in ("MQTT", "MQIsdp"):
            return self._reject(CONNACK_BAD_PROTOCOL, f"协议名不支持：{proto_name}")
        if proto_level not in (3, 4):
            return self._reject(
                CONNACK_BAD_PROTOCOL,
                f"协议版本 {proto_level} 不支持（本 Broker 支持 MQTT 3.1 / 3.1.1）",
            )

        self.protocol_level = proto_level
        self.keepalive = keepalive

        clean_session = bool(flags & 0x02)
        will_flag = bool(flags & 0x04)
        will_qos = (flags >> 3) & 0x03
        will_retain = bool(flags & 0x20)
        password_flag = bool(flags & 0x40)
        username_flag = bool(flags & 0x80)

        client_id, offset = decode_string(body, offset)
        # 3.1.1 允许空 client_id（服务端分配），3.1 不允许
        if not client_id:
            if proto_level == 4 and clean_session:
                client_id = f"auto-{os.urandom(4).hex()}"
            else:
                return self._reject(CONNACK_ID_REJECTED, "客户端标识符为空且未设置 Clean Session")

        will = None
        if will_flag:
            will_topic, offset = decode_string(body, offset)
            if offset + 2 > len(body):
                raise ProtocolError("遗嘱内容长度字段不完整")
            (will_len,) = struct.unpack_from("!H", body, offset)
            offset += 2
            if offset + will_len > len(body):
                raise ProtocolError("遗嘱内容不完整")
            will_payload = body[offset:offset + will_len]
            offset += will_len
            will = {
                "topic": will_topic,
                "payload": will_payload,
                "qos": min(will_qos, 2),
                "retain": will_retain,
            }

        username = ""
        if username_flag:
            username, offset = decode_string(body, offset)

        password = ""
        if password_flag:
            if offset + 2 > len(body):
                raise ProtocolError("密码字段不完整")
            (pwd_len,) = struct.unpack_from("!H", body, offset)
            offset += 2
            if offset + pwd_len > len(body):
                raise ProtocolError("密码内容不完整")
            password = body[offset:offset + pwd_len].decode("utf-8", errors="replace")
            offset += pwd_len

        # ---- 认证 ---- #
        ok, reason = self.broker._check_auth(username, password, self.remote)
        if not ok:
            code = CONNACK_BAD_CREDENTIALS if username else CONNACK_NOT_AUTHORIZED
            return self._reject(code, reason)

        # ---- 分配 client_id ---- #
        ok, reason = self.broker._register_client(self, client_id, clean_session)
        if not ok:
            return self._reject(CONNACK_ID_REJECTED, reason)

        self.client_id = client_id
        self.username = username
        self.clean_session = clean_session
        self.subscriptions = self.broker._take_session_subscriptions(client_id, clean_session)
        self.will = will

        # 恢复的订阅必须重新登记到 Broker 的全局订阅表，
        # 否则这些订阅只存在于会话对象里，路由时查不到，等于形同虚设。
        for filt, sub_qos in self.subscriptions.items():
            self.broker._add_subscription(self, filt, sub_qos)

        # CONNACK: byte1 = 会话是否已存在
        session_present = bool(self.broker._session_present)
        if session_present and self.subscriptions:
            self.broker._log(
                f"客户端 {client_id} 恢复会话，共 {len(self.subscriptions)} 条订阅", "success", self,
            )
        return self.send_packet(CONNACK, 0, bytes([session_present, CONNACK_ACCEPTED]))

    def _reject(self, code: int, reason: str) -> bool:
        self.broker._log(f"拒绝 {self.remote}（{self.client_id or '未标识'}）：{reason}", "warn", self)
        self.send_packet(CONNACK, 0, bytes([0, code]))
        time.sleep(0.05)   # 给对端时间读完 CONNACK，再关闭
        return False

    # ------------------------------------------------------------------ #
    def _read_loop(self) -> None:
        while self._alive and not self.broker.stopping:
            if self.keepalive:
                timeout = self.keepalive * 1.5
                if time.time() - self.last_seen > timeout:
                    raise ProtocolError(f"Keep Alive 超时（{self.keepalive}s 无数据）")
                self.conn.settimeout(max(1.0, timeout))

            header = self._read_bytes(1)
            if not header:
                raise ProtocolError("连接已关闭")
            packet_type = header[0] >> 4
            flags = header[0] & 0x0F

            if packet_type == PINGREQ:
                decode_remaining_length(lambda: self._read_bytes(1))
                self.send_packet(PINGRESP, 0, b"")
                continue
            if packet_type == DISCONNECT:
                decode_remaining_length(lambda: self._read_bytes(1))
                self.broker._log(f"客户端 {self.client_id} 正常断开", "info", self)
                self.will = None
                self._alive = False
                return

            length = decode_remaining_length(lambda: self._read_bytes(1))
            body = self._read_bytes(length) if length else b""

            if packet_type == PUBLISH:
                self._on_publish(flags, body)
            elif packet_type == PUBACK:
                self._on_puback(body)
            elif packet_type == PUBREC:
                self._on_pubrec(body)
            elif packet_type == PUBREL:
                self._on_pubrel(body)
            elif packet_type == PUBCOMP:
                self._on_pubcomp(body)
            elif packet_type == SUBSCRIBE:
                self._on_subscribe(body)
            elif packet_type == UNSUBSCRIBE:
                self._on_unsubscribe(body)
            elif packet_type == CONNECT:
                raise ProtocolError("重复的 CONNECT 报文")
            else:
                self.broker._log(
                    f"暂不支持的报文类型 {PACKET_NAMES.get(packet_type, packet_type)}，已忽略",
                    "warn", self,
                )

    # ------------------------------------------------------------------ #
    # PUBLISH
    # ------------------------------------------------------------------ #
    def _on_publish(self, flags: int, body: bytes) -> None:
        if len(body) < 2:
            raise ProtocolError("PUBLISH 报文过短")
        qos = (flags >> 1) & 0x03
        retain = bool(flags & 0x01)
        dup = bool(flags & 0x08)
        if qos > 2:
            raise ProtocolError(f"非法的 QoS 等级 {qos}")

        topic, offset = decode_string(body, 0)
        if not topic:
            raise ProtocolError("PUBLISH 主题为空")
        if "+" in topic or "#" in topic:
            raise ProtocolError("PUBLISH 主题不能包含通配符")

        mid = 0
        if qos > 0:
            if offset + 2 > len(body):
                raise ProtocolError("PUBLISH 缺少报文标识符")
            (mid,) = struct.unpack_from("!H", body, offset)
            offset += 2

        payload = body[offset:]
        self.broker._log(
            f"收到 PUBLISH ← {self.client_id}　{topic}　QoS{qos}"
            f"{'　Retain' if retain else ''}　{len(payload)} 字节",
            "info", self,
        )

        # QoS 2 需要去重：同一个 mid 重复到达只处理一次
        if qos == 2:
            if mid in self._incoming_qos2:
                self.send_packet(PUBREC, 0, struct.pack("!H", mid))
                return
            self._incoming_qos2.add(mid)
            self.send_packet(PUBREC, 0, struct.pack("!H", mid))

        # 交给 Broker 路由
        self.broker._publish(topic, payload, qos, retain, source=self)

        if qos == 1:
            self.send_packet(PUBACK, 0, struct.pack("!H", mid))
        elif qos == 2 and dup:
            pass

    def _on_puback(self, body: bytes) -> None:
        if len(body) >= 2:
            (mid,) = struct.unpack_from("!H", body, 0)
            self._outgoing.pop(mid, None)

    def _on_pubrec(self, body: bytes) -> None:
        if len(body) >= 2:
            (mid,) = struct.unpack_from("!H", body, 0)
            self.send_packet(PUBREL, 0x02, struct.pack("!H", mid))
            self._outgoing[mid] = 2

    def _on_pubrel(self, body: bytes) -> None:
        if len(body) >= 2:
            (mid,) = struct.unpack_from("!H", body, 0)
            self._incoming_qos2.discard(mid)
            self.send_packet(PUBCOMP, 0, struct.pack("!H", mid))

    def _on_pubcomp(self, body: bytes) -> None:
        if len(body) >= 2:
            (mid,) = struct.unpack_from("!H", body, 0)
            self._outgoing.pop(mid, None)

    # ------------------------------------------------------------------ #
    # SUBSCRIBE / UNSUBSCRIBE
    # ------------------------------------------------------------------ #
    def _on_subscribe(self, body: bytes) -> None:
        if len(body) < 3:
            raise ProtocolError("SUBSCRIBE 报文过短")
        (mid,) = struct.unpack_from("!H", body, 0)
        offset = 2

        granted: List[int] = []
        accepted: List[Tuple[str, int]] = []
        while offset < len(body):
            topic, offset = decode_string(body, offset)
            if offset >= len(body):
                raise ProtocolError("SUBSCRIBE 缺少 QoS 字节")
            requested_qos = body[offset] & 0x03
            offset += 1

            valid, err = _validate_filter(topic)
            if not valid:
                self.broker._log(f"订阅 {topic} 被拒绝：{err}", "warn", self)
                granted.append(SUBACK_FAILURE)
                continue

            qos = min(requested_qos, self.broker.cfg.max_qos)
            self.subscriptions[topic] = qos
            self.broker._add_subscription(self, topic, qos)
            granted.append(qos)
            accepted.append((topic, qos))

        self.send_packet(SUBACK, 0, struct.pack("!H", mid) + bytes(granted))

        for topic, qos in accepted:
            self.broker._log(f"客户端 {self.client_id} 订阅 {topic}（QoS {qos}）", "success", self)
            # 推送匹配的保留消息
            self.broker._send_retained(self, topic, qos)

    def _on_unsubscribe(self, body: bytes) -> None:
        if len(body) < 3:
            raise ProtocolError("UNSUBSCRIBE 报文过短")
        (mid,) = struct.unpack_from("!H", body, 0)
        offset = 2
        while offset < len(body):
            topic, offset = decode_string(body, offset)
            self.subscriptions.pop(topic, None)
            self.broker._remove_subscription(self, topic)
            self.broker._log(f"客户端 {self.client_id} 取消订阅 {topic}", "info", self)
        self.send_packet(UNSUBACK, 0, struct.pack("!H", mid))

    # ------------------------------------------------------------------ #
    def deliver(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        """向本客户端投递一条消息。"""
        if not self._alive:
            return
        effective_qos = min(qos, self.broker.cfg.max_qos)
        flags = (effective_qos << 1) | (0x01 if retain else 0x00)

        if effective_qos == 0:
            ok = self.send_packet(PUBLISH, flags, encode_string(topic) + payload)
        else:
            mid = self.next_mid()
            ok = self.send_packet(
                PUBLISH, flags,
                encode_string(topic) + struct.pack("!H", mid) + payload,
            )
            if ok:
                self._outgoing[mid] = effective_qos
        if not ok:
            # 发送失败说明这条连接已经废了，主动收尸，
            # 免得它继续挂在订阅表里被反复投递。
            self.broker.on_delivery_failure(self)

    def close(self, send_will: bool = True) -> None:
        if not self._alive:
            return
        self._alive = False
        will = self.will
        self.will = None

        # WS 连接最好先给对方一个 Close 帧，浏览器端才会干净地触发 onclose
        if self.transport == "WebSocket":
            try:
                self.conn.sendall(encode_ws_frame(struct.pack("!H", 1000), WS_OP_CLOSE))
            except OSError:
                pass
        if send_will and will:
            self.broker._log(
                f"客户端 {self.client_id} 异常掉线，代发遗嘱 → {will['topic']}", "warn", self,
            )
            self.broker._publish(
                will["topic"], will["payload"], will["qos"], will["retain"], source=None,
            )
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass
        self.broker.stats["connections_closed"] = \
            self.broker.stats.get("connections_closed", 0) + 1


def _validate_filter(topic: str) -> Tuple[bool, str]:
    if not topic:
        return False, "主题为空"
    parts = topic.split("/")
    for idx, part in enumerate(parts):
        if "#" in part:
            if part != "#":
                return False, "通配符 # 必须独占一层"
            if idx != len(parts) - 1:
                return False, "通配符 # 只能出现在最后"
        if "+" in part and part != "+":
            return False, "通配符 + 必须独占一层"
    return True, ""


# --------------------------------------------------------------------------- #
# Broker 主体
# --------------------------------------------------------------------------- #

class MQTTBroker:
    """一个可被局域网内其他主机连接的 MQTT Broker。"""

    def __init__(self, cfg: BrokerConfig,
                 on_event: Optional[Callable[[BrokerEvent], None]] = None) -> None:
        self.cfg = cfg
        self._on_event = on_event
        # 发布 tap：每条被接受的 PUBLISH 都会回调一次（在客户端线程中执行，
        # 回调自身必须线程安全且足够快；异常会被吞掉，不影响 Broker 运行）。
        self.on_packet: Optional[Callable[[str, bytes, int, bool], None]] = None

        self.clients: Dict[str, ClientSession] = {}
        self._retained: Dict[str, Tuple[bytes, int]] = {}
        # topic filter -> {client_id: qos}
        self._subs: Dict[str, Dict[str, int]] = {}
        # 断开但保留会话的订阅：client_id -> {topic: qos}
        self._saved_sessions: Dict[str, Dict[str, int]] = {}
        # 已执行过清理的 client_id，保证 _on_client_gone 幂等
        self._gone_ids: Set[str] = set()

        self._lock = threading.RLock()
        self._server_sock: Optional[socket.socket] = None
        self._ws_server_sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._ws_accept_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._stop_flag = threading.Event()
        self._session_present = False

        self.started_at = 0.0
        self.stats = {
            "connections_total": 0,
            "connections_rejected": 0,
            "connections_closed": 0,
            "messages_in": 0,
            "messages_out": 0,
            "bytes_in": 0,
            "bytes_out": 0,
        }
        self._log_history: List[Dict[str, Any]] = []

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    @property
    def running(self) -> bool:
        return self._running

    @property
    def stopping(self) -> bool:
        return self._stop_flag.is_set()

    @property
    def listen_summary(self) -> str:
        """当前监听地址的可读汇总，给界面显示用。"""
        cfg = self.cfg
        items = [f"TCP {cfg.bind_address}:{cfg.port}"]
        if cfg.enable_websocket:
            items.append(f"WS {cfg.bind_address}:{cfg.websocket_port}{cfg.websocket_path}")
        return "　|　".join(items)

    def _open_listener(self, port: int) -> Tuple[Optional[socket.socket], str]:
        """按与其他端口一致的策略开一个监听 socket。返回 (socket, 错误信息)。"""
        cfg = self.cfg
        family = socket.AF_INET6 if ":" in cfg.bind_address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        # Windows 上 SO_REUSEADDR 允许两个进程绑同一端口（语义与 Linux 不同），
        # 会让用户以为启动成功但实际收不到连接。这里只在非 Windows 上开启，
        # Windows 改用 SO_EXCLUSIVEADDRUSE 保证端口独占。
        try:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (OSError, AttributeError):
            pass
        try:
            sock.bind((cfg.bind_address, port))
        except OSError as exc:
            sock.close()
            return None, _explain_bind_error(cfg.bind_address, port, exc)
        try:
            sock.listen(max(1, int(cfg.backlog)))
        except OSError as exc:
            sock.close()
            return None, f"监听失败：{exc}"
        return sock, ""

    def start(self) -> Tuple[bool, str]:
        """开始监听（TCP，可选再开一个 WebSocket 端口）。返回 (是否成功, 说明)。"""
        if self._running:
            return False, "Broker 已在运行"
        cfg = self.cfg
        if not (1 <= cfg.port <= 65535):
            return False, "端口号必须在 1 ~ 65535 之间"
        if cfg.enable_websocket:
            if not (1 <= cfg.websocket_port <= 65535):
                return False, "WebSocket 端口号必须在 1 ~ 65535 之间"
            if cfg.websocket_port == cfg.port:
                return False, "WebSocket 端口不能与 MQTT 端口相同"
            if not (cfg.websocket_path or "").startswith("/"):
                return False, "WebSocket 路径必须以 / 开头，例如 /mqtt"
        if cfg.require_auth and not cfg.username:
            return False, "启用了认证但未设置用户名"

        sock, err = self._open_listener(cfg.port)
        if sock is None:
            return False, err

        ws_sock: Optional[socket.socket] = None
        if cfg.enable_websocket:
            ws_sock, ws_err = self._open_listener(cfg.websocket_port)
            if ws_sock is None:
                try:
                    sock.close()
                except OSError:
                    pass
                return False, f"WebSocket 端口不可用：{ws_err}"

        self._server_sock = sock
        self._ws_server_sock = ws_sock
        self._stop_flag.clear()
        self._running = True
        self.started_at = time.time()
        self._session_present = False

        self._accept_thread = threading.Thread(
            target=self._accept_loop, args=("TCP",), daemon=True,
            name="mqtt-broker-accept")
        self._accept_thread.start()
        if ws_sock is not None:
            self._ws_accept_thread = threading.Thread(
                target=self._accept_loop, args=("WebSocket",), daemon=True,
                name="mqtt-broker-ws-accept")
            self._ws_accept_thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True,
                                                name="mqtt-broker-monitor")
        self._monitor_thread.start()

        bind = cfg.bind_address
        scope = "所有网卡（局域网可访问）" if bind in ("0.0.0.0", "::") else f"仅 {bind}"
        if ws_sock is not None:
            self._log(
                f"Broker 已启动，监听 {bind}:{cfg.port}（TCP）"
                f" 与 {bind}:{cfg.websocket_port}{cfg.websocket_path}（WebSocket）（{scope}）",
                "success")
        else:
            self._log(f"Broker 已启动，监听 {bind}:{cfg.port}（{scope}）", "success")
        self._emit("state", "running")
        return True, f"已监听 {bind}:{cfg.port}" + (
            f" 与 WS :{cfg.websocket_port}" if ws_sock is not None else "")

    def stop(self) -> None:
        if not self._running and self._server_sock is None and self._ws_server_sock is None:
            return
        self._log("正在停止 Broker …", "warn")
        self._stop_flag.set()
        self._running = False

        for attr in ("_server_sock", "_ws_server_sock"):
            s = getattr(self, attr)
            setattr(self, attr, None)
            if s is not None:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    s.close()
                except OSError:
                    pass

        with self._lock:
            sessions = list(self.clients.values())
        for session in sessions:
            session.close(send_will=False)
        with self._lock:
            self.clients.clear()
            self._subs.clear()
            self._gone_ids.clear()

        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=1.0)
        if self._ws_accept_thread and self._ws_accept_thread.is_alive():
            self._ws_accept_thread.join(timeout=1.0)
        self._accept_thread = None
        self._ws_accept_thread = None
        self._monitor_thread = None

        self._log("Broker 已停止", "warn")
        self._emit("state", "stopped")

    # ================================================================== #
    # 收发循环
    # ================================================================== #
    def _accept_loop(self, transport: str = "TCP") -> None:
        while not self._stop_flag.is_set():
            sock = self._ws_server_sock if transport == "WebSocket" else self._server_sock
            if sock is None:
                break
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            if self._stop_flag.is_set():
                try:
                    conn.close()
                except OSError:
                    pass
                break

            # 连接数上限
            with self._lock:
                full = len(self.clients) >= self.cfg.max_connections
            if full:
                with self._lock:
                    self.stats["connections_rejected"] += 1
                self._log(
                    f"拒绝 {addr[0]}:{addr[1]}：已达最大连接数 {self.cfg.max_connections}",
                    "warn",
                )
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            with self._lock:
                self.stats["connections_total"] += 1
            label = "WebSocket " if transport == "WebSocket" else ""
            self._log(f"新连接来自 {addr[0]}:{addr[1]}（{label}传输）", "info")
            ClientSession(
                self, conn, addr, transport=transport,
                ws_path=self.cfg.websocket_path,
            ).start()

    def _monitor_loop(self) -> None:
        """周期性检查 Keep Alive 超时，并刷新界面统计。"""
        while not self._stop_flag.is_set():
            self._stop_flag.wait(2.0)
            if self._stop_flag.is_set():
                break
            now = time.time()
            with self._lock:
                sessions = list(self.clients.values())
            for session in sessions:
                if session.keepalive and now - session.last_seen > session.keepalive * 1.5:
                    self._log(
                        f"客户端 {session.client_id} Keep Alive 超时，断开连接", "warn", session,
                    )
                    session.close(send_will=True)
            self._emit("stats", "")

    # ================================================================== #
    # 认证与注册
    # ================================================================== #
    def _check_auth(self, username: str, password: str, remote: str) -> Tuple[bool, str]:
        cfg = self.cfg
        # 黑名单优先级最高，无论是否开启认证都生效
        if cfg.block_list and _ip_allowed(remote, cfg.block_list):
            return False, f"来源地址 {remote} 在黑名单中"
        if cfg.allow_list and not _ip_allowed(remote, cfg.allow_list):
            return False, f"来源地址 {remote} 不在允许列表中"
        if not cfg.require_auth:
            return True, ""
        if username != cfg.username:
            return False, f"用户名不匹配（收到「{username or '空'}」）"
        if password != cfg.password:
            return False, "密码错误"
        return True, ""

    def _register_client(self, session: ClientSession, client_id: str,
                         clean_session: bool) -> Tuple[bool, str]:
        with self._lock:
            if not clean_session and client_id in self._saved_sessions:
                self._session_present = True
            else:
                self._session_present = False

            existing = self.clients.get(client_id)
            if existing is not None and existing is not session:
                self._log(
                    f"客户端标识符 {client_id} 已存在，踢掉旧连接（来自 {existing.remote}）", "warn",
                )
                existing.close(send_will=False)
                self.clients.pop(client_id, None)

            if not clean_session:
                self._saved_sessions.setdefault(client_id, {})
            else:
                self._saved_sessions.pop(client_id, None)

            self.clients[client_id] = session
            self._gone_ids.discard(client_id)
            return True, ""

    def _take_session_subscriptions(self, client_id: str, clean_session: bool) -> Dict[str, int]:
        with self._lock:
            if clean_session:
                self._saved_sessions.pop(client_id, None)
                return {}
            saved = self._saved_sessions.get(client_id)
            return dict(saved) if saved else {}

    # ================================================================== #
    # 客户端生命周期回调
    # ================================================================== #
    def _on_client_ready(self, session: ClientSession) -> None:
        self._log(f"客户端 {session.client_id} 已连接（{session.remote}）", "success", session)
        self._emit("client_join", session.client_id,
                   client_id=session.client_id, remote=session.remote)

    def _on_client_gone(self, session: ClientSession) -> None:
        with self._lock:
            # 幂等：session.close() 可能被多处调用（本方法、stop()、投递失败），
            # 只有第一次才是真正的"离开"。
            if session.client_id in self.clients and self.clients.get(session.client_id) is session:
                self.clients.pop(session.client_id, None)
            else:
                already_gone = self._gone_ids
                if session.client_id in already_gone:
                    return
            self._gone_ids.add(session.client_id)

            # 清理该客户端的订阅
            dead = [f for f, subs in self._subs.items() if session.client_id in subs]
            for filt in dead:
                self._subs[filt].pop(session.client_id, None)
                if not self._subs[filt]:
                    self._subs.pop(filt, None)

            if not session.clean_session and session.subscriptions:
                self._saved_sessions[session.client_id] = dict(session.subscriptions)
            elif session.clean_session:
                self._saved_sessions.pop(session.client_id, None)

        # 触发遗嘱
        session.close(send_will=True)
        self._log(f"客户端 {session.client_id} 已断开", "info", session)
        self._emit("client_leave", session.client_id)

    def on_delivery_failure(self, session: ClientSession) -> None:
        """投递失败（对端已不可达）时由 :meth:`ClientSession.deliver` 回调。

        这里只做收尸，不重复发遗嘱——对端是"发送失败"而非"读循环退出"，
        遗嘱的裁决权交给 ``_read_loop`` 退出后的 :meth:`_on_client_gone`。
        """
        self._log(
            f"向客户端 {session.client_id} 投递失败，判定连接已失效并清理", "warn", session,
        )

    # ================================================================== #
    # 订阅表
    # ================================================================== #
    def _add_subscription(self, session: ClientSession, topic: str, qos: int) -> None:
        with self._lock:
            self._subs.setdefault(topic, {})[session.client_id] = qos

    def _remove_subscription(self, session: ClientSession, topic: str) -> None:
        with self._lock:
            subs = self._subs.get(topic)
            if subs is None:
                return
            subs.pop(session.client_id, None)
            if not subs:
                self._subs.pop(topic, None)

    # ================================================================== #
    # 消息路由
    # ================================================================== #
    def _publish(self, topic: str, payload: bytes, qos: int, retain: bool,
                 source: Optional[ClientSession]) -> None:
        self.stats["messages_in"] += 1
        self.stats["bytes_in"] += len(payload)
        self._tap_packet(topic, payload, qos, retain)

        if retain:
            with self._lock:
                if payload:
                    self._retained[topic] = (payload, qos)
                else:
                    # 空载荷的保留消息表示清除该主题的保留值
                    self._retained.pop(topic, None)
            self._log(
                f"更新保留消息 → {topic}（{len(payload)} 字节）" if payload else f"清除保留消息 → {topic}",
                "info", source,
            )

        # 找出所有匹配的订阅者，同一个客户端只投递一次。
        # 每个订阅者实际收到的 QoS = min(发布 QoS, 该订阅的 QoS)，这是 MQTT 规范要求。
        targets: Dict[str, int] = {}
        with self._lock:
            for filt, subs in self._subs.items():
                if not topic_matches(filt, topic):
                    continue
                for cid, sub_qos in subs.items():
                    effective = min(qos, sub_qos)
                    if cid not in targets or effective > targets[cid]:
                        targets[cid] = effective
            sessions = {cid: self.clients.get(cid) for cid in targets}

        delivered = 0
        for cid, effective_qos in targets.items():
            session = sessions.get(cid)
            if session is None or not session._alive:
                continue
            session.deliver(topic, payload, effective_qos, retain)
            delivered += 1

        self.stats["messages_out"] += delivered
        self.stats["bytes_out"] += len(payload) * delivered
        if delivered:
            self._log(
                f"路由 {topic} → {delivered} 个订阅者（发布 QoS {qos}）", "info", source,
            )

    def _tap_packet(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        """把发布内容交给外部解析钩子（若设置）。任何异常都不能影响转发。"""
        cb = self.on_packet
        if cb is None:
            return
        try:
            cb(topic, payload, qos, retain)
        except Exception:  # noqa: BLE001 - 界面回调异常不能影响协议处理
            pass

    def _send_retained(self, session: ClientSession, topic_filter: str, qos: int) -> None:
        with self._lock:
            items = list(self._retained.items())
        for topic, (payload, rqos) in items:
            if topic_matches(topic_filter, topic):
                session.deliver(topic, payload, min(rqos, qos), retain=True)
                self._log(f"推送保留消息 → {session.client_id}　{topic}", "info", session)

    # ================================================================== #
    # 对外查询
    # ================================================================== #
    def client_snapshot(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            sessions = list(self.clients.values())
            subs_copy = {f: dict(s) for f, s in self._subs.items()}
        rows = []
        for s in sessions:
            sub_count = sum(1 for subs in subs_copy.values() if s.client_id in subs)
            rows.append({
                "client_id": s.client_id,
                "remote": s.remote,
                "username": s.username or "—",
                "protocol": "3.1.1" if s.protocol_level == 4 else "3.1",
                "keepalive": s.keepalive,
                "clean": s.clean_session,
                "subscriptions": sub_count,
                "uptime": now - s.joined_at,
                "idle": now - s.last_seen,
                "will": s.will["topic"] if s.will else "",
            })
        rows.sort(key=lambda r: r["client_id"])
        return rows

    def subscription_snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [(f, dict(s)) for f, s in self._subs.items()]
        rows = []
        for filt, subs in sorted(items):
            for cid, qos in subs.items():
                rows.append({"topic": filt, "client_id": cid, "qos": qos})
        return rows

    def retained_snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._retained.items())
        rows = []
        for topic, (payload, qos) in sorted(items):
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = payload.hex(" ").upper()
            rows.append({"topic": topic, "qos": qos, "size": len(payload), "payload": text})
        return rows

    def uptime(self) -> float:
        return time.time() - self.started_at if self._running else 0.0

    def get_log_history(self) -> List[Dict[str, Any]]:
        return list(self._log_history)

    def clear_log(self) -> None:
        self._log_history.clear()

    # ================================================================== #
    # 事件与日志
    # ================================================================== #
    def _emit(self, kind: str, text: str = "", **extra: Any) -> None:
        """投递事件给界面。

        注意：这里必须绝对安全。早期版本直接把 ``**extra`` 塞进 ``BrokerEvent``，
        一旦传了不存在的字段就会抛 TypeError，而调用点都在协议处理路径上，
        结果是「一个界面事件把整条连接搞挂」。现在显式挑字段，多余的直接丢弃。
        """
        if self._on_event is None:
            return
        known = {"level", "client_id", "remote"}
        event = BrokerEvent(
            kind=kind,
            text=text,
            level=str(extra.get("level", "info")),
            client_id=str(extra.get("client_id", "")),
            remote=str(extra.get("remote", "")),
        )
        unknown = set(extra) - known
        if unknown:
            # 不抛异常，只记一笔，方便开发期发现问题
            self._log(f"[内部] 事件 {kind} 含未知字段 {sorted(unknown)}，已忽略", "warn")
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 - 界面回调异常不能影响协议处理
            pass

    def _log(self, text: str, level: str = "info",
             session: Optional[ClientSession] = None) -> None:
        entry = {
            "timestamp": time.time(),
            "text": text,
            "level": level,
            "client_id": session.client_id if session else "",
            "remote": session.remote if session else "",
        }
        self._log_history.append(entry)
        limit = max(100, int(self.cfg.log_limit))
        if len(self._log_history) > limit:
            del self._log_history[: len(self._log_history) - limit]
        self._emit("log", text, level=level,
                   client_id=entry["client_id"], remote=entry["remote"])


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #

def _ip_allowed(remote: str, allow_list: Any) -> bool:
    """检查来源 IP 是否在允许列表。

    支持三种写法：
        · 列表 / 元组，如 ``["192.168.1.5", "10.0.0."]``
        · 字符串，逗号或分号分隔，如 ``"192.168.1.5, 10.0.0."``
        · 网段，如 ``"192.168.1.0/24"``

    条目为前缀匹配，因此 ``192.168.1.`` 可匹配整个网段。
    """
    ip = remote.rsplit(":", 1)[0] if ":" in remote else remote
    if isinstance(allow_list, (list, tuple, set)):
        items = [str(x) for x in allow_list]
    else:
        items = str(allow_list).replace(";", ",").split(",")

    for item in items:
        item = item.strip()
        if not item:
            continue
        if "/" in item:
            if _ip_in_cidr(ip, item):
                return True
            continue
        if ip == item or ip.startswith(item.rstrip(".")):
            return True
    return False


def _ip_in_cidr(ip: str, cidr: str) -> bool:
    """判断 IPv4 地址是否落在 CIDR 网段内。"""
    try:
        network, _, bits = cidr.partition("/")
        mask_bits = int(bits)
        if not 0 <= mask_bits <= 32:
            return False

        def _to_int(text: str) -> int:
            parts = text.strip().split(".")
            if len(parts) != 4:
                raise ValueError(text)
            value = 0
            for part in parts:
                value = (value << 8) | (int(part) & 0xFF)
            return value

        mask = (0xFFFFFFFF << (32 - mask_bits)) & 0xFFFFFFFF if mask_bits else 0
        return (_to_int(ip) & mask) == (_to_int(network) & mask)
    except (ValueError, TypeError):
        return False


def _explain_bind_error(address: str, port: int, exc: OSError) -> str:
    """把 socket 绑定错误翻译成可操作的中文提示。"""
    code = getattr(exc, "errno", None)
    if code == 10048 or "10048" in str(exc) or "address already in use" in str(exc).lower():
        return (f"端口 {port} 已被占用。请换一个端口，"
                f"或先关闭占用该端口的程序（Windows 可用 netstat -ano | findstr :{port} 查看）。")
    if code == 10049 or "cannot assign requested address" in str(exc).lower():
        return f"地址 {address} 不是本机可用的地址，请改成 0.0.0.0 或本机具体 IP。"
    if code == 10013 or "permission" in str(exc).lower() or "access" in str(exc).lower():
        return f"没有权限监听 {address}:{port}，请换端口（1024 以下端口通常需要管理员权限）。"
    return f"启动失败：{exc}"


def local_ip_addresses() -> List[str]:
    """列出本机的所有 IPv4 地址，供其他主机连接时使用。"""
    addresses: List[str] = []
    try:
        # 通过连接一个外部地址来获取默认出口 IP（不会真的发包）
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addresses.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addresses and not ip.startswith("127."):
                addresses.append(ip)
    except OSError:
        pass

    if not addresses:
        addresses.append("127.0.0.1")
    return addresses
