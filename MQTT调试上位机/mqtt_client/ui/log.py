"""消息日志页。

功能：实时列表、多条件过滤（主题 / 内容 / 方向 / QoS）、关键字高亮、
JSON 格式化详情、单条重发、批量导出（TXT / CSV / JSON）、
以及基于最近 N 条的简易流速曲线（纯 Canvas 绘制，无第三方依赖）。
"""

from __future__ import annotations

import base64
import csv
import json
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..config import MessageRecord
from ..core import pretty_json
from . import theme
from .widgets import Card, ToolTip

MAX_ROWS = 5000          # Treeview 里保留的最大行数
CHART_SAMPLES = 60       # 流速曲线采样点数


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}K"
    return f"{size / 1024 / 1024:.1f}M"


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int((ts % 1) * 1000):03d}"


def _fmt_time_full(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.") + f"{int((ts % 1) * 1000):03d}"


def _one_line(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class LogPanel(ttk.Frame):
    """消息日志与过滤。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.records: List[Tuple[str, MessageRecord]] = []   # (iid, record)
        self._paused = False
        self._auto_scroll = True
        self._display_counter: Dict[str, int] = {}
        self._rate_rx: Deque[float] = deque(maxlen=CHART_SAMPLES)
        self._rate_tx: Deque[float] = deque(maxlen=CHART_SAMPLES)
        self._last_rates: Tuple[float, float] = (0.0, 0.0)
        self._build()
        self._schedule_rate_sample()

    # ================================================================== #
    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame")
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        # ---------------- 过滤工具条 ---------------- #
        filter_card = Card(outer)
        filter_card.pack(fill="x", pady=(0, 10))
        bar = filter_card.body

        row1 = ttk.Frame(bar, style="Card.TFrame")
        row1.pack(fill="x")

        ttk.Label(row1, text="主题过滤", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_topic_filter = tk.StringVar()
        entry_topic = ttk.Entry(row1, textvariable=self.var_topic_filter, style="Card.TEntry", width=26)
        entry_topic.pack(side="left", padx=(0, 10))
        ToolTip(entry_topic, "支持 * 通配，例如 sensor/* ；留空表示不过滤")
        entry_topic.bind("<KeyRelease>", lambda e: self._rebuild_view())

        ttk.Label(row1, text="内容包含", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_content_filter = tk.StringVar()
        entry_content = ttk.Entry(row1, textvariable=self.var_content_filter, style="Card.TEntry", width=20)
        entry_content.pack(side="left", padx=(0, 10))
        entry_content.bind("<KeyRelease>", lambda e: self._rebuild_view())

        ttk.Label(row1, text="高亮关键字", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_highlight = tk.StringVar()
        entry_hl = ttk.Entry(row1, textvariable=self.var_highlight, style="Card.TEntry", width=14)
        entry_hl.pack(side="left", padx=(0, 10))
        entry_hl.bind("<KeyRelease>", lambda e: self._rebuild_view())

        self.var_regex = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="正则", variable=self.var_regex, style="Card.TCheckbutton",
                        command=self._rebuild_view).pack(side="left")

        row2 = ttk.Frame(bar, style="Card.TFrame")
        row2.pack(fill="x", pady=(8, 0))

        ttk.Label(row2, text="方向", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_dir = tk.StringVar(value="全部")
        dir_combo = ttk.Combobox(row2, textvariable=self.var_dir, state="readonly",
                                 values=("全部", "仅收到", "仅发出", "仅系统"), width=9,
                                 font=theme.FONTS["base"])
        dir_combo.pack(side="left", padx=(0, 12))
        dir_combo.bind("<<ComboboxSelected>>", lambda e: self._rebuild_view())

        ttk.Label(row2, text="QoS", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_qos_filter = tk.StringVar(value="全部")
        qos_combo = ttk.Combobox(row2, textvariable=self.var_qos_filter, state="readonly",
                                 values=("全部", "QoS 0", "QoS 1", "QoS 2"), width=8,
                                 font=theme.FONTS["base"])
        qos_combo.pack(side="left", padx=(0, 12))
        qos_combo.bind("<<ComboboxSelected>>", lambda e: self._rebuild_view())

        self.var_retain_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="仅 Retain", variable=self.var_retain_only,
                        style="Card.TCheckbutton", command=self._rebuild_view).pack(side="left", padx=(0, 12))

        ttk.Button(row2, text="清空过滤", style="Ghost.TButton",
                   command=self._clear_filters).pack(side="left")

        row3 = ttk.Frame(bar, style="Card.TFrame")
        row3.pack(fill="x", pady=(8, 0))
        ttk.Separator(row3, orient="horizontal").pack(fill="x", pady=(0, 8))

        actions = ttk.Frame(row3, style="Card.TFrame")
        actions.pack(fill="x")

        self.btn_pause = ttk.Button(actions, text="暂停", style="Flat.TButton", command=self._toggle_pause)
        self.btn_pause.pack(side="left")
        self.var_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text="自动滚动", variable=self.var_autoscroll,
                        style="Card.TCheckbutton").pack(side="left", padx=8)

        self.var_limit = tk.StringVar(value="全部显示")
        ttk.Label(actions, text="显示范围", style="CardMuted.TLabel").pack(side="left", padx=(12, 4))
        lim = ttk.Combobox(actions, textvariable=self.var_limit, state="readonly",
                           values=("全部显示", "最近 100 条", "最近 500 条", "最近 1000 条"),
                           width=12, font=theme.FONTS["base"])
        lim.pack(side="left")
        lim.bind("<<ComboboxSelected>>", lambda e: self._rebuild_view())

        ttk.Button(actions, text="导出日志", style="Flat.TButton", command=self._export).pack(side="right")
        ttk.Button(actions, text="删除选中", style="Flat.TButton", command=self._delete_selected).pack(side="right", padx=6)
        ttk.Button(actions, text="清空日志", style="Flat.TButton", command=self._clear).pack(side="right")

        self.lbl_stats = ttk.Label(actions, text="", style="CardMuted.TLabel")
        self.lbl_stats.pack(side="right", padx=14)

        # ---------------- 列表 + 详情 ---------------- #
        pane = ttk.Panedwindow(outer, orient="vertical")
        pane.pack(fill="both", expand=True)

        list_frame = ttk.Frame(pane, style="Card.TFrame")
        pane.add(list_frame, weight=3)

        columns = [
            ("time", "时间", 96, "w"),
            ("dir", "方向", 50, "center"),
            ("topic", "主题", 250, "w"),
            ("qos", "QoS", 40, "center"),
            ("retain", "R", 26, "center"),
            ("size", "字节", 48, "e"),
            ("payload", "内容", 340, "w"),
        ]
        self.tree = ttk.Treeview(list_frame, columns=[c[0] for c in columns],
                                 show="headings", height=10, selectmode="extended")
        for key, label, width, anchor in columns:
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("payload", "topic")))
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        c = theme.COLORS
        self.tree.tag_configure("in", foreground=c["text"])
        self.tree.tag_configure("out", foreground=c["tx"], background=c["row_tx"])
        self.tree.tag_configure("sys", foreground=c["sys"], background=c["row_sys"])
        self.tree.tag_configure("error", foreground=c["danger"], background=c["row_err"])
        self.tree.tag_configure("match", background="#FFF3A3")

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._republish_selected())
        self.tree.bind("<Control-c>", self._copy_selected)
        self.tree.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self, tearoff=0, font=theme.FONTS["base"])
        self.menu.add_command(label="复制主题", command=lambda: self._copy_field("topic"))
        self.menu.add_command(label="复制内容", command=lambda: self._copy_field("payload"))
        self.menu.add_command(label="复制整行", command=self._copy_selected)
        self.menu.add_separator()
        self.menu.add_command(label="载入到发布页", command=self._republish_selected)
        self.menu.add_command(label="用该主题过滤", command=self._filter_by_selected_topic)
        self.menu.add_separator()
        self.menu.add_command(label="删除选中", command=self._delete_selected)

        # ---- 详情 ---- #
        detail_frame = ttk.Frame(pane, style="Card.TFrame")
        pane.add(detail_frame, weight=2)

        head = ttk.Frame(detail_frame, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="消息详情", style="H3.TLabel").pack(side="left")
        self.lbl_detail_meta = ttk.Label(head, text="选中一条消息查看详情", style="CardMuted.TLabel")
        self.lbl_detail_meta.pack(side="left", padx=(10, 0))
        ttk.Button(head, text="复制内容", style="Ghost.TButton",
                   command=lambda: self._copy_field("payload")).pack(side="right")

        body = ttk.Frame(detail_frame, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 0))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # 内容视图
        content_wrap = tk.Frame(body, bg=theme.COLORS["border"], bd=0)
        content_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        inner = tk.Frame(content_wrap, bg=theme.COLORS["surface"], bd=0)
        inner.grid(row=0, column=0, sticky="nsew")
        content_wrap.rowconfigure(0, weight=1)
        content_wrap.columnconfigure(0, weight=1)

        text_holder = tk.Frame(inner, bg=theme.COLORS["surface"], bd=0)
        text_holder.grid(row=0, column=0, sticky="nsew")
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)

        self.detail_text = tk.Text(
            text_holder, width=1, height=8, wrap="none", font=theme.FONTS["mono"],
            bg=c["surface"], fg=c["text"], relief="flat", bd=0, highlightthickness=0,
            padx=8, pady=6, selectbackground=c["selection"], selectforeground=c["text"],
        )
        ysb2 = ttk.Scrollbar(inner, orient="vertical", command=self.detail_text.yview)
        xsb2 = ttk.Scrollbar(inner, orient="horizontal", command=self.detail_text.xview)
        self.detail_text.configure(yscrollcommand=ysb2.set, xscrollcommand=xsb2.set)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        ysb2.grid(row=0, column=1, sticky="ns")
        xsb2.grid(row=1, column=0, sticky="ew")
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        text_holder.rowconfigure(0, weight=1)
        text_holder.columnconfigure(0, weight=1)
        self.detail_text.tag_configure("hl", background="#FFF3A3")
        self.detail_text.configure(state="disabled")

        # 右侧：流速曲线 + 元信息
        side = ttk.Frame(body, style="Card.TFrame")
        side.grid(row=0, column=1, sticky="nsew")
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)

        self.chart = tk.Canvas(side, height=110, bg=c["surface"], highlightthickness=1,
                               highlightbackground=c["border"], bd=0)
        self.chart.grid(row=0, column=0, sticky="ew")
        self.chart.bind("<Configure>", lambda e: self._draw_chart())

        meta_frame = ttk.Frame(side, style="Card.TFrame")
        meta_frame.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.meta_text = tk.Text(
            meta_frame, width=1, height=6, wrap="word", font=theme.FONTS["small"],
            bg=c["surface_alt"], fg=c["text_muted"], relief="flat", bd=0,
            highlightthickness=0, padx=8, pady=6,
        )
        self.meta_text.pack(fill="both", expand=True)
        self.meta_text.configure(state="disabled")

    # ================================================================== #
    # 数据写入
    # ================================================================== #
    def add_record(self, record: MessageRecord, properties: Optional[Dict[str, Any]] = None) -> None:
        """由主窗口调用：新增一条消息。"""
        rec = record
        rec.raw = rec.raw or rec.payload.encode("utf-8", errors="ignore")
        if not rec.size:
            rec.size = len(rec.raw)
        iid = f"m{int(rec.timestamp * 1000)}_{len(self.records)}"
        self.records.append((iid, rec))

        # 计数（用于订阅页与统计）
        counter = getattr(self.app, "topic_counter", None)
        if counter is not None and rec.direction == "in":
            for sub in self.app.cfg.subscriptions:
                from ..core import topic_matches

                if topic_matches(sub.topic, rec.topic):
                    key = sub.topic
                    counter[key] = counter.get(key, 0) + 1

        if self._paused:
            self._update_stats()
            return
        if self._passes_filter(rec):
            self._insert_row(iid, rec)
        self._trim()
        self._update_stats()

    def _insert_row(self, iid: str, rec: MessageRecord) -> None:
        tags = [rec.direction if rec.direction in ("in", "out", "sys") else "in"]
        if self._highlight_match(rec):
            tags.append("match")
        values = (
            _fmt_time(rec.timestamp),
            {"in": "← 收到", "out": "→ 发出", "sys": "⚙ 系统"}.get(rec.direction, "← 收到"),
            rec.topic,
            rec.qos,
            "✓" if rec.retain else "",
            rec.size,
            _one_line(rec.payload, 200),
        )
        self.tree.insert("", "end", iid=iid, values=values, tags=tuple(tags))
        if self.var_autoscroll.get():
            self.tree.see(iid)

    def _trim(self) -> None:
        children = self.tree.get_children()
        if len(children) <= MAX_ROWS:
            return
        for iid in children[: len(children) - MAX_ROWS]:
            self.tree.delete(iid)

    # ================================================================== #
    # 过滤
    # ================================================================== #
    def _passes_filter(self, rec: MessageRecord) -> bool:
        topic_filter = self.var_topic_filter.get().strip()
        if topic_filter:
            from fnmatch import fnmatch

            pattern = topic_filter.replace(".", "[.]")
            if not (fnmatch(rec.topic, pattern) or topic_filter.lower() in rec.topic.lower()):
                return False

        content_filter = self.var_content_filter.get().strip()
        if content_filter:
            if self.var_regex.get():
                import re

                try:
                    if not re.search(content_filter, rec.payload):
                        return False
                except re.error:
                    return False
            elif content_filter.lower() not in rec.payload.lower():
                return False

        direction = self.var_dir.get()
        if direction == "仅收到" and rec.direction != "in":
            return False
        if direction == "仅发出" and rec.direction != "out":
            return False
        if direction == "仅系统" and rec.direction != "sys":
            return False

        qos_filter = self.var_qos_filter.get()
        if qos_filter != "全部" and rec.qos != int(qos_filter[-1]):
            return False

        if self.var_retain_only.get() and not rec.retain:
            return False
        return True

    def _highlight_match(self, rec: MessageRecord) -> bool:
        keyword = self.var_highlight.get().strip()
        if not keyword:
            return False
        if self.var_regex.get():
            import re

            try:
                return bool(re.search(keyword, rec.payload) or re.search(keyword, rec.topic))
            except re.error:
                return False
        return keyword.lower() in rec.payload.lower() or keyword.lower() in rec.topic.lower()

    def _rebuild_view(self) -> None:
        self.tree.delete(*self.tree.get_children())
        limit = self._limit_value()
        source = self.records[-limit:] if limit else self.records
        for iid, rec in source:
            if self._passes_filter(rec):
                self._insert_row(iid, rec)
        self._update_stats()
        self._refresh_counts()

    def _limit_value(self) -> int:
        text = self.var_limit.get()
        if text.startswith("最近"):
            try:
                return int(text.replace("最近", "").replace("条", "").strip())
            except ValueError:
                return 0
        return 0

    def _clear_filters(self) -> None:
        self.var_topic_filter.set("")
        self.var_content_filter.set("")
        self.var_highlight.set("")
        self.var_dir.set("全部")
        self.var_qos_filter.set("全部")
        self.var_retain_only.set(False)
        self._rebuild_view()

    def set_topic_filter(self, topic: str) -> None:
        self.var_topic_filter.set(topic)
        self._rebuild_view()

    def _refresh_counts(self) -> None:
        panel = self.app.panels.get("subscribe")
        if panel is not None and hasattr(panel, "update_counts"):
            panel.update_counts()

    def _update_stats(self) -> None:
        shown = len(self.tree.get_children())
        c = self.app.service.stats
        self.lbl_stats.configure(
            text=f"显示 {shown} / 缓存 {len(self.records)}　收 {c['rx_count']} 发 {c['tx_count']}　"
                 f"↑{_fmt_bytes(c['tx_bytes'])} ↓{_fmt_bytes(c['rx_bytes'])}"
        )
        if self._paused:
            self.lbl_stats.configure(text="⏸ 已暂停　" + self.lbl_stats.cget("text"))

    # ================================================================== #
    # 详情
    # ================================================================== #
    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        rec = self._find_record(sel[0])
        if rec is None:
            return
        self._show_detail(rec)

    def _find_record(self, iid: str) -> Optional[MessageRecord]:
        for rid, rec in reversed(self.records):
            if rid == iid:
                return rec
        return None

    def _show_detail(self, rec: MessageRecord) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        pretty = pretty_json(rec.payload)
        self.detail_text.insert("1.0", pretty if pretty else rec.payload)
        self.detail_text.configure(state="disabled")

        keyword = self.var_highlight.get().strip()
        if keyword and not self.var_regex.get():
            self._highlight_in_detail(keyword)

        arrow = {"in": "收到", "out": "发出", "sys": "系统"}.get(rec.direction, "收到")
        self.lbl_detail_meta.configure(
            text=f"{arrow}　{rec.topic}　QoS {rec.qos}　{rec.size} 字节　{_fmt_time_full(rec.timestamp)}"
        )
        self._build_meta(rec)

    def _highlight_in_detail(self, keyword: str) -> None:
        self.detail_text.tag_remove("hl", "1.0", "end")
        start = "1.0"
        while True:
            pos = self.detail_text.search(keyword, start, nocase=True, stopindex="end")
            if not pos:
                break
            end = f"{pos}+{len(keyword)}c"
            self.detail_text.tag_add("hl", pos, end)
            start = end

    def _build_meta(self, rec: MessageRecord) -> None:
        lines = [
            f"时间戳　{_fmt_time_full(rec.timestamp)}",
            f"方向　　{'发布' if rec.direction == 'out' else '接收'}",
            f"主题　　{rec.topic}",
            f"QoS　　 {rec.qos}　Retain {'是' if rec.retain else '否'}　DUP {'是' if rec.dup else '否'}",
            f"大小　　{rec.size} 字节（字符 {len(rec.payload)}）",
        ]
        if rec.raw and rec.direction == "in":
            try:
                text = rec.raw.decode("utf-8")
            except UnicodeDecodeError:
                lines.append(f"HEX　　 {rec.raw[:64].hex(' ').upper()}")
            lines.append(f"MID　　 {rec.mid}")
        self.meta_text.configure(state="normal")
        self.meta_text.delete("1.0", "end")
        self.meta_text.insert("1.0", "\n".join(lines))
        self.meta_text.configure(state="disabled")

    # ================================================================== #
    # 操作
    # ================================================================== #
    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.btn_pause.configure(text="继续" if self._paused else "暂停",
                                 style="Success.TButton" if self._paused else "Flat.TButton")
        if not self._paused:
            self._rebuild_view()

    def _clear(self) -> None:
        if self.records and not messagebox.askyesno("确认", "确定清空全部日志缓存吗？", parent=self):
            return
        self.records.clear()
        self.tree.delete(*self.tree.get_children())
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.configure(state="disabled")
        self.lbl_detail_meta.configure(text="选中一条消息查看详情")
        self._update_stats()

    def _delete_selected(self) -> None:
        sel = set(self.tree.selection())
        if not sel:
            return
        self.tree.delete(*sel)
        self.records = [(iid, rec) for iid, rec in self.records if iid not in sel]
        self._update_stats()

    def _copy_field(self, field: str) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        rec = self._find_record(sel[0])
        if rec is None:
            return
        text = rec.topic if field == "topic" else rec.payload
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast(f"已复制{'主题' if field == 'topic' else '内容'}", "success")

    def _copy_selected(self, _event=None) -> str:
        sel = self.tree.selection()
        if not sel:
            return "break"
        rec = self._find_record(sel[0])
        if rec is None:
            return "break"
        line = f"{_fmt_time_full(rec.timestamp)}\t{rec.topic}\t{rec.payload}"
        self.clipboard_clear()
        self.clipboard_append(line)
        self.app.toast("已复制到剪贴板", "success")
        return "break"

    def _republish_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        rec = self._find_record(sel[0])
        if rec is None:
            return
        panel = self.app.panels.get("publish")
        if panel is not None:
            panel.on_broker_publish(rec.topic, rec.payload, rec.qos, rec.retain)
            self.app.select_tab("publish")
            self.app.toast("已载入到发布页", "success")

    def _filter_by_selected_topic(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        rec = self._find_record(sel[0])
        if rec is None:
            return
        self.set_topic_filter(rec.topic)

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    # ================================================================== #
    # 导出
    # ================================================================== #
    def _export(self) -> None:
        if not self.records:
            messagebox.showinfo("提示", "没有可导出的日志。", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="导出消息日志",
            defaultextension=".csv",
            initialfile=f"mqtt_log_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV 文件", "*.csv"), ("文本文件", "*.txt"),
                       ("JSON 文件", "*.json"), ("JSON Lines", "*.jsonl")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                self._export_txt(path)
            elif path.lower().endswith((".json", ".jsonl")):
                self._export_json(path)
            else:
                self._export_csv(path)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self.app.toast(f"已导出到 {path}", "success")

    def _visible_records(self) -> List[MessageRecord]:
        iids = set(self.tree.get_children())
        return [rec for iid, rec in self.records if iid in iids]

    def _export_txt(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# MQTT 调试日志导出  {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"# 记录数：{len(self.records)}\n\n")
            for rec in self._visible_records():
                arrow = {"in": "RX", "out": "TX", "sys": "SYS"}.get(rec.direction, "RX")
                fh.write(f"[{_fmt_time_full(rec.timestamp)}] {arrow} topic={rec.topic} "
                         f"qos={rec.qos} retain={int(rec.retain)} size={rec.size}\n")
                fh.write(f"{rec.payload}\n")
                fh.write("-" * 78 + "\n")

    def _export_csv(self, path: str) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["时间", "方向", "主题", "QoS", "Retain", "字节数", "内容"])
            for rec in self._visible_records():
                writer.writerow([
                    _fmt_time_full(rec.timestamp),
                    rec.direction,
                    rec.topic,
                    rec.qos,
                    int(rec.retain),
                    rec.size,
                    rec.payload,
                ])

    def _export_json(self, path: str) -> None:
        data = [r.to_dict() for r in self._visible_records()]
        with open(path, "w", encoding="utf-8") as fh:
            if path.lower().endswith(".jsonl"):
                for item in data:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            else:
                json.dump(data, fh, ensure_ascii=False, indent=2)

    def export_payloads(self, path: str) -> None:
        """只导出内容，方便直接拿来喂给其它脚本。"""
        with open(path, "w", encoding="utf-8") as fh:
            for rec in self._visible_records():
                fh.write(rec.payload + "\n")

    # ================================================================== #
    # 流速曲线
    # ================================================================== #
    def _schedule_rate_sample(self) -> None:
        prev_rx, prev_tx = self._last_rates
        c = self.app.service.stats
        self._rate_rx.append(max(0.0, (c["rx_count"] - prev_rx) / 1.0))
        self._rate_tx.append(max(0.0, (c["tx_count"] - prev_tx) / 1.0))
        self._last_rates = (c["rx_count"], c["tx_count"])
        self._draw_chart()
        self._update_stats()
        try:
            self.after(1000, self._schedule_rate_sample)
        except tk.TclError:  # pragma: no cover
            pass

    def _draw_chart(self) -> None:
        cv = self.chart
        try:
            w = cv.winfo_width()
            h = cv.winfo_height()
        except tk.TclError:
            return
        if w < 20 or h < 20:
            return
        cv.delete("all")
        c = theme.COLORS
        pad_l, pad_r, pad_t, pad_b = 6, 6, 16, 4
        plot_w = max(1, w - pad_l - pad_r)
        plot_h = max(1, h - pad_t - pad_b)

        cv.create_text(pad_l, 6, anchor="w", text="每秒消息数（实时）",
                       fill=c["text_muted"], font=theme.FONTS["tiny"])

        rx = list(self._rate_rx)
        tx = list(self._rate_tx)
        peak = max(rx + tx + [1.0])
        mx = max(1, len(rx) - 1)

        for i, (values, color) in enumerate(((rx, c["rx"]), (tx, c["tx"]))):
            if len(values) < 2:
                continue
            points: List[float] = []
            for idx, val in enumerate(values):
                x = pad_l + plot_w * idx / mx
                y = pad_t + plot_h - (val / peak) * plot_h
                points.extend((x, y))
            cv.create_line(*points, fill=color, width=2, smooth=True)

        cv.create_text(w - pad_r, 6, anchor="e", text=f"峰值 {peak:.0f}/s",
                       fill=c["text_muted"], font=theme.FONTS["tiny"])
        cv.create_text(pad_l + 2, pad_t + 4, anchor="nw", text="◼ 接收",
                       fill=c["rx"], font=theme.FONTS["tiny"])
        cv.create_text(pad_l + 46, pad_t + 4, anchor="nw", text="◼ 发送",
                       fill=c["tx"], font=theme.FONTS["tiny"])
