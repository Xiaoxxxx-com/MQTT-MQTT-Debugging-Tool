"""主窗口：串联 MQTT 服务、事件循环、各功能页与全局快捷键。

事件流
------
paho 回调线程 → ``queue.Queue`` → Tk ``after`` 轮询 → 更新控件。
控件回调 → ``MQTTService`` 的线程安全方法。
两边从不直接互调，避免 Tkinter 跨线程崩溃。
"""

from __future__ import annotations

import json
import queue
import random
import re
import time
import tkinter as tk
import uuid as uuid_mod
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

from .. import __version__
from ..config import AppConfig, MessageRecord, Subscription, load, load_history, save, save_history
from ..core import (
    STATE_TEXT,
    ConnState,
    Event,
    EventType,
    MQTTService,
    decode_payload,
    topic_matches,
)
from ..script import PeriodicPublisher, ScriptEngine
from . import theme
from .automation import AutomationPanel
from .broker_panel import BrokerPanel
from .connection import ConnectionPanel
from .log import LogPanel
from .publish import PublishPanel
from .settings import SettingsPanel
from .subscribe import SubscribePanel
from .tree import TopicTreePanel
from .widgets import Card, StatusPill, Toast, ToolTip

TAB_DEFS = [
    ("connection", "连接", "⚙"),
    ("subscribe", "订阅", "☰"),
    ("publish", "发布", "➤"),
    ("log", "消息日志", "▤"),
    ("tree", "主题树", "⊞"),
    ("automation", "自动化", "⚡"),
    ("broker", "本地 Broker", "⊛"),
    ("settings", "设置", "☷"),
]


class MQTTDebuggerApp:
    """应用主控。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg: AppConfig = load()
        self.dirty = False

        theme.setup(root)
        self._configure_window()

        # ---- 服务层 ---- #
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.service = MQTTService(self.events)
        self.scripts = ScriptEngine(
            publish_func=self._script_publish,
            log_func=self.log,
            emit_record=self._script_record,
        )
        self.periodic = PeriodicPublisher(self._periodic_publish, self.log)

        # ---- 统计辅助 ---- #
        self.topic_counter: Dict[str, int] = {}
        self.recent_topics: List[str] = []
        self.panels: Dict[str, Any] = {}
        self._counter_tick = 0

        self._build_ui()
        self.service.subscriptions = self.cfg.subscriptions
        self.sync_scripts()

        # 恢复日志 / 配置
        if self.cfg.ui.get("restore_log"):
            self._restore_log()

        self._poll_events()
        self._autosave_loop()
        self._update_clock()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._bind_shortcuts()
        self.log(f"MQTT 调试上位机 v{__version__} 已就绪", "info")

        # 本地 Broker 按设置自动启动（需要在界面就绪后执行）
        self.root.after(300, self.panels["broker"].autostart_if_needed)

    # ================================================================== #
    # 窗口骨架
    # ================================================================== #
    def _configure_window(self) -> None:
        c = theme.COLORS
        self.root.title(f"MQTT 全功能调试上位机 v{__version__}")
        self.root.configure(bg=c["bg"])

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        # 最小尺寸随屏幕收缩，小屏（1024×768 / 1366×768 等）也能完整显示
        self.root.minsize(max(320, min(980, sw - 40)), max(240, min(620, sh - 110)))

        geo = self.cfg.ui.get("geometry")
        restored = self._validated_geometry(geo, sw, sh) if isinstance(geo, str) else None
        if restored:
            self.root.geometry(restored)
        else:
            # 按屏幕大小自适应，避免超出可视区域
            self.root.update_idletasks()
            w = max(720, min(1440, int(sw * 0.86), sw - 40))
            h = max(520, min(920, int(sh * 0.86), sh - 80))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        try:
            self.root.iconname("MQTT 调试上位机")
        except tk.TclError:
            pass

    @staticmethod
    def _validated_geometry(geo: str, sw: int, sh: int) -> Optional[str]:
        """把保存的窗口几何限制在当前屏幕可视范围内。

        场景：换分辨率 / 换显示器 / 接投影仪后，旧配置里记录的
        尺寸或坐标可能超出屏幕，导致窗口「打不开」或只剩一角。
        """
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geo.strip())
        if not m:
            return None
        w, h, x, y = (int(g) for g in m.groups())
        if w <= 0 or h <= 0:
            return None
        w = max(320, min(w, sw - 20))
        h = max(240, min(h, sh - 60))
        x = max(0, min(x, max(0, sw - 160)))
        y = max(0, min(y, max(0, sh - 80)))
        return f"{w}x{h}+{x}+{y}"

    def _ui_scaling(self) -> float:
        """当前 tk 字体缩放系数（点 → 像素），用于固定高度控件的换算。"""
        try:
            return max(0.5, min(3.0, float(self.root.tk.call("tk", "scaling"))))
        except (tk.TclError, TypeError, ValueError):
            return 1.333

    def _build_ui(self) -> None:
        self._build_header()
        self._build_body()
        self._build_statusbar()

    # ------------------------------------------------------------------ #
    def _build_header(self) -> None:
        c = theme.COLORS
        # 高度随 tk 缩放系数走，高分屏下不会压住两行文字
        header_h = max(52, int(23 * self._ui_scaling()) + 18)
        header = tk.Frame(self.root, bg=c["surface"], height=header_h)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        # 品牌区
        brand = tk.Frame(header, bg=c["surface"])
        brand.pack(side="left", padx=(16, 20), pady=8)
        tk.Label(brand, text="MQTT", bg=c["surface"], fg=c["primary"],
                 font=(theme.FONTS["h1"][0], 16, "bold")).pack(side="left")
        tk.Label(brand, text=" 调试上位机", bg=c["surface"], fg=c["text"],
                 font=(theme.FONTS["base"][0], 11)).pack(side="left", pady=(4, 0))

        # 状态胶囊
        self.pill = StatusPill(header, "未连接", c["text_faint"], bg=c["surface"], width=104)
        self.pill.pack(side="left", padx=(0, 16), pady=12)

        # 连接信息
        info = tk.Frame(header, bg=c["surface"])
        info.pack(side="left", pady=10)
        self.lbl_broker = tk.Label(info, text="—", bg=c["surface"], fg=c["text"],
                                   font=theme.FONTS["bold"])
        self.lbl_broker.pack(anchor="w")
        self.lbl_broker_sub = tk.Label(info, text="尚未连接任何 Broker", bg=c["surface"],
                                       fg=c["text_faint"], font=theme.FONTS["tiny"])
        self.lbl_broker_sub.pack(anchor="w")

        # 右侧：计时 / 统计 / 快捷动作
        right = tk.Frame(header, bg=c["surface"])
        right.pack(side="right", padx=16, pady=8)

        self.lbl_clock = tk.Label(right, text="", bg=c["surface"], fg=c["text_muted"],
                                  font=theme.FONTS["small"])
        self.lbl_clock.pack(side="right", padx=(12, 0))

        self.btn_quick_connect = ttk.Button(right, text="连接", style="Success.TButton",
                                            command=self._header_connect)
        self.btn_quick_connect.pack(side="right", padx=6)

        self.lbl_uptime = tk.Label(right, text="", bg=c["surface"], fg=c["text_muted"],
                                   font=theme.FONTS["small"])
        self.lbl_uptime.pack(side="right", padx=(0, 8))

        # 分隔线
        tk.Frame(self.root, bg=c["border"], height=1).pack(fill="x", side="top")

    # ------------------------------------------------------------------ #
    def _build_body(self) -> None:
        body = ttk.Frame(self.root, style="TFrame")
        body.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        self.panels["connection"] = ConnectionPanel(self.notebook, self)
        self.panels["subscribe"] = SubscribePanel(self.notebook, self)
        self.panels["publish"] = PublishPanel(self.notebook, self)
        self.panels["log"] = LogPanel(self.notebook, self)
        self.panels["tree"] = TopicTreePanel(self.notebook, self)
        self.panels["automation"] = AutomationPanel(self.notebook, self)
        self.panels["broker"] = BrokerPanel(self.notebook, self)
        self.panels["settings"] = SettingsPanel(self.notebook, self)

        self._tab_index: Dict[str, int] = {}
        for idx, (key, label, icon) in enumerate(TAB_DEFS):
            self.notebook.add(self.panels[key], text=f"  {icon}  {label}  ")
            self._tab_index[key] = idx

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ------------------------------------------------------------------ #
    def _build_statusbar(self) -> None:
        c = theme.COLORS
        tk.Frame(self.root, bg=c["border"], height=1).pack(fill="x", side="bottom")
        bar_h = max(26, int(11 * self._ui_scaling()) + 12)
        bar = tk.Frame(self.root, bg=c["surface"], height=bar_h)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self.lbl_status = tk.Label(bar, text="就绪", bg=c["surface"], fg=c["text_muted"],
                                   font=theme.FONTS["small"], anchor="w")
        self.lbl_status.pack(side="left", padx=12)

        self.lbl_stats = tk.Label(bar, text="", bg=c["surface"], fg=c["text_faint"],
                                  font=theme.FONTS["mono_small"])
        self.lbl_stats.pack(side="right", padx=12)

        self.lbl_dirty = tk.Label(bar, text="", bg=c["surface"], fg=c["warning"],
                                  font=theme.FONTS["small"])
        self.lbl_dirty.pack(side="right", padx=6)

    # ================================================================== #
    # 快捷键
    # ================================================================== #
    def _bind_shortcuts(self) -> None:
        binds = [
            ("<Control-Return>", lambda e: self._publish_shortcut()),
            ("<F5>", lambda e: self._header_connect()),
            ("<Control-s>", lambda e: self.save_config()),
            ("<Control-l>", lambda e: self.panels["log"]._clear()),
            ("<Control-k>", lambda e: self.panels["log"]._clear_filters()),
            ("<Control-Key-1>", lambda e: self.select_tab("connection")),
            ("<Control-Key-2>", lambda e: self.select_tab("subscribe")),
            ("<Control-Key-3>", lambda e: self.select_tab("publish")),
            ("<Control-Key-4>", lambda e: self.select_tab("log")),
            ("<Control-Key-5>", lambda e: self.select_tab("tree")),
            ("<Control-Key-6>", lambda e: self.select_tab("automation")),
            ("<Control-Key-7>", lambda e: self.select_tab("broker")),
            ("<Control-Key-8>", lambda e: self.select_tab("settings")),
        ]
        for seq, func in binds:
            self.root.bind_all(seq, func, add="+")

    def _publish_shortcut(self) -> None:
        if self.current_tab() == "publish":
            self.panels["publish"]._publish()

    # ================================================================== #
    # 事件循环
    # ================================================================== #
    def _poll_events(self) -> None:
        """每 40ms 批量消费服务层投递过来的事件。"""
        processed = 0
        try:
            while processed < 400:
                event = self.events.get_nowait()
                processed += 1
                self._dispatch(event)
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - 界面不能因为单个回调崩溃
            self.log(f"事件处理异常：{exc}", "error")

        self._counter_tick += 1
        if self._counter_tick % 12 == 0:      # 约 0.5 秒
            self._refresh_status_stats()
        try:
            self.root.after(40, self._poll_events)
        except tk.TclError:  # pragma: no cover
            pass

    def _dispatch(self, event: Event) -> None:
        kind = event.type
        data = event.payload

        if kind == EventType.STATE:
            state: ConnState = data["state"]
            detail = data.get("detail", "")
            self._on_state(state, detail)
        elif kind == EventType.MESSAGE:
            self._on_message(data["record"], data.get("properties") or {})
        elif kind == EventType.LOG:
            self._append_console(data)
        elif kind == EventType.SUBSCRIBED:
            self.panels["subscribe"].refresh()
        elif kind == EventType.PUBLISHED:
            pass
        elif kind == EventType.ERROR:
            self._append_console({"text": str(data), "level": "error", "ts": time.time()})

    # ------------------------------------------------------------------ #
    def _on_state(self, state: ConnState, detail: str = "") -> None:
        text = STATE_TEXT.get(state, str(state))
        color = theme.STATE_COLOR.get(state.value, theme.COLORS["text_faint"])
        self.pill.set(text, color)
        self.panels["connection"].on_state_changed(state.value, detail)

        cfg = self.cfg.active
        self.lbl_broker.configure(text=cfg.name or cfg.host)
        if state == ConnState.CONNECTED:
            self.lbl_broker_sub.configure(text=f"{cfg.protocol} · {cfg.summary()}", fg=theme.COLORS["success"])
            self.btn_quick_connect.configure(text="断开", style="Danger.TButton")
            self.set_status(f"已连接到 {cfg.summary()}")
        elif state in (ConnState.CONNECTING, ConnState.RECONNECTING):
            self.lbl_broker_sub.configure(text=f"正在连接 {cfg.summary()} …", fg=theme.COLORS["warning"])
            self.btn_quick_connect.configure(text="取消", style="Flat.TButton")
            self.set_status(text)
        else:
            self.lbl_broker_sub.configure(text="尚未连接任何 Broker", fg=theme.COLORS["text_faint"])
            self.btn_quick_connect.configure(text="连接", style="Success.TButton")
            self.set_status(f"{text}　{detail}".strip())
        self.panels["subscribe"].refresh()

        if state == ConnState.CONNECTED:
            self._fetch_session_info()

    def _fetch_session_info(self) -> None:
        """连接成功后读取 MQTT 5.0 会话属性，展示在连接页。"""
        client = self.service.client
        if client is None:
            return
        info: Dict[str, Any] = {}
        try:
            info["session_present"] = getattr(client, "_session_present", None)
        except AttributeError:
            pass
        # paho 未暴露 server keepalive，这里给出本地配置值作为参考
        cfg = self.cfg.active
        info["server_keepalive"] = f"{cfg.keepalive}s（本地配置）"
        if cfg.protocol == "MQTT 5.0":
            info["server_version"] = "MQTT 5.0"
        else:
            info["server_version"] = cfg.protocol
        self.panels["connection"].update_session_info(info)

    # ------------------------------------------------------------------ #
    def _on_message(self, record: MessageRecord, properties: Dict[str, Any]) -> None:
        self.panels["log"].add_record(record, properties)
        if record.direction in ("in", "sys"):
            self.panels["tree"].on_message(record)

        if record.topic not in self.recent_topics:
            self.recent_topics.insert(0, record.topic)
            del self.recent_topics[60:]

        # 规则引擎
        if self.scripts.enabled and record.direction == "in":
            hits = self.scripts.on_message(record.topic, record.payload)
            if hits:
                panel = self.panels.get("automation")
                if panel is not None:
                    self.root.after(200, panel.reload_hits)

        # 语法糖：把消息同步到发布页的主题历史里，方便快速回复
        if record.direction == "in" and record.topic not in self.cfg.publish_history:
            self.cfg.publish_history.append(record.topic)
            del self.cfg.publish_history[:-200]
            self.panels["publish"].refresh_history()

    # ------------------------------------------------------------------ #
    def _append_console(self, data: Dict[str, Any]) -> None:
        self.log(data.get("text", ""), data.get("level", "info"))

    def log(self, text: str, level: str = "info") -> None:
        """写入底栏状态，并把重要信息回显到消息日志的"系统"条目。"""
        self.set_status(text)
        if level in ("error", "warn", "success"):
            record = MessageRecord(
                timestamp=time.time(),
                topic="[系统]",
                payload=text,
                direction="sys",
                size=len(text.encode("utf-8")),
                raw=text.encode("utf-8"),
            )
            panel = self.panels.get("log")
            if panel is not None:
                tags = ("error",) if level == "error" else ()
                panel.add_record(record)
                # 给这一行打上颜色
                children = panel.tree.get_children()
                if children:
                    panel.tree.item(children[-1], tags=("sys",) + tags)

    def set_status(self, text: str) -> None:
        try:
            self.lbl_status.configure(text=text[:160])
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    def _refresh_status_stats(self) -> None:
        c = self.service.stats
        self.lbl_stats.configure(
            text=f"RX {c['rx_count']}　TX {c['tx_count']}　"
                 f"↓{_fmt_bytes(c['rx_bytes'])} ↑{_fmt_bytes(c['tx_bytes'])}　"
                 f"重连 {c['reconnects']}"
        )
        self.panels["log"]._update_stats()
        self.panels["subscribe"].update_counts()
        if self.dirty:
            self.lbl_dirty.configure(text="● 有未保存的更改")

    def _update_clock(self) -> None:
        now = datetime.now()
        text = now.strftime("%Y-%m-%d %H:%M:%S")
        self.lbl_clock.configure(text=text)
        if self.service.state == ConnState.CONNECTED:
            up = self.service.uptime()
            self.lbl_uptime.configure(text=f"在线 {_fmt_duration(up)}")
        else:
            self.lbl_uptime.configure(text="")
        try:
            self.root.after(1000, self._update_clock)
        except tk.TclError:  # pragma: no cover
            pass

    def _autosave_loop(self) -> None:
        if self.dirty and self.cfg.ui.get("autosave_config", True):
            self.save_config(quiet=True)
        try:
            self.root.after(20000, self._autosave_loop)
        except tk.TclError:  # pragma: no cover
            pass

    # ================================================================== #
    # 页面与状态
    # ================================================================== #
    def current_tab(self) -> str:
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return "connection"
        for key, i in self._tab_index.items():
            if i == idx:
                return key
        return "connection"

    def select_tab(self, key: str) -> None:
        idx = self._tab_index.get(key)
        if idx is not None:
            try:
                self.notebook.select(idx)
            except tk.TclError:
                pass

    def _on_tab_changed(self, _event=None) -> None:
        tab = self.current_tab()
        if tab == "subscribe":
            self.panels["subscribe"].refresh()
        elif tab == "automation":
            self.panels["automation"].reload_hits()
        elif tab == "broker":
            self.panels["broker"].refresh_from_config()
        elif tab == "settings":
            self.panels["settings"].refresh_env()

    def toast(self, message: str, kind: str = "info") -> None:
        try:
            Toast(self.root, message, kind)
        except tk.TclError:  # pragma: no cover
            pass

    def mark_dirty(self) -> None:
        self.dirty = True
        self.lbl_dirty.configure(text="● 有未保存的更改")

    def save_config(self, quiet: bool = False) -> None:
        try:
            self.cfg.ui["geometry"] = self.root.geometry()
            save(self.cfg)
            self.dirty = False
            self.lbl_dirty.configure(text="")
            if not quiet:
                self.set_status("配置已保存")
        except OSError as exc:
            self.set_status(f"保存配置失败：{exc}")

    def on_profile_changed(self) -> None:
        cfg = self.cfg.active
        self.service.subscriptions = self.cfg.subscriptions
        if self.service.is_connected:
            cfg_now = self.service._cfg
            if cfg_now is not None and cfg_now.name != cfg.name:
                self.set_status("连接参数已修改，需要重新连接才会生效")

    def cfg_save_to(self, path: str) -> None:
        save(self.cfg, path)

    def cfg_load_from(self, path: str) -> bool:
        cfg = load(path)
        self.cfg.connections = cfg.connections
        self.cfg.active_index = cfg.active_index
        self.cfg.subscriptions = cfg.subscriptions
        self.cfg.scripts = cfg.scripts
        self.cfg.payload_templates = cfg.payload_templates
        self.cfg.publish_history = cfg.publish_history
        self.service.subscriptions = self.cfg.subscriptions
        self.sync_scripts()
        self._reload_all_panels()
        self.mark_dirty()
        return True

    def reset_config(self) -> None:
        self.cfg = AppConfig.default()
        self.service.subscriptions = self.cfg.subscriptions
        self.sync_scripts()
        self._reload_all_panels()
        self.save_config(quiet=True)

    def _reload_all_panels(self) -> None:
        self.panels["connection"].refresh_from_config()
        self.panels["subscribe"].refresh()
        self.panels["publish"].refresh_history()
        self.panels["publish"].refresh_templates()
        self.panels["automation"].refresh()
        self.panels["broker"].refresh_from_config()

    # ================================================================== #
    # 连接 / 订阅 / 发布
    # ================================================================== #
    def connect(self) -> None:
        self.cfg.ui["geometry"] = self.root.geometry()
        self.service.subscriptions = self.cfg.subscriptions
        self.service.connect(self.cfg.active)

    def disconnect(self) -> None:
        self.service.disconnect()

    def _header_connect(self) -> None:
        if self.service.is_connected:
            self.disconnect()
        elif self.service.state in (ConnState.CONNECTING, ConnState.RECONNECTING):
            self.service.disconnect()
            self.toast("已取消连接", "warn")
        else:
            self.connect()

    def apply_ui_settings(self) -> None:
        max_rows = int(self.cfg.ui.get("max_log_rows", 5000))
        panel = self.panels.get("log")
        if panel is not None:
            global MAX_ROWS
            import mqtt_client.ui.log as log_mod

            log_mod.MAX_ROWS = max(200, max_rows)

    def _restore_log(self) -> None:
        rows = load_history()
        if not rows:
            return
        for row in rows[-500:]:
            try:
                record = MessageRecord(
                    timestamp=float(row.get("timestamp", time.time())),
                    topic=row.get("topic", ""),
                    payload=row.get("payload", ""),
                    qos=int(row.get("qos", 0)),
                    retain=bool(row.get("retain", False)),
                    direction=row.get("direction", "in"),
                    mid=int(row.get("mid", 0)),
                    size=int(row.get("size", 0)),
                    raw=row.get("payload", "").encode("utf-8", errors="ignore"),
                )
            except (TypeError, ValueError):
                continue
            self.panels["log"].add_record(record)
            if record.direction == "in":
                self.panels["tree"].on_message(record)
        self.set_status(f"已恢复 {len(rows)} 条历史日志")

    # ------------------------------------------------------------------ #
    def render_variables(self, text: str) -> str:
        """替换发布内容/主题里的占位符。"""
        if "{" not in text:
            return text
        now = datetime.now()

        def _rand(match: "object") -> str:
            return str(random.randint(0, 99999))

        replacements = {
            "{ts}": str(int(time.time() * 1000)),
            "{time}": now.strftime("%Y-%m-%d %H:%M:%S"),
            "{date}": now.strftime("%Y-%m-%d"),
            "{iso}": now.isoformat(timespec="seconds"),
            "{uuid}": uuid_mod.uuid4().hex,
            "{rand}": str(random.randint(0, 99999)),
            "{topic}": self.cfg.active.name,
        }
        for key, value in replacements.items():
            text = text.replace(key, value)
        # {topicN} 供规则的模板复用
        return text

    def remember_publish_topic(self, topic: str) -> None:
        history = self.cfg.publish_history
        if topic in history:
            history.remove(topic)
        history.append(topic)
        del history[:-200]
        self.mark_dirty()

    # ------------------------------------------------------------------ #
    def sync_scripts(self) -> None:
        self.scripts.set_rules(self.cfg.scripts)

    def _script_publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        return self.service.publish(topic, payload, qos, retain)

    def _script_record(self, info: Dict[str, Any]) -> None:
        record = MessageRecord(
            timestamp=time.time(),
            topic=info.get("topic", ""),
            payload=info.get("payload", ""),
            qos=int(info.get("qos", 0)),
            retain=bool(info.get("retain", False)),
            direction="out",
            size=len(str(info.get("payload", "")).encode("utf-8")),
            raw=str(info.get("payload", "")).encode("utf-8"),
        )
        self.events.put(Event(EventType.MESSAGE, {"record": record, "properties": {}}))

    def _periodic_publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> bool:
        ok = self.service.publish(topic, payload, qos, retain)
        if ok:
            record = MessageRecord(
                timestamp=time.time(), topic=topic, payload=decode_payload(payload),
                qos=qos, retain=retain, direction="out",
                size=len(payload), raw=payload,
            )
            self.events.put(Event(EventType.MESSAGE, {"record": record, "properties": {}}))
        return ok

    def set_log_filter(self, topic: str) -> None:
        self.panels["log"].set_topic_filter(topic)
        self.select_tab("log")

    def refresh_topic_shortcuts(self) -> None:
        self.panels["automation"]._refresh_topic_choices()

    def get_topic_list(self) -> List[str]:
        return [s.topic for s in self.cfg.subscriptions]

    # ================================================================== #
    # 退出
    # ================================================================== #
    def on_close(self) -> None:
        if self.cfg.ui.get("confirm_exit") and not messagebox.askyesno(
            "确认退出", "确定要关闭 MQTT 调试上位机吗？", parent=self.root
        ):
            return
        try:
            self.cfg.ui["geometry"] = self.root.geometry()
            if self.cfg.ui.get("autosave_log", True):
                export = [
                    rec.to_dict() for _iid, rec in self.panels["log"].records[-2000:]
                ]
                save_history(export)
            # 先把 Broker 页上的设置写回配置，再落盘
            broker_panel = self.panels.get("broker")
            if broker_panel is not None:
                broker_panel.on_close()
            save(self.cfg)
        except (OSError, ValueError):
            pass

        self.periodic.stop_all()
        self.service.shutdown()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    return f"{size / 1024 / 1024:.1f}M"


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def run() -> None:
    """程序入口。"""
    _enable_windows_dpi_awareness()

    root = tk.Tk()
    try:
        # 按系统 DPI 自动缩放字体（96DPI→1.33、120DPI→1.67、144DPI→2.0 …）
        # 必须在创建任何字体之前设置，且依赖上面已声明 DPI 感知
        dpi = root.winfo_fpixels("1i")
        scaling = max(0.5, min(3.0, dpi / 72.0))
        root.tk.call("tk", "scaling", scaling)
    except tk.TclError:
        pass

    MQTTDebuggerApp(root)
    root.mainloop()


def _enable_windows_dpi_awareness() -> None:
    """在创建 Tk 窗口之前声明 DPI 感知。

    若在窗口创建之后才设置，Windows 会先按 96DPI 虚拟化坐标，
    高分屏下窗口会被系统位图拉伸、字体发虚。
    """
    try:
        from ctypes import windll  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        windll.shcore.SetProcessDpiAwareness(1)  # 系统 DPI 感知
    except (AttributeError, OSError):
        # 旧版 Windows 没有 shcore，退回经典 API
        try:
            windll.user32.SetProcessDPIAware()
        except OSError:
            pass


if __name__ == "__main__":  # pragma: no cover
    run()
