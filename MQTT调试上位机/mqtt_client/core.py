"""MQTT 服务层：把 paho-mqtt 封装成一个线程安全、对界面友好的服务对象。

设计要点
--------
* paho 的网络循环跑在后台线程，回调里 **绝不** 直接碰 Tkinter 控件；
  所有回调统一转成 :class:`Event` 投递到 ``queue.Queue``，由界面用
  ``after()`` 轮询消费。
* 兼容 paho-mqtt 2.x（``CallbackAPIVersion.VERSION2``）与 1.x。
* 支持 MQTT 3.1 / 3.1.1 / 5.0、TCP / WebSocket(s)、TLS、遗嘱、认证、
  自动重连、订阅通配符、QoS 0/1/2、Retain。
"""

from __future__ import annotations

import base64
import json
import queue
import ssl
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt
import paho.mqtt.publish as mqtt_publish

from .config import (
    MQTT_VERSIONS,
    TRANSPORTS,
    ConnectionConfig,
    MessageRecord,
    Subscription,
)

try:  # paho >= 2.0
    from paho.mqtt.client import CallbackAPIVersion

    _HAS_V2 = True
except ImportError:  # pragma: no cover - paho 1.x
    CallbackAPIVersion = None  # type: ignore[assignment]
    _HAS_V2 = False


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #

class EventType(str, Enum):
    STATE = "state"            # 连接状态变化
    MESSAGE = "message"        # 收到消息
    PUBLISHED = "published"    # 发布结果
    SUBSCRIBED = "subscribed"  # 订阅结果
    UNSUBSCRIBED = "unsubscribed"
    LOG = "log"                # 普通日志
    ERROR = "error"
    STATS = "stats"            # 统计信息刷新


@dataclass
class Event:
    type: EventType
    payload: Any = None


# 连接状态
class ConnState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


STATE_TEXT = {
    ConnState.DISCONNECTED: "未连接",
    ConnState.CONNECTING: "连接中…",
    ConnState.CONNECTED: "已连接",
    ConnState.RECONNECTING: "重连中…",
    ConnState.FAILED: "连接失败",
}

# MQTT 5.0 原因码 → 中文说明（常见的那些）
REASON_TEXT = {
    0: "成功",
    128: "未指定错误",
    129: "报文格式错误",
    130: "协议错误",
    131: "实现特定错误",
    132: "协议版本不支持",
    133: "客户端标识符无效",
    134: "用户名或密码错误",
    135: "未授权",
    136: "服务端不可用",
    137: "服务端繁忙",
    138: "客户端已被禁用",
    139: "服务端正在关闭",
    140: "需要认证",
    141: "主题过滤器无效",
    142: "主题名无效",
    143: "报文标识符已被占用",
    144: "主题过滤器已存在（共享订阅）",
    145: "主题过滤器不存在（订阅）",
    147: "会话过期时间非法",
    148: "订阅选项非法",
    149: "通配符订阅不支持",
    150: "共享订阅不支持",
    151: "订阅标识符不支持",
    153: "保留消息不支持",
    154: "QoS 等级不支持",
    155: "不支持保留标志",
    156: "需要 TLS",
    157: "不支持 QoS 0",
    158: "保持连接超时非法",
    159: "最大包大小非法",
    161: "超出接收最大值",
    162: "主题名超出长度限制",
    163: "通配符订阅不支持",
    -1: "内部错误",
}


def reason_text(code: Any) -> str:
    """把原因码/错误码转成可读文本。"""
    if code is None:
        return ""
    try:
        ivalue = int(getattr(code, "value", code))
    except (TypeError, ValueError):
        return str(code)
    text = REASON_TEXT.get(ivalue)
    if text:
        return f"{text}({ivalue})"
    return str(ivalue)


# --------------------------------------------------------------------------- #
# 主题工具
# --------------------------------------------------------------------------- #

def topic_matches(filt: str, topic: str) -> bool:
    """判断具体主题是否匹配 MQTT 主题过滤器（支持 + 与 #）。"""
    if filt == topic:
        return True
    f_parts = filt.split("/")
    t_parts = topic.split("/")
    # $SYS 主题不被 # / + 匹配（符合 MQTT 规范）
    if t_parts and t_parts[0].startswith("$") and filt.startswith("#"):
        return False
    i = 0
    for i, f in enumerate(f_parts):
        if f == "#":
            # "#" 只在最后一层合法；按 MQTT 规范 "a/#" 也匹配 "a" 本身
            # （"#" 可代表零个或多个层级 / 空层），所以这里直接返回 True
            return i == len(f_parts) - 1
        if i >= len(t_parts):
            return False
        if f != "+" and f != t_parts[i]:
            return False
    return len(f_parts) == len(t_parts)


def is_valid_topic_filter(filt: str) -> Tuple[bool, str]:
    if not filt:
        return False, "主题不能为空"
    parts = filt.split("/")
    for idx, part in enumerate(parts):
        if "#" in part:
            if part != "#":
                return False, "通配符 # 必须独占一层"
            if idx != len(parts) - 1:
                return False, "通配符 # 只能出现在最后"
        if "+" in part and part != "+":
            return False, "通配符 + 必须独占一层"
    return True, ""


def decode_payload(payload: bytes) -> str:
    """尽量把字节流解码成可读文本。"""
    if not payload:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return base64.b64encode(payload).decode("ascii")


def pretty_json(text: str, indent: int = 2) -> Optional[str]:
    """尝试格式化 JSON，失败返回 None。"""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.dumps(json.loads(stripped), ensure_ascii=False, indent=indent)
    except (ValueError, TypeError):
        return None


def encode_payload(text: str, mode: str = "文本") -> bytes:
    """按模式把界面上的字符串编码成字节。"""
    if mode == "HEX":
        cleaned = text.replace("0x", "").replace("0X", "")
        for sep in (",", " ", "\n", "\t", "-", ":"):
            cleaned = cleaned.replace(sep, "")
        if len(cleaned) % 2:
            cleaned = "0" + cleaned
        try:
            return bytes.fromhex(cleaned)
        except ValueError:
            raise ValueError("HEX 内容非法，请使用 0-9 A-F 组成的偶数长度字符串")
    if mode == "Base64":
        try:
            return base64.b64decode(text.strip(), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Base64 内容非法：{exc}") from exc
    if mode == "JSON":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except ValueError as exc:
            raise ValueError(f"JSON 内容非法：{exc}") from exc
    return text.encode("utf-8")


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class MQTTService:
    """封装好的 MQTT 客户端服务。

    典型用法::

        svc = MQTTService()
        svc.connect(cfg)
        ...
        svc.publish("a/b", "hi", qos=1)
        svc.disconnect()
    """

    def __init__(self, events: Optional["queue.Queue[Event]"] = None) -> None:
        self.events: "queue.Queue[Event]" = events or queue.Queue()
        self._client: Optional[mqtt.Client] = None
        self._cfg: Optional[ConnectionConfig] = None
        self._lock = threading.RLock()

        self.state: ConnState = ConnState.DISCONNECTED
        self.subscriptions: List[Subscription] = []

        # 统计
        self.stats = {
            "rx_count": 0,
            "tx_count": 0,
            "rx_bytes": 0,
            "tx_bytes": 0,
            "connected_at": 0.0,
            "last_rx": 0.0,
            "last_tx": 0.0,
            "reconnects": 0,
        }
        self._started_at = time.time()

    # ------------------------------------------------------------------ #
    # 事件投递
    # ------------------------------------------------------------------ #
    def emit(self, etype: EventType, payload: Any = None) -> None:
        self.events.put(Event(etype, payload))

    def _log(self, text: str, level: str = "info") -> None:
        self.emit(EventType.LOG, {"text": text, "level": level, "ts": time.time()})

    def _set_state(self, state: ConnState, detail: str = "") -> None:
        self.state = state
        self.emit(EventType.STATE, {"state": state, "detail": detail})

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> Optional[mqtt.Client]:
        return self._client

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected())

    def connect(self, cfg: ConnectionConfig) -> None:
        """发起连接（立即返回，结果通过事件通知）。"""
        self.disconnect(quiet=True)
        with self._lock:
            self._cfg = cfg
            self._set_state(ConnState.CONNECTING, cfg.summary())

            protocol = MQTT_VERSIONS.get(cfg.protocol, 4)
            client_id = cfg.client_id.strip() or f"mqttdbg_{int(time.time()*1000)%1000000}"
            # MQTT 5.0 才允许 clean_start 之外的语义，3.1.1 用 clean_session
            clean = bool(cfg.clean_session)

            try:
                if _HAS_V2:
                    client = mqtt.Client(
                        callback_api_version=CallbackAPIVersion.VERSION2,
                        client_id=client_id,
                        clean_session=clean if protocol != 5 else None,
                        protocol=protocol,
                        transport=TRANSPORTS.get(cfg.transport, "tcp"),
                        reconnect_on_failure=False,
                    )
                else:  # pragma: no cover
                    client = mqtt.Client(
                        client_id=client_id,
                        clean_session=clean,
                        protocol=protocol,
                        transport=TRANSPORTS.get(cfg.transport, "tcp"),
                    )
            except (ValueError, TypeError) as exc:
                self._set_state(ConnState.FAILED, str(exc))
                self._log(f"创建客户端失败：{exc}", "error")
                return

            if protocol == 5:
                try:
                    client.clean_start = clean
                except AttributeError:
                    pass

            # WebSocket 路径
            if TRANSPORTS.get(cfg.transport) == "websockets":
                path = cfg.ws_path or "/mqtt"
                client.ws_set_options(path=path)

            # TLS
            if cfg.use_tls:
                try:
                    ctx = self._build_ssl_context(cfg)
                    client.tls_set_context(ctx)
                    if cfg.tls_insecure:
                        client.tls_insecure_set(True)
                except (ssl.SSLError, OSError, ValueError) as exc:
                    self._set_state(ConnState.FAILED, f"TLS 配置错误：{exc}")
                    self._log(f"TLS 配置错误：{exc}", "error")
                    return

            # 认证
            if cfg.username:
                client.username_pw_set(cfg.username, cfg.password or None)

            # 遗嘱
            if cfg.will.enabled and cfg.will.topic:
                client.will_set(
                    cfg.will.topic,
                    payload=cfg.will.payload.encode("utf-8"),
                    qos=int(cfg.will.qos),
                    retain=bool(cfg.will.retain),
                )

            self._bind_callbacks(client)
            client.reconnect_delay_set(
                min_delay=max(1, int(cfg.reconnect_delay)),
                max_delay=max(30, int(cfg.reconnect_delay) * 6),
            )
            self._client = client

            host = cfg.host.strip()
            if not host:
                self._set_state(ConnState.FAILED, "Broker 地址为空")
                self._log("Broker 地址为空", "error")
                return

            self._log(f"正在连接 {cfg.summary()}（{cfg.protocol} / {cfg.transport}）…")
            try:
                # connect_async 不会阻塞，适合界面线程
                client.connect_async(host, int(cfg.port), keepalive=int(cfg.keepalive))
                client.loop_start()
            except (ValueError, OSError, ssl.SSLError) as exc:
                self._set_state(ConnState.FAILED, str(exc))
                self._log(f"连接失败：{exc}", "error")

    @staticmethod
    def _build_ssl_context(cfg: ConnectionConfig) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=cfg.ca_file or None)
        if cfg.cert_file and cfg.key_file:
            ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
        return ctx

    # ------------------------------------------------------------------ #
    # 回调绑定
    # ------------------------------------------------------------------ #
    def _bind_callbacks(self, client: mqtt.Client) -> None:
        if _HAS_V2:
            client.on_connect = self._on_connect_v2
            client.on_disconnect = self._on_disconnect_v2
            client.on_message = self._on_message_v2
            client.on_subscribe = self._on_subscribe_v2
            client.on_unsubscribe = self._on_unsubscribe_v2
            client.on_publish = self._on_publish_v2
        else:  # pragma: no cover
            client.on_connect = self._on_connect_v1
            client.on_disconnect = self._on_disconnect_v1
            client.on_message = self._on_message_v1
            client.on_subscribe = self._on_subscribe_v1
            client.on_unsubscribe = self._on_unsubscribe_v1
            client.on_publish = self._on_publish_v1

    # ---- v2 回调 ---- #
    def _on_connect_v2(self, client, userdata, flags, reason_code, properties=None):
        code = int(getattr(reason_code, "value", reason_code))
        if code == 0:
            self.stats["connected_at"] = time.time()
            self._set_state(ConnState.CONNECTED, reason_text(reason_code))
            self._log(f"连接成功：{self._cfg.summary() if self._cfg else ''}", "success")
            # 连接成功后根据 clean_session 决定是否恢复订阅
            self._restore_subscriptions()
        else:
            self._set_state(ConnState.FAILED, reason_text(reason_code))
            self._log(f"连接被拒绝：{reason_text(reason_code)}", "error")

    def _on_disconnect_v2(self, client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        code = int(getattr(reason_code, "value", reason_code if reason_code is not None else 0))
        if code == 0:
            self._set_state(ConnState.DISCONNECTED, "正常断开")
            self._log("已断开连接")
        else:
            self._log(f"连接意外断开：{reason_text(reason_code)}", "warn")
            if self._cfg and self._cfg.auto_reconnect and not self._manual_close:
                self.stats["reconnects"] += 1
                self._set_state(ConnState.RECONNECTING, reason_text(reason_code))
                self._schedule_reconnect()
            else:
                self._set_state(ConnState.DISCONNECTED, reason_text(reason_code))

    def _on_message_v2(self, client, userdata, msg):
        self._handle_message(msg)

    def _on_subscribe_v2(self, client, userdata, mid, reason_codes, properties=None):
        codes = reason_codes
        try:
            codes = [int(getattr(rc, "value", rc)) for rc in reason_codes]
        except TypeError:
            codes = [int(getattr(reason_codes, "value", reason_codes))]
        ok = all(c < 128 for c in codes)
        self._log(
            f"订阅确认 (mid={mid})：{', '.join(reason_text(c) for c in codes)}",
            "success" if ok else "error",
        )
        self.emit(EventType.SUBSCRIBED, {"mid": mid, "codes": codes})

    def _on_unsubscribe_v2(self, client, userdata, mid, reason_codes, properties=None):
        self._log(f"已取消订阅 (mid={mid})")
        self.emit(EventType.UNSUBSCRIBED, {"mid": mid})

    def _on_publish_v2(self, client, userdata, mid, reason_code=None, properties=None):
        self._log(f"发布成功 (mid={mid})", "success")
        self.emit(EventType.PUBLISHED, {"mid": mid, "reason": reason_text(reason_code)})

    # ---- v1 回调（兼容旧版 paho）---- #
    def _on_connect_v1(self, client, userdata, flags, rc):  # pragma: no cover
        self._on_connect_v2(client, userdata, flags, rc)

    def _on_disconnect_v1(self, client, userdata, rc):  # pragma: no cover
        self._on_disconnect_v2(client, userdata, None, rc)

    def _on_message_v1(self, client, userdata, msg):  # pragma: no cover
        self._handle_message(msg)

    def _on_subscribe_v1(self, client, userdata, mid, granted_qos):  # pragma: no cover
        self._log(f"订阅确认 (mid={mid}, QoS={granted_qos})", "success")
        self.emit(EventType.SUBSCRIBED, {"mid": mid, "codes": list(granted_qos or [])})

    def _on_unsubscribe_v1(self, client, userdata, mid):  # pragma: no cover
        self._log(f"已取消订阅 (mid={mid})")
        self.emit(EventType.UNSUBSCRIBED, {"mid": mid})

    def _on_publish_v1(self, client, userdata, mid):  # pragma: no cover
        self._log(f"发布成功 (mid={mid})", "success")
        self.emit(EventType.PUBLISHED, {"mid": mid, "reason": ""})

    # ------------------------------------------------------------------ #
    # 消息处理
    # ------------------------------------------------------------------ #
    def _handle_message(self, msg) -> None:
        raw = bytes(msg.payload or b"")
        text = decode_payload(raw)
        props: Dict[str, Any] = {}
        try:
            if msg.properties is not None:
                props = {
                    "content_type": getattr(msg.properties, "ContentType", None),
                    "user_property": getattr(msg.properties, "UserProperty", None),
                    "response_topic": getattr(msg.properties, "ResponseTopic", None),
                    "correlation_data": getattr(msg.properties, "CorrelationData", None),
                }
                props = {k: v for k, v in props.items() if v}
        except AttributeError:
            props = {}

        record = MessageRecord(
            timestamp=time.time(),
            topic=msg.topic,
            payload=text,
            qos=int(getattr(msg, "qos", 0) or 0),
            retain=bool(getattr(msg, "retain", False)),
            direction="in",
            dup=bool(getattr(msg, "dup", False)),
            mid=int(getattr(msg, "mid", 0) or 0),
            raw=raw,
            size=len(raw),
        )
        self.stats["rx_count"] += 1
        self.stats["rx_bytes"] += len(raw)
        self.stats["last_rx"] = record.timestamp
        self.emit(EventType.MESSAGE, {"record": record, "properties": props})

    def note_tx(self, size: int) -> None:
        self.stats["tx_count"] += 1
        self.stats["tx_bytes"] += size
        self.stats["last_tx"] = time.time()

    # ------------------------------------------------------------------ #
    # 订阅管理
    # ------------------------------------------------------------------ #
    def _restore_subscriptions(self) -> None:
        """连接成功后恢复订阅列表。"""
        subs = [s for s in self.subscriptions if s.enabled and s.topic]
        if not subs:
            return
        try:
            for sub in subs:
                assert self._client is not None
                self._client.subscribe(sub.topic, qos=int(sub.qos))
            self._log(f"已恢复 {len(subs)} 条订阅")
        except (ValueError, OSError) as exc:
            self._log(f"恢复订阅失败：{exc}", "error")

    def subscribe(self, topic: str, qos: int = 0, alias: str = "") -> bool:
        ok, err = is_valid_topic_filter(topic)
        if not ok:
            self._log(f"订阅失败：{err}", "error")
            return False
        sub = Subscription(topic=topic, qos=int(qos), alias=alias)
        if not any(s.topic == topic for s in self.subscriptions):
            self.subscriptions.append(sub)
        if not self.is_connected:
            self._log(f"已记录订阅 {topic}（QoS {qos}），连接后自动生效", "warn")
            return True
        try:
            assert self._client is not None
            self._client.subscribe(topic, qos=int(qos))
            self._log(f"已订阅 {topic}（QoS {qos}）")
            return True
        except (ValueError, OSError) as exc:
            self._log(f"订阅 {topic} 失败：{exc}", "error")
            return False

    def unsubscribe(self, topic: str) -> bool:
        self.subscriptions = [s for s in self.subscriptions if s.topic != topic]
        if not self.is_connected:
            return True
        try:
            assert self._client is not None
            self._client.unsubscribe(topic)
            self._log(f"已取消订阅 {topic}")
            return True
        except (ValueError, OSError) as exc:
            self._log(f"取消订阅失败：{exc}", "error")
            return False

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #
    def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not topic:
            self._log("发布失败：主题为空", "error")
            return False
        if "#" in topic or "+" in topic:
            self._log("发布失败：发布主题不能包含通配符", "error")
            return False
        if not self.is_connected:
            self._log("发布失败：当前未连接", "error")
            return False
        try:
            assert self._client is not None
            info = self._client.publish(topic, payload, qos=int(qos), retain=bool(retain))
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self._log(f"发布失败：{mqtt.error_string(info.rc)}", "error")
                return False
            self.note_tx(len(payload))
            self.emit(
                EventType.MESSAGE,
                {
                    "record": MessageRecord(
                        timestamp=time.time(),
                        topic=topic,
                        payload=decode_payload(payload),
                        qos=int(qos),
                        retain=bool(retain),
                        direction="out",
                        mid=int(info.mid or 0),
                        raw=payload,
                        size=len(payload),
                    ),
                    "properties": {},
                },
            )
            return True
        except (ValueError, OSError, TypeError) as exc:
            self._log(f"发布异常：{exc}", "error")
            return False

    # ------------------------------------------------------------------ #
    # 断开 / 重连
    # ------------------------------------------------------------------ #
    _manual_close = False
    _reconnect_timer: Optional[threading.Timer] = None

    def disconnect(self, quiet: bool = False) -> None:
        self._manual_close = True
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        client = self._client
        if client is None:
            if not quiet:
                self._set_state(ConnState.DISCONNECTED, "未连接")
            return
        try:
            client.disconnect()
        except (OSError, ValueError):
            pass
        try:
            client.loop_stop()
        except (OSError, ValueError):
            pass
        if not quiet:
            self._log("已主动断开连接")
        self._set_state(ConnState.DISCONNECTED, "主动断开")
        self._client = None

    def _schedule_reconnect(self) -> None:
        delay = max(1, int(self._cfg.reconnect_delay)) if self._cfg else 5
        limit = int(self._cfg.max_reconnect) if self._cfg else 0
        if limit and self.stats["reconnects"] > limit:
            self._set_state(ConnState.FAILED, "已达最大重连次数")
            self._log("已达最大重连次数，停止重连", "error")
            return

        def _do() -> None:
            if self._manual_close or not self._cfg:
                return
            self._log(f"第 {self.stats['reconnects']} 次重连…", "warn")
            self.connect(self._cfg)

        self._reconnect_timer = threading.Timer(delay, _do)
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    def reconnect_now(self) -> None:
        if not self._cfg:
            return
        self._manual_close = False
        self.stats["reconnects"] += 1
        self.connect(self._cfg)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def uptime(self) -> float:
        if self.state == ConnState.CONNECTED and self.stats["connected_at"]:
            return time.time() - self.stats["connected_at"]
        return 0.0

    def shutdown(self) -> None:
        self._manual_close = True
        self.disconnect(quiet=True)


# --------------------------------------------------------------------------- #
# 独立发布工具（不建立会话，类似 mosquitto_pub）
# --------------------------------------------------------------------------- #

def one_shot_publish(
    host: str,
    port: int,
    topic: str,
    payload: bytes,
    qos: int = 0,
    retain: bool = False,
    keepalive: int = 30,
    username: Optional[str] = None,
    password: Optional[str] = None,
    use_tls: bool = False,
    protocol: int = 4,
) -> Tuple[bool, str]:
    """打开连接 → 发布 → 关闭，用于“快速发布”功能。"""
    auth = None
    if username:
        auth = {"username": username, "password": password or None}
    tls_cfg = None
    if use_tls:
        tls_cfg = {"ca_certs": None, "insecure": True}
    try:
        mqtt_publish.single(
            topic,
            payload=payload,
            qos=qos,
            retain=retain,
            hostname=host,
            port=port,
            keepalive=keepalive,
            auth=auth,
            tls=tls_cfg,
            protocol=protocol,
        )
        return True, "发布成功"
    except Exception as exc:  # noqa: BLE001 - 需要把各种底层异常都反馈给界面
        return False, str(exc)
