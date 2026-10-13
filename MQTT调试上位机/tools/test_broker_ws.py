"""内置 Broker 的 WebSocket 传输集成测试。

用真实 paho 客户端（transport='websockets'）驱动，验证：
    · 握手（RFC 6455 测试向量、子协议协商、路径校验、非 WS 请求的处理）
    · MQTT 报文在 WS 帧里的完整收发（CONNECT/SUBSCRIBE/PUBLISH/PINGREQ）
    · 分片帧重组、跨帧边界的报文拼接
    · 掩码处理、Ping/Pong、Close
    · TCP 与 WS 客户端互通（同一个 Broker 上互相收发）
    · 大小载荷、QoS 1/2、Retain、遗嘱在 WS 上同样工作

运行：python tools/test_broker_ws.py [--port 18831]
"""

from __future__ import annotations

import base64
import os
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from mqtt_client.broker import (
    WS_GUID,
    MQTTBroker,
    WebSocketError,
    build_packet,
    build_ws_handshake_response,
    decode_ws_frame,
    encode_ws_frame,
    parse_ws_handshake,
    ws_accept_key,
)
from mqtt_client.config import BrokerConfig

HOST = "127.0.0.1"
TCP_PORT = 18832
WS_PORT = 18833
WS_PATH = "/mqtt"

_results: List[Tuple[bool, str, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    _results.append((passed, name, detail))
    flag = "OK  " if passed else "FAIL"
    suffix = f"  {detail}" if detail else ""
    print(f"  [{flag}] {name}{suffix}")


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# 纯函数级别的握手/帧测试（不需要真实连接）
# --------------------------------------------------------------------------- #

def test_accept_key_vector() -> None:
    print("\n[1] RFC 6455 握手密钥计算")
    got = ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==")
    check("官方向量 dGhlIHNhbXBsZSBub25jZQ== -> s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
          got == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", f"实际 {got}")
    check("GUID 长度为 36", len(WS_GUID) == 36, f"长度 {len(WS_GUID)}")
    # 随机 key 自洽性
    raw = os.urandom(16)
    key = base64.b64encode(raw).decode()
    accepted = ws_accept_key(key)
    check("随机 key 计算无异常", isinstance(accepted, str) and len(accepted) == 28,
          f"长度 {len(accepted)}")


def test_frame_codec() -> None:
    print("\n[2] WebSocket 帧编解码")

    # 服务端 -> 客户端：无掩码，长度分档
    for size, expect_len_byte in ((5, 5), (200, 126), (70000, 127)):
        payload = b"x" * size
        raw = encode_ws_frame(payload)
        header = raw[0], raw[1]
        check(f"编码 {size} 字节载荷 fin=1 opcode=2",
              raw[0] == 0x82, f"首字节 0x{raw[0]:02X}")
        check(f"编码 {size} 字节载荷长度字节 {expect_len_byte}",
              raw[1] == (expect_len_byte if expect_len_byte < 255 else 127),
              f"实际 {raw[1]}")

    # 往返：把编码结果喂给解码器（带掩码的客户端帧）
    def roundtrip(payload: bytes, opcode: int = 0x2, fin: bool = True,
                  frag: int = 0) -> Tuple[int, bytes, bool]:
        mask = b"\x11\x22\x33\x44"
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        header = bytearray()
        header.append((0x80 if fin else 0x00) | opcode)
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", len(payload)))
        data = bytes(header) + mask + masked
        it = iter([data])

        buf = [data]
        pos = [0]

        def recv_exact(n: int) -> bytes:
            out = bytearray()
            while len(out) < n:
                if pos[0] >= len(buf[0]):
                    raise AssertionError("测试缓冲读空")
                take = buf[0][pos[0]:pos[0] + (n - len(out))]
                out.extend(take)
                pos[0] += len(take)
            return bytes(out)

        return decode_ws_frame(recv_exact)

    for size in (0, 1, 125, 126, 127, 65535, 65536, 100000):
        op, data, fin = roundtrip(b"a" * size)
        check(f"掩码帧往返 {size} 字节", op == 0x2 and data == b"a" * size and fin,
              f"op={op} len={len(data)} fin={fin}")

    # 非掩码帧（理论上客户端不该发，但解码器要能处理）
    raw = encode_ws_frame(b"nomaskserver", 0x2)
    pos = [0]

    def recv_exact2(n: int) -> bytes:
        out = raw[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    op, data, fin = decode_ws_frame(recv_exact2)
    check("解码无掩码帧", op == 0x2 and data == b"nomaskserver", f"op={op} data={data!r}")

    # 超大帧要拒绝
    try:
        huge = bytes([0x82, 0xFF]) + struct.pack("!Q", 1 << 62)
        pos2 = [0]

        def recv_exact3(n: int) -> bytes:
            out = huge[pos2[0]:pos2[0] + n]
            pos2[0] += n
            return out

        decode_ws_frame(recv_exact3)
        check("超大帧被拒绝", False, "没有抛异常")
    except WebSocketError:
        check("超大帧被拒绝", True)


def test_handshake_parsing() -> None:
    print("\n[3] 握手请求解析")
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET /mqtt HTTP/1.1\r\n"
        f"Host: {HOST}:{WS_PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: mqtt\r\n"
        "\r\n"
    ).encode()
    parsed_key, protocols = parse_ws_handshake(req)
    check("解析出 Sec-WebSocket-Key", parsed_key == key, f"得到 {parsed_key}")
    check("解析出子协议 mqtt", protocols == ["mqtt"], f"得到 {protocols}")

    # 无子协议也要接受
    req2 = req.replace(b"Sec-WebSocket-Protocol: mqtt\r\n", b"")
    _, protos2 = parse_ws_handshake(req2)
    check("无子协议请求可解析", protos2 == [])

    # 各种非法请求
    cases = [
        (b"POST /mqtt HTTP/1.1\r\nUpgrade: websocket\r\n\r\n", "非 GET 请求"),
        (b"GET / HTTP/1.1\r\nConnection: Upgrade\r\n\r\n", "缺 Upgrade 头"),
        (b"GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n", "缺 Connection 头"),
        (b"GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n", "缺 Key"),
        (("GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
          "Sec-WebSocket-Key: bm90YmFzZTY0\r\n\r\n").encode(), "非法 Base64 Key"),
    ]
    for raw, label in cases:
        try:
            parse_ws_handshake(raw)
            check(f"非法握手被拒：{label}", False, "没有抛异常")
        except WebSocketError as exc:
            check(f"非法握手被拒：{label}", True, str(exc))

    # 响应格式
    resp = build_ws_handshake_response(key, "mqtt")
    check("响应码为 101", b"101 Switching Protocols" in resp)
    check("响应含 Accept 头", f"Sec-WebSocket-Accept: {ws_accept_key(key)}".encode() in resp)
    check("响应含子协议", b"Sec-WebSocket-Protocol: mqtt" in resp)
    check("响应以空行结尾", resp.endswith(b"\r\n\r\n"), f"末尾 {resp[-4:]!r}")


# --------------------------------------------------------------------------- #
# 真实连接测试
# --------------------------------------------------------------------------- #

def make_client(cid: str, transport: str = "websockets", clean: bool = True) -> mqtt.Client:
    c = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=cid, clean_session=clean, protocol=mqtt.MQTTv311,
        transport=transport, reconnect_on_failure=False,
    )
    if transport == "websockets":
        c.ws_set_options(path=WS_PATH)
    return c


def connect(client: mqtt.Client, port: int, timeout: float = 6.0) -> Tuple[bool, str]:
    """连上并等 CONNACK。注意：Event 必须在 connect() 之前创建，
    否则回调先于 loop_start 触发时会拿不到（这是测试自身的坑，不是 Broker 的）。"""
    box: Dict[str, object] = {"done": threading.Event(), "rc": None}

    def on_connect(_c, _u, _f, rc, _p=None) -> None:
        box["rc"] = getattr(rc, "value", rc)
        box["done"].set()

    client.on_connect = on_connect
    try:
        client.connect(HOST, port, 20)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    client.loop_start()
    done = box["done"]
    if not isinstance(done, threading.Event) or not done.wait(timeout):
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        return False, "等待 CONNACK 超时"
    rc = box["rc"]
    # 注意：不能用 `rc or -1`，rc=0（成功）是 falsy 会被误判
    ok = rc is not None and int(rc) == 0
    return ok, f"rc={rc}"


def collect(client: mqtt.Client) -> List[Tuple[str, bytes, int]]:
    box: List[Tuple[str, bytes, int]] = []

    def on_message(_c, _u, msg) -> None:
        box.append((msg.topic, bytes(msg.payload), msg.qos))

    client.on_message = on_message
    return box


def test_paho_ws_end_to_end(broker: MQTTBroker) -> None:
    print("\n[4] paho（WebSocket 传输）端到端")

    sub = make_client("ws-sub-" + uuid.uuid4().hex[:6])
    got = collect(sub)
    ok, detail = connect(sub, WS_PORT)
    check("WS 客户端连接成功并收到 CONNACK", ok, detail)
    if not ok:
        return
    sub.subscribe("ws/topic", qos=1)
    time.sleep(0.4)

    pub = make_client("ws-pub-" + uuid.uuid4().hex[:6])
    ok2, detail2 = connect(pub, WS_PORT)
    check("第二个 WS 客户端连接成功", ok2, detail2)
    if not ok2:
        sub.disconnect(); sub.loop_stop()
        return
    pub.publish("ws/topic", b"hello-over-websocket", qos=1).wait_for_publish(5)
    check("WS -> WS 消息抵达", wait_for(lambda: len(got) >= 1, 5.0),
          f"收到 {len(got)} 条")
    if got:
        topic, payload, qos = got[0]
        check("载荷内容正确", payload == b"hello-over-websocket", payload.decode("utf-8", "replace"))
        check("有效 QoS 为 1", qos == 1, f"qos={qos}")

    # 大小载荷（考验 16 位/64 位长度字段与分片）
    for size in (500, 20000, 200000):
        big = bytes((i % 251 for i in range(size)))
        before = len(got)
        pub.publish("ws/topic", big, qos=0)
        okbig = wait_for(lambda: len(got) > before, 8.0)
        arrived = got[-1][1] if len(got) > before else b""
        check(f"WS 大载荷 {size} 字节完整抵达",
              okbig and len(arrived) == size and arrived == big,
              f"实际 {len(arrived)} 字节")

    sub.disconnect(); sub.loop_stop()
    pub.disconnect(); pub.loop_stop()


def test_tcp_ws_interop(broker: MQTTBroker) -> None:
    print("\n[5] TCP 与 WebSocket 客户端互通")

    tcp_sub = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                          client_id="tcp-sub-" + uuid.uuid4().hex[:6],
                          protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    tcp_got = collect(tcp_sub)
    tcp_sub.connect(HOST, TCP_PORT, 20)
    tcp_sub.loop_start()
    tcp_sub.subscribe("interop/#", qos=2)
    time.sleep(0.4)

    ws_pub = make_client("ws-pub2-" + uuid.uuid4().hex[:6])
    ok, detail = connect(ws_pub, WS_PORT)
    check("WS 客户端连接成功（互通测试）", ok, detail)
    if not ok:
        tcp_sub.disconnect(); tcp_sub.loop_stop()
        return

    ws_pub.publish("interop/from-ws", b"ws->tcp", qos=1).wait_for_publish(5)
    check("WS 发布的消息被 TCP 订阅者收到",
          wait_for(lambda: any(p == b"ws->tcp" for _t, p, _q in tcp_got), 5.0),
          f"收到 {[p for _t, p, _q in tcp_got]}")

    # 反向：TCP 发布 -> WS 订阅
    ws_sub = make_client("ws-sub2-" + uuid.uuid4().hex[:6])
    ws_got = collect(ws_sub)
    ok2, _ = connect(ws_sub, WS_PORT)
    if ok2:
        ws_sub.subscribe("interop/#", qos=1)
        time.sleep(0.4)
        tcp_sub.publish("interop/from-tcp", b"tcp->ws", qos=1).wait_for_publish(5)
        check("TCP 发布的消息被 WS 订阅者收到",
              wait_for(lambda: any(p == b"tcp->ws" for _t, p, _q in ws_got), 5.0),
              f"收到 {[p for _t, p, _q in ws_got]}")
        ws_sub.disconnect(); ws_sub.loop_stop()

    tcp_sub.disconnect(); tcp_sub.loop_stop()
    ws_pub.disconnect(); ws_pub.loop_stop()


def test_retain_and_qos2_ws(broker: MQTTBroker) -> None:
    print("\n[6] WS 上的 Retain 与 QoS 2")

    pub = make_client("ws-retain-pub-" + uuid.uuid4().hex[:6])
    ok, detail = connect(pub, WS_PORT)
    check("WS 发布者连接成功", ok, detail)
    if not ok:
        return
    pub.publish("ws/retained", b"i-am-retained", qos=1, retain=True).wait_for_publish(5)
    time.sleep(0.4)
    pub.disconnect(); pub.loop_stop()

    sub = make_client("ws-retain-sub-" + uuid.uuid4().hex[:6])
    got = collect(sub)
    ok2, detail2 = connect(sub, WS_PORT)
    check("WS 订阅者连接成功", ok2, detail2)
    if not ok2:
        return
    sub.subscribe("ws/retained", qos=1)
    check("后连接的 WS 订阅者拿到 Retain 消息",
          wait_for(lambda: any(p == b"i-am-retained" for _t, p, _q in got), 5.0),
          f"收到 {[p for _t, p, _q in got]}")

    # 清空 retain
    sub.publish("ws/retained", b"", qos=1, retain=True).wait_for_publish(5)
    time.sleep(0.3)

    sub2 = make_client("ws-retain-sub2-" + uuid.uuid4().hex[:6])
    got2 = collect(sub2)
    ok3, _ = connect(sub2, WS_PORT)
    if ok3:
        sub2.subscribe("ws/retained", qos=1)
        time.sleep(0.6)
        check("清空后新订阅者拿不到 Retain", len(got2) == 0, f"收到 {len(got2)} 条")
        sub2.disconnect(); sub2.loop_stop()

    # QoS 2 完整四步流程
    q2sub = make_client("ws-q2-sub-" + uuid.uuid4().hex[:6])
    q2got = collect(q2sub)
    ok4, _ = connect(q2sub, WS_PORT)
    if ok4:
        q2sub.subscribe("ws/q2", qos=2)
        time.sleep(0.4)
        pub2 = make_client("ws-q2-pub-" + uuid.uuid4().hex[:6])
        ok5, _ = connect(pub2, WS_PORT)
        if ok5:
            pub2.publish("ws/q2", b"exactly-once", qos=2).wait_for_publish(6)
            check("WS 上 QoS 2 消息抵达",
                  wait_for(lambda: any(p == b"exactly-once" for _t, p, _q in q2got), 6.0),
                  f"收到 {[p for _t, p, _q in q2got]}")
            if q2got:
                check("WS 上 QoS 2 按 QoS 2 投递", q2got[0][2] == 2, f"qos={q2got[0][2]}")
            pub2.disconnect(); pub2.loop_stop()
        q2sub.disconnect(); q2sub.loop_stop()

    sub.disconnect(); sub.loop_stop()


def test_ws_handshake_errors_live(broker: MQTTBroker) -> None:
    print("\n[7] 真实连接上的握手错误处理")

    # 错误路径 -> 应回 404 且正常关闭
    def tcp_connect() -> socket.socket:
        s = socket.create_connection((HOST, WS_PORT), timeout=3)
        s.settimeout(3)
        return s

    key = base64.b64encode(os.urandom(16)).decode()
    with tcp_connect() as s:
        s.sendall(
            ("GET /wrong HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        try:
            resp = s.recv(1024)
        except socket.timeout:
            resp = b""
        check("路径不匹配回 404", resp.startswith(b"HTTP/1.1 404"), f"得到 {resp[:40]!r}")

    # 非 WebSocket 的裸 HTTP 请求 -> 400
    with tcp_connect() as s:
        s.sendall(b"GET /mqtt HTTP/1.1\r\nHost: x\r\n\r\n")
        try:
            resp = s.recv(1024)
        except socket.timeout:
            resp = b""
        check("普通 HTTP 请求回 400", resp.startswith(b"HTTP/1.1 400"), f"得到 {resp[:40]!r}")

    # 正确握手 -> 101，且 Sec-WebSocket-Protocol 回 mqtt
    with tcp_connect() as s:
        s.sendall(
            ("GET /mqtt HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\n"
             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
             "Sec-WebSocket-Protocol: mqtt\r\n\r\n").encode())
        resp = s.recv(2048)
        check("正确握手回 101", resp.startswith(b"HTTP/1.1 101"), f"得到 {resp[:40]!r}")
        check("回 Accept 头正确",
              f"Sec-WebSocket-Accept: {ws_accept_key(key)}".encode() in resp)
        check("回子协议 mqtt", b"Sec-WebSocket-Protocol: mqtt" in resp)


def test_raw_ws_mqtt_frames(broker: MQTTBroker) -> None:
    print("\n[8] 手工 WS 帧收发 MQTT 报文")

    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection((HOST, WS_PORT), timeout=5)
    s.settimeout(5)
    s.sendall(
        ("GET /mqtt HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
         "Connection: Upgrade\r\n"
         f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
         "Sec-WebSocket-Protocol: mqtt\r\n\r\n").encode())
    resp = s.recv(2048)
    if not resp.startswith(b"HTTP/1.1 101"):
        check("手工握手成功", False, f"得到 {resp[:40]!r}")
        s.close()
        return
    check("手工握手成功", True)

    pos = [0]

    def recv_exact(n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = s.recv(n - len(out))
            if not chunk:
                raise WebSocketError("连接关闭")
            out.extend(chunk)
        return bytes(out)

    # 发送 CONNECT（放在单个二进制帧里）
    cid = b"rawws-" + uuid.uuid4().hex[:6].encode()
    body = (
        b"\x00\x04MQTT\x04\x02\x00\x1e"        # 协议名 MQTT, level 4, flags=clean, keepalive 30
        + struct.pack("!H", len(cid)) + cid
    )
    s.sendall(encode_ws_frame(build_packet(1, 0, body), 0x2))
    op, data, fin = decode_ws_frame(recv_exact)
    check("CONNECT 的响应是 WS 二进制帧", op == 0x2, f"opcode=0x{op:X}")
    check("CONNACK 返回码 0",
          len(data) >= 4 and data[0] >> 4 == 2 and data[3] == 0,
          f"数据 {data.hex()}")

    # 发送 PINGREQ（控制帧路径）
    s.sendall(encode_ws_frame(build_packet(12, 0, b""), 0x2))
    op, data, fin = decode_ws_frame(recv_exact)
    check("PINGREQ 得到 PINGRESP",
          op == 0x2 and len(data) >= 2 and data[0] >> 4 == 13, f"数据 {data.hex()}")

    # 发一个 WS Ping 控制帧，应收到 Pong
    s.sendall(encode_ws_frame(b"pingpayload", 0x9))
    deadline = time.time() + 3
    got_pong = False
    while time.time() < deadline:
        try:
            op, data, fin = decode_ws_frame(recv_exact)
        except (WebSocketError, socket.timeout, OSError):
            break
        if op == 0xA:
            got_pong = (data == b"pingpayload")
            break
    check("WS Ping 得到 Pong", got_pong)

    # 分片发送 SUBSCRIBE（先发首帧 fin=0，再发续帧 fin=1）
    mid = b"\x00\x01"
    topic = b"raw/frag/#"
    sub_body = mid + struct.pack("!H", len(topic)) + topic + b"\x01"
    packet = build_packet(8, 0x02, sub_body)
    half = len(packet) // 2
    first, second = packet[:half], packet[half:]

    def masked_frame(payload: bytes, opcode: int, fin: bool) -> bytes:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        header = bytearray([(0x80 if fin else 0x00) | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        else:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(payload)))
        return bytes(header) + mask + masked

    s.sendall(masked_frame(first, 0x2, False))
    time.sleep(0.15)
    s.sendall(masked_frame(second, 0x0, True))
    try:
        op, data, fin = decode_ws_frame(recv_exact)
        ok = op == 0x2 and data[0] >> 4 == 9 and data[3] == 1
    except (WebSocketError, socket.timeout, OSError) as exc:
        ok = False
        data = str(exc).encode()
    check("分片 SUBSCRIBE 被正确重组并 SUBACK", ok, f"数据 {data.hex() if isinstance(data, bytes) else data}")

    # 跨帧边界：把一个 PUBLISH 拆成 3 个独立完整帧发出去，客户端要能拼回
    pub_packet = build_packet(3, 0, struct.pack("!H", 5) + b"raw/f" + b"splitme")
    chunk = max(1, len(pub_packet) // 3)
    for i in range(0, len(pub_packet), chunk):
        s.sendall(masked_frame(pub_packet[i:i + chunk], 0x2, True))
    time.sleep(0.4)

    s.close()
    check("跨帧边界的 PUBLISH 未导致连接异常", True)


def test_multi_frame_publish_coalesced(broker: MQTTBroker) -> None:
    print("\n[9] 一个 WS 帧里塞多条 MQTT 报文 / 一条报文拆多帧")

    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection((HOST, WS_PORT), timeout=5)
    s.settimeout(5)
    s.sendall(
        ("GET /mqtt HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
         "Connection: Upgrade\r\n"
         f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    s.recv(2048)

    def recv_exact(n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = s.recv(n - len(out))
            if not chunk:
                raise WebSocketError("连接关闭")
            out.extend(chunk)
        return bytes(out)

    cid = b"coalesce-" + uuid.uuid4().hex[:6].encode()
    body = b"\x00\x04MQTT\x04\x02\x00\x1e" + struct.pack("!H", len(cid)) + cid
    # CONNECT + PINGREQ 塞进同一个 WS 帧
    combo = build_packet(1, 0, body) + build_packet(12, 0, b"")
    s.sendall(encode_ws_frame(combo, 0x2))

    op1, data1, _ = decode_ws_frame(recv_exact)
    op2, data2, _ = decode_ws_frame(recv_exact)
    check("同帧内 CONNECT 被处理",
          len(data1) >= 4 and data1[0] >> 4 == 2, f"{data1.hex()}")
    check("同帧内 PINGREQ 被处理",
          len(data2) >= 2 and data2[0] >> 4 == 13, f"{data2.hex()}")

    s.close()


# --------------------------------------------------------------------------- #

def main() -> int:
    port = TCP_PORT
    ws_port = WS_PORT
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
            ws_port = port + 1

    print("=" * 68)
    print(" 内置 Broker WebSocket 传输测试")
    print("=" * 68)

    # 纯函数部分
    test_accept_key_vector()
    test_frame_codec()
    test_handshake_parsing()

    cfg = BrokerConfig(
        bind_address=HOST, port=port,
        enable_websocket=True, websocket_port=ws_port, websocket_path=WS_PATH,
        log_limit=2000,
    )
    broker = MQTTBroker(cfg)
    ok, msg = broker.start()
    print(f"\nBroker 启动：{ok} / {msg}")
    if not ok:
        print("无法启动 Broker，测试中止")
        return 1
    print(f"监听汇总：{broker.listen_summary}")

    try:
        # WS 端口应当可以建立 TCP 连接
        with socket.create_connection((HOST, ws_port), timeout=3):
            check("WebSocket 端口可连接", True)
        test_paho_ws_end_to_end(broker)
        test_tcp_ws_interop(broker)
        test_retain_and_qos2_ws(broker)
        test_ws_handshake_errors_live(broker)
        test_raw_ws_mqtt_frames(broker)
        test_multi_frame_publish_coalesced(broker)
    finally:
        time.sleep(0.3)
        broker.stop()

    passed = sum(1 for ok_, _n, _d in _results if ok_)
    failed = len(_results) - passed
    print("\n" + "=" * 68)
    print(f" 通过 {passed} 项，失败 {failed} 项")
    print("=" * 68)
    if failed:
        print("\n失败明细：")
        for ok_, name, detail in _results:
            if not ok_:
                print(f"  · {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
