"""配置数据结构与本地持久化。

所有可调参数集中在这里，界面只负责读写这些 dataclass，
再通过 :mod:`mqtt_client.config` 的 ``save`` / ``load`` 落盘到 JSON。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

CONFIG_DIR_NAME = ".mqtt_debugger"
CONFIG_FILE_NAME = "config.json"
HISTORY_FILE_NAME = "history.json"


def app_dir() -> str:
    """配置目录。优先放在用户主目录，避免程序目录只读。"""
    base = os.environ.get("MQTT_DEBUGGER_HOME")
    if base:
        return base
    home = os.path.expanduser("~")
    return os.path.join(home, CONFIG_DIR_NAME)


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# 连接配置
# --------------------------------------------------------------------------- #

MQTT_VERSIONS = {
    "MQTT 3.1.1": 4,
    "MQTT 5.0": 5,
    "MQTT 3.1": 3,
}

TRANSPORTS = {
    "TCP": "tcp",
    "WebSocket": "websockets",
}

QOS_CHOICES = ("0 - 至多一次", "1 - 至少一次", "2 - 恰好一次")


def qos_from_label(label: str) -> int:
    try:
        return int(str(label).strip().split(" ")[0])
    except (ValueError, IndexError):
        return 0


def qos_label(qos: int) -> str:
    for item in QOS_CHOICES:
        if item.startswith(str(qos)):
            return item
    return QOS_CHOICES[0]


# --------------------------------------------------------------------------- #
# 连接地址解析
# --------------------------------------------------------------------------- #

#: URL 协议前缀 -> 传输方式（TRANSPORTS 的键）
SCHEME_TRANSPORT = {
    "mqtt": "TCP",
    "tcp": "TCP",
    "mqtts": "TCP",
    "ssl": "TCP",
    "tls": "TCP",
    "ws": "WebSocket",
    "wss": "WebSocket",
}

#: 用 URL 协议前缀就能推断出要开 TLS
SCHEME_TLS = {"mqtts", "ssl", "tls", "wss"}

#: 各协议前缀在没写端口时的默认端口
SCHEME_DEFAULT_PORT = {
    "mqtt": 1883, "tcp": 1883, "mqtts": 8883, "ssl": 8883, "tls": 8883,
    "ws": 8083, "wss": 8084,
}


def parse_broker_url(text: str) -> Dict[str, Any]:
    """把用户粘贴的完整 Broker 地址拆成可填进界面的各项参数。

    支持这些写法（都是为了让人能直接把文档里的地址粘进来）：
        ws://10.99.79.54:8083/mqtt
        wss://host:8084/mqtt
        mqtt://host:1883
        mqtts://host:8883
        ssl://host:8883
        10.99.79.54:8083
        10.99.79.54

    返回 dict，只包含能解析出来的键：
        host / port / transport / ws_path / use_tls

    解析不出来就返回空 dict，调用方保持原值不变。
    """
    raw = str(text or "").strip()
    if not raw:
        return {}

    result: Dict[str, Any] = {}

    scheme = ""
    rest = raw
    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        scheme = scheme.strip().lower()
    elif raw.lower().startswith(("ws:", "wss:")):
        # 容忍 "ws:host:port" 这种漏了双斜杠的写法
        scheme, _, rest = raw.partition(":")
        scheme = scheme.strip().lower()

    if scheme:
        transport = SCHEME_TRANSPORT.get(scheme)
        if transport is None:
            return {}                      # 不认识的协议前缀，不猜
        result["transport"] = transport
        result["use_tls"] = scheme in SCHEME_TLS

    # 去掉查询串与 fragment
    for sep in ("?", "#"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]

    # 拆路径
    path = ""
    if "/" in rest:
        rest, _, path = rest.partition("/")
        path = "/" + path.lstrip("/")

    host = rest.strip()
    port: Optional[int] = None

    # IPv6 字面量：[::1]:1883
    if host.startswith("["):
        close = host.find("]")
        if close != -1:
            addr, tail = host[: close + 1], host[close + 1:]
            if tail.startswith(":") and tail[1:].strip().isdigit():
                port = int(tail[1:].strip())
            host = addr
    elif host.count(":") == 1:
        addr, _, port_text = host.partition(":")
        port_text = port_text.strip()
        if port_text.isdigit():
            host, port = addr.strip(), int(port_text)
        elif port_text:
            return {}                      # 端口不是数字，交给用户自己改

    if host:
        result["host"] = host
    if port is not None and 1 <= port <= 65535:
        result["port"] = port
    elif scheme and "port" not in result:
        default_port = SCHEME_DEFAULT_PORT.get(scheme)
        if default_port:
            result["port"] = default_port
    if path and path != "/":
        result["ws_path"] = path

    # 至少要能拿到主机或传输方式，否则视为无效
    if "host" not in result and "transport" not in result:
        return {}
    return result


def normalize_broker_url(cfg: "ConnectionConfig") -> str:
    """把连接配置还原成一个完整 URL，用于界面回显和日志。"""
    scheme = "wss" if cfg.use_tls else "ws" if cfg.transport == "WebSocket" \
        else "mqtts" if cfg.use_tls else "mqtt"
    host = cfg.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                       # IPv6 需要加方括号
    url = f"{scheme}://{host}:{cfg.port}"
    if cfg.transport == "WebSocket":
        path = (cfg.ws_path or "/mqtt").strip()
        if not path.startswith("/"):
            path = "/" + path
        url += path
    return url


@dataclass
class WillConfig:
    """遗嘱消息（Last Will and Testament）。"""

    enabled: bool = False
    topic: str = ""
    payload: str = ""
    qos: int = 0
    retain: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WillConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ConnectionConfig:
    """一次客户端连接所需的全部参数。"""

    name: str = "默认连接"
    host: str = "broker.emqx.io"
    port: int = 1883
    client_id: str = field(default_factory=lambda: f"mqttdbg_{uuid.uuid4().hex[:8]}")
    clean_session: bool = True
    protocol: str = "MQTT 3.1.1"          # MQTT_VERSIONS 的键
    transport: str = "TCP"                 # TRANSPORTS 的键
    ws_path: str = "/mqtt"                 # WebSocket 路径
    keepalive: int = 60
    auto_reconnect: bool = True
    reconnect_delay: int = 5               # 秒
    max_reconnect: int = 0                 # 0 = 无限
    username: str = ""
    password: str = ""
    use_tls: bool = False
    tls_insecure: bool = False
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    will: WillConfig = field(default_factory=WillConfig)

    # ---------------- 序列化 ---------------- #
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["will"] = self.will.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionConfig":
        data = dict(data or {})
        will = WillConfig.from_dict(data.pop("will", {}) or {})
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        obj = cls(**valid)
        obj.will = will
        obj._migrate_legacy_url()
        return obj

    def _migrate_legacy_url(self) -> None:
        """历史坏配置自动修复：host 里被塞了完整 URL。

        真实踩过的坑：用户在「地址」框里直接粘贴了
        `ws://10.99.79.54:8083/mqtt`，于是 host 变成了这一整串。
        paho 会把它当成域名去解析，解析失败后**静默无限重试**，
        界面就永远停在「连接中…」——什么提示都没有，极难自查。

        所以只要检测到 host 里带 `://`（或带路径），就地拆解成
        host / port / transport / ws_path / use_tls，让老配置自动可用。
        """
        host = str(self.host or "")
        if "://" not in host and "/" not in host:
            return
        parsed = parse_broker_url(host)
        if not parsed:
            return
        if parsed.get("host"):
            self.host = str(parsed["host"])
        if parsed.get("port"):
            self.port = int(parsed["port"])
        if parsed.get("transport"):
            self.transport = str(parsed["transport"])
        if parsed.get("ws_path"):
            self.ws_path = str(parsed["ws_path"])
            if self.transport == "WebSocket" and not self.ws_path.startswith("/"):
                self.ws_path = "/" + self.ws_path
        if "use_tls" in parsed:
            self.use_tls = bool(parsed["use_tls"])

    def clone(self, new_name: str = "") -> "ConnectionConfig":
        obj = ConnectionConfig.from_dict(self.to_dict())
        if new_name:
            obj.name = new_name
        obj.client_id = f"mqttdbg_{uuid.uuid4().hex[:8]}"
        return obj

    def summary(self) -> str:
        scheme = "mqtts" if self.use_tls else "mqtt"
        return f"{scheme}://{self.host}:{self.port}"


# --------------------------------------------------------------------------- #
# 订阅项
# --------------------------------------------------------------------------- #

@dataclass
class Subscription:
    """一条订阅记录。"""

    topic: str
    qos: int = 0
    color: str = ""       # 空字符串表示用默认配色
    alias: str = ""       # 备注名
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.alias or self.topic

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Subscription":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- #
# 消息记录
# --------------------------------------------------------------------------- #

@dataclass
class MessageRecord:
    """一条已收到或已发出的消息，用于日志与主题树。"""

    timestamp: float
    topic: str
    payload: str
    qos: int = 0
    retain: bool = False
    direction: str = "in"        # "in" 收到 / "out" 发出 / "sys" 系统
    dup: bool = False
    mid: int = 0
    raw: bytes = b""
    size: int = 0

    def to_dict(self, with_raw: bool = False) -> Dict[str, Any]:
        data = {
            "timestamp": self.timestamp,
            "topic": self.topic,
            "payload": self.payload,
            "qos": self.qos,
            "retain": self.retain,
            "direction": self.direction,
            "mid": self.mid,
            "size": self.size or len(self.raw),
        }
        if with_raw:
            import base64

            data["raw"] = base64.b64encode(self.raw).decode("ascii")
        return data


# --------------------------------------------------------------------------- #
# 自动化脚本规则
# --------------------------------------------------------------------------- #

@dataclass
class ScriptRule:
    """按条件自动应答的规则。

    match_mode:
        contains / equals / startswith / endswith / regex / wildcard(主题通配)
    action:
        publish      —— 在指定主题发布内容
        reply        —— 以收到消息的主题为模板回复
        log          —— 仅记录
    """

    name: str = "新规则"
    enabled: bool = True
    topic_filter: str = "#"           # 监听的主题
    match_mode: str = "contains"
    match_value: str = ""
    action: str = "publish"
    reply_topic: str = ""             # 为空则用原主题（可含 {topic} 占位）
    payload: str = ""
    qos: int = 0
    retain: bool = False
    delay_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScriptRule":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- #
# 内置 Broker 配置
# --------------------------------------------------------------------------- #

#: 监听地址预设（供界面下拉框使用）
BIND_PRESETS = (
    ("0.0.0.0 —— 所有网卡（局域网可连）", "0.0.0.0"),
    ("127.0.0.1 —— 仅本机", "127.0.0.1"),
)

#: 内置 Broker 支持的最大 QoS
BROKER_MAX_QOS_CHOICES = ("0 - 至多一次", "1 - 至少一次", "2 - 恰好一次")


@dataclass
class BrokerConfig:
    """内置 MQTT Broker（服务端）配置。

    该配置与 :class:`ConnectionConfig` 相互独立：
    前者描述"我这台机器如何对外提供 Broker 服务"，
    后者描述"我要连哪个 Broker"。两者可以同时启用。
    """

    # -------- 监听 --------
    bind_address: str = "0.0.0.0"
    """监听地址。0.0.0.0 表示所有网卡（其他主机可连），127.0.0.1 仅本机。"""

    port: int = 1884
    """监听端口。默认 1884 避免与本机已装的 mosquitto 等占用 1883 冲突。"""

    max_connections: int = 64
    """最大并发连接数，0 表示不限制。"""

    backlog: int = 16
    """socket listen backlog。"""

    # -------- WebSocket --------
    enable_websocket: bool = False
    """是否同时监听 WebSocket 端口。

    很多前端（浏览器 / 小程序）和 EMQX 的默认部署只开放 ws://host:8083/mqtt，
    打开这个开关后内置 Broker 会在第二个端口上提供同样的服务。
    """

    websocket_port: int = 8083
    """WebSocket 监听端口。默认 8083，与 EMQX 保持一致。"""

    websocket_path: str = "/mqtt"
    """WebSocket 路径，必须以 / 开头。EMQX 默认是 /mqtt。"""

    # -------- 协议行为 --------
    max_qos: int = 2
    """允许的最大 QoS，超出的订阅/发布会被降级。"""

    enable_retain: bool = True
    """是否启用保留消息。"""

    session_expiry: int = 3600
    """非 clean 会话在客户端离线后的保留秒数（本实现用内存保存）。"""

    max_payload_kb: int = 256
    """单条报文载荷上限（KB），超出直接断开，防止内存被打爆。"""

    # -------- 认证 --------
    require_auth: bool = False
    """是否要求用户名密码。"""

    username: str = ""
    password: str = ""

    # -------- 访问控制 --------
    allow_list: List[str] = field(default_factory=list)
    """IP 白名单。为空表示不限制；支持 `192.168.1.0/24` 网段写法。"""

    block_list: List[str] = field(default_factory=list)
    """IP 黑名单，优先级高于白名单。"""

    # -------- 日志 --------
    log_limit: int = 500
    """内存中保留的 Broker 日志条数。"""

    log_connect: bool = True
    log_publish: bool = False
    """是否记录每一条 PUBLISH（调试期可开，量大时建议关）。"""

    # -------- 界面 --------
    auto_start: bool = False
    """启动上位机时是否自动开启 Broker。"""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrokerConfig":
        data = dict(data or {})
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        obj = cls(**valid)
        # 容错：端口/数值字段可能是字符串
        try:
            obj.port = int(obj.port)
        except (TypeError, ValueError):
            obj.port = 1884
        try:
            obj.websocket_port = int(obj.websocket_port)
        except (TypeError, ValueError):
            obj.websocket_port = 8083
        if not (1 <= obj.websocket_port <= 65535):
            obj.websocket_port = 8083
        if not isinstance(obj.websocket_path, str) or not obj.websocket_path.startswith("/"):
            obj.websocket_path = "/mqtt"
        obj.enable_websocket = bool(obj.enable_websocket)
        try:
            obj.max_qos = max(0, min(2, int(obj.max_qos)))
        except (TypeError, ValueError):
            obj.max_qos = 2
        if not isinstance(obj.allow_list, list):
            obj.allow_list = []
        if not isinstance(obj.block_list, list):
            obj.block_list = []
        return obj

    @property
    def summary(self) -> str:
        return f"mqtt://{self.bind_address}:{self.port}"


# --------------------------------------------------------------------------- #
# 应用总配置
# --------------------------------------------------------------------------- #

@dataclass
class AppConfig:
    connections: List[ConnectionConfig] = field(default_factory=list)
    active_index: int = 0
    subscriptions: List[Subscription] = field(default_factory=list)
    scripts: List[ScriptRule] = field(default_factory=list)
    publish_history: List[str] = field(default_factory=list)
    payload_templates: Dict[str, str] = field(default_factory=dict)
    ui: Dict[str, Any] = field(default_factory=dict)
    broker: BrokerConfig = field(default_factory=BrokerConfig)

    # ---------------- 默认值 ---------------- #
    @classmethod
    def default(cls) -> "AppConfig":
        cfg = cls()
        cfg.connections = [ConnectionConfig()]
        cfg.subscriptions = [
            Subscription("demo/#", 0),
        ]
        cfg.payload_templates = {
            "空 JSON 对象": "{}",
            "常用控制指令": '{"cmd": "on", "value": 1}',
            "传感器数据": '{"temp": 25.6, "hum": 60.2, "ts": 0}',
            "纯文本 hello": "hello from mqtt debugger",
            "JSON 数组": "[1, 2, 3]",
            "HEX 示例": "01 02 03 FF",
        }
        return cfg

    # ---------------- 序列化 ---------------- #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "connections": [c.to_dict() for c in self.connections],
            "active_index": self.active_index,
            "subscriptions": [s.to_dict() for s in self.subscriptions],
            "scripts": [s.to_dict() for s in self.scripts],
            "publish_history": self.publish_history,
            "payload_templates": self.payload_templates,
            "ui": self.ui,
            "broker": self.broker.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        cfg = cls()
        cfg.connections = [ConnectionConfig.from_dict(d) for d in data.get("connections", [])] or [
            ConnectionConfig()
        ]
        cfg.active_index = int(data.get("active_index", 0))
        cfg.subscriptions = [Subscription.from_dict(d) for d in data.get("subscriptions", [])]
        cfg.scripts = [ScriptRule.from_dict(d) for d in data.get("scripts", [])]
        cfg.publish_history = list(data.get("publish_history", []))
        cfg.payload_templates = dict(data.get("payload_templates", {}))
        cfg.ui = dict(data.get("ui", {}))
        # v1 配置没有 broker 字段，缺失时用默认值（向后兼容）
        cfg.broker = BrokerConfig.from_dict(data.get("broker", {}) or {})
        if not cfg.payload_templates:
            cfg.payload_templates = cls.default().payload_templates
        if not 0 <= cfg.active_index < len(cfg.connections):
            cfg.active_index = 0
        return cfg

    @property
    def active(self) -> ConnectionConfig:
        return self.connections[self.active_index]


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #

def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_FILE_NAME)


def history_path() -> str:
    return os.path.join(app_dir(), HISTORY_FILE_NAME)


def save(cfg: AppConfig, path: Optional[str] = None) -> str:
    path = path or config_path()
    _ensure_dir(os.path.dirname(path) or ".")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def load(path: Optional[str] = None) -> AppConfig:
    path = path or config_path()
    if not os.path.isfile(path):
        return AppConfig.default()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return AppConfig.from_dict(data)
    except (OSError, ValueError, TypeError):
        # 配置损坏时退回默认值，不阻断启动
        return AppConfig.default()


def save_history(items: List[Dict[str, Any]], limit: int = 2000) -> str:
    """把消息日志落盘，方便下次启动回看。"""
    path = history_path()
    _ensure_dir(os.path.dirname(path) or ".")
    payload = items[-limit:]
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return path


def load_history(limit: int = 2000) -> List[Dict[str, Any]]:
    path = history_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data[-limit:] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []
