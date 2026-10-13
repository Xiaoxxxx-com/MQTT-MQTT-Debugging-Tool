"""连接配置页。

字段分四组：基础、认证、TLS、遗嘱。右边是服务器返回的会话信息
（MQTT 5.0 才有），底部是连接/断开按钮与实时状态。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Dict, List, Optional

from ..config import (
    MQTT_VERSIONS,
    TRANSPORTS,
    AppConfig,
    ConnectionConfig,
    normalize_broker_url,
    parse_broker_url,
)
from . import theme
from .widgets import Card, LabeledEntry, ScrollableFrame, ToolTip

PRESET_BROKERS = [
    ("EMQX 公共测试服（推荐）", "broker.emqx.io", 1883, False),
    ("EMQX 公共测试服 (TLS)", "broker.emqx.io", 8883, True),
    ("HiveMQ 公共测试服", "broker.hivemq.com", 1883, False),
    ("Mosquitto 官方测试服", "test.mosquitto.org", 1883, False),
    ("Mosquitto 官方测试服 (TLS)", "test.mosquitto.org", 8883, True),
    ("本地 Broker", "127.0.0.1", 1883, False),
    ("本地 Broker (WS)", "127.0.0.1", 8083, False),
]


class ConnectionPanel(ttk.Frame):
    """连接参数编辑 + 连接控制。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.cfg: AppConfig = app.cfg
        self._syncing = False

        # ---- 变量 ---- #
        self.var_name = tk.StringVar()
        self.var_host = tk.StringVar()
        self.var_port = tk.StringVar()
        self.var_client_id = tk.StringVar()
        self.var_protocol = tk.StringVar(value="MQTT 3.1.1")
        self.var_transport = tk.StringVar(value="TCP")
        self.var_ws_path = tk.StringVar(value="/mqtt")
        self.var_keepalive = tk.StringVar(value="60")
        self.var_clean = tk.BooleanVar(value=True)
        self.var_auto_reconnect = tk.BooleanVar(value=True)
        self.var_reconnect_delay = tk.StringVar(value="5")
        self.var_max_reconnect = tk.StringVar(value="0")

        self.var_username = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_show_pwd = tk.BooleanVar(value=False)

        self.var_tls = tk.BooleanVar(value=False)
        self.var_tls_insecure = tk.BooleanVar(value=False)
        self.var_ca = tk.StringVar()
        self.var_cert = tk.StringVar()
        self.var_key = tk.StringVar()

        self.var_will_enabled = tk.BooleanVar(value=False)
        self.var_will_topic = tk.StringVar()
        self.var_will_payload = tk.StringVar()
        self.var_will_qos = tk.StringVar(value="0 - 至多一次")
        self.var_will_retain = tk.BooleanVar(value=False)

        self._build()
        self._bind_traces()
        self.refresh_from_config()

    # ================================================================== #
    # 构建界面
    # ================================================================== #
    def _build(self) -> None:
        wrap = ScrollableFrame(self, bg_style="Card")
        wrap.pack(fill="both", expand=True)
        root = wrap.inner

        # ---------- 顶部工具条：配置档案 ---------- #
        top = ttk.Frame(root, style="Card.TFrame")
        top.pack(fill="x", padx=12, pady=(10, 4))

        ttk.Label(top, text="配置档案", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        self.combo_profile = ttk.Combobox(top, state="readonly", width=22, font=theme.FONTS["base"])
        self.combo_profile.pack(side="left")
        self.combo_profile.bind("<<ComboboxSelected>>", self._on_profile_selected)

        for label, cmd in (("新建", self._new_profile), ("另存副本", self._duplicate_profile),
                           ("重命名", self._rename_profile), ("删除", self._delete_profile)):
            ttk.Button(top, text=label, style="Flat.TButton", command=cmd).pack(side="left", padx=3)

        # ---------- 第一行：基础 + 认证 ---------- #
        row1 = ttk.Frame(root, style="Card.TFrame")
        row1.pack(fill="x", padx=12, pady=4)
        row1.columnconfigure(0, weight=5, minsize=440)
        row1.columnconfigure(1, weight=4, minsize=360)

        basic = Card(row1, title="① 基础连接")
        basic.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._build_basic(basic.body)

        auth = Card(row1, title="② 认证")
        auth.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._build_auth(auth.body)

        # ---------- 第二行：TLS + 遗嘱 ---------- #
        row2 = ttk.Frame(root, style="Card.TFrame")
        row2.pack(fill="x", padx=12, pady=4)
        row2.columnconfigure(0, weight=5, minsize=440)
        row2.columnconfigure(1, weight=4, minsize=360)

        tls = Card(row2, title="③ TLS / SSL")
        tls.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._build_tls(tls.body)

        will = Card(row2, title="④ 遗嘱消息 (LWT)")
        will.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._build_will(will.body)

        # ---------- 第三行：会话信息 ---------- #
        info = Card(root, title="服务器会话信息")
        info.pack(fill="x", padx=12, pady=4)
        self._build_info(info.body)

        # ---------- 底部：连接按钮 ---------- #
        bottom = ttk.Frame(root, style="Card.TFrame")
        bottom.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Separator(bottom, orient="horizontal").pack(fill="x", pady=(0, 8))

        btns = ttk.Frame(bottom, style="Card.TFrame")
        btns.pack(fill="x")
        self.btn_connect = ttk.Button(btns, text="连接", style="Primary.TButton",
                                      command=self._toggle_connect)
        self.btn_connect.pack(side="left")
        ttk.Button(btns, text="测试连通性", style="Flat.TButton",
                   command=self._test_connection).pack(side="left", padx=6)
        ttk.Button(btns, text="保存配置", style="Flat.TButton",
                   command=self.app.save_config).pack(side="left")

        self.lbl_hint = ttk.Label(btns, text="", style="CardMuted.TLabel")
        self.lbl_hint.pack(side="left", padx=12)

    # ------------------------------------------------------------------ #
    def _build_basic(self, parent: ttk.Frame) -> None:
        # ---- 整段地址粘贴框 ---- #
        url_row = ttk.Frame(parent, style="Card.TFrame")
        url_row.pack(fill="x", pady=(0, 8))
        url_row.columnconfigure(1, weight=1)

        ttk.Label(url_row, text="地址栏", style="CardMuted.TLabel").grid(
            row=0, column=0, sticky="w", pady=2, padx=(0, 6))
        self.var_url = tk.StringVar()
        e_url = ttk.Entry(url_row, textvariable=self.var_url, style="Card.TEntry")
        e_url.grid(row=0, column=1, sticky="ew", pady=2, padx=(0, 6))
        ToolTip(
            e_url,
            "可以直接粘贴完整地址，回车后自动拆分到下面各项。\n"
            "例：ws://10.99.79.54:8083/mqtt　或　mqtts://host:8883",
        )
        ttk.Button(url_row, text="解析", style="Flat.TButton",
                   command=self._apply_url).grid(row=0, column=2, sticky="w", pady=2)
        e_url.bind("<Return>", lambda _e: self._apply_url())

        self.lbl_url_hint = ttk.Label(url_row, text="粘贴 ws:// / mqtts:// 等完整地址，回车自动填充下方各项",
                                      style="CardMuted.TLabel")
        self.lbl_url_hint.grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 2))

        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=(0, 8))

        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        ttk.Label(grid, text="地址", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", pady=2, padx=(0, 6))
        e_host = ttk.Entry(grid, textvariable=self.var_host, style="Card.TEntry")
        e_host.grid(row=0, column=1, sticky="ew", pady=2, padx=(0, 10))

        ttk.Label(grid, text="端口", style="CardMuted.TLabel").grid(row=0, column=2, sticky="w", pady=2, padx=(0, 6))
        e_port = ttk.Entry(grid, textvariable=self.var_port, style="Card.TEntry", width=8)
        e_port.grid(row=0, column=3, sticky="ew", pady=2)

        ttk.Label(grid, text="客户端 ID", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=2, padx=(0, 6))
        e_cid = ttk.Entry(grid, textvariable=self.var_client_id, style="Card.TEntry")
        e_cid.grid(row=1, column=1, sticky="ew", pady=2, padx=(0, 10))
        ToolTip(e_cid, "留空则由程序自动生成；同一 Broker 上 ID 重复会互相踢下线")

        btn_rand = ttk.Button(grid, text="随机", style="Ghost.TButton", command=self._random_client_id)
        btn_rand.grid(row=1, column=2, sticky="w", pady=2)

        ttk.Label(grid, text="协议", style="CardMuted.TLabel").grid(row=2, column=0, sticky="w", pady=2, padx=(0, 6))
        combo_proto = ttk.Combobox(grid, textvariable=self.var_protocol, state="readonly",
                                   values=list(MQTT_VERSIONS.keys()), font=theme.FONTS["base"])
        combo_proto.grid(row=2, column=1, sticky="ew", pady=2, padx=(0, 10))

        ttk.Label(grid, text="传输", style="CardMuted.TLabel").grid(row=2, column=2, sticky="w", pady=2, padx=(0, 6))
        combo_trans = ttk.Combobox(grid, textvariable=self.var_transport, state="readonly",
                                   values=list(TRANSPORTS.keys()), font=theme.FONTS["base"], width=10)
        combo_trans.grid(row=2, column=3, sticky="ew", pady=2)
        combo_trans.bind("<<ComboboxSelected>>", lambda e: self._sync_ws_state())

        ttk.Label(grid, text="WS 路径", style="CardMuted.TLabel").grid(row=3, column=0, sticky="w", pady=2, padx=(0, 6))
        self.e_ws = ttk.Entry(grid, textvariable=self.var_ws_path, style="Card.TEntry")
        self.e_ws.grid(row=3, column=1, sticky="ew", pady=2, padx=(0, 10))

        ttk.Label(grid, text="心跳(s)", style="CardMuted.TLabel").grid(row=3, column=2, sticky="w", pady=2, padx=(0, 6))
        e_ka = ttk.Entry(grid, textvariable=self.var_keepalive, style="Card.TEntry", width=8)
        e_ka.grid(row=3, column=3, sticky="ew", pady=2)
        ToolTip(e_ka, "Keep Alive，建议 30~120 秒；0 表示禁用")

        # 常用服务器快捷选择
        quick = ttk.Frame(parent, style="Card.TFrame")
        quick.pack(fill="x", pady=(8, 0))
        ttk.Label(quick, text="快速填充", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        chips = ttk.Frame(quick, style="Card.TFrame")
        chips.pack(fill="x")
        chips.columnconfigure(0, weight=1)

        preset_labels = [
            f"{label}  ({host}:{port}{'  TLS' if tls else ''})"
            for label, host, port, tls in PRESET_BROKERS
        ]
        self.combo_preset = ttk.Combobox(chips, state="readonly", values=preset_labels,
                                         font=theme.FONTS["small"])
        self.combo_preset.grid(row=0, column=0, sticky="ew")
        self.combo_preset.set("选择常用 Broker 一键填入参数…")
        self.combo_preset.bind("<<ComboboxSelected>>", self._on_preset_selected)

    # ------------------------------------------------------------------ #
    def _build_auth(self, parent: ttk.Frame) -> None:
        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="用户名", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", pady=2, padx=(0, 6))
        ttk.Entry(grid, textvariable=self.var_username, style="Card.TEntry").grid(
            row=0, column=1, sticky="ew", pady=2)

        ttk.Label(grid, text="密码", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=2, padx=(0, 6))
        self.e_pwd = ttk.Entry(grid, textvariable=self.var_password, style="Card.TEntry", show="•")
        self.e_pwd.grid(row=1, column=1, sticky="ew", pady=2)

        opts = ttk.Frame(parent, style="Card.TFrame")
        opts.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(opts, text="显示密码", variable=self.var_show_pwd, style="Card.TCheckbutton",
                        command=self._toggle_pwd).pack(anchor="w", pady=1)
        cb = ttk.Checkbutton(opts, text="Clean Session / Clean Start", variable=self.var_clean,
                             style="Card.TCheckbutton")
        cb.pack(anchor="w", pady=1)
        ToolTip(cb, "勾选：每次连接都是全新会话，不接收离线消息\n取消：保留会话与离线消息（需固定 Client ID）")

        ttk.Label(parent, text="公共测试服务器通常不需要用户名密码。",
                  style="CardMuted.TLabel", wraplength=300, justify="left").pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ #
    def _build_tls(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(parent, text="启用 TLS/SSL 加密", variable=self.var_tls,
                        style="Card.TCheckbutton", command=self._sync_tls_state).pack(anchor="w", pady=2)
        self.cb_insecure = ttk.Checkbutton(parent, text="跳过证书校验（测试用，不安全）",
                                           variable=self.var_tls_insecure, style="Card.TCheckbutton")
        self.cb_insecure.pack(anchor="w", pady=2)

        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.pack(fill="x", pady=(6, 0))
        grid.columnconfigure(1, weight=1)

        self.tls_widgets: List[ttk.Widget] = []
        for row, (label, var, hint) in enumerate([
            ("CA 证书", self.var_ca, "服务器 CA 证书路径（.pem/.crt），留空用系统根证书"),
            ("客户端证书", self.var_cert, "双向认证时的客户端证书"),
            ("客户端私钥", self.var_key, "双向认证时的客户端私钥"),
        ]):
            ttk.Label(grid, text=label, style="CardMuted.TLabel").grid(
                row=row, column=0, sticky="w", pady=2, padx=(0, 6))
            entry = ttk.Entry(grid, textvariable=var, style="Card.TEntry")
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            btn = ttk.Button(grid, text="…", style="Ghost.TButton", width=3,
                             command=lambda v=var: self._pick_file(v))
            btn.grid(row=row, column=2, sticky="w", padx=(4, 0))
            self.tls_widgets.extend([entry, btn])
            ToolTip(entry, hint)

        self._sync_tls_state()

    # ------------------------------------------------------------------ #
    def _build_will(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(parent, text="启用遗嘱消息", variable=self.var_will_enabled,
                        style="Card.TCheckbutton", command=self._sync_will_state).pack(anchor="w", pady=2)

        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.pack(fill="x", pady=(4, 0))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="遗嘱主题", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", pady=2, padx=(0, 6))
        e_topic = ttk.Entry(grid, textvariable=self.var_will_topic, style="Card.TEntry")
        e_topic.grid(row=0, column=1, sticky="ew", pady=2)
        ToolTip(e_topic, "客户端异常掉线时，Broker 会代发这条消息到该主题")

        ttk.Label(grid, text="遗嘱内容", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=2, padx=(0, 6))
        ttk.Entry(grid, textvariable=self.var_will_payload, style="Card.TEntry").grid(
            row=1, column=1, sticky="ew", pady=2)

        row = ttk.Frame(grid, style="Card.TFrame")
        row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(row, text="QoS", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        combo = ttk.Combobox(row, textvariable=self.var_will_qos, state="readonly", width=14,
                             values=("0 - 至多一次", "1 - 至少一次", "2 - 恰好一次"), font=theme.FONTS["base"])
        combo.pack(side="left", padx=(0, 10))
        ttk.Checkbutton(row, text="Retain", variable=self.var_will_retain,
                        style="Card.TCheckbutton").pack(side="left")

        self.will_widgets: List[ttk.Widget] = [e_topic] + [w for w in grid.winfo_children()
                                                          if isinstance(w, ttk.Entry)] + [row, combo]
        self._sync_will_state()

    # ------------------------------------------------------------------ #
    def _build_info(self, parent: ttk.Frame) -> None:
        grid = ttk.Frame(parent, style="Card.TFrame")
        grid.pack(fill="x")
        for col in range(6):
            grid.columnconfigure(col, weight=1)

        self.info_labels: Dict[str, ttk.Label] = {}
        items = [
            ("conn_state", "连接状态", "未连接"),
            ("server_ver", "协议版本", "—"),
            ("session_present", "会话是否存在", "—"),
            ("assigned_id", "分配的 Client ID", "—"),
            ("reason", "最近原因码", "—"),
            ("keepalive_resp", "心跳间隔", "—"),
        ]
        for idx, (key, label, default) in enumerate(items):
            cell = ttk.Frame(grid, style="Card.TFrame")
            cell.grid(row=0, column=idx, sticky="ew", pady=1, padx=(0, 14))
            ttk.Label(cell, text=label, style="Section.TLabel").pack(anchor="w")
            value = ttk.Label(cell, text=default, style="Value.TLabel")
            value.pack(anchor="w")
            self.info_labels[key] = value

        self.info_labels["conn_state"].configure(foreground=theme.COLORS["text_faint"])

    # ================================================================== #
    # 状态同步
    # ================================================================== #
    def _bind_traces(self) -> None:
        for var in (self.var_name, self.var_host, self.var_port, self.var_client_id,
                    self.var_protocol, self.var_transport, self.var_ws_path, self.var_keepalive,
                    self.var_clean, self.var_auto_reconnect, self.var_reconnect_delay,
                    self.var_max_reconnect, self.var_username, self.var_password,
                    self.var_tls, self.var_tls_insecure, self.var_ca, self.var_cert,
                    self.var_key, self.var_will_enabled, self.var_will_topic,
                    self.var_will_payload, self.var_will_qos, self.var_will_retain):
            var.trace_add("write", self._on_field_changed)
        self.var_transport.trace_add("write", lambda *_: self._sync_ws_state())

    def _on_field_changed(self, *_args) -> None:
        if self._syncing:
            return
        self._apply_to_config()
        self._check_host_input()
        self.app.mark_dirty()

    def _check_host_input(self) -> None:
        """主机框里若带了协议前缀/路径，实时给出修正提示。

        这是真实踩过的坑：把整条 "ws://10.99.79.54:8083/mqtt" 粘进"地址"框，
        paho 会把 "ws://10.99.79.54" 当域名去做 DNS 解析，失败后静默重试，
        界面永远停在「连接中…」且不报错。所以这里必须提前拦住。
        """
        host = self.var_host.get().strip()
        if not host:
            self._set_url_hint("", "normal")
            return

        looks_like_url = "://" in host or host.lower().startswith(("ws:", "wss:", "mqtt:", "mqtts:"))
        has_path = "/" in host
        if looks_like_url or has_path:
            self._set_url_hint(
                "⚠ 地址栏只需填主机名/IP，不要带 ws:// 前缀和路径。"
                "用上面的「地址栏」粘贴完整地址后回车即可。", "warn")
        elif host.count(":") >= 1 and not host.startswith("["):
            self._set_url_hint("⚠ 端口请填到右侧「端口」框，地址栏只填主机名/IP", "warn")
        else:
            self._set_url_hint("", "normal")

    def _apply_to_config(self) -> None:
        cfg = self.cfg.active
        cfg.name = self.var_name.get().strip() or cfg.host or "未命名连接"
        cfg.host = self.var_host.get().strip()
        cfg.port = _safe_int(self.var_port.get(), 1883)
        cfg.client_id = self.var_client_id.get().strip()
        cfg.protocol = self.var_protocol.get() or "MQTT 3.1.1"
        cfg.transport = self.var_transport.get() or "TCP"
        cfg.ws_path = self.var_ws_path.get().strip() or "/mqtt"
        cfg.keepalive = _safe_int(self.var_keepalive.get(), 60)
        cfg.clean_session = bool(self.var_clean.get())
        cfg.auto_reconnect = bool(self.var_auto_reconnect.get())
        cfg.reconnect_delay = _safe_int(self.var_reconnect_delay.get(), 5)
        cfg.max_reconnect = _safe_int(self.var_max_reconnect.get(), 0)
        cfg.username = self.var_username.get()
        cfg.password = self.var_password.get()
        cfg.use_tls = bool(self.var_tls.get())
        cfg.tls_insecure = bool(self.var_tls_insecure.get())
        cfg.ca_file = self.var_ca.get().strip()
        cfg.cert_file = self.var_cert.get().strip()
        cfg.key_file = self.var_key.get().strip()
        cfg.will.enabled = bool(self.var_will_enabled.get())
        cfg.will.topic = self.var_will_topic.get().strip()
        cfg.will.payload = self.var_will_payload.get()
        cfg.will.qos = int(self.var_will_qos.get().split(" ")[0])
        cfg.will.retain = bool(self.var_will_retain.get())

    def refresh_from_config(self) -> None:
        """把配置读进界面控件。"""
        self._syncing = True
        try:
            cfg = self.cfg.active
            self.var_name.set(cfg.name)
            self.var_host.set(cfg.host)
            self.var_port.set(str(cfg.port))
            self.var_client_id.set(cfg.client_id)
            self.var_protocol.set(cfg.protocol if cfg.protocol in MQTT_VERSIONS else "MQTT 3.1.1")
            self.var_transport.set(cfg.transport if cfg.transport in TRANSPORTS else "TCP")
            self.var_ws_path.set(cfg.ws_path)
            self.var_keepalive.set(str(cfg.keepalive))
            self.var_clean.set(cfg.clean_session)
            self.var_auto_reconnect.set(cfg.auto_reconnect)
            self.var_reconnect_delay.set(str(cfg.reconnect_delay))
            self.var_max_reconnect.set(str(cfg.max_reconnect))
            self.var_username.set(cfg.username)
            self.var_password.set(cfg.password)
            self.var_tls.set(cfg.use_tls)
            self.var_tls_insecure.set(cfg.tls_insecure)
            self.var_ca.set(cfg.ca_file)
            self.var_cert.set(cfg.cert_file)
            self.var_key.set(cfg.key_file)
            self.var_will_enabled.set(cfg.will.enabled)
            self.var_will_topic.set(cfg.will.topic)
            self.var_will_payload.set(cfg.will.payload)
            self.var_will_qos.set(f"{cfg.will.qos} - " + ("至多一次", "至少一次", "恰好一次")[
                min(2, max(0, cfg.will.qos))])
            self.var_will_retain.set(cfg.will.retain)

            self._refresh_profile_combo()
            self._refresh_url_bar()
        finally:
            self._syncing = False
        self._sync_ws_state()
        self._sync_tls_state()
        self._sync_will_state()

    def _refresh_url_bar(self) -> None:
        """把当前配置回显到地址栏（已连接时用实际生效的地址）。"""
        url = normalize_broker_url(self.cfg.active)
        self.var_url.set(url)
        self._set_url_hint("", "normal")

    # ------------------------------------------------------------------ #
    # 地址栏解析
    # ------------------------------------------------------------------ #
    def _apply_url(self) -> None:
        """解析地址栏里的完整 URL，自动拆分填入下方各项。"""
        raw = self.var_url.get().strip()
        if not raw:
            self._set_url_hint("地址栏为空，请粘贴完整地址", "warn")
            return

        parsed = parse_broker_url(raw)
        if not parsed:
            self._set_url_hint(
                "✗ 无法识别。示例：ws://10.99.79.54:8083/mqtt　mqtts://host:8883", "error")
            self.app.toast("地址解析失败，请检查格式", "error")
            return

        self._syncing = True
        try:
            if "host" in parsed:
                self.var_host.set(str(parsed["host"]))
            if "port" in parsed:
                self.var_port.set(str(parsed["port"]))
            if "transport" in parsed:
                self.var_transport.set(str(parsed["transport"]))
            if "ws_path" in parsed:
                self.var_ws_path.set(str(parsed["ws_path"]))
            if "use_tls" in parsed:
                self.var_tls.set(bool(parsed["use_tls"]))
        finally:
            self._syncing = False

        self._sync_ws_state()
        self._sync_tls_state()
        self._apply_to_config()
        self.app.mark_dirty()

        # 反馈解析结果
        bits: List[str] = []
        if "host" in parsed:
            bits.append(f"{parsed['host']}:{parsed.get('port', self.var_port.get())}")
        bits.append(str(parsed.get("transport", self.var_transport.get())))
        if parsed.get("use_tls"):
            bits.append("TLS")
        if parsed.get("transport") == "WebSocket":
            bits.append(str(parsed.get("ws_path", self.var_ws_path.get())))
        self._set_url_hint("✓ 已解析 → " + " · ".join(bits), "ok")
        self.app.toast(f"已解析地址：{' · '.join(bits)}", "success")

    def _set_url_hint(self, text: str, level: str = "normal") -> None:
        color = {
            "normal": theme.COLORS["text_faint"],
            "ok": theme.COLORS.get("success", theme.COLORS["text"]),
            "warn": theme.COLORS.get("warning", theme.COLORS["text"]),
            "error": theme.COLORS.get("danger", theme.COLORS["text"]),
        }.get(level, theme.COLORS["text_faint"])
        try:
            self.lbl_url_hint.configure(text=text, foreground=color)
        except tk.TclError:  # pragma: no cover
            pass

    def _refresh_profile_combo(self) -> None:
        names = [c.name for c in self.cfg.connections]
        self.combo_profile.configure(values=names)
        if names:
            self.combo_profile.current(min(self.cfg.active_index, len(names) - 1))

    # ------------------------------------------------------------------ #
    def _sync_ws_state(self) -> None:
        is_ws = self.var_transport.get() == "WebSocket"
        self.e_ws.configure(state="normal" if is_ws else "disabled")

    def _sync_tls_state(self) -> None:
        enabled = bool(self.var_tls.get())
        state = "normal" if enabled else "disabled"
        for widget in self.tls_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.cb_insecure.configure(state="normal" if enabled else "disabled")
        if self._syncing:
            return
        # 自动联动端口：TCP 1883↔8883 / WS 8083↔8084
        is_ws = self.var_transport.get() == "WebSocket"
        plain, tls_port = ("8083", "8084") if is_ws else ("1883", "8883")
        cur = self.var_port.get().strip()
        if enabled and cur in (plain, ""):
            self.var_port.set(tls_port)
        elif not enabled and cur == tls_port:
            self.var_port.set(plain)

    def _sync_will_state(self) -> None:
        enabled = bool(self.var_will_enabled.get())
        for widget in self.will_widgets:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="readonly" if enabled else "disabled")
                elif isinstance(widget, ttk.Frame):
                    for child in widget.winfo_children():
                        try:
                            if isinstance(child, ttk.Combobox):
                                child.configure(state="readonly" if enabled else "disabled")
                            else:
                                child.configure(state="normal" if enabled else "disabled")
                        except tk.TclError:
                            pass
                else:
                    widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _toggle_pwd(self) -> None:
        self.e_pwd.configure(show="" if self.var_show_pwd.get() else "•")

    def _random_client_id(self) -> None:
        import uuid

        self.var_client_id.set(f"mqttdbg_{uuid.uuid4().hex[:8]}")

    def _on_preset_selected(self, _event=None) -> None:
        idx = self.combo_preset.current()
        if not (0 <= idx < len(PRESET_BROKERS)):
            return
        label, host, port, tls = PRESET_BROKERS[idx]
        self._apply_preset(host, port, tls)

    def _apply_preset(self, host: str, port: int, tls: bool) -> None:
        self.var_host.set(host)
        self.var_port.set(str(port))
        self.var_tls.set(tls)
        self.var_tls_insecure.set(False)
        self._sync_tls_state()
        self.app.toast(f"已填入 {host}:{port}")

    def _pick_file(self, var: tk.StringVar) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="选择证书文件",
            filetypes=[("证书文件", "*.pem *.crt *.cer *.key"), ("所有文件", "*.*")],
        )
        if path:
            var.set(path)

    # ================================================================== #
    # 档案管理
    # ================================================================== #
    def _on_profile_selected(self, _event=None) -> None:
        idx = self.combo_profile.current()
        if idx < 0 or idx >= len(self.cfg.connections):
            return
        self.cfg.active_index = idx
        self.refresh_from_config()
        self.app.mark_dirty()
        self.app.on_profile_changed()

    def _new_profile(self) -> None:
        self.cfg.connections.append(ConnectionConfig(name=f"连接 {len(self.cfg.connections) + 1}"))
        self.cfg.active_index = len(self.cfg.connections) - 1
        self.refresh_from_config()
        self.app.mark_dirty()
        self.app.on_profile_changed()
        self.app.toast("已新建连接档案", "success")

    def _duplicate_profile(self) -> None:
        clone = self.cfg.active.clone(self.cfg.active.name + " 副本")
        self.cfg.connections.append(clone)
        self.cfg.active_index = len(self.cfg.connections) - 1
        self.refresh_from_config()
        self.app.mark_dirty()
        self.app.on_profile_changed()

    def _rename_profile(self) -> None:
        from tkinter import simpledialog

        new_name = simpledialog.askstring("重命名", "新的档案名称：",
                                          initialvalue=self.cfg.active.name, parent=self)
        if new_name:
            self.cfg.active.name = new_name.strip()
            self.refresh_from_config()
            self.app.mark_dirty()
            self.app.on_profile_changed()

    def _delete_profile(self) -> None:
        if len(self.cfg.connections) <= 1:
            messagebox.showinfo("提示", "至少需要保留一个连接档案。", parent=self)
            return
        name = self.cfg.active.name
        if not messagebox.askyesno("确认删除", f"确定删除档案「{name}」吗？", parent=self):
            return
        self.cfg.connections.pop(self.cfg.active_index)
        self.cfg.active_index = max(0, self.cfg.active_index - 1)
        self.refresh_from_config()
        self.app.mark_dirty()
        self.app.on_profile_changed()

    # ================================================================== #
    # 连接动作
    # ================================================================== #
    def _toggle_connect(self) -> None:
        if self.app.service.is_connected:
            self.app.disconnect()
        else:
            self._do_connect()

    def _do_connect(self) -> None:
        self._apply_to_config()
        cfg = self.cfg.active
        if not cfg.host:
            messagebox.showwarning("提示", "请先填写 Broker 地址。", parent=self)
            self.var_host.set("broker.emqx.io")
            return

        # 地址栏里塞了完整 URL？自动拆一次，别让用户卡在「连接中…」
        if "://" in cfg.host or cfg.host.lower().startswith(("ws:", "wss:", "mqtt:", "mqtts:")) \
                or "/" in cfg.host:
            parsed = parse_broker_url(cfg.host)
            if parsed:
                self.var_url.set(cfg.host)
                self._apply_url()
                self._apply_to_config()
                cfg = self.cfg.active
            else:
                messagebox.showwarning(
                    "格式不对",
                    "「地址」框只填主机名或 IP，不要带 ws:// 前缀和路径。\n\n"
                    "正确写法：主机框填 10.99.79.54，端口框填 8083，"
                    "传输选 WebSocket，WS 路径填 /mqtt。\n\n"
                    "也可以把完整地址粘到上面的「地址栏」再回车，会自动拆分。",
                    parent=self)
                return

        if not (0 < cfg.port < 65536):
            messagebox.showwarning("提示", "端口号应在 1 ~ 65535 之间。", parent=self)
            return
        self.app.connect()

    def _test_connection(self) -> None:
        """在后台线程直接做一次 socket/TLS 握手，不污染当前会话。"""
        import socket
        import ssl as ssl_mod
        import threading

        self._apply_to_config()
        cfg = self.cfg.active
        self.lbl_hint.configure(text=f"正在测试 {cfg.host}:{cfg.port} …")

        def _worker() -> None:
            result = ("error", "")
            timeout = max(2.0, min(8.0, cfg.keepalive / 10.0))
            try:
                with socket.create_connection((cfg.host, cfg.port), timeout=timeout) as sock:
                    if cfg.use_tls:
                        ctx = ssl_mod.create_default_context()
                        if cfg.tls_insecure:
                            ctx.check_hostname = False
                            ctx.verify_mode = ssl_mod.CERT_NONE
                        with ctx.wrap_socket(sock, server_hostname=cfg.host) as tls_sock:
                            result = ("ok", f"TLS 握手成功，协议 {tls_sock.version()}")
                    else:
                        result = ("ok", "TCP 连接成功")
            except Exception as exc:  # noqa: BLE001
                result = ("error", str(exc))

            def _report() -> None:
                ok, msg = result
                if ok == "ok":
                    self.lbl_hint.configure(text=f"✓ {msg}")
                    self.app.toast(f"连通性正常：{msg}", "success")
                else:
                    self.lbl_hint.configure(text=f"✗ {msg}")
                    self.app.toast(f"连通性测试失败：{msg}", "error")

            try:
                self.after(0, _report)
            except tk.TclError:  # pragma: no cover
                pass

        threading.Thread(target=_worker, daemon=True).start()

    # ================================================================== #
    def on_state_changed(self, state: str, detail: str = "") -> None:
        color = theme.STATE_COLOR.get(state, theme.COLORS["text_faint"])
        text = {"connected": "已连接", "connecting": "连接中…", "reconnecting": "重连中…",
                "disconnected": "未连接", "failed": "连接失败"}.get(state, state)
        self.info_labels["conn_state"].configure(text=text, foreground=color)
        self.info_labels["reason"].configure(text=detail or "—")

        if state == "connected":
            self.btn_connect.configure(text="断开", style="Danger.TButton")
            self.lbl_hint.configure(text=f"已连接 {self.cfg.active.summary()}")
        elif state in ("connecting", "reconnecting"):
            self.btn_connect.configure(text="取消", style="Flat.TButton")
        else:
            self.btn_connect.configure(text="连接", style="Primary.TButton")
            if not detail:
                self.lbl_hint.configure(text="")

    def update_session_info(self, info: Dict[str, object]) -> None:
        mapping = {
            "server_ver": info.get("server_version"),
            "session_present": info.get("session_present"),
            "assigned_id": info.get("assigned_client_id"),
            "keepalive_resp": info.get("server_keepalive"),
        }
        for key, value in mapping.items():
            if value is None:
                continue
            label = self.info_labels.get(key)
            if label is None:
                continue
            if isinstance(value, bool):
                text = "是" if value else "否"
            else:
                text = str(value)
            label.configure(text=text)


def _safe_int(text: str, default: int) -> int:
    try:
        return int(str(text).strip())
    except (ValueError, TypeError):
        return default
