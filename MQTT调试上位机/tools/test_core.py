"""核心逻辑单元测试：主题匹配、载荷编解码、规则引擎、配置持久化。"""
import base64, json, os, struct, sys, tempfile, time
sys.path.insert(0, '.')

from mqtt_client.config import (AppConfig, ConnectionConfig, ScriptRule, Subscription,
                               WillConfig, qos_from_label, qos_label, save, load)
from mqtt_client.core import (topic_matches, is_valid_topic_filter, encode_payload,
                              decode_payload, pretty_json, reason_text)
from mqtt_client.script import match_condition, render_template, ScriptEngine, PeriodicTask

fails = []
def check(name, got, want):
    if got == want:
        print(f"  [OK] {name}")
    else:
        fails.append(name)
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")

print("== 主题匹配 ==")
check("精确匹配", topic_matches("a/b/c", "a/b/c"), True)
check("单层+", topic_matches("a/+/c", "a/b/c"), True)
check("+不跨层", topic_matches("a/+", "a/b/c"), False)
check("多层#", topic_matches("a/#", "a/b/c"), True)
check("# 尾部", topic_matches("a/#", "a/b"), True)
check("a/# 匹配 a 本身", topic_matches("a/#", "a"), True)
check("a/# 匹配 a/", topic_matches("a/#", "a/"), True)
check("# 匹配一切", topic_matches("#", "x/y/z"), True)
check("$SYS不被#匹配", topic_matches("#", "$SYS/x"), False)
check("$SYS显式订阅", topic_matches("$SYS/#", "$SYS/x"), True)
check("共享订阅", topic_matches("$share/g/a/#", "$share/g/a/b"), True)
check("不同分支不匹配", topic_matches("a/b", "a/c"), False)

print("== 主题过滤器校验 ==")
check("空主题", is_valid_topic_filter("")[0], False)
check("合法#", is_valid_topic_filter("a/#")[0], True)
check("#不在末尾", is_valid_topic_filter("a/#/b")[0], False)
check("#非独占", is_valid_topic_filter("a/b#")[0], False)
check("合法+", is_valid_topic_filter("a/+/b")[0], True)
check("+非独占", is_valid_topic_filter("a/b+")[0], False)

print("== 载荷编解码 ==")
check("HEX空格", encode_payload("01 02 FF", "HEX"), b"\x01\x02\xff")
check("HEX冒号", encode_payload("01:02:ff", "HEX"), b"\x01\x02\xff")
check("HEX 0x前缀", encode_payload("0x01,0x02", "HEX"), b"\x01\x02")
check("Base64", encode_payload("aGVsbG8=", "Base64"), b"hello")
check("JSON压缩", encode_payload('{ "a" : 1 }', "JSON"), b'{"a":1}')
check("文本UTF8", encode_payload("中文", "文本"), "中文".encode())
check("空载荷", decode_payload(b""), "")
check("UTF8解码", decode_payload("你好".encode()), "你好")
check("JSON格式化", pretty_json('{"a":1}'), '{\n  "a": 1\n}')
check("非JSON返回None", pretty_json("hello"), None)

print("== QoS 辅助 ==")
check("qos_from_label", qos_from_label("1 - 至少一次"), 1)
check("qos_from_label非法", qos_from_label("abc"), 0)
check("qos_label", qos_label(2), "2 - 恰好一次")

print("== 原因码 ==")
check("成功", reason_text(0), "成功(0)")
check("认证失败", reason_text(134), "用户名或密码错误(134)")
check("None", reason_text(None), "")

print("== 规则匹配 ==")
r = ScriptRule(topic_filter="dev/+/data", match_mode="contains", match_value="on")
check("包含命中", match_condition(r, "dev/a/data", '{"s":"on"}')[0], True)
check("主题不匹配", match_condition(r, "other/x", "on")[0], False)
r2 = ScriptRule(topic_filter="#", match_mode="regex", match_value=r"temp=(\d+)")
check("正则命中", match_condition(r2, "a/b", "temp=25")[0], True)
check("正则捕获组", match_condition(r2, "a/b", "temp=25")[1], "temp=25")
r3 = ScriptRule(topic_filter="#", match_mode="equals", match_value="on")
check("等于", match_condition(r3, "a", "on")[0], True)
check("不等于", match_condition(r3, "a", "on!")[0], False)
r4 = ScriptRule(topic_filter="#", match_mode="any", match_value="")
check("任意内容", match_condition(r4, "a", "x")[0], True)

print("== 模板占位符 ==")
out = render_template("{topic} -> {topic2} | {payload} | {match}", "a/b", "hello", "hel")
check("占位符渲染", out, "a/b -> b | hello | hel")
check("未知占位符保留", render_template("{nope}", "a", "b"), "{nope}")
check("{ts} 是数字", render_template("{ts}", "a", "b").isdigit(), True)

print("== 规则引擎 ==")
sent = []
eng = ScriptEngine(publish_func=lambda t, p, q, r: (sent.append((t, p.decode(), q, r)), True)[1],
                   log_func=lambda msg, lv: None)
eng.set_rules([ScriptRule(name="应答", topic_filter="dev/+/status", match_mode="equals",
                          match_value="on", action="publish", reply_topic="{topic}/ack",
                          payload='{"ack":1}', qos=1)])
hits = eng.on_message("dev/light1/status", "on")
check("命中1条", len(hits), 1)
time.sleep(0.15)
check("已发布", len(sent), 1)
check("主题正确", sent[0][0], "dev/light1/status/ack")
check("内容正确", sent[0][1], '{"ack":1}')
check("QoS正确", sent[0][2], 1)
check("未命中的消息", len(eng.on_message("dev/light1/status", "off")), 0)
eng.enabled = False
check("引擎关闭后不触发", len(eng.on_message("dev/light1/status", "on")), 0)

print("== 配置持久化 ==")
cfg = AppConfig.default()
cfg.connections[0].host = "test.example.com"
cfg.connections[0].port = 1884
cfg.connections[0].will = WillConfig(enabled=True, topic="w/t", payload="bye", qos=1)
cfg.subscriptions.append(Subscription("x/y", 1, alias="测试"))
cfg.scripts.append(ScriptRule(name="r1", topic_filter="p/#"))
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "c.json")
    save(cfg, p)
    back = load(p)
    check("host 往返", back.connections[0].host, "test.example.com")
    check("port 往返", back.connections[0].port, 1884)
    check("遗嘱往返", back.connections[0].will.topic, "w/t")
    check("遗嘱QoS", back.connections[0].will.qos, 1)
    check("订阅往返", back.subscriptions[-1].topic, "x/y")
    check("订阅别名", back.subscriptions[-1].alias, "测试")
    check("规则往返", back.scripts[-1].name, "r1")
    # 损坏文件回退
    broken = os.path.join(d, "broken.json")
    open(broken, "w").write("{ not json")
    check("损坏配置回退默认", load(broken).connections[0].host, "broker.emqx.io")

print("== 周期任务载荷生成 ==")
from mqtt_client.script import PeriodicPublisher
check("自增值", PeriodicPublisher._build_payload(PeriodicTask(topic="a", template_mode="自增"), 5.0), "5.0")
tp2 = PeriodicTask(topic="a", payload='{"v":0}', template_mode="JSON自增")
res = json.loads(PeriodicPublisher._build_payload(tp2, 3.0))
check("JSON自增 v", res["v"], 3.0)
check("JSON自增含ts", "ts" in res, True)
check("时间戳模式是数字", PeriodicPublisher._build_payload(
    PeriodicTask(topic="a", template_mode="时间戳"), 0).isdigit(), True)
check("文本原样", PeriodicPublisher._build_payload(
    PeriodicTask(topic="a", payload="hello", template_mode="文本"), 0), "hello")

print("== Broker 配置 ==")
from mqtt_client.config import BrokerConfig
bc = BrokerConfig(port=1885, require_auth=True, username="u", password="p",
                  allow_list=["192.168.1.0/24"], max_qos=1)
bc2 = BrokerConfig.from_dict(bc.to_dict())
check("端口往返", bc2.port, 1885)
check("认证往返", bc2.require_auth, True)
check("白名单往返", bc2.allow_list, ["192.168.1.0/24"])
check("max_qos 往返", bc2.max_qos, 1)
check("端口容错", BrokerConfig.from_dict({"port": "abc"}).port, 1884)
check("max_qos 越界收敛", BrokerConfig.from_dict({"max_qos": 9}).max_qos, 2)
check("白名单类型容错", BrokerConfig.from_dict({"allow_list": "x"}).allow_list, [])

print("== Broker 配置向后兼容 ==")
legacy = {"version": 1, "connections": [{"name": "旧配置"}]}
acc = AppConfig.from_dict(legacy)
check("旧配置无 broker 字段可用默认", acc.broker.port, 1884)
check("默认监听地址", acc.broker.bind_address, "0.0.0.0")
check("旧连接仍可读", acc.connections[0].name, "旧配置")

print("== Broker 报文编解码 ==")
from mqtt_client.broker import (encode_remaining_length, decode_remaining_length,
                                encode_string, decode_string, build_packet,
                                _validate_filter, _ip_allowed, PACKET_NAMES, PUBLISH)
for n, want in [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"),
                (16383, b"\xff\x7f"), (16384, b"\x80\x80\x01")]:
    check(f"可变长度编码 {n}", encode_remaining_length(n), want)
    it = iter(want)
    check(f"可变长度解码 {n}", decode_remaining_length(lambda: bytes([next(it)])), n)
check("字符串编码", encode_string("ab"), b"\x00\x02ab")
check("字符串往返", decode_string(b"\x00\x02ab", 0), ("ab", 4))
check("中文往返", decode_string(encode_string("中文"), 0)[0], "中文")
pkt = build_packet(PUBLISH, 0, b"\x00\x01a")
check("报文类型字节", pkt[0], 0x30)
check("报文名表", PACKET_NAMES[PUBLISH], "PUBLISH")

print("== Broker 过滤器与 ACL ==")
check("合法 #", _validate_filter("a/#"), (True, ""))
check("非法 # 位置", _validate_filter("a/#/b")[0], False)
check("非法 + 非独占", _validate_filter("a/b+")[0], False)
check("空过滤器", _validate_filter("")[0], False)
check("IP 精确匹配", _ip_allowed("10.0.0.5:1234", ["10.0.0.5"]), True)
check("IP 前缀匹配", _ip_allowed("10.0.0.5:1234", ["10.0.0."]), True)
check("IP 网段匹配", _ip_allowed("192.168.1.77:9", ["192.168.1.0/24"]), True)
check("IP 网段不匹配", _ip_allowed("192.168.2.77:9", ["192.168.1.0/24"]), False)
check("IP 字符串写法", _ip_allowed("10.1.2.3:5", "10.1.2.3, 10.9."), True)
check("IP 不匹配", _ip_allowed("8.8.8.8:5", ["10.0.0."]), False)

print("== Broker WebSocket 配置 ==")
wbc = BrokerConfig(enable_websocket=True, websocket_port=8083, websocket_path="/mqtt")
wbc2 = BrokerConfig.from_dict(wbc.to_dict())
check("WS 开关往返", wbc2.enable_websocket, True)
check("WS 端口往返", wbc2.websocket_port, 8083)
check("WS 路径往返", wbc2.websocket_path, "/mqtt")
check("WS 端口默认值", BrokerConfig().websocket_port, 8083)
check("WS 默认关闭", BrokerConfig().enable_websocket, False)
check("WS 端口容错", BrokerConfig.from_dict({"websocket_port": "x"}).websocket_port, 8083)
check("WS 端口越界收敛", BrokerConfig.from_dict({"websocket_port": 99999}).websocket_port, 8083)
check("WS 路径容错（无斜杠）",
      BrokerConfig.from_dict({"websocket_path": "mqtt"}).websocket_path, "/mqtt")
check("WS 路径容错（非字符串）",
      BrokerConfig.from_dict({"websocket_path": 123}).websocket_path, "/mqtt")
check("旧配置无 WS 字段仍可用", BrokerConfig.from_dict({"port": 1884}).enable_websocket, False)

print("== Broker WebSocket 帧编解码 ==")
from mqtt_client.broker import (ws_accept_key, encode_ws_frame, decode_ws_frame,
                                parse_ws_handshake, build_ws_handshake_response,
                                build_ws_http_error, WS_GUID, WebSocketError,
                                WS_OP_BINARY, WS_OP_CLOSE, WS_OP_PING)

check("GUID 长度为 36", len(WS_GUID), 36)
check("RFC 6455 官方测试向量",
      ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

# 编码长度分档
check("短载荷长度字节", encode_ws_frame(b"x" * 10)[1], 10)
check("126 边界用 16 位长度", encode_ws_frame(b"x" * 126)[1], 126)
check("64K 边界用 64 位长度", encode_ws_frame(b"x" * 70000)[1], 127)
check("首字节 fin+opcode", encode_ws_frame(b"x")[0], 0x82)
check("文本帧 opcode", encode_ws_frame(b"x", 0x1)[0], 0x81)

# 解码掩码帧往返
def _ws_roundtrip(payload, opcode=WS_OP_BINARY, fin=True, masked=True):
    mask = b"\x0a\x0b\x0c\x0d"
    body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if masked else payload
    head = bytearray()
    head.append((0x80 if fin else 0) | opcode)
    n = len(payload)
    if n < 126:
        head.append((0x80 if masked else 0) | n)
    elif n < 65536:
        head.append((0x80 if masked else 0) | 126)
        head.extend(struct.pack("!H", n))
    else:
        head.append((0x80 if masked else 0) | 127)
        head.extend(struct.pack("!Q", n))
    data = bytes(head) + (mask if masked else b"") + body
    pos = [0]

    def rd(k):
        out = data[pos[0]:pos[0] + k]
        pos[0] += k
        return out
    return decode_ws_frame(rd)

for size in (0, 1, 125, 126, 65535, 65536):
    op, data, fin = _ws_roundtrip(b"z" * size)
    check(f"掩码帧往返 {size} 字节",
          (op, len(data), fin), (WS_OP_BINARY, size, True))
op, data, fin = _ws_roundtrip(b"plain", masked=False)
check("无掩码帧解码", (op, data), (WS_OP_BINARY, b"plain"))

# 握手解析
_wskey = base64.b64encode(os.urandom(16)).decode()
_req = (f"GET /mqtt HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {_wskey}\r\n"
        f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: mqtt\r\n\r\n").encode()
check("握手解析 key", parse_ws_handshake(_req)[0], _wskey)
check("握手解析子协议", parse_ws_handshake(_req)[1], ["mqtt"])
check("握手大小写不敏感（paho 用 Sec-Websocket-*）",
      parse_ws_handshake(_req.replace(b"Sec-WebSocket-Protocol", b"Sec-Websocket-Protocol"))[1],
      ["mqtt"])

for _raw, _label in [
    (b"POST / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n", "非 GET"),
    (b"GET / HTTP/1.1\r\nConnection: Upgrade\r\n\r\n", "缺 Upgrade"),
    (b"GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n", "缺 Connection"),
    (b"GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n", "缺 Key"),
    (b"GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
     b"Sec-WebSocket-Key: bm90YmFzZTY0\r\n\r\n", "非法 Key"),
]:
    try:
        parse_ws_handshake(_raw)
        check(f"非法握手被拒：{_label}", False, True)
    except WebSocketError:
        check(f"非法握手被拒：{_label}", True, True)

_resp = build_ws_handshake_response(_wskey, "mqtt")
check("响应含 101", b"101 Switching Protocols" in _resp, True)
check("响应含 Accept", f"Sec-WebSocket-Accept: {ws_accept_key(_wskey)}".encode() in _resp, True)
check("响应含子协议", b"Sec-WebSocket-Protocol: mqtt" in _resp, True)
check("响应末尾双 CRLF", _resp.endswith(b"\r\n\r\n"), True)

# 错误响应必须能编码（中文只能放 body，不能放状态行）
_err = build_ws_http_error(404, "Not Found", "路径 /x 不存在，本 Broker 的路径是 /mqtt")
check("错误响应含状态码", _err.startswith(b"HTTP/1.1 404 Not Found"), True)
check("错误响应 body 为 UTF-8 中文", "不存在".encode("utf-8") in _err, True)
check("含中文的 reason 也被转成 ASCII 状态行",
      build_ws_http_error(400, "路径错误", "x").split(b"\r\n")[0].decode("ascii").startswith(
          "HTTP/1.1 400"), True)

print("== 连接地址解析 ==")
from mqtt_client.config import parse_broker_url, normalize_broker_url, ConnectionConfig
check("用户报的地址可解析",
      parse_broker_url("ws://10.99.79.54:8083/mqtt"),
      {"transport": "WebSocket", "use_tls": False, "host": "10.99.79.54",
       "port": 8083, "ws_path": "/mqtt"})
check("wss 推断 TLS", parse_broker_url("wss://h:8084/mqtt")["use_tls"], True)
check("mqtts 推断 TLS", parse_broker_url("mqtts://h:8883")["use_tls"], True)
check("ws 默认端口 8083", parse_broker_url("ws://h/mqtt")["port"], 8083)
check("mqtt 默认端口 1883", parse_broker_url("mqtt://h")["port"], 1883)
check("裸 IP:端口", parse_broker_url("10.0.0.1:1883"), {"host": "10.0.0.1", "port": 1883})
check("裸主机名", parse_broker_url("broker.emqx.io"), {"host": "broker.emqx.io"})
check("未知协议不猜", parse_broker_url("ftp://h"), {})
check("空串", parse_broker_url(""), {})
check("非法端口不猜", parse_broker_url("ws://h:abc/mqtt"), {})
check("忽略查询串", parse_broker_url("ws://h:8083/mqtt?x=1")["ws_path"], "/mqtt")
check("IPv6 字面量", parse_broker_url("[::1]:1883")["host"], "[::1]")
_c1 = ConnectionConfig(host="10.99.79.54", port=8083, transport="WebSocket", ws_path="/mqtt")
check("还原 WS URL", normalize_broker_url(_c1), "ws://10.99.79.54:8083/mqtt")
_c1.use_tls = True
check("还原 WSS URL", normalize_broker_url(_c1), "wss://10.99.79.54:8083/mqtt")
check("还原 TCP URL", normalize_broker_url(
    ConnectionConfig(host="a.b", port=1883, transport="TCP")), "mqtt://a.b:1883")

# ---------------- 历史坏配置自动迁移 ---------------- #
print("== 历史坏配置自动迁移 ==")
_bad = ConnectionConfig.from_dict({
    "name": "坏配置", "host": "ws://10.99.79.54:8083/mqtt", "transport": "TCP"})
check("坏配置 host 已拆干净", _bad.host, "10.99.79.54")
check("坏配置 port 已修正", _bad.port, 8083)
check("坏配置 transport 已修正", _bad.transport, "WebSocket")
check("坏配置 ws_path 已补上", _bad.ws_path, "/mqtt")
_bad2 = ConnectionConfig.from_dict({"host": "wss://h:8084/mqtt", "transport": "TCP"})
check("wss 坏配置推断 TLS", _bad2.use_tls, True)
check("wss 坏配置保持 WS", _bad2.transport, "WebSocket")
check("wss 坏配置 host", _bad2.host, "h")
_bad3 = ConnectionConfig.from_dict({"host": "mqtt://broker.emqx.io"})
check("mqtt:// 坏配置 host", _bad3.host, "broker.emqx.io")
check("mqtt:// 坏配置端口", _bad3.port, 1883)
check("mqtt:// 保持 TCP", _bad3.transport, "TCP")
_bad4 = ConnectionConfig.from_dict({"host": "10.0.0.5"})
check("正常 host 不受影响", _bad4.host, "10.0.0.5")
check("正常 host 不改端口", _bad4.port, 1883)
check("正常 host 不改传输", _bad4.transport, "TCP")
_bad5 = ConnectionConfig.from_dict({"host": "host/path"})
check("host 带路径也拆", _bad5.host, "host")
# 迁移后必须能还原成合法 URL（回归：不能越拆越坏）
check("迁移后可还原 URL", normalize_broker_url(_bad), "ws://10.99.79.54:8083/mqtt")
# to_dict / from_dict 往返稳定（迁移是幂等的）
check("迁移幂等", ConnectionConfig.from_dict(_bad.to_dict()).host, "10.99.79.54")

print()
if fails:
    print(f"!! {len(fails)} 项失败: {fails}")
    sys.exit(1)
print("全部单元测试通过")
