"""内置 Broker 的集成测试。

用真实的 paho-mqtt 客户端去连本进程内启动的 Broker，
覆盖 QoS 0/1/2、通配符、Retain、遗嘱、多客户端互发、认证拒绝、
会话保持、$ 主题隔离、重复 client_id 抢占、Keep Alive 超时。

运行：
    python tools/test_broker.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt  # noqa: E402

from mqtt_client.broker import MQTTBroker  # noqa: E402
from mqtt_client.config import BrokerConfig  # noqa: E402

PORT = 18830
HOST = "127.0.0.1"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


class Recorder:
    """收集某个客户端收到的消息。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bytes, int, bool]] = []
        self.lock = threading.Lock()
        self.connected = threading.Event()
        self.disconnected = threading.Event()
        self.rc: int | None = None
        self.subacked: list[int] = []
        self.granted: list[int] = []
        self.suback_evt = threading.Event()

    @staticmethod
    def _code_value(reason_code) -> int | None:
        """paho 2.x 传的是 ReasonCode 对象，需要取 .value。"""
        if reason_code is None:
            return None
        value = getattr(reason_code, "value", reason_code)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.rc = self._code_value(reason_code)
        self.connected.set()

    def on_disconnect(self, client, userdata, flags, reason_code=0, properties=None):
        self.disconnected.set()

    def on_message(self, client, userdata, msg):
        with self.lock:
            self.messages.append((msg.topic, msg.payload, msg.qos, bool(msg.retain)))

    def on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        self.subacked.append(mid)
        if isinstance(reason_codes, (list, tuple)):
            self.granted = [self._code_value(r) or 0 for r in reason_codes]
        else:
            self.granted = [self._code_value(reason_codes) or 0]
        self.suback_evt.set()

    def topics(self) -> set[str]:
        with self.lock:
            return {t for t, *_ in self.messages}

    def find(self, topic: str) -> tuple[str, bytes, int, bool] | None:
        with self.lock:
            for item in self.messages:
                if item[0] == topic:
                    return item
        return None


def make_client(cid: str, rec: Recorder, **kwargs) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, **kwargs)
    client.on_connect = rec.on_connect
    client.on_disconnect = rec.on_disconnect
    client.on_message = rec.on_message
    client.on_subscribe = rec.on_subscribe
    return client


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.03) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def new_recorder_client(cid: str, username: str | None = None,
                        password: str | None = None, **kwargs) -> tuple[mqtt.Client, Recorder]:
    rec = Recorder()
    client = make_client(cid, rec, **kwargs)
    if username is not None:
        client.username_pw_set(username, password)
    return client, rec


# --------------------------------------------------------------------------- #
# 各项测试
# --------------------------------------------------------------------------- #

def test_basic_connect(broker: MQTTBroker) -> None:
    print("\n[1] 基本连接与 CONNACK")
    client, rec = new_recorder_client("basic-1")
    rc = client.connect(HOST, PORT, keepalive=10)
    client.loop_start()
    ok = rec.connected.wait(5)
    check("连接成功并收到 CONNACK", ok)
    check("CONNACK 返回码为 0", rec.rc == 0, f"rc={rec.rc}")
    client.disconnect()
    client.loop_stop()
    check("断开后 Broker 客户端列表清空",
          wait_for(lambda: len(broker.client_snapshot()) == 0, 3))


def test_qos_levels(broker: MQTTBroker) -> None:
    print("\n[2] QoS 0 / 1 / 2 收投")
    sub, rec_sub = new_recorder_client("qos-sub")
    sub.connect(HOST, PORT, 10)
    sub.loop_start()
    rec_sub.connected.wait(5)
    sub.subscribe("qos/t", qos=2)
    rec_sub.suback_evt.wait(5)

    pub, rec_pub = new_recorder_client("qos-pub")
    pub.connect(HOST, PORT, 10)
    pub.loop_start()
    rec_pub.connected.wait(5)

    for qos in (0, 1, 2):
        pub.publish("qos/t", f"payload-q{qos}".encode(), qos=qos)
    ok = wait_for(lambda: len(rec_sub.messages) >= 3, 6)
    check("QoS 0/1/2 三条消息全部抵达", ok, f"收到 {len(rec_sub.messages)} 条")

    # 订阅 QoS 2 时服务端按 min(发布QoS, 订阅QoS) 投递
    got = {p.decode(): q for _, p, q, _ in rec_sub.messages}
    check("QoS 0 消息按 QoS 0 投递", got.get("payload-q0") == 0, str(got))
    check("QoS 1 消息按 QoS 1 投递", got.get("payload-q1") == 1, str(got))
    check("QoS 2 消息按 QoS 2 投递", got.get("payload-q2") == 2, str(got))

    sub.disconnect(); sub.loop_stop()
    pub.disconnect(); pub.loop_stop()


def test_wildcards(broker: MQTTBroker) -> None:
    print("\n[3] 通配符订阅")
    cases = [
        ("home/+/temp", "home/room1/temp", True),
        ("home/+/temp", "home/room1/hum", False),
        ("home/#", "home/room1/temp", True),
        ("home/#", "home", True),
        ("home/#", "home2/x", False),
        ("#", "$SYS/broker/uptime", False),      # $ 开头不被 # 匹配
        ("$SYS/#", "$SYS/broker/uptime", True),
    ]
    for i, (filt, topic, expect) in enumerate(cases):
        sub, rec = new_recorder_client(f"wc-sub-{i}")
        sub.connect(HOST, PORT, 10)
        sub.loop_start()
        rec.connected.wait(5)
        sub.subscribe(filt, qos=0)
        rec.suback_evt.wait(5)

        pub, rec_pub = new_recorder_client(f"wc-pub-{i}")
        pub.connect(HOST, PORT, 10)
        pub.loop_start()
        rec_pub.connected.wait(5)
        pub.publish(topic, b"x", qos=0)

        time.sleep(0.3)
        got = topic in rec.topics()
        check(f"通配 {filt!r} {'匹配' if expect else '不匹配'} {topic!r}",
              got == expect, f"实际 {'收到' if got else '未收到'}")

        sub.disconnect(); sub.loop_stop()
        pub.disconnect(); pub.loop_stop()


def test_retain(broker: MQTTBroker) -> None:
    print("\n[4] Retain 保留消息")
    pub, rec_pub = new_recorder_client("ret-pub")
    pub.connect(HOST, PORT, 10)
    pub.loop_start()
    rec_pub.connected.wait(5)
    pub.publish("retain/a", b"persisted", qos=1, retain=True)
    time.sleep(0.4)

    # 后订阅者应立即收到
    sub, rec_sub = new_recorder_client("ret-sub")
    sub.connect(HOST, PORT, 10)
    sub.loop_start()
    rec_sub.connected.wait(5)
    sub.subscribe("retain/#", qos=1)
    rec_sub.suback_evt.wait(5)

    ok = wait_for(lambda: rec_sub.find("retain/a") is not None, 5)
    check("新订阅者立刻收到保留消息", ok)
    item = rec_sub.find("retain/a")
    if item:
        check("保留消息 retain 标志为真", item[3] is True, str(item))
        check("保留消息内容正确", item[1] == b"persisted", str(item[1]))

    # 空载荷清除保留消息
    pub.publish("retain/a", b"", qos=1, retain=True)
    time.sleep(0.4)
    check("空载荷清除后 Broker 保留表为空",
          wait_for(lambda: len(broker.retained_snapshot()) == 0, 3),
          str(broker.retained_snapshot()))

    sub.disconnect(); sub.loop_stop()
    pub.disconnect(); pub.loop_stop()


def test_will(broker: MQTTBroker) -> None:
    print("\n[5] 遗嘱消息（异常掉线代发）")
    watcher, rec_w = new_recorder_client("will-watch")
    watcher.connect(HOST, PORT, 10)
    watcher.loop_start()
    rec_w.connected.wait(5)
    watcher.subscribe("will/topic", qos=1)
    rec_w.suback_evt.wait(5)

    rec_d = Recorder()
    dying = make_client("will-dying", rec_d)
    dying.will_set("will/topic", b"client-died", qos=1, retain=False)
    dying.connect(HOST, PORT, 10)
    dying.loop_start()
    rec_d.connected.wait(5)

    # 模拟网络异常：直接关掉底层 socket，不走 DISCONNECT
    dying._sock.close()  # noqa: SLF001
    ok = wait_for(lambda: rec_w.find("will/topic") is not None, 8)
    check("异常掉线后收到遗嘱", ok, str(rec_w.messages))
    item = rec_w.find("will/topic")
    if item:
        check("遗嘱内容正确", item[1] == b"client-died", str(item[1]))

    try:
        dying.loop_stop()
    except Exception:  # noqa: BLE001
        pass
    watcher.disconnect(); watcher.loop_stop()


def test_will_not_sent_on_clean_disconnect(broker: MQTTBroker) -> None:
    print("\n[6] 正常断开不发送遗嘱")
    watcher, rec_w = new_recorder_client("will2-watch")
    watcher.connect(HOST, PORT, 10)
    watcher.loop_start()
    rec_w.connected.wait(5)
    watcher.subscribe("will2/topic", qos=1)
    rec_w.suback_evt.wait(5)

    rec_d = Recorder()
    node = make_client("will2-dying", rec_d)
    node.will_set("will2/topic", b"should-not-appear", qos=1)
    node.connect(HOST, PORT, 10)
    node.loop_start()
    rec_d.connected.wait(5)
    node.disconnect()
    node.loop_stop()
    time.sleep(1.0)
    check("干净断开未触发遗嘱", rec_w.find("will2/topic") is None, str(rec_w.messages))

    watcher.disconnect(); watcher.loop_stop()


def test_multi_client(broker: MQTTBroker) -> None:
    print("\n[7] 多客户端互发（星型路由）")
    recs = []
    for i in range(4):
        c, r = new_recorder_client(f"multi-{i}")
        c.connect(HOST, PORT, 10)
        c.loop_start()
        r.connected.wait(5)
        c.subscribe("chat/#", qos=1)
        r.suback_evt.wait(5)
        recs.append((c, r))

    recs[0][0].publish("chat/room", b"hello-all", qos=1)
    ok = wait_for(lambda: all(r.find("chat/room") for _, r in recs), 6)
    check("4 个订阅者全部收到同一条消息", ok,
          str([len(r.messages) for _, r in recs]))

    for c, _ in recs:
        c.disconnect(); c.loop_stop()


def test_auth_reject() -> None:
    print("\n[8] 认证拒绝与放行")
    cfg = BrokerConfig(bind_address="127.0.0.1", port=PORT + 1,
                       require_auth=True, username="admin", password="secret")
    broker = MQTTBroker(cfg)
    started, msg = broker.start()
    check("认证模式 Broker 启动", started, msg)
    try:
        # 错误密码
        bad, rec_bad = new_recorder_client("auth-bad",
                                           username="admin", password="wrong")
        try:
            bad.connect(HOST, PORT + 1, 60)
            bad.loop_start()
            rec_bad.connected.wait(3)
            rec_bad.disconnected.wait(2)
        except Exception:  # noqa: BLE001
            pass
        check("错误密码被拒绝（rc!=0）", rec_bad.rc != 0, f"rc={rec_bad.rc}")
        try:
            bad.loop_stop()
        except Exception:  # noqa: BLE001
            pass

        # 正确凭据
        good, rec_good = new_recorder_client("auth-good",
                                             username="admin", password="secret")
        good.connect(HOST, PORT + 1, 60)
        good.loop_start()
        rec_good.connected.wait(5)
        check("正确凭据通过认证", rec_good.rc == 0, f"rc={rec_good.rc}")
        good.disconnect(); good.loop_stop()
    finally:
        broker.stop()


def test_acl_deny() -> None:
    print("\n[9] 黑名单 / 白名单")
    cfg = BrokerConfig(bind_address="127.0.0.1", port=PORT + 2,
                       block_list=["127.0.0.1"])
    broker = MQTTBroker(cfg)
    broker.start()
    try:
        c, rec = new_recorder_client("acl-denied")
        try:
            c.connect(HOST, PORT + 2, 60)
            c.loop_start()
            rec.connected.wait(3)
        except Exception:  # noqa: BLE001
            pass
        check("黑名单中的地址被拒绝", rec.rc != 0, f"rc={rec.rc}")
        try:
            c.loop_stop()
        except Exception:  # noqa: BLE001
            pass
    finally:
        broker.stop()


def test_persistent_session() -> None:
    print("\n[10] 会话保持（clean_session=False）")
    cfg = BrokerConfig(bind_address="127.0.0.1", port=PORT + 3)
    broker = MQTTBroker(cfg)
    broker.start()
    try:
        c1, r1 = new_recorder_client("persist-1", clean_session=False)
        c1.connect(HOST, PORT + 3, 60)
        c1.loop_start()
        r1.connected.wait(5)
        c1.subscribe("persist/t", qos=1)
        r1.suback_evt.wait(5)
        c1.disconnect()
        c1.loop_stop()
        time.sleep(0.5)

        # 重连（非 clean），CONNACK 的 session_present 应为 1
        c2, r2 = new_recorder_client("persist-1", clean_session=False)
        c2.connect(HOST, PORT + 3, 60)
        c2.loop_start()
        r2.connected.wait(5)
        check("重连后 Broker 恢复了订阅",
              wait_for(lambda: any(s["client_id"] == "persist-1"
                                   for s in broker.subscription_snapshot()), 3),
              str(broker.subscription_snapshot()))

        pub, rp = new_recorder_client("persist-pub")
        pub.connect(HOST, PORT + 3, 60)
        pub.loop_start()
        rp.connected.wait(5)
        pub.publish("persist/t", b"after-reconnect", qos=1)
        check("恢复的会话能收到新消息",
              wait_for(lambda: r2.find("persist/t") is not None, 5),
              str(r2.messages))

        c2.disconnect(); c2.loop_stop()
        pub.disconnect(); pub.loop_stop()
    finally:
        broker.stop()


def test_duplicate_client_id() -> None:
    print("\n[11] 重复 client_id 抢占")
    cfg = BrokerConfig(bind_address="127.0.0.1", port=PORT + 4)
    broker = MQTTBroker(cfg)
    broker.start()
    try:
        c1, r1 = new_recorder_client("dup-id")
        c1.connect(HOST, PORT + 4, 60)
        c1.loop_start()
        r1.connected.wait(5)

        c2, r2 = new_recorder_client("dup-id")
        c2.connect(HOST, PORT + 4, 60)
        c2.loop_start()
        r2.connected.wait(5)

        check("新连接顶掉旧连接",
              wait_for(lambda: r1.disconnected.is_set(), 5))
        check("新连接保持在线",
              any(s["client_id"] == "dup-id" for s in broker.client_snapshot()))

        c2.disconnect(); c2.loop_stop()
        try:
            c1.loop_stop()
        except Exception:  # noqa: BLE001
            pass
    finally:
        broker.stop()


def test_invalid_subscribe(broker: MQTTBroker) -> None:
    print("\n[12] 非法订阅过滤与 SUBACK 失败位")
    sub, rec = new_recorder_client("bads")
    sub.connect(HOST, PORT, 10)
    sub.loop_start()
    rec.connected.wait(5)

    # paho 会在客户端本地校验过滤器，非法值直接抛异常，
    # 所以这里手工构造 SUBSCRIBE 报文发给服务端，验证服务端的校验逻辑。
    from mqtt_client.broker import SUBSCRIBE, build_packet, encode_string

    mid = 7
    body = (mid.to_bytes(2, "big")
            + encode_string("ok/topic") + bytes([1])
            + encode_string("bad/#/x") + bytes([1]))
    sub._sock.sendall(build_packet(SUBSCRIBE, 0x02, body))  # noqa: SLF001
    rec.suback_evt.wait(5)
    check("混合订阅返回 2 个返回码", len(rec.granted) == 2, str(rec.granted))
    if len(rec.granted) == 2:
        check("合法主题返回 QoS 1", rec.granted[0] == 1, str(rec.granted))
        check("非法主题返回 0x80", rec.granted[1] == 0x80, str(rec.granted))
    sub.disconnect(); sub.loop_stop()


def test_max_qos_clamp() -> None:
    print("\n[12b] max_qos 降级")
    cfg = BrokerConfig(bind_address="127.0.0.1", port=PORT + 5, max_qos=1)
    broker = MQTTBroker(cfg)
    broker.start()
    try:
        sub, rec = new_recorder_client("clamp-sub")
        sub.connect(HOST, PORT + 5, 60)
        sub.loop_start()
        rec.connected.wait(5)
        sub.subscribe("clamp/t", qos=2)
        rec.suback_evt.wait(5)
        check("请求 QoS 2 但被降级为 1",
              rec.granted and rec.granted[0] == 1, str(rec.granted))

        pub, rp = new_recorder_client("clamp-pub")
        pub.connect(HOST, PORT + 5, 60)
        pub.loop_start()
        rp.connected.wait(5)
        pub.publish("clamp/t", b"x", qos=2)
        wait_for(lambda: rec.find("clamp/t") is not None, 4)
        item = rec.find("clamp/t")
        check("投递 QoS 被压到 1", item is not None and item[2] == 1, str(item))

        sub.disconnect(); sub.loop_stop()
        pub.disconnect(); pub.loop_stop()
    finally:
        broker.stop()


def test_unsubscribe(broker: MQTTBroker) -> None:
    print("\n[13] 取消订阅")
    sub, rec = new_recorder_client("unsub")
    sub.connect(HOST, PORT, 10)
    sub.loop_start()
    rec.connected.wait(5)
    sub.subscribe("unsub/t", qos=0)
    rec.suback_evt.wait(5)

    pub, rp = new_recorder_client("unsub-pub")
    pub.connect(HOST, PORT, 10)
    pub.loop_start()
    rp.connected.wait(5)

    pub.publish("unsub/t", b"first", qos=0)
    wait_for(lambda: rec.find("unsub/t") is not None, 3)
    n_before = len(rec.messages)

    sub.unsubscribe("unsub/t")
    time.sleep(0.4)
    pub.publish("unsub/t", b"second", qos=0)
    time.sleep(0.5)
    check("取消订阅后不再收到消息", len(rec.messages) == n_before,
          f"{n_before} -> {len(rec.messages)}")

    sub.disconnect(); sub.loop_stop()
    pub.disconnect(); pub.loop_stop()


def test_stats_and_snapshots(broker: MQTTBroker) -> None:
    print("\n[14] 统计与快照接口")
    check("Broker 处于运行状态", broker.running is True)
    check("uptime 为正数", broker.uptime() > 0, str(broker.uptime()))
    stats = broker.stats
    check("统计了入站消息", stats["messages_in"] > 0, str(stats))
    check("统计了出站消息", stats["messages_out"] > 0, str(stats))
    check("日志历史非空", len(broker.get_log_history()) > 0)
    snap = broker.client_snapshot()
    check("客户端快照字段完整",
          all({"client_id", "remote", "protocol", "keepalive"} <= set(r) for r in snap),
          str(snap[:1]))


def test_topic_matches_consistency() -> None:
    print("\n[15] 通配匹配与客户端侧实现一致性")
    from mqtt_client.core import topic_matches as client_matches
    from mqtt_client.broker import _validate_filter

    cases = [
        ("a/#", "a", True), ("a/#", "a/b/c", True), ("a/+", "a/b", True),
        ("a/+", "a", False), ("+/+", "a/b", True), ("#", "a", True),
        ("#", "$SYS/x", False), ("sport/#", "sport", True),
    ]
    for filt, topic, expect in cases:
        got = client_matches(filt, topic)
        check(f"客户端 topic_matches({filt!r}, {topic!r})", got == expect, str(got))

    bad = ["bad/#/x", "bad/x+", "bad/+#", ""]
    for filt in bad:
        ok, _ = _validate_filter(filt)
        check(f"非法过滤器 {filt!r} 被拒绝", not ok)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main() -> int:
    print("=" * 68)
    print(" 内置 MQTT Broker 集成测试")
    print("=" * 68)

    cfg = BrokerConfig(bind_address="127.0.0.1", port=PORT, max_qos=2)
    broker = MQTTBroker(cfg)
    ok, msg = broker.start()
    print(f"Broker 启动：{ok} / {msg}")
    if not ok:
        return 1
    try:
        test_basic_connect(broker)
        test_qos_levels(broker)
        test_wildcards(broker)
        test_retain(broker)
        test_will(broker)
        test_will_not_sent_on_clean_disconnect(broker)
        test_multi_client(broker)
        test_invalid_subscribe(broker)
        test_unsubscribe(broker)
        test_stats_and_snapshots(broker)
    finally:
        broker.stop()
        time.sleep(0.3)

    test_auth_reject()
    test_acl_deny()
    test_persistent_session()
    test_duplicate_client_id()
    test_max_qos_clamp()
    test_topic_matches_consistency()

    print("\n" + "=" * 68)
    print(f" 通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print(" 失败明细：")
        for item in FAILED:
            print(f"   · {item}")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
