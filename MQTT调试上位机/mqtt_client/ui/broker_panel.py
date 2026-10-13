"""本地 Broker 页：把本机变成一个可供局域网其他主机连接的 MQTT 服务端。

页面结构
--------
左列：启动/停止 + 监听参数 + 认证与访问控制
中列：在线客户端 / 订阅表 / 保留消息 / 数据包解析（四个子标签）
右列：连接信息（告诉别人填什么地址）+ 实时日志 + 统计

事件流与主界面一致：
    Broker 线程 → ``queue.Queue`` → ``after`` 轮询 → 更新控件。

「数据包解析」标签会把 Broker 收到的 ``dtp.*`` 业务载荷（见
docs/interface/mqtt/data_payload_reference.md）按 schema 解析成表格，
支持类型过滤、msg_id 去重与原始 JSON 详情查看。
"""

from __future__ import annotations

import copy
import json
import queue
import socket
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

from ..broker import BrokerEvent, MQTTBroker, local_ip_addresses
from ..config import BROKER_MAX_QOS_CHOICES, BrokerConfig, qos_label
from ..dtp import SCHEMA_LABELS, describe, parse_packet
from . import theme
from .widgets import Card, LogView, StatusPill, ToolTip

#: 0.0.0.0 的中文说明，界面上反复使用
BIND_HINT = {
    "0.0.0.0": "所有网卡 —— 局域网内其他主机可连接",
    "127.0.0.1": "仅本机 —— 其他主机无法连接",
}


class BrokerPanel(ttk.Frame):
    """内置 MQTT Broker 的管理界面。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.cfg: BrokerConfig = app.cfg.broker

        self.events: "queue.Queue[BrokerEvent]" = queue.Queue()
        self.broker = MQTTBroker(self._working_cfg(), on_event=self.events.put)
        # dtp.* 数据包解析：Broker 线程回调 → 独立队列 → 界面轮询
        self.packet_events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.broker.on_packet = self._on_raw_packet

        self._log_rows = 0
        self._client_iids: List[str] = []
        self._sub_iids: List[str] = []
        self._ret_iids: List[str] = []
        self._tick = 0
        self._running_ui = False

        # 数据包解析视图状态
        self._pkt_list: List[Dict[str, Any]] = []   # 全部已解析包（最新在前）
        self._pkt_rows: Dict[str, Dict[str, Any]] = {}   # iid → 包
        self._pkt_iids: List[str] = []              # 当前显示顺序（最新在前）
        self._pkt_seen: Dict[str, None] = {}        # msg_id 去重（QoS1 可能重复投递）
        self._pkt_dedup = 0
        self._pkt_seq = 0

        self._build()
        self.refresh_from_config()
        self._poll()

    # ================================================================== #
    # 构建界面
    # ================================================================== #
    def _build(self) -> None:
        # 三列布局在窄屏上会超出窗口，用 ScrollableFrame 兜住：
        # 内容自然宽度小于窗口时它就和普通 Frame 一样（表格正常拉伸），
        # 只有真的放不下才出现横向滚动条，不会把右列裁掉。
        from .widgets import ScrollableFrame

        outer = ScrollableFrame(self, bg_style="TFrame")
        outer.pack(fill="both", expand=True)
        wrap = outer.inner
        wrap.configure(style="TFrame")
        wrap.columnconfigure(0, weight=0)     # 左：设置（固定宽）
        wrap.columnconfigure(1, weight=3)     # 中：运行时数据（吃掉多余空间）
        wrap.columnconfigure(2, weight=2)     # 右：日志
        wrap.rowconfigure(0, weight=1)

        self._build_left(wrap)
        self._build_middle(wrap)
        self._build_right(wrap)

    # ------------------------------------------------------------------ #
    def _build_left(self, parent: ttk.Frame) -> None:
        col = ttk.Frame(parent, style="TFrame")
        col.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        col.columnconfigure(0, weight=1)

        # ---------------- 开关 ---------------- #
        ctrl = Card(col, title="Broker 服务")
        ctrl.grid(row=0, column=0, sticky="ew")

        state_row = ttk.Frame(ctrl.body, style="Card.TFrame")
        state_row.pack(fill="x")
        self.pill = StatusPill(state_row, "未启动", theme.COLORS["text_faint"],
                               bg=theme.COLORS["surface"], width=104)
        self.pill.pack(side="left")
        self.lbl_bind = ttk.Label(state_row, text="—", style="CardMuted.TLabel")
        self.lbl_bind.pack(side="left", padx=(8, 0))

        btns = ttk.Frame(ctrl.body, style="Card.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        self.btn_start = ttk.Button(btns, text="启动 Broker", style="Success.TButton",
                                    command=self._toggle)
        self.btn_start.pack(side="left")
        ttk.Button(btns, text="打开防火墙提示", style="Flat.TButton",
                   command=self._show_firewall_tip).pack(side="left", padx=6)

        self.lbl_uptime = ttk.Label(ctrl.body, text="运行时长 0s", style="CardMuted.TLabel")
        self.lbl_uptime.pack(anchor="w", pady=(8, 0))

        # ---------------- 监听参数 ---------------- #
        listen = Card(col, title="监听参数")
        listen.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        g = ttk.Frame(listen.body, style="Card.TFrame")
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(g, text="监听地址", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_bind = tk.StringVar(value=self.cfg.bind_address)
        cmb = ttk.Combobox(g, textvariable=self.var_bind, width=16,
                           values=["0.0.0.0", "127.0.0.1"], state="normal")
        cmb.grid(row=r, column=1, sticky="ew", pady=3)
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_bind_changed())
        ToolTip(cmb, "0.0.0.0 表示监听所有网卡，局域网内其他主机才能连上")

        r += 1
        self.lbl_bind_hint = ttk.Label(g, text="", style="CardMuted.TLabel", wraplength=190)
        self.lbl_bind_hint.grid(row=r, column=1, sticky="w", pady=(0, 4))

        r += 1
        ttk.Label(g, text="端口", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_port = tk.StringVar(value=str(self.cfg.port))
        e_port = ttk.Entry(g, textvariable=self.var_port, style="Card.TEntry", width=16)
        e_port.grid(row=r, column=1, sticky="ew", pady=3)
        ToolTip(e_port, "默认 1884，避开本机可能已安装的 mosquitto（1883）")

        r += 1
        ttk.Label(g, text="最大连接数", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_maxconn = tk.StringVar(value=str(self.cfg.max_connections))
        ttk.Entry(g, textvariable=self.var_maxconn, style="Card.TEntry", width=16).grid(
            row=r, column=1, sticky="ew", pady=3)

        r += 1
        ttk.Label(g, text="最大 QoS", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_maxqos = tk.StringVar(value=qos_label(self.cfg.max_qos))
        ttk.Combobox(g, textvariable=self.var_maxqos, width=14,
                     values=list(BROKER_MAX_QOS_CHOICES),
                     state="readonly").grid(row=r, column=1, sticky="ew", pady=3)

        r += 1
        self.var_retain = tk.BooleanVar(value=self.cfg.enable_retain)
        ttk.Checkbutton(g, text="启用保留消息（Retain）", variable=self.var_retain,
                        style="Card.TCheckbutton").grid(row=r, column=0, columnspan=2,
                                                        sticky="w", pady=(6, 2))

        r += 1
        self.var_logpub = tk.BooleanVar(value=self.cfg.log_publish)
        ttk.Checkbutton(g, text="记录每一条 PUBLISH（日志会很多）",
                        variable=self.var_logpub,
                        style="Card.TCheckbutton").grid(row=r, column=0, columnspan=2,
                                                        sticky="w", pady=2)

        r += 1
        self.var_autostart = tk.BooleanVar(value=self.cfg.auto_start)
        ttk.Checkbutton(g, text="启动上位机时自动开启 Broker", variable=self.var_autostart,
                        style="Card.TCheckbutton").grid(row=r, column=0, columnspan=2,
                                                        sticky="w", pady=(2, 4))

        save_row = ttk.Frame(listen.body, style="Card.TFrame")
        save_row.pack(fill="x", pady=(8, 0))
        ttk.Button(save_row, text="保存设置", style="Flat.TButton",
                   command=self._save_settings).pack(side="left")

        # ---------------- WebSocket 传输 ---------------- #
        ws = Card(col, title="WebSocket 传输")
        ws.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        gw = ttk.Frame(ws.body, style="Card.TFrame")
        gw.pack(fill="x")
        gw.columnconfigure(1, weight=1)

        self.var_ws_enable = tk.BooleanVar(value=self.cfg.enable_websocket)
        cb_ws = ttk.Checkbutton(
            gw, text="同时监听 WebSocket 端口", variable=self.var_ws_enable,
            style="Card.TCheckbutton", command=self._sync_ws_state)
        cb_ws.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ToolTip(cb_ws,
                "浏览器 / 小程序 / 微信云函数只能用 WebSocket 连 MQTT。\n"
                "打开后本机同时提供 TCP 与 WS 两个入口，连的是同一个 Broker。")

        ttk.Label(gw, text="WS 端口", style="CardMuted.TLabel").grid(
            row=1, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_ws_port = tk.StringVar(value=str(self.cfg.websocket_port))
        self.e_ws_port = ttk.Entry(gw, textvariable=self.var_ws_port,
                                   style="Card.TEntry", width=16)
        self.e_ws_port.grid(row=1, column=1, sticky="ew", pady=3)
        ToolTip(self.e_ws_port, "默认 8083（与 EMQX 一致）。不能与上面的 MQTT 端口相同。")

        ttk.Label(gw, text="WS 路径", style="CardMuted.TLabel").grid(
            row=2, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_ws_path = tk.StringVar(value=self.cfg.websocket_path)
        self.e_ws_path = ttk.Entry(gw, textvariable=self.var_ws_path,
                                   style="Card.TEntry", width=16)
        self.e_ws_path.grid(row=2, column=1, sticky="ew", pady=3)
        ToolTip(self.e_ws_path, "必须以 / 开头，例如 /mqtt（EMQX 默认值）")

        self.lbl_ws_hint = ttk.Label(gw, text="", style="CardMuted.TLabel",
                                     wraplength=190, justify="left")
        self.lbl_ws_hint.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # 输入变化时实时刷新地址提示
        for var in (self.var_ws_port, self.var_ws_path, self.var_port):
            var.trace_add("write", lambda *_: self._sync_ws_state())

        # ---------------- 认证与访问控制 ---------------- #
        auth = Card(col, title="认证与访问控制")
        auth.grid(row=3, column=0, sticky="ew", pady=(8, 0))

        ga = ttk.Frame(auth.body, style="Card.TFrame")
        ga.pack(fill="x")
        ga.columnconfigure(1, weight=1)

        self.var_require_auth = tk.BooleanVar(value=self.cfg.require_auth)
        ttk.Checkbutton(ga, text="要求用户名密码", variable=self.var_require_auth,
                        style="Card.TCheckbutton").grid(row=0, column=0, columnspan=2,
                                                        sticky="w", pady=(0, 4))

        ttk.Label(ga, text="用户名", style="CardMuted.TLabel").grid(
            row=1, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_user = tk.StringVar(value=self.cfg.username)
        ttk.Entry(ga, textvariable=self.var_user, style="Card.TEntry", width=16).grid(
            row=1, column=1, sticky="ew", pady=3)

        ttk.Label(ga, text="密码", style="CardMuted.TLabel").grid(
            row=2, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_pass = tk.StringVar(value=self.cfg.password)
        ttk.Entry(ga, textvariable=self.var_pass, style="Card.TEntry", width=16,
                  show="•").grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(ga, text="IP 白名单", style="CardMuted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(8, 3), padx=(0, 8))
        self.var_allow = tk.StringVar(value=", ".join(self.cfg.allow_list))
        e_allow = ttk.Entry(ga, textvariable=self.var_allow, style="Card.TEntry", width=16)
        e_allow.grid(row=3, column=1, sticky="ew", pady=(8, 3))
        ToolTip(e_allow, "留空表示不限制。多条用逗号分隔，支持 192.168.1.0/24 网段写法")

        ttk.Label(ga, text="IP 黑名单", style="CardMuted.TLabel").grid(
            row=4, column=0, sticky="w", pady=3, padx=(0, 8))
        self.var_block = tk.StringVar(value=", ".join(self.cfg.block_list))
        e_block = ttk.Entry(ga, textvariable=self.var_block, style="Card.TEntry", width=16)
        e_block.grid(row=4, column=1, sticky="ew", pady=3)
        ToolTip(e_block, "黑名单优先级高于白名单，无论是否开启认证都会拦截")

    # ------------------------------------------------------------------ #
    def _build_middle(self, parent: ttk.Frame) -> None:
        col = ttk.Frame(parent, style="TFrame")
        col.grid(row=0, column=1, sticky="nsew")
        col.columnconfigure(0, weight=1)
        col.rowconfigure(1, weight=1)

        # ---------------- 连接信息（最重要，放最上面） ---------------- #
        info = Card(col, title="其他主机连接本机时填写")
        info.grid(row=0, column=0, sticky="ew")

        self.lbl_conn_hint = tk.Text(
            info.body, width=1, height=4, wrap="none", font=theme.FONTS["mono_small"],
            bg=theme.COLORS["primary_soft"], fg=theme.COLORS["text"],
            relief="flat", bd=0, highlightthickness=0, padx=10, pady=7,
        )
        self.lbl_conn_hint.pack(fill="x")
        self.lbl_conn_hint.configure(state="disabled")

        act = ttk.Frame(info.body, style="Card.TFrame")
        act.pack(fill="x", pady=(8, 0))
        ttk.Button(act, text="复制连接地址", style="Flat.TButton",
                   command=self._copy_address).pack(side="left")
        ttk.Button(act, text="让本机连上自己", style="Primary.TButton",
                   command=self._connect_self).pack(side="left", padx=6)
        ToolTip(act.winfo_children()[-1],
                "把本机也当作一个客户端连到刚启动的 Broker，方便单机自测收发")

        # ---------------- 运行时数据（三个子标签） ---------------- #
        nb = ttk.Notebook(col)
        nb.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.nb = nb

        # 客户端
        frame_cli = ttk.Frame(nb, style="Card.TFrame")
        nb.add(frame_cli, text="  在线客户端  ")
        self.tv_clients = LogView(
            frame_cli,
            columns=[
                ("client_id", "客户端标识", 128, "w"),
                ("remote", "来源地址", 112, "w"),
                ("protocol", "协议", 56, "center"),
                ("keepalive", "心跳", 56, "center"),
                ("clean", "Clean", 52, "center"),
                ("subs", "订阅", 44, "center"),
                ("uptime", "在线", 60, "center"),
                ("username", "用户名", 66, "w"),
            ],
            height=10,
        )
        self.tv_clients.pack(fill="both", expand=True, padx=8, pady=8)

        # 订阅
        frame_sub = ttk.Frame(nb, style="Card.TFrame")
        nb.add(frame_sub, text="  订阅表  ")
        self.tv_subs = LogView(
            frame_sub,
            columns=[
                ("topic", "主题过滤器", 220, "w"),
                ("qos", "QoS", 56, "center"),
                ("client_id", "订阅者", 160, "w"),
            ],
            height=10,
        )
        self.tv_subs.pack(fill="both", expand=True, padx=8, pady=8)

        # 保留消息
        frame_ret = ttk.Frame(nb, style="Card.TFrame")
        nb.add(frame_ret, text="  保留消息  ")
        self.tv_ret = LogView(
            frame_ret,
            columns=[
                ("topic", "主题", 190, "w"),
                ("qos", "QoS", 46, "center"),
                ("size", "大小", 58, "center"),
                ("payload", "内容", 200, "w"),
            ],
            height=10,
        )
        self.tv_ret.pack(fill="both", expand=True, padx=8, pady=8)

        # 数据包解析（dtp.* 业务载荷）
        frame_pkt = ttk.Frame(nb, style="Card.TFrame")
        nb.add(frame_pkt, text="  数据包解析  ")

        bar = ttk.Frame(frame_pkt, style="Card.TFrame")
        bar.pack(fill="x", padx=8, pady=(8, 4))
        self.var_parse_dtp = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="解析 dtp.* 数据包", variable=self.var_parse_dtp,
                        style="Card.TCheckbutton").pack(side="left")
        ToolTip(bar.winfo_children()[-1],
                "关闭后只丢弃新收到的包；再次打开前收到的包不会补显")
        ttk.Label(bar, text="类型", style="CardMuted.TLabel").pack(side="left", padx=(12, 4))
        self.var_pkt_filter = tk.StringVar(value="全部")
        cmb_filter = ttk.Combobox(bar, textvariable=self.var_pkt_filter, state="readonly",
                                  width=10, values=("全部",) + tuple(SCHEMA_LABELS.values()),
                                  font=theme.FONTS["base"])
        cmb_filter.pack(side="left")
        cmb_filter.bind("<<ComboboxSelected>>", lambda e: self._render_packets())
        ttk.Button(bar, text="清空", style="Ghost.TButton",
                   command=self._clear_packets).pack(side="left", padx=(12, 0))
        self.lbl_pkt_counts = ttk.Label(bar, text="", style="CardMuted.TLabel")
        self.lbl_pkt_counts.pack(side="right")

        # 详情视图：先 pack 占住底部，列表吃掉剩余空间
        detail = ttk.Frame(frame_pkt, style="Card.TFrame")
        detail.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        self.txt_pkt_detail = tk.Text(
            detail, width=1, height=8, wrap="none", font=theme.FONTS["mono_small"],
            bg=theme.COLORS["surface_alt"], fg=theme.COLORS["text"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=theme.COLORS["border"],
            highlightcolor=theme.COLORS["border"], padx=8, pady=6,
        )
        pkt_ysb = ttk.Scrollbar(detail, orient="vertical", command=self.txt_pkt_detail.yview)
        self.txt_pkt_detail.configure(yscrollcommand=pkt_ysb.set)
        self.txt_pkt_detail.pack(side="left", fill="both", expand=True)
        pkt_ysb.pack(side="right", fill="y")
        self.txt_pkt_detail.tag_configure("head", font=theme.FONTS["mono_bold"],
                                          foreground=theme.COLORS["primary"])
        self.txt_pkt_detail.configure(state="disabled")

        self.tv_packets = LogView(
            frame_pkt,
            columns=[
                ("time", "时间", 76, "center"),
                ("station", "工位", 56, "w"),
                ("kind", "类型", 72, "center"),
                ("slot", "槽位", 44, "center"),
                ("subject", "用例 / 步骤", 124, "w"),
                ("status", "判定 / 状态", 88, "w"),
                ("value", "数值", 76, "w"),
                ("spec", "规格", 78, "w"),
                ("msg_id", "消息 ID", 96, "w"),
            ],
            on_select=self._on_pkt_select,
            height=8,
        )
        self.tv_packets.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        tree = self.tv_packets.tree
        tree.tag_configure("ok", foreground=theme.COLORS["success"])
        tree.tag_configure("fail", foreground=theme.COLORS["danger"])
        tree.tag_configure("warn", foreground=theme.COLORS["warning"])
        tree.tag_configure("retained", foreground=theme.COLORS["retain"])

        btn_row = ttk.Frame(col, style="TFrame")
        btn_row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(btn_row, text="刷新", style="Flat.TButton",
                   command=self._refresh_tables).pack(side="left")
        ttk.Button(btn_row, text="断开选中客户端", style="Flat.TButton",
                   command=self._kick_selected).pack(side="left", padx=6)
        ttk.Button(btn_row, text="清空保留消息", style="Flat.TButton",
                   command=self._clear_retained).pack(side="left")
        self.lbl_counts = ttk.Label(btn_row, text="", style="Muted.TLabel")
        self.lbl_counts.pack(side="left", padx=12)

    # ------------------------------------------------------------------ #
    def _build_right(self, parent: ttk.Frame) -> None:
        col = ttk.Frame(parent, style="TFrame")
        col.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        col.columnconfigure(0, weight=1)
        col.rowconfigure(1, weight=1)

        # ---------------- 统计 ---------------- #
        stat = Card(col, title="运行统计")
        stat.grid(row=0, column=0, sticky="ew")

        self.stat_labels: Dict[str, ttk.Label] = {}
        rows = [
            ("connections_total", "累计连接"),
            ("active", "当前在线"),
            ("connections_rejected", "被拒绝"),
            ("messages_in", "收到消息"),
            ("messages_out", "转发消息"),
            ("bytes", "收发流量"),
        ]
        gs = ttk.Frame(stat.body, style="Card.TFrame")
        gs.pack(fill="x")
        gs.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate(rows):
            ttk.Label(gs, text=label, style="CardMuted.TLabel").grid(
                row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            value = ttk.Label(gs, text="0", style="Value.TLabel")
            value.grid(row=i, column=1, sticky="e", pady=2)
            self.stat_labels[key] = value

        # ---------------- 日志 ---------------- #
        logcard = Card(col, title="Broker 日志")
        logcard.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        # 文本区先占满剩余空间（side="top" + expand），按钮最后再 side="bottom"，
        # 这样两者各占一行，不会因为混用 side 把按钮挤到底部之外。
        holder = tk.Frame(logcard.body, bg=theme.COLORS["border"],
                          bd=0, highlightthickness=0)
        holder.pack(side="top", fill="both", expand=True)
        inner = tk.Frame(holder, bg=theme.COLORS["surface"], bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.log_text = tk.Text(
            inner, width=38, height=12, wrap="word",
            font=theme.FONTS["mono_small"],
            bg=theme.COLORS["surface"], fg=theme.COLORS["text"],
            relief="flat", bd=0, highlightthickness=0, padx=8, pady=6,
        )
        ysb = ttk.Scrollbar(inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")

        for name, color in theme.LOG_LEVEL_COLOR.items():
            self.log_text.tag_configure(name, foreground=color)
        self.log_text.tag_configure("time", foreground=theme.COLORS["text_faint"])
        self.log_text.configure(state="disabled")

        logbtns = ttk.Frame(logcard.body, style="Card.TFrame")
        logbtns.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(logbtns, text="清空日志", style="Ghost.TButton",
                   command=self._clear_log).pack(side="left")
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(logbtns, text="自动滚动", variable=self.var_autoscroll,
                        style="Card.TCheckbutton").pack(side="right")

    # ================================================================== #
    # 配置读写
    # ================================================================== #
    def _working_cfg(self) -> BrokerConfig:
        """构造一份 Broker 使用的配置副本（不直接改用户配置对象）。"""
        try:
            return copy.deepcopy(self.app.cfg.broker)
        except Exception:  # noqa: BLE001
            return BrokerConfig()

    def refresh_from_config(self) -> None:
        """从 app 配置同步到界面控件。"""
        cfg = self.app.cfg.broker
        self.cfg = cfg
        self.var_bind.set(cfg.bind_address)
        self.var_port.set(str(cfg.port))
        self.var_maxconn.set(str(cfg.max_connections))
        self.var_maxqos.set(qos_label(cfg.max_qos))
        self.var_retain.set(cfg.enable_retain)
        self.var_logpub.set(cfg.log_publish)
        self.var_autostart.set(cfg.auto_start)
        self.var_require_auth.set(cfg.require_auth)
        self.var_user.set(cfg.username)
        self.var_pass.set(cfg.password)
        self.var_allow.set(", ".join(cfg.allow_list))
        self.var_block.set(", ".join(cfg.block_list))
        self.var_ws_enable.set(cfg.enable_websocket)
        self.var_ws_port.set(str(cfg.websocket_port))
        self.var_ws_path.set(cfg.websocket_path)
        self._on_bind_changed()
        self._sync_ws_state()
        self._refresh_conn_hint()
        self._refresh_tables()

    def _apply_to_config(self) -> BrokerConfig:
        """把界面上的值写回 app.cfg.broker 并返回它。"""
        cfg = self.app.cfg.broker

        cfg.bind_address = (self.var_bind.get().strip() or "0.0.0.0")
        try:
            port = int(self.var_port.get().strip())
        except ValueError:
            port = 1884
        cfg.port = max(1, min(65535, port))

        try:
            cfg.max_connections = max(1, int(self.var_maxconn.get().strip()))
        except ValueError:
            cfg.max_connections = 64

        cfg.max_qos = _qos_from_label(self.var_maxqos.get())
        cfg.enable_retain = bool(self.var_retain.get())
        cfg.log_publish = bool(self.var_logpub.get())
        cfg.auto_start = bool(self.var_autostart.get())

        cfg.require_auth = bool(self.var_require_auth.get())
        cfg.username = self.var_user.get().strip()
        cfg.password = self.var_pass.get()
        cfg.allow_list = _split_list(self.var_allow.get())
        cfg.block_list = _split_list(self.var_block.get())

        cfg.enable_websocket = bool(self.var_ws_enable.get())
        try:
            ws_port = int(self.var_ws_port.get().strip())
        except ValueError:
            ws_port = 8083
        cfg.websocket_port = max(1, min(65535, ws_port))
        path = self.var_ws_path.get().strip() or "/mqtt"
        if not path.startswith("/"):
            path = "/" + path
        cfg.websocket_path = path
        return cfg

    def _save_settings(self) -> None:
        if self.broker.running:
            messagebox.showinfo(
                "提示", "Broker 正在运行，请先停止后再修改监听参数。", parent=self)
            return
        self._apply_to_config()
        self.cfg = self.app.cfg.broker
        self.broker.cfg = self.cfg
        self.app.mark_dirty()
        self.app.save_config(quiet=True)
        self._refresh_conn_hint()
        self.app.toast("Broker 设置已保存", "success")

    # ================================================================== #
    # 启停
    # ================================================================== #
    def _toggle(self) -> None:
        if self.broker.running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if self.broker.running:
            return
        cfg = self._apply_to_config()
        self.cfg = cfg
        self.broker.cfg = cfg
        # 清掉上一次运行留下的状态
        self.broker.clear_log()
        self._clear_log_widget()
        self._running_ui = False

        ok, msg = self.broker.start()
        if not ok:
            messagebox.showerror("启动失败", msg, parent=self)
            self.app.toast(msg, "error")
            self._append_log(msg, "error")
            return

        self._running_ui = True
        self.pill.set("运行中", theme.COLORS["success"])
        self.lbl_bind.configure(text=self.broker.listen_summary.replace("　|　", " | "))
        self.btn_start.configure(text="停止 Broker", style="Danger.TButton")
        self.app.toast(msg, "success")
        self._refresh_conn_hint()
        self._seed_retained_packets()
        self.app.set_status(f"本地 Broker 已启动：{msg}")

    def _stop(self) -> None:
        if not self.broker.running:
            return
        self.broker.stop()
        self._running_ui = False
        self.pill.set("未启动", theme.COLORS["text_faint"])
        self.lbl_bind.configure(text="—")
        self.btn_start.configure(text="启动 Broker", style="Success.TButton")
        self.lbl_uptime.configure(text="运行时长 0s")
        self._refresh_conn_hint()
        self._refresh_tables()
        self.app.toast("Broker 已停止", "warn")
        self.app.set_status("本地 Broker 已停止")

    def autostart_if_needed(self) -> None:
        """由主窗口在启动完成后调用。"""
        if self.app.cfg.broker.auto_start and not self.broker.running:
            self._start()
            self.app.toast("已按设置自动启动本地 Broker", "info")

    def on_close(self) -> None:
        try:
            self._apply_to_config()
        except tk.TclError:
            pass
        if self.broker.running:
            self.broker.stop()

    # ================================================================== #
    # 事件轮询
    # ================================================================== #
    def _poll(self) -> None:
        processed = 0
        refresh = False
        try:
            while processed < 300:
                event = self.events.get_nowait()
                processed += 1
                if event.kind == "log":
                    self._append_log(event.text, event.level)
                elif event.kind == "client_join":
                    self._append_log(f"► {event.client_id} 上线", "success")
                    refresh = True
                elif event.kind == "client_leave":
                    self._append_log(f"◄ {event.client_id} 下线", "info")
                    refresh = True
                elif event.kind == "state":
                    refresh = True
                elif event.kind == "stats":
                    refresh = True
        except queue.Empty:
            pass

        # ---- 数据包解析队列 ---- #
        pkt_budget = 200
        pkt_new = False
        while pkt_budget > 0:
            try:
                pkt = self.packet_events.get_nowait()
            except queue.Empty:
                break
            pkt_budget -= 1
            if not self.var_parse_dtp.get():
                continue        # 开关关闭：只丢弃，不显示
            self._add_packet(pkt)
            pkt_new = True
        if pkt_new:
            self._update_pkt_counts()

        self._tick += 1
        if self._tick % 6 == 0 or refresh:      # 约 0.3 秒
            self._refresh_tables()
            self._refresh_stats()
        if self._tick % 12 == 0:
            self._update_uptime()

        try:
            self.after(50, self._poll)
        except tk.TclError:  # pragma: no cover
            pass

    # ================================================================== #
    # 数据包解析（dtp.*）
    # ================================================================== #
    _PKT_MAX = 500          # 界面上保留的最大行数
    _PKT_SEEN_MAX = 4000    # msg_id 去重缓存上限

    def _on_raw_packet(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        """Broker 客户端线程回调：解析 dtp.* 载荷并投递给界面队列。

        ``parse_packet`` 是纯函数且开销很小，放在线程里执行没问题；
        真正的界面更新统一走 ``_poll``。
        """
        pkt = parse_packet(topic, payload, qos, retain)
        if pkt is not None:
            self.packet_events.put(pkt)

    def _add_packet(self, pkt: Dict[str, Any]) -> None:
        """新包入列：去重 → 进总表 → 若通过类型过滤则插入列表顶部。"""
        msg_id = pkt.get("msg_id") or ""
        if msg_id:
            if msg_id in self._pkt_seen:
                self._pkt_dedup += 1
                return
            self._pkt_seen[msg_id] = None
            if len(self._pkt_seen) > self._PKT_SEEN_MAX:
                for key in list(self._pkt_seen)[:1000]:
                    del self._pkt_seen[key]
        self._pkt_list.insert(0, pkt)
        del self._pkt_list[self._PKT_MAX:]
        if self.var_pkt_filter.get() in ("全部", pkt.get("kind", "")):
            self._append_pkt_row(pkt)

    def _append_pkt_row(self, pkt: Dict[str, Any]) -> None:
        self._pkt_seq += 1
        iid = f"pkt{self._pkt_seq}"
        ts = datetime.fromtimestamp(pkt.get("time") or time.time()).strftime("%H:%M:%S")
        values = [
            ts,
            pkt.get("station", ""),
            pkt.get("kind", ""),
            pkt.get("slot", ""),
            pkt.get("subject", ""),
            pkt.get("status", ""),
            pkt.get("value", ""),
            pkt.get("spec", ""),
            pkt.get("msg_id", ""),
        ]
        tags = []
        if pkt.get("tag"):
            tags.append(pkt["tag"])
        if pkt.get("retain"):
            tags.append("retained")
        try:
            self.tv_packets.add_row(values, tuple(tags), item_id=iid, at_top=True)
        except tk.TclError:
            return
        self._pkt_rows[iid] = pkt
        self._pkt_iids.insert(0, iid)
        if len(self._pkt_iids) > self._PKT_MAX:
            old = self._pkt_iids.pop()
            self._pkt_rows.pop(old, None)
            try:
                self.tv_packets.tree.delete(old)
            except tk.TclError:
                pass

    def _render_packets(self) -> None:
        """按当前类型过滤重建整表（切换过滤条件 / Broker 启动时调用）。"""
        if not hasattr(self, "tv_packets"):
            return
        self.tv_packets.clear()
        self._pkt_rows.clear()
        self._pkt_iids.clear()
        filt = self.var_pkt_filter.get()
        for pkt in self._pkt_list:
            if filt in ("全部", pkt.get("kind", "")):
                self._append_pkt_row(pkt)
        self._update_pkt_counts()

    def _update_pkt_counts(self) -> None:
        try:
            shown = len(self.tv_packets.tree.get_children())
        except tk.TclError:
            return
        self.lbl_pkt_counts.configure(
            text=f"显示 {shown} / 共 {len(self._pkt_list)} 包"
                 + (f" · 去重跳过 {self._pkt_dedup}" if self._pkt_dedup else ""))

    def _on_pkt_select(self, _event=None) -> None:
        """选中一行时在下方展示可读摘要 + 原始 JSON。"""
        sel = self.tv_packets.tree.selection()
        pkt = self._pkt_rows.get(sel[0]) if sel else None
        self.txt_pkt_detail.configure(state="normal")
        self.txt_pkt_detail.delete("1.0", "end")
        if pkt is not None:
            self.txt_pkt_detail.insert("end", describe(pkt) + "\n\n", ("head",))
            raw = pkt.get("raw") or {}
            try:
                pretty = json.dumps(raw, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                pretty = str(raw)
            self.txt_pkt_detail.insert("end", pretty)
        self.txt_pkt_detail.configure(state="disabled")

    def _clear_packets(self) -> None:
        self._pkt_list.clear()
        self._pkt_rows.clear()
        self._pkt_iids.clear()
        self._pkt_seen.clear()
        self._pkt_dedup = 0
        self.tv_packets.clear()
        self._on_pkt_select()
        self._update_pkt_counts()

    def _seed_retained_packets(self) -> None:
        """Broker 启动后把 retained 的 dtp.* 包解析进来。

        按文档 §8.4：retained 的 agent/status 应视作"当前状态"而非新事件，
        这里正好让刚启动的 Broker 页立刻能看到工位最后状态。
        """
        try:
            rows = self.broker.retained_snapshot()
        except Exception:  # noqa: BLE001
            return
        added = False
        for row in rows:
            pkt = parse_packet(row.get("topic", ""), row.get("payload", ""),
                               row.get("qos", 0), retain=True)
            if pkt is not None:
                self._add_packet(pkt)
                added = True
        if added:
            self._render_packets()

    # ================================================================== #
    # 数据刷新
    # ================================================================== #
    def _refresh_tables(self) -> None:
        # ---- 客户端 ---- #
        try:
            clients = self.broker.client_snapshot()
        except Exception:  # noqa: BLE001
            clients = []
        # 增量更新：先删掉已不在列表里的行
        want = {f"c_{r['client_id']}" for r in clients}
        for iid in list(self._client_iids):
            if iid not in want:
                try:
                    self.tv_clients.tree.delete(iid)
                except tk.TclError:
                    pass
                self._client_iids.remove(iid)
        for i, row in enumerate(clients):
            iid = f"c_{row['client_id']}"
            values = [
                row["client_id"],
                row["remote"],
                row["protocol"],
                f"{row['keepalive']}s" if row["keepalive"] else "—",
                "是" if row["clean"] else "否",
                str(row["subscriptions"]),
                _fmt_dur(row["uptime"]),
                row["username"],
            ]
            tags = ("even",) if i % 2 else ()
            if row["will"]:
                tags = tags + ("retain",)
            try:
                if iid in self.tv_clients.tree.get_children():
                    self.tv_clients.tree.item(iid, values=values, tags=tags)
                else:
                    self.tv_clients.tree.insert("", "end", iid=iid, values=values, tags=tags)
                    self._client_iids.append(iid)
            except tk.TclError:
                pass
        # 客户端列表即使为空也要能反映出来
        self._client_iids = [i for i in self._client_iids
                             if i in self.tv_clients.tree.get_children()]

        # ---- 订阅 ---- #
        try:
            subs = self.broker.subscription_snapshot()
        except Exception:  # noqa: BLE001
            subs = []
        self.tv_subs.clear()
        for i, row in enumerate(subs):
            self.tv_subs.tree.insert(
                "", "end",
                values=[row["topic"], str(row["qos"]), row["client_id"]],
                tags=("even",) if i % 2 else (),
            )

        # ---- 保留消息 ---- #
        try:
            retained = self.broker.retained_snapshot()
        except Exception:  # noqa: BLE001
            retained = []
        self.tv_ret.clear()
        for i, row in enumerate(retained):
            payload = row["payload"].replace("\n", " ")
            if len(payload) > 120:
                payload = payload[:120] + "…"
            self.tv_ret.tree.insert(
                "", "end",
                values=[row["topic"], str(row["qos"]), _fmt_bytes(row["size"]), payload],
                tags=("retain",) if i % 2 else ("retain", "even"),
            )

        self.lbl_counts.configure(
            text=f"客户端 {len(clients)}　订阅 {len(subs)}　保留 {len(retained)}"
        )

    def _refresh_stats(self) -> None:
        s = self.broker.stats
        try:
            active = len(self.broker.client_snapshot())
        except Exception:  # noqa: BLE001
            active = 0
        values = {
            "connections_total": str(s.get("connections_total", 0)),
            "active": str(active),
            "connections_rejected": str(s.get("connections_rejected", 0)),
            "messages_in": str(s.get("messages_in", 0)),
            "messages_out": str(s.get("messages_out", 0)),
            "bytes": f"↓{_fmt_bytes(s.get('bytes_in', 0))} ↑{_fmt_bytes(s.get('bytes_out', 0))}",
        }
        for key, label in self.stat_labels.items():
            try:
                label.configure(text=values.get(key, "0"))
            except tk.TclError:
                pass

    def _update_uptime(self) -> None:
        if self.broker.running:
            self.lbl_uptime.configure(text=f"运行时长 {_fmt_dur(self.broker.uptime())}")

    def _refresh_conn_hint(self) -> None:
        cfg = self.app.cfg.broker
        ips = local_ip_addresses()
        others = [ip for ip in ips if not ip.startswith("127.")] or ["<本机IP获取失败>"]

        lines = []
        if cfg.bind_address == "127.0.0.1":
            lines.append("当前仅监听 127.0.0.1，其他主机无法连接。")
            lines.append("要供局域网调试，请把「监听地址」改成 0.0.0.0 后重启 Broker。")
        else:
            lines.append("其他主机在上位机的「连接」页填写：")
            lines.append(f"    主机  {others[0]}        端口  {cfg.port}")
            for ip in others[1:3]:
                lines.append(f"    备用IP  {ip}")
            lines.append(f"    协议  MQTT 3.1.1 · TCP{'  用户名/密码  ' + cfg.username if cfg.require_auth else ''}")
            lines.append("本机自测可直接填 127.0.0.1")
            if cfg.enable_websocket:
                scheme = "ws"
                lines.append("")
                lines.append("WebSocket 客户端（浏览器 / 小程序）连接地址：")
                lines.append(f"    {scheme}://{others[0]}:{cfg.websocket_port}{cfg.websocket_path}")
                for ip in others[1:3]:
                    lines.append(f"    {scheme}://{ip}:{cfg.websocket_port}{cfg.websocket_path}")
                lines.append("    （传输方式选 WebSocket，WS 路径填 "
                             f"{cfg.websocket_path}，其余同上）")
        if cfg.require_auth:
            lines.append(f"注意：已开启认证，用户名「{cfg.username}」")

        text = "\n".join(lines)
        self.lbl_conn_hint.configure(state="normal")
        self.lbl_conn_hint.delete("1.0", "end")
        self.lbl_conn_hint.insert("1.0", text)
        self.lbl_conn_hint.configure(state="disabled")

    def _sync_ws_state(self) -> None:
        """按 WS 开关启用/禁用相关输入框，并给出可复制的地址提示。"""
        enabled = bool(self.var_ws_enable.get())
        state = "normal" if enabled else "disabled"
        for widget in (getattr(self, "e_ws_port", None), getattr(self, "e_ws_path", None)):
            if widget is not None:
                try:
                    widget.configure(state=state)
                except tk.TclError:
                    pass
        label = getattr(self, "lbl_ws_hint", None)
        if label is None:
            return
        if not enabled:
            label.configure(text="关闭时只提供 TCP 入口")
            return
        try:
            port = int(self.var_ws_port.get().strip())
        except ValueError:
            port = 0
        path = self.var_ws_path.get().strip() or "/mqtt"
        mqtt_port = self.var_port.get().strip()
        if port == 0 or not (1 <= port <= 65535):
            label.configure(text="⚠ WS 端口号不合法")
        elif str(port) == mqtt_port:
            label.configure(text="⚠ WS 端口不能与上面的 MQTT 端口相同")
        else:
            ips = [ip for ip in local_ip_addresses() if not ip.startswith("127.")]
            host = ips[0] if ips else "127.0.0.1"
            label.configure(text=f"其他主机填 ws://{host}:{port}{path}")

    # ================================================================== #
    # 操作
    # ================================================================== #
    def _on_bind_changed(self) -> None:
        addr = self.var_bind.get().strip()
        self.lbl_bind_hint.configure(text=BIND_HINT.get(addr, "自定义监听地址"))

    def _copy_address(self) -> None:
        cfg = self.app.cfg.broker
        ips = [ip for ip in local_ip_addresses() if not ip.startswith("127.")]
        host = ips[0] if ips else "127.0.0.1"
        parts = [f"mqtt://{host}:{cfg.port}"]
        if cfg.enable_websocket:
            parts.append(f"ws://{host}:{cfg.websocket_port}{cfg.websocket_path}")
        text = "　".join(parts)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.toast(f"已复制 {text}", "success")
        except tk.TclError:
            self.app.toast("复制失败，请手动选取", "error")

    def _connect_self(self) -> None:
        """把主界面的连接目标改成 127.0.0.1:<端口> 并连接。

        Broker 开了 WebSocket 时默认走 WS 路径自测 —— 因为 WS 是最容易
        配错的一环（协议前缀、路径、端口），自测走一遍更有意义。
        """
        cfg = self.app.cfg.broker
        if not self.broker.running:
            self.app.toast("请先启动 Broker", "warn")
            return
        conn = self.app.cfg.active
        conn.host = "127.0.0.1"
        conn.protocol = "MQTT 3.1.1"
        conn.use_tls = False
        if cfg.enable_websocket:
            conn.port = cfg.websocket_port
            conn.transport = "WebSocket"
            conn.ws_path = cfg.websocket_path
            conn.name = "本地 Broker（WS 自测）"
        else:
            conn.port = cfg.port
            conn.transport = "TCP"
            conn.name = "本地 Broker（自测）"
        if cfg.require_auth:
            conn.username = cfg.username
            conn.password = cfg.password
        else:
            conn.username = ""
            conn.password = ""
        self.app.panels["connection"].refresh_from_config()
        self.app.mark_dirty()
        self.app.select_tab("connection")
        self.app.connect()
        self.app.toast("已切换到本地 Broker，正在连接…", "info")

    def _kick_selected(self) -> None:
        sel = self.tv_clients.tree.selection()
        if not sel:
            self.app.toast("请先在上面选中一个客户端", "warn")
            return
        cids = []
        for iid in sel:
            values = self.tv_clients.tree.item(iid, "values")
            if values:
                cids.append(values[0])
        closed = 0
        with self.broker_lock():
            for cid in cids:
                session = self.broker.clients.get(cid)
                if session is not None:
                    session.close(send_will=False)
                    closed += 1
        self._append_log(f"手动断开 {closed} 个客户端", "warn")
        self.app.toast(f"已断开 {closed} 个客户端", "warn")
        self._refresh_tables()

    def broker_lock(self):
        return self.broker._lock

    def _clear_retained(self) -> None:
        with self.broker_lock():
            count = len(self.broker._retained)
            self.broker._retained.clear()
        self._append_log(f"清空保留消息（{count} 条）", "warn")
        self.app.toast(f"已清空 {count} 条保留消息", "warn")
        self._refresh_tables()

    def _show_firewall_tip(self) -> None:
        port = self.app.cfg.broker.port
        messagebox.showinfo(
            "让其他主机能连上",
            "其他主机连不上时，通常是 Windows 防火墙拦住了入站连接。\n\n"
            f"以管理员身份打开 PowerShell，执行：\n\n"
            f'New-NetFirewallRule -DisplayName "MQTT Debugger Broker" '
            f'-Direction Inbound -Protocol TCP -LocalPort {port} -Action Allow\n\n'
            "如果两台机器不在同一网段，还需要确认路由器/交换机未隔离客户端。",
            parent=self,
        )

    # ================================================================== #
    # 日志
    # ================================================================== #
    def _append_log(self, text: str, level: str = "info") -> None:
        try:
            state = self.log_text.cget("state")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"{datetime.now().strftime('%H:%M:%S')} ",
                                 ("time",))
            self.log_text.insert("end", f"{text}\n", (level,))
            self._log_rows += 1
            if self._log_rows > 2000:
                # 截掉最旧的一半，避免长时间运行吃内存
                self.log_text.delete("1.0", "1000.0")
                self._log_rows = 1000
            if self.var_autoscroll.get():
                self.log_text.see("end")
            self.log_text.configure(state=state)
        except tk.TclError:  # pragma: no cover
            pass

    def _clear_log_widget(self) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
            self._log_rows = 0
        except tk.TclError:  # pragma: no cover
            pass

    def _clear_log(self) -> None:
        self._clear_log_widget()
        try:
            self.broker.clear_log()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _qos_from_label(label: str) -> int:
    try:
        return max(0, min(2, int(str(label).strip().split(" ")[0])))
    except (ValueError, IndexError):
        return 2


def _split_list(text: str) -> List[str]:
    items: List[str] = []
    for raw in str(text).replace("；", ";").replace(";", ",").split(","):
        raw = raw.strip()
        if raw and raw not in items:
            items.append(raw)
    return items


def _fmt_bytes(size: int) -> str:
    size = int(size or 0)
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    return f"{size / 1024 / 1024:.1f}M"


def _fmt_dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
