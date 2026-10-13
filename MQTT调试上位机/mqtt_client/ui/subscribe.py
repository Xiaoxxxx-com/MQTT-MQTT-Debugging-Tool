"""订阅管理页：订阅列表 + 通配符说明 + 便捷订阅（$SYS 等）。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional

from ..config import QOS_CHOICES, Subscription, qos_from_label
from . import theme
from .widgets import Card, ToolTip

QUICK_TOPICS = [
    ("全部主题", "#", 0, "订阅所有主题，调试时最有用的一个"),
    ("所有传感器", "sensor/#", 0, "示例：传感器数据层级"),
    ("设备状态", "device/+/status", 1, "示例：匹配一级设备的状态主题"),
    ("保留消息", "retained/#", 0, "示例：常驻主题根"),
    ("Broker 自身状态", "$SYS/#", 0, "Broker 的运行统计，多数公共服务器开放"),
    ("共享订阅", "$share/g1/demo/#", 1, "共享订阅：同一组内只有一个客户端收到"),
]


class SubscribePanel(ttk.Frame):
    """订阅的增删改查。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self._build()
        self.refresh()

    # ================================================================== #
    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame")
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        # ---------------- 新增订阅 ---------------- #
        add = Card(outer, title="新增订阅")
        add.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(add.body, style="Card.TFrame")
        row.pack(fill="x")

        ttk.Label(row, text="主题 / 通配符", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        self.var_topic = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.var_topic, style="Card.TEntry", width=40)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        entry.bind("<Return>", lambda e: self._add())
        ToolTip(entry, "支持 +（单层通配）与 #（多层通配），例如 sensor/+/temp、home/#")

        ttk.Label(row, text="QoS", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_qos = tk.StringVar(value=QOS_CHOICES[0])
        combo = ttk.Combobox(row, textvariable=self.var_qos, state="readonly",
                             values=QOS_CHOICES, width=14, font=theme.FONTS["base"])
        combo.pack(side="left", padx=(0, 10))

        ttk.Label(row, text="备注", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_alias = tk.StringVar()
        ttk.Entry(row, textvariable=self.var_alias, style="Card.TEntry", width=14).pack(side="left", padx=(0, 10))

        ttk.Button(row, text="订阅", style="Primary.TButton", command=self._add).pack(side="left")

        # 快捷订阅
        quick = ttk.Frame(add.body, style="Card.TFrame")
        quick.pack(fill="x", pady=(10, 0))
        ttk.Label(quick, text="快捷订阅", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        chips = ttk.Frame(quick, style="Card.TFrame")
        chips.pack(fill="x")
        for idx, (label, topic, qos, tip) in enumerate(QUICK_TOPICS):
            btn = ttk.Button(chips, text=f"{label}  {topic}", style="Ghost.TButton",
                             command=lambda t=topic, q=qos: self._quick(t, q))
            btn.grid(row=idx // 2, column=idx % 2, sticky="w", padx=(0, 8), pady=1)
            ToolTip(btn, f"{tip}\nQoS {qos}")

        # ---------------- 订阅列表 ---------------- #
        list_card = Card(outer, title="当前订阅")
        list_card.pack(fill="both", expand=True)

        toolbar = ttk.Frame(list_card.body, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="全部重新订阅", style="Flat.TButton",
                   command=self._resubscribe_all).pack(side="left")
        ttk.Button(toolbar, text="全部取消", style="Flat.TButton",
                   command=self._unsubscribe_all).pack(side="left", padx=6)
        self.lbl_count = ttk.Label(toolbar, text="", style="CardMuted.TLabel")
        self.lbl_count.pack(side="left", padx=10)

        tree_wrap = ttk.Frame(list_card.body, style="Card.TFrame")
        tree_wrap.pack(fill="both", expand=True)

        columns = ("topic", "alias", "qos", "status", "rx")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings", height=10)
        for key, label, width, anchor in (
            ("topic", "主题过滤器", 260, "w"),
            ("alias", "备注", 120, "w"),
            ("qos", "QoS", 60, "center"),
            ("status", "状态", 90, "center"),
            ("rx", "收到条数", 80, "center"),
        ):
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "topic"))
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        c = theme.COLORS
        self.tree.tag_configure("active", foreground=c["text"])
        self.tree.tag_configure("inactive", foreground=c["text_faint"])
        self.tree.tag_configure("disabled", foreground=c["text_faint"])

        self.tree.bind("<Double-1>", lambda e: self._edit_selected())
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self, tearoff=0, font=theme.FONTS["base"])
        self.menu.add_command(label="重新订阅", command=self._resubscribe_selected)
        self.menu.add_command(label="取消订阅", command=self._remove_selected)
        self.menu.add_separator()
        self.menu.add_command(label="复制主题", command=self._copy_topic)
        self.menu.add_command(label="仅保留此主题的日志", command=self._filter_this)

        self._build_help(outer)

    # ------------------------------------------------------------------ #
    def _build_help(self, parent: ttk.Frame) -> None:
        help_card = Card(parent, title="通配符速查")
        help_card.pack(fill="x", pady=(10, 0))
        lines = [
            ("#", "匹配任意层级（含空层），只能出现在结尾", "home/#  →  home/a、home/a/b、home"),
            ("+", "匹配单层任意内容", "sensor/+/temp  →  sensor/room1/temp"),
            ("$", "以 $ 开头的主题不被 # / + 匹配，需显式订阅", "$SYS/#  →  订阅 Broker 状态"),
            ("$share", "共享订阅，同组客户端轮询消费同一条消息", "$share/g1/data/#"),
        ]
        grid = ttk.Frame(help_card.body, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(2, weight=1)
        for row, (sym, desc, example) in enumerate(lines):
            ttk.Label(grid, text=sym, style="Card.TLabel", font=theme.FONTS["mono_bold"]).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=2)
            ttk.Label(grid, text=desc, style="CardMuted.TLabel").grid(
                row=row, column=1, sticky="w", pady=2)
            ttk.Label(grid, text=example, style="CardMuted.TLabel",
                      font=theme.FONTS["mono_small"]).grid(
                row=row, column=2, sticky="w", padx=(20, 0), pady=2)

    # ================================================================== #
    def refresh(self) -> None:
        """根据配置重绘列表。"""
        self.tree.delete(*self.tree.get_children())
        counter = getattr(self.app, "topic_counter", {})
        connected = self.app.service.is_connected
        for idx, sub in enumerate(self.app.cfg.subscriptions):
            status = "已订阅" if (connected and sub.enabled) else ("未连接" if sub.enabled else "已禁用")
            tag = "active" if (connected and sub.enabled) else "inactive"
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(sub.topic, sub.alias or "—", sub.qos, status, counter.get(sub.topic, 0)),
                tags=(tag,),
            )
        total = len(self.app.cfg.subscriptions)
        enabled = sum(1 for s in self.app.cfg.subscriptions if s.enabled)
        self.lbl_count.configure(text=f"共 {total} 条，启用 {enabled} 条")

    def update_counts(self) -> None:
        counter = getattr(self.app, "topic_counter", {})
        for iid in self.tree.get_children():
            try:
                sub = self.app.cfg.subscriptions[int(iid)]
            except (ValueError, IndexError):
                continue
            self.tree.set(iid, "rx", counter.get(sub.topic, 0))

    # ================================================================== #
    def _add(self) -> None:
        topic = self.var_topic.get().strip()
        if not topic:
            messagebox.showwarning("提示", "请输入主题或通配符。", parent=self)
            return
        qos = qos_from_label(self.var_qos.get())
        alias = self.var_alias.get().strip()

        existing = next((s for s in self.app.cfg.subscriptions if s.topic == topic), None)
        if existing:
            if not messagebox.askyesno("已存在", f"主题「{topic}」已在订阅列表中，是否覆盖配置？", parent=self):
                return
            existing.qos = qos
            existing.alias = alias
            existing.enabled = True
        else:
            self.app.cfg.subscriptions.append(Subscription(topic=topic, qos=qos, alias=alias))

        ok = self.app.service.subscribe(topic, qos, alias)
        if ok:
            self.app.toast(f"已订阅 {topic}", "success")
            self.var_topic.set("")
            self.var_alias.set("")
        self.refresh()
        self.app.mark_dirty()
        self.app.refresh_topic_shortcuts()

    def _quick(self, topic: str, qos: int) -> None:
        self.var_topic.set(topic)
        self.var_qos.set(next((q for q in QOS_CHOICES if q.startswith(str(qos))), QOS_CHOICES[0]))
        self._add()

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _remove_selected(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        try:
            sub = self.app.cfg.subscriptions[idx]
        except IndexError:
            return
        self.app.service.unsubscribe(sub.topic)
        self.app.cfg.subscriptions.pop(idx)
        self.refresh()
        self.app.mark_dirty()
        self.app.refresh_topic_shortcuts()
        self.app.toast(f"已取消订阅 {sub.topic}", "warn")

    def _resubscribe_selected(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        sub = self.app.cfg.subscriptions[idx]
        self.app.service.subscribe(sub.topic, sub.qos, sub.alias)
        self.refresh()

    def _resubscribe_all(self) -> None:
        if not self.app.service.is_connected:
            self.app.toast("当前未连接，订阅列表会在连接后自动生效", "warn")
            return
        for sub in self.app.cfg.subscriptions:
            if sub.enabled:
                self.app.service.subscribe(sub.topic, sub.qos, sub.alias)
        self.refresh()
        self.app.toast("已重新订阅全部主题", "success")

    def _unsubscribe_all(self) -> None:
        if not self.app.cfg.subscriptions:
            return
        if not messagebox.askyesno("确认", "确定取消全部订阅吗？", parent=self):
            return
        for sub in list(self.app.cfg.subscriptions):
            self.app.service.unsubscribe(sub.topic)
        self.app.cfg.subscriptions.clear()
        self.refresh()
        self.app.mark_dirty()
        self.app.refresh_topic_shortcuts()

    def _edit_selected(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        sub = self.app.cfg.subscriptions[idx]
        from tkinter import simpledialog

        new_topic = simpledialog.askstring("编辑订阅", "主题：", initialvalue=sub.topic, parent=self)
        if not new_topic:
            return
        new_alias = simpledialog.askstring("编辑订阅", "备注：", initialvalue=sub.alias or "", parent=self)
        self.app.service.unsubscribe(sub.topic)
        sub.topic = new_topic.strip()
        sub.alias = (new_alias or "").strip()
        self.app.service.subscribe(sub.topic, sub.qos, sub.alias)
        self.refresh()
        self.app.mark_dirty()

    def _copy_topic(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self.clipboard_clear()
        self.clipboard_append(self.app.cfg.subscriptions[idx].topic)
        self.app.toast("主题已复制到剪贴板", "success")

    def _filter_this(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        topic = self.app.cfg.subscriptions[idx].topic
        self.app.set_log_filter(topic)

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()
