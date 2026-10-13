"""自动化页：规则自动应答 + 触发记录。

规则形式友好，不需要写代码：
「收到 [主题过滤器] 且 [匹配方式] [匹配内容] 的消息时 → [动作]」

占位符说明直接内嵌在界面上，用户不需要查文档。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import List, Optional

from ..config import QOS_CHOICES, ScriptRule, qos_from_label
from ..script import match_condition, render_template, topic_matches
from . import theme
from .widgets import Card, ToolTip

MATCH_MODES = [
    ("包含", "contains"),
    ("完全等于", "equals"),
    ("以…开头", "startswith"),
    ("以…结尾", "endswith"),
    ("不包含", "notcontains"),
    ("正则表达式", "regex"),
    ("任意内容", "any"),
]

ACTIONS = [
    ("发布到指定主题", "publish"),
    ("回复到原主题", "reply"),
    ("仅记录，不发送", "log"),
]

PLACEHOLDERS = "{topic} {payload} {match} {topic1} {topic2} {time} {date} {ts}"


class AutomationPanel(ttk.Frame):
    """规则列表 + 编辑器 + 触发记录。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self._editing_index: Optional[int] = None
        self._build()
        self.refresh()

    # ================================================================== #
    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame")
        outer.pack(fill="both", expand=True, padx=14, pady=12)
        outer.rowconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        top = ttk.Frame(outer, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        top.rowconfigure(0, weight=1)

        # ---------------- 左：规则列表 ---------------- #
        list_card = Card(top, title="自动应答规则")
        list_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        bar = ttk.Frame(list_card.body, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="新建规则", style="Primary.TButton",
                   command=self._new_rule).pack(side="left")
        ttk.Button(bar, text="编辑", style="Flat.TButton",
                   command=self._edit_selected).pack(side="left", padx=4)
        ttk.Button(bar, text="删除", style="Flat.TButton",
                   command=self._delete_selected).pack(side="left")
        ttk.Button(bar, text="上移", style="Flat.TButton",
                   command=lambda: self._move(-1)).pack(side="left", padx=4)
        ttk.Button(bar, text="下移", style="Flat.TButton",
                   command=lambda: self._move(1)).pack(side="left")

        self.var_engine_on = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(bar, text="启用引擎", variable=self.var_engine_on,
                             style="Card.TCheckbutton", command=self._toggle_engine)
        cb.pack(side="right")
        ToolTip(cb, "关闭后所有规则都不再生效，但配置会保留")

        cols = ("on", "name", "topic", "cond", "action")
        self.tree = ttk.Treeview(list_card.body, columns=cols, show="headings", height=9)
        for key, label, width, anchor in (
            ("on", "启用", 44, "center"),
            ("name", "规则名", 110, "w"),
            ("topic", "监听主题", 130, "w"),
            ("cond", "触发条件", 130, "w"),
            ("action", "动作", 150, "w"),
        ):
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key in ("cond", "action")))
        vsb = ttk.Scrollbar(list_card.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        c = theme.COLORS
        self.tree.tag_configure("on", foreground=c["text"])
        self.tree.tag_configure("off", foreground=c["text_faint"])
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())
        self.tree.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self, tearoff=0, font=theme.FONTS["base"])
        self.menu.add_command(label="编辑", command=self._edit_selected)
        self.menu.add_command(label="启用 / 禁用", command=self._toggle_selected)
        self.menu.add_command(label="复制规则", command=self._duplicate_selected)
        self.menu.add_separator()
        self.menu.add_command(label="删除", command=self._delete_selected)

        # ---------------- 右：编辑器 ---------------- #
        edit = Card(top, title="规则编辑")
        edit.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        body = edit.body
        body.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(body, text="规则名称", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 6))
        self.var_name = tk.StringVar()
        ttk.Entry(body, textvariable=self.var_name, style="Card.TEntry").grid(
            row=r, column=1, sticky="ew", pady=3)
        r += 1

        ttk.Label(body, text="监听主题", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 6))
        topic_row = ttk.Frame(body, style="Card.TFrame")
        topic_row.grid(row=r, column=1, sticky="ew", pady=3)
        topic_row.columnconfigure(0, weight=1)
        self.var_topic = tk.StringVar(value="#")
        combo_topic = ttk.Combobox(topic_row, textvariable=self.var_topic, font=theme.FONTS["base"])
        combo_topic.grid(row=0, column=0, sticky="ew")
        combo_topic.bind("<Button-1>", lambda e: self._refresh_topic_choices())
        self.combo_topic = combo_topic
        r += 1

        ttk.Label(body, text="匹配方式", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 6))
        self.var_mode = tk.StringVar(value=MATCH_MODES[0][0])
        combo_mode = ttk.Combobox(body, textvariable=self.var_mode, state="readonly",
                                  values=[m[0] for m in MATCH_MODES], font=theme.FONTS["base"])
        combo_mode.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1

        ttk.Label(body, text="匹配内容", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 6))
        self.var_match = tk.StringVar()
        ttk.Entry(body, textvariable=self.var_match, style="Card.TEntry").grid(
            row=r, column=1, sticky="ew", pady=3)
        r += 1

        ttk.Label(body, text="执行动作", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 6))
        self.var_action = tk.StringVar(value=ACTIONS[0][0])
        combo_action = ttk.Combobox(body, textvariable=self.var_action, state="readonly",
                                    values=[a[0] for a in ACTIONS], font=theme.FONTS["base"])
        combo_action.grid(row=r, column=1, sticky="ew", pady=3)
        combo_action.bind("<<ComboboxSelected>>", lambda e: self._sync_action_state())
        r += 1

        ttk.Label(body, text="目标主题", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=3, padx=(0, 6))
        self.var_reply_topic = tk.StringVar()
        self.e_reply = ttk.Entry(body, textvariable=self.var_reply_topic, style="Card.TEntry")
        self.e_reply.grid(row=r, column=1, sticky="ew", pady=3)
        ToolTip(self.e_reply, f"支持占位符：{PLACEHOLDERS}")
        r += 1

        ttk.Label(body, text="回复内容", style="CardMuted.TLabel").grid(
            row=r, column=0, sticky="w", pady=(3, 0), padx=(0, 6))
        self.e_payload = tk.Text(body, width=1, height=4, font=theme.FONTS["mono_small"],
                                 bg=theme.COLORS["surface"], fg=theme.COLORS["text"],
                                 relief="flat", highlightthickness=1,
                                 highlightbackground=theme.COLORS["border_strong"],
                                 highlightcolor=theme.COLORS["primary"], padx=6, pady=4)
        self.e_payload.grid(row=r, column=1, sticky="ew", pady=3)
        r += 1

        opt = ttk.Frame(body, style="Card.TFrame")
        opt.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(opt, text="QoS", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_qos = tk.StringVar(value=QOS_CHOICES[0])
        ttk.Combobox(opt, textvariable=self.var_qos, state="readonly", values=QOS_CHOICES,
                     width=13, font=theme.FONTS["base"]).pack(side="left", padx=(0, 10))
        self.var_retain = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Retain", variable=self.var_retain,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 10))
        ttk.Label(opt, text="延迟(ms)", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_delay = tk.StringVar(value="0")
        ttk.Entry(opt, textvariable=self.var_delay, style="Card.TEntry", width=6).pack(side="left")
        r += 1

        hint = ttk.Label(
            body,
            text=f"可用占位符：{PLACEHOLDERS}\n"
                 "示例：目标主题 device/{topic2}/cmd，内容 {\"ack\": 1, \"src\": \"{topic}\"}",
            style="CardMuted.TLabel", justify="left", wraplength=280,
        )
        hint.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))
        r += 1

        btns = ttk.Frame(body, style="Card.TFrame")
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(btns, text="保存规则", style="Primary.TButton",
                   command=self._save_rule).pack(side="left")
        ttk.Button(btns, text="测试匹配", style="Flat.TButton",
                   command=self._test_rule).pack(side="left", padx=6)
        ttk.Button(btns, text="取消编辑", style="Flat.TButton",
                   command=self._reset_editor).pack(side="left")

        # ---------------- 下：触发记录 ---------------- #
        log_card = Card(outer, title="规则触发记录")
        log_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        bar2 = ttk.Frame(log_card.body, style="Card.TFrame")
        bar2.pack(fill="x", pady=(0, 6))
        ttk.Button(bar2, text="清空记录", style="Flat.TButton",
                   command=self._clear_hits).pack(side="left")
        ttk.Button(bar2, text="导出记录", style="Flat.TButton",
                   command=self._export_hits).pack(side="left", padx=6)
        self.lbl_hits = ttk.Label(bar2, text="", style="CardMuted.TLabel")
        self.lbl_hits.pack(side="right")

        cols2 = ("time", "rule", "topic", "payload", "reply")
        self.hit_tree = ttk.Treeview(log_card.body, columns=cols2, show="headings", height=6)
        for key, label, width, anchor in (
            ("time", "时间", 82, "w"),
            ("rule", "规则", 110, "w"),
            ("topic", "触发主题", 170, "w"),
            ("payload", "收到内容", 220, "w"),
            ("reply", "回复主题", 170, "w"),
        ):
            self.hit_tree.heading(key, text=label, anchor="w")
            self.hit_tree.column(key, width=width, anchor=anchor,
                                 stretch=(key in ("payload", "topic")))
        vsb2 = ttk.Scrollbar(log_card.body, orient="vertical", command=self.hit_tree.yview)
        self.hit_tree.configure(yscrollcommand=vsb2.set)
        self.hit_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")

        self._sync_action_state()

    # ================================================================== #
    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for idx, rule in enumerate(self.app.cfg.scripts):
            mode_label = next((m[0] for m in MATCH_MODES if m[1] == rule.match_mode), rule.match_mode)
            cond = f"{mode_label}" + (f"「{rule.match_value}」" if rule.match_mode != "any" else "")
            action_label = next((a[0] for a in ACTIONS if a[1] == rule.action), rule.action)
            target = rule.reply_topic or "（原主题）"
            self.tree.insert(
                "", "end", iid=str(idx),
                values=("✓" if rule.enabled else "✗", rule.name, rule.topic_filter,
                        cond, f"{action_label} → {target}"),
                tags=("on" if rule.enabled else "off",),
            )
        self.var_engine_on.set(self.app.scripts.enabled)
        self.lbl_hits.configure(text=f"共 {len(self.app.scripts.history)} 条触发记录")
        self._refresh_hits()

    def _refresh_hits(self) -> None:
        self.hit_tree.delete(*self.hit_tree.get_children())
        for hit in self.app.scripts.history[-200:][::-1]:
            import time

            self.hit_tree.insert(
                "", "end",
                values=(
                    time.strftime("%H:%M:%S", time.localtime(hit.timestamp)),
                    hit.rule_name,
                    hit.topic,
                    hit.payload[:120],
                    hit.reply_topic or "—",
                ),
            )

    def reload_hits(self) -> None:
        self._refresh_hits()
        self.lbl_hits.configure(text=f"共 {len(self.app.scripts.history)} 条触发记录")

    # ================================================================== #
    def _refresh_topic_choices(self) -> None:
        topics = [s.topic for s in self.app.cfg.subscriptions]
        topics += list(getattr(self.app, "recent_topics", []))[:30]
        if "#" not in topics:
            topics.insert(0, "#")
        self.combo_topic.configure(values=list(dict.fromkeys(topics)))

    def _sync_action_state(self) -> None:
        action = self.var_action.get()
        is_log = self.var_action.get() == ACTIONS[2][0]
        self.e_reply.configure(state="disabled" if is_log else "normal")
        self.e_payload.configure(state="disabled" if is_log else "normal")

    # ================================================================== #
    def _current_action_key(self) -> str:
        label = self.var_action.get()
        return next((a[1] for a in ACTIONS if a[0] == label), "publish")

    def _current_mode_key(self) -> str:
        label = self.var_mode.get()
        return next((m[1] for m in MATCH_MODES if m[0] == label), "contains")

    def _new_rule(self) -> None:
        self._editing_index = None
        self._reset_editor()
        self.var_name.set(f"规则 {len(self.app.cfg.scripts) + 1}")
        self.var_topic.set("#")
        self.var_match.set("")

    def _reset_editor(self) -> None:
        self._editing_index = None
        self.var_name.set("")
        self.var_topic.set("#")
        self.var_mode.set(MATCH_MODES[0][0])
        self.var_match.set("")
        self.var_action.set(ACTIONS[0][0])
        self.var_reply_topic.set("")
        self.e_payload.delete("1.0", "end")
        self.var_qos.set(QOS_CHOICES[0])
        self.var_retain.set(False)
        self.var_delay.set("0")
        self._sync_action_state()

    def _edit_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            rule = self.app.cfg.scripts[idx]
        except (ValueError, IndexError):
            return
        self._editing_index = idx
        self.var_name.set(rule.name)
        self.var_topic.set(rule.topic_filter)
        self.var_mode.set(next((m[0] for m in MATCH_MODES if m[1] == rule.match_mode), MATCH_MODES[0][0]))
        self.var_match.set(rule.match_value)
        self.var_action.set(next((a[0] for a in ACTIONS if a[1] == rule.action), ACTIONS[0][0]))
        self.var_reply_topic.set(rule.reply_topic)
        self.e_payload.delete("1.0", "end")
        self.e_payload.insert("1.0", rule.payload)
        self.var_qos.set(next((q for q in QOS_CHOICES if q.startswith(str(rule.qos))), QOS_CHOICES[0]))
        self.var_retain.set(rule.retain)
        self.var_delay.set(str(rule.delay_ms))
        self._sync_action_state()

    def _save_rule(self) -> None:
        name = self.var_name.get().strip()
        topic = self.var_topic.get().strip()
        if not name:
            messagebox.showwarning("提示", "请填写规则名称。", parent=self)
            return
        if not topic:
            messagebox.showwarning("提示", "请填写监听主题。", parent=self)
            return

        rule = ScriptRule(
            name=name,
            enabled=True,
            topic_filter=topic,
            match_mode=self._current_mode_key(),
            match_value=self.var_match.get(),
            action=self._current_action_key(),
            reply_topic=self.var_reply_topic.get().strip(),
            payload=self.e_payload.get("1.0", "end-1c"),
            qos=qos_from_label(self.var_qos.get()),
            retain=bool(self.var_retain.get()),
            delay_ms=_safe_int(self.var_delay.get(), 0),
        )

        if self._editing_index is not None and 0 <= self._editing_index < len(self.app.cfg.scripts):
            old = self.app.cfg.scripts[self._editing_index]
            rule.enabled = old.enabled
            self.app.cfg.scripts[self._editing_index] = rule
            self.app.toast(f"规则「{name}」已更新", "success")
        else:
            self.app.cfg.scripts.append(rule)
            self.app.toast(f"规则「{name}」已创建", "success")

        self._editing_index = None
        self.app.sync_scripts()
        self.refresh()
        self.app.mark_dirty()

    def _test_rule(self) -> None:
        """用一条示例消息测试当前编辑中的规则是否命中。"""
        from tkinter import simpledialog

        sample = simpledialog.askstring(
            "测试匹配", "输入一条示例消息内容（用于验证匹配与占位符替换）：",
            initialvalue='{"status": "on"}', parent=self,
        )
        if sample is None:
            return
        rule = ScriptRule(
            topic_filter=self.var_topic.get().strip() or "#",
            match_mode=self._current_mode_key(),
            match_value=self.var_match.get(),
        )
        sample_topic = simpledialog.askstring("测试匹配", "示例消息的主题：",
                                              initialvalue="sensor/room1/data", parent=self)
        sample_topic = sample_topic or "sensor/room1/data"
        ok, fragment = match_condition(rule, sample_topic, sample)
        if not topic_matches(rule.topic_filter, sample_topic):
            messagebox.showinfo("测试结果", f"主题不匹配：\n{sample_topic} 不匹配 {rule.topic_filter}", parent=self)
            return
        if not ok:
            messagebox.showinfo("测试结果", "主题匹配，但内容条件不满足，规则不会触发。", parent=self)
            return
        rendered_topic = render_template(self.var_reply_topic.get() or sample_topic,
                                         sample_topic, sample, fragment)
        rendered_payload = render_template(self.e_payload.get("1.0", "end-1c"),
                                           sample_topic, sample, fragment)
        messagebox.showinfo(
            "测试结果 · 命中",
            f"规则会触发。\n\n回复主题：{rendered_topic}\n回复内容：{rendered_payload}",
            parent=self,
        )

    def _delete_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
            rule = self.app.cfg.scripts[idx]
        except (ValueError, IndexError):
            return
        if not messagebox.askyesno("确认", f"删除规则「{rule.name}」？", parent=self):
            return
        self.app.cfg.scripts.pop(idx)
        self.app.sync_scripts()
        self.refresh()
        self.app.mark_dirty()

    def _toggle_engine(self) -> None:
        self.app.scripts.enabled = bool(self.var_engine_on.get())
        self.app.toast("规则引擎已" + ("启用" if self.app.scripts.enabled else "停用"),
                       "success" if self.app.scripts.enabled else "warn")

    def _toggle_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        rule = self.app.cfg.scripts[idx]
        rule.enabled = not rule.enabled
        self.app.sync_scripts()
        self.refresh()
        self.app.mark_dirty()

    def _duplicate_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        clone = ScriptRule.from_dict(self.app.cfg.scripts[idx].to_dict())
        clone.name += " 副本"
        self.app.cfg.scripts.insert(idx + 1, clone)
        self.app.sync_scripts()
        self.refresh()
        self.app.mark_dirty()

    def _move(self, delta: int) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        new_idx = idx + delta
        scripts = self.app.cfg.scripts
        if not (0 <= new_idx < len(scripts)):
            return
        scripts[idx], scripts[new_idx] = scripts[new_idx], scripts[idx]
        self.app.sync_scripts()
        self.refresh()
        self.tree.selection_set(str(new_idx))
        self.app.mark_dirty()

    def _clear_hits(self) -> None:
        self.app.scripts.clear_history()
        self._refresh_hits()
        self.lbl_hits.configure(text="共 0 条触发记录")

    def _export_hits(self) -> None:
        import csv
        import json
        from datetime import datetime

        from tkinter import filedialog

        hits = self.app.scripts.export_history()
        if not hits:
            messagebox.showinfo("提示", "没有触发记录可导出。", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="导出触发记录", defaultextension=".csv",
            initialfile=f"mqtt_rules_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV 文件", "*.csv"), ("JSON 文件", "*.json")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(hits, fh, ensure_ascii=False, indent=2)
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["时间", "规则", "触发主题", "收到内容", "回复主题"])
                    for hit in hits:
                        writer.writerow([
                            datetime.fromtimestamp(hit["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                            hit["rule_name"], hit["topic"], hit["payload"], hit["reply_topic"],
                        ])
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self.app.toast(f"已导出 {len(hits)} 条记录", "success")

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()


def _safe_int(text: str, default: int) -> int:
    try:
        return int(str(text).strip())
    except (ValueError, TypeError):
        return default
