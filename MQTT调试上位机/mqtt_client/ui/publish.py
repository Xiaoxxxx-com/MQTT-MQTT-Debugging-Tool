"""发布消息页。

支持三种载荷编码（文本 / HEX / Base64 / JSON）、QoS 0-2、Retain、
历史主题下拉、内容模板、周期发布，以及"不连接直接单次发布"的快捷通道。
"""

from __future__ import annotations

import json
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional

from ..config import QOS_CHOICES, qos_from_label
from ..core import encode_payload, pretty_json
from ..script import PeriodicPublisher, PeriodicTask
from . import theme
from .widgets import Card, ToolTip

ENCODINGS = ("文本", "JSON", "HEX", "Base64")


class PublishPanel(ttk.Frame):
    """消息发布面板。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.publisher: PeriodicPublisher = app.periodic
        self._build()
        self.refresh_history()
        self.refresh_templates()

    # ================================================================== #
    def _build(self) -> None:
        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=12)

        top = ttk.Frame(wrap, style="Card.TFrame")
        top.pack(fill="both", expand=True)
        top.columnconfigure(0, weight=3, minsize=340)
        top.columnconfigure(1, weight=2, minsize=300)
        top.rowconfigure(0, weight=1)

        # ---------------- 左：发布编辑器 ---------------- #
        editor = Card(top, title="发布消息")
        editor.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        body = editor.body

        # 主题行
        trow = ttk.Frame(body, style="Card.TFrame")
        trow.pack(fill="x")
        ttk.Label(trow, text="主题", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        self.var_topic = tk.StringVar()
        self.combo_topic = ttk.Combobox(trow, textvariable=self.var_topic, font=theme.FONTS["base"])
        self.combo_topic.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ToolTip(self.combo_topic, "支持 {topic} {ts} {time} {uuid} {rand} 等变量，发布时自动替换")
        ttk.Button(trow, text="清历史", style="Ghost.TButton",
                   command=self._clear_history).pack(side="left")

        # 载荷
        prow = ttk.Frame(body, style="Card.TFrame")
        prow.pack(fill="x", pady=(10, 4))
        ttk.Label(prow, text="内容", style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
        ttk.Label(prow, text="编码", style="CardMuted.TLabel").pack(side="left", padx=(10, 4))
        self.var_encoding = tk.StringVar(value="文本")
        combo_enc = ttk.Combobox(prow, textvariable=self.var_encoding, state="readonly",
                                 values=ENCODINGS, width=8, font=theme.FONTS["base"])
        combo_enc.pack(side="left")
        combo_enc.bind("<<ComboboxSelected>>", lambda e: self._on_encoding_changed())

        ttk.Button(prow, text="格式化 JSON", style="Ghost.TButton",
                   command=self._format_json).pack(side="left", padx=(10, 0))
        ttk.Button(prow, text="插入时间戳", style="Ghost.TButton",
                   command=self._insert_timestamp).pack(side="left", padx=4)
        ttk.Button(prow, text="插入随机数", style="Ghost.TButton",
                   command=self._insert_random).pack(side="left", padx=4)
        ttk.Button(prow, text="插入 UUID", style="Ghost.TButton",
                   command=self._insert_uuid).pack(side="left")

        # 文本编辑区
        text_wrap = tk.Frame(body, bg=theme.COLORS["border"], bd=0, highlightthickness=0)
        text_wrap.pack(fill="both", expand=True, pady=(4, 0))
        inner = tk.Frame(text_wrap, bg=theme.COLORS["surface"], bd=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        c = theme.COLORS
        self.text = tk.Text(
            inner, width=1, height=12, wrap="word", font=theme.FONTS["mono"],
            bg=c["surface"], fg=c["text"], insertbackground=c["text"],
            relief="flat", bd=0, highlightthickness=0, padx=8, pady=6,
            selectbackground=c["selection"], selectforeground=c["text"], undo=True,
        )
        ysb = ttk.Scrollbar(inner, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ysb.set)
        self.text.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.text.insert("1.0", '{"cmd": "on", "value": 1}')
        self.text.bind("<Control-Return>", lambda e: (self._publish(), "break")[1])

        # 选项行
        orow = ttk.Frame(body, style="Card.TFrame")
        orow.pack(fill="x", pady=(10, 0))
        ttk.Label(orow, text="QoS", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_qos = tk.StringVar(value=QOS_CHOICES[0])
        ttk.Combobox(orow, textvariable=self.var_qos, state="readonly", values=QOS_CHOICES,
                     width=14, font=theme.FONTS["base"]).pack(side="left", padx=(0, 12))
        self.var_retain = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(orow, text="Retain（保留消息）", variable=self.var_retain,
                             style="Card.TCheckbutton")
        cb.pack(side="left")
        ToolTip(cb, "Broker 会保存该主题最后一条消息，新订阅者立刻收到")

        self.lbl_bytes = ttk.Label(orow, text="", style="CardMuted.TLabel")
        self.lbl_bytes.pack(side="right")
        self.text.bind("<KeyRelease>", lambda e: self._update_byte_count())

        # 操作按钮
        brow = ttk.Frame(body, style="Card.TFrame")
        brow.pack(fill="x", pady=(10, 0))
        ttk.Button(brow, text="发布", style="Primary.TButton", command=self._publish).pack(side="left")
        ttk.Label(brow, text="Ctrl+Enter 快捷发布", style="CardMuted.TLabel").pack(side="left", padx=10)
        ttk.Button(brow, text="清空内容", style="Flat.TButton",
                   command=lambda: self.text.delete("1.0", "end")).pack(side="right")

        # ---------------- 右：模板 / 周期 / 快捷发布 ---------------- #
        right = ttk.Frame(top, style="Card.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        tpl = Card(right, title="内容模板")
        tpl.grid(row=0, column=0, sticky="nsew")
        lst = tk.Listbox(
            tpl.body, height=7, font=theme.FONTS["base"],
            bg=theme.COLORS["surface"], fg=theme.COLORS["text"],
            selectbackground=theme.COLORS["primary"], selectforeground="#FFFFFF",
            relief="flat", highlightthickness=0, activestyle="none",
        )
        lst.pack(fill="both", expand=True)
        self.template_list = lst
        lst.bind("<Double-1>", lambda e: self._use_template())

        tb = ttk.Frame(tpl.body, style="Card.TFrame")
        tb.pack(fill="x", pady=(6, 0))
        ttk.Button(tb, text="使用", style="Flat.TButton", command=self._use_template).pack(side="left")
        ttk.Button(tb, text="存为新模板", style="Flat.TButton", command=self._save_template).pack(side="left", padx=4)
        ttk.Button(tb, text="删除", style="Flat.TButton", command=self._delete_template).pack(side="left")

        # ---- 周期发布 ---- #
        periodic = Card(right, title="周期 / 循环发布")
        periodic.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        g = ttk.Frame(periodic.body, style="Card.TFrame")
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)

        ttk.Label(g, text="间隔 (秒)", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w", pady=2, padx=(0, 6))
        self.var_interval = tk.StringVar(value="1")
        ttk.Entry(g, textvariable=self.var_interval, style="Card.TEntry", width=8).grid(
            row=0, column=1, sticky="w", pady=2)

        ttk.Label(g, text="发送次数", style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=2, padx=(0, 6))
        self.var_count = tk.StringVar(value="0")
        e = ttk.Entry(g, textvariable=self.var_count, style="Card.TEntry", width=8)
        e.grid(row=1, column=1, sticky="w", pady=2)
        ToolTip(e, "0 表示一直发送，直到手动停止")

        ttk.Label(g, text="值时变模式", style="CardMuted.TLabel").grid(row=2, column=0, sticky="w", pady=2, padx=(0, 6))
        self.var_mode = tk.StringVar(value="文本（原样发送）")
        combo_mode = ttk.Combobox(
            g, textvariable=self.var_mode, state="readonly", width=18,
            values=("文本（原样发送）", "自增", "JSON自增", "随机数", "时间戳"),
            font=theme.FONTS["base"],
        )
        combo_mode.grid(row=2, column=1, sticky="w", pady=2)

        pb = ttk.Frame(periodic.body, style="Card.TFrame")
        pb.pack(fill="x", pady=(8, 4))
        self.btn_start = ttk.Button(pb, text="开始循环", style="Success.TButton", command=self._toggle_periodic)
        self.btn_start.pack(side="left")
        ttk.Button(pb, text="停止", style="Flat.TButton", command=self._stop_periodic).pack(side="left", padx=6)

        self.lbl_periodic = ttk.Label(periodic.body, text="未启动", style="CardMuted.TLabel")
        self.lbl_periodic.pack(anchor="w")
        self._periodic_index: Optional[int] = None

        # ---- 单次发布（无需连接当前会话）---- #
        quick = Card(right, title="单次发布（不建立会话）")
        quick.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        ttk.Label(
            quick.body,
            text="直接用当前 Broker 配置新建连接、发布一条消息后立即断开。\n"
                 "适合验证 Broker 是否可达、消息是否真的发出。",
            style="CardMuted.TLabel", justify="left", wraplength=280,
        ).pack(anchor="w", pady=(0, 6))
        ttk.Button(quick.body, text="单次发布", style="Flat.TButton",
                   command=self._one_shot).pack(anchor="w")

        self._update_byte_count()

    # ================================================================== #
    # 历史与模板
    # ================================================================== #
    def refresh_history(self) -> None:
        self.combo_topic.configure(values=self.app.cfg.publish_history[-40:][::-1])

    def refresh_templates(self) -> None:
        self.template_list.delete(0, "end")
        for name in self.app.cfg.payload_templates:
            self.template_list.insert("end", name)

    def _clear_history(self) -> None:
        self.app.cfg.publish_history.clear()
        self.refresh_history()
        self.app.mark_dirty()

    def _use_template(self) -> None:
        sel = self.template_list.curselection()
        if not sel:
            return
        name = self.template_list.get(sel[0])
        content = self.app.cfg.payload_templates.get(name, "")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        pretty = pretty_json(content)
        self.var_encoding.set("JSON" if pretty else "文本")
        self._update_byte_count()

    def _save_template(self) -> None:
        from tkinter import simpledialog

        content = self.text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo("提示", "内容为空，无法保存模板。", parent=self)
            return
        name = simpledialog.askstring("保存模板", "模板名称：", parent=self)
        if not name:
            return
        self.app.cfg.payload_templates[name.strip()] = content
        self.refresh_templates()
        self.app.mark_dirty()
        self.app.toast(f"模板「{name}」已保存", "success")

    def _delete_template(self) -> None:
        sel = self.template_list.curselection()
        if not sel:
            return
        name = self.template_list.get(sel[0])
        if not messagebox.askyesno("确认", f"删除模板「{name}」？", parent=self):
            return
        self.app.cfg.payload_templates.pop(name, None)
        self.refresh_templates()
        self.app.mark_dirty()

    # ================================================================== #
    # 编辑器辅助
    # ================================================================== #
    def _on_encoding_changed(self) -> None:
        enc = self.var_encoding.get()
        content = self.text.get("1.0", "end-1c")
        if enc == "JSON":
            pretty = pretty_json(content)
            if pretty:
                self.text.delete("1.0", "end")
                self.text.insert("1.0", pretty)
        self._update_byte_count()

    def _format_json(self) -> None:
        content = self.text.get("1.0", "end-1c")
        pretty = pretty_json(content)
        if pretty is None:
            self.app.toast("内容不是合法的 JSON", "error")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", pretty)
        self.var_encoding.set("JSON")
        self._update_byte_count()
        self.app.toast("已格式化", "success")

    def _insert_timestamp(self) -> None:
        self.text.insert("insert", "{ts}")

    def _insert_random(self) -> None:
        self.text.insert("insert", "{rand}")

    def _insert_uuid(self) -> None:
        self.text.insert("insert", "{uuid}")

    def _update_byte_count(self) -> None:
        content = self.text.get("1.0", "end-1c")
        try:
            data = encode_payload(content, self.var_encoding.get())
            self.lbl_bytes.configure(text=f"{len(data)} 字节 / {len(content)} 字符")
        except ValueError as exc:
            self.lbl_bytes.configure(text=str(exc)[:40])

    # ================================================================== #
    # 发布
    # ================================================================== #
    def _publish(self) -> None:
        topic = self.var_topic.get().strip()
        if not topic:
            messagebox.showwarning("提示", "请输入发布主题。", parent=self)
            return
        template_mode = self.var_encoding.get() == "文本" or "{" in topic
        topic = self.app.render_variables(topic)
        content = self.text.get("1.0", "end-1c")

        if template_mode and "{" in content:
            content = self.app.render_variables(content)
            if content != self.text.get("1.0", "end-1c"):
                self.text.delete("1.0", "end")
                self.text.insert("1.0", content)

        try:
            data = encode_payload(content, self.var_encoding.get())
        except ValueError as exc:
            messagebox.showerror("编码错误", str(exc), parent=self)
            return

        qos = qos_from_label(self.var_qos.get())
        retain = bool(self.var_retain.get())

        if not self.app.service.is_connected:
            if messagebox.askyesno(
                "未连接",
                "当前未连接 Broker，是否用「单次发布」方式发送这条消息？",
                parent=self,
            ):
                self._one_shot(data=data, topic=topic, qos=qos, retain=retain)
            return

        if self.app.service.publish(topic, data, qos, retain):
            self.app.remember_publish_topic(topic)
            self.refresh_history()
            self.app.toast(f"已发布到 {topic}", "success")

    def _one_shot(self, data: Optional[bytes] = None, topic: str = "", qos: int = -1,
                  retain: bool = False) -> None:
        import threading

        from ..core import one_shot_publish

        cfg = self.app.cfg.active
        if data is None:
            topic = self.app.render_variables(self.var_topic.get().strip())
            if not topic:
                messagebox.showwarning("提示", "请输入发布主题。", parent=self)
                return
            content = self.text.get("1.0", "end-1c")
            if "{" in content:
                content = self.app.render_variables(content)
            try:
                data = encode_payload(content, self.var_encoding.get())
            except ValueError as exc:
                messagebox.showerror("编码错误", str(exc), parent=self)
                return
            qos = qos_from_label(self.var_qos.get())
            retain = bool(self.var_retain.get())

        self.app.toast("正在发送…", "info")

        def _worker() -> None:
            ok, msg = one_shot_publish(
                host=cfg.host, port=cfg.port, topic=topic, payload=data or b"",
                qos=qos, retain=retain, keepalive=min(30, max(5, cfg.keepalive)),
                username=cfg.username or None, password=cfg.password or None,
                use_tls=cfg.use_tls,
            )
            def _report() -> None:
                self.app.toast(f"单次发布{'成功' if ok else '失败'}：{msg}",
                               "success" if ok else "error")
                self.app.log(f"[单次发布] {topic} → {'成功' if ok else '失败：' + msg}",
                             "success" if ok else "error")
            try:
                self.after(0, _report)
            except tk.TclError:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    # ================================================================== #
    # 周期发布
    # ================================================================== #
    def _toggle_periodic(self) -> None:
        if self._periodic_index is not None and self.publisher.is_running(self._periodic_index):
            self._stop_periodic()
            return

        topic = self.app.render_variables(self.var_topic.get().strip())
        if not topic:
            messagebox.showwarning("提示", "请输入发布主题。", parent=self)
            return
        if not self.app.service.is_connected:
            messagebox.showwarning("提示", "请先连接 Broker 再启动循环发布。", parent=self)
            return

        content = self.text.get("1.0", "end-1c")
        if self.var_encoding.get() != "文本":
            try:
                encode_payload(content, self.var_encoding.get())
            except ValueError as exc:
                messagebox.showerror("编码错误", str(exc), parent=self)
                return

        mode_map = {
            "文本（原样发送）": "文本",
            "自增": "自增",
            "JSON自增": "JSON自增",
            "随机数": "随机数",
            "时间戳": "时间戳",
        }
        task = PeriodicTask(
            topic=topic,
            payload=content,
            interval=_safe_float(self.var_interval.get(), 1.0),
            qos=qos_from_label(self.var_qos.get()),
            retain=bool(self.var_retain.get()),
            count=_safe_int(self.var_count.get(), 0),
            template_mode=mode_map.get(self.var_mode.get(), "文本"),
        )
        index = self.publisher.add(task)
        self._periodic_index = index
        self.publisher.start(index)
        self.btn_start.configure(text="停止循环", style="Danger.TButton")
        self.lbl_periodic.configure(
            text=f"运行中：{topic}，间隔 {task.interval}s，"
                 f"{'无限次' if not task.count else str(task.count) + ' 次'}"
        )
        self.app.toast("循环发布已启动", "success")
        self._poll_periodic()

    def _stop_periodic(self) -> None:
        if self._periodic_index is not None:
            self.publisher.stop(self._periodic_index)
        self.btn_start.configure(text="开始循环", style="Success.TButton")
        self.lbl_periodic.configure(text="已停止")
        self._periodic_index = None

    def _poll_periodic(self) -> None:
        """定期刷新已发送次数。"""
        if self._periodic_index is None:
            return
        if not self.publisher.is_running(self._periodic_index):
            self.btn_start.configure(text="开始循环", style="Success.TButton")
            self.lbl_periodic.configure(text="已结束")
            self._periodic_index = None
            return
        task = self.publisher.tasks[self._periodic_index]
        self.lbl_periodic.configure(
            text=f"运行中：{task.topic}，已发送 {task.sent} 次，间隔 {task.interval}s"
        )
        self.after(500, self._poll_periodic)

    # ================================================================== #
    def on_broker_publish(self, topic: str, payload: str, qos: int, retain: bool,
                          source: str = "") -> None:
        """供脚本规则调用：把内容展示在编辑器里（不自动发送）。"""
        self.var_topic.set(topic)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", payload)
        self._update_byte_count()


def _safe_int(text: str, default: int) -> int:
    try:
        return int(str(text).strip())
    except (ValueError, TypeError):
        return default


def _safe_float(text: str, default: float) -> float:
    try:
        return float(str(text).strip())
    except (ValueError, TypeError):
        return default
