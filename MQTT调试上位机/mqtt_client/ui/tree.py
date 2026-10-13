"""主题树视图。

把收到的消息按 ``/`` 层级聚合成一棵可折叠的树，每个节点显示
"最后一条内容 + 消息条数 + 最后更新时间"，非常适合观察一整个
设备/传感器树的状态快照。右侧可查看选中节点的最新原始消息。
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from ..config import MessageRecord
from ..core import pretty_json
from . import theme
from .widgets import Card, ToolTip


class _Node:
    """主题树的一个节点。"""

    __slots__ = ("name", "path", "children", "count", "last_payload", "last_time",
                 "last_qos", "last_retain", "iid", "expanded")

    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path
        self.children: Dict[str, "_Node"] = {}
        self.count = 0
        self.last_payload = ""
        self.last_time = 0.0
        self.last_qos = 0
        self.last_retain = False
        self.iid = ""
        self.expanded = False


class TopicTreePanel(ttk.Frame):
    """主题层级树。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self.root_node = _Node("", "")
        self.nodes: Dict[str, _Node] = {"": self.root_node}
        self._dirty = False
        self._build()
        self._schedule_refresh()

    # ================================================================== #
    def _build(self) -> None:
        outer = ttk.Frame(self, style="Card.TFrame")
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        # ---------------- 工具条 ---------------- #
        card = Card(outer)
        card.pack(fill="x", pady=(0, 10))
        bar = card.body

        ttk.Label(bar, text="过滤", style="CardMuted.TLabel").pack(side="left", padx=(0, 4))
        self.var_filter = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var_filter, style="Card.TEntry", width=26)
        entry.pack(side="left", padx=(0, 12))
        entry.bind("<KeyRelease>", lambda e: self._refresh_tree())

        ttk.Button(bar, text="全部展开", style="Flat.TButton",
                   command=lambda: self._expand_all(True)).pack(side="left", padx=2)
        ttk.Button(bar, text="全部折叠", style="Flat.TButton",
                   command=lambda: self._expand_all(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="清空树", style="Flat.TButton", command=self._clear).pack(side="left", padx=2)

        self.var_autofresh = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="实时刷新", variable=self.var_autofresh,
                        style="Card.TCheckbutton").pack(side="left", padx=(12, 0))

        self.lbl_stats = ttk.Label(bar, text="", style="CardMuted.TLabel")
        self.lbl_stats.pack(side="right")

        # ---------------- 树 + 详情 ---------------- #
        pane = ttk.Panedwindow(outer, orient="horizontal")
        pane.pack(fill="both", expand=True)

        tree_frame = ttk.Frame(pane, style="Card.TFrame")
        pane.add(tree_frame, weight=3)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("name", "count", "qos", "time", "payload"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="主题层级", anchor="w")
        self.tree.column("#0", width=210, minwidth=130, anchor="w", stretch=False)
        for key, label, width, anchor in (
            ("name", "主题", 180, "w"),
            ("count", "条数", 52, "e"),
            ("qos", "QoS", 40, "center"),
            ("time", "最后更新", 78, "center"),
            ("payload", "最后内容", 240, "w"),
        ):
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "payload"))

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        c = theme.COLORS
        self.tree.tag_configure("leaf", foreground=c["text"])
        self.tree.tag_configure("branch", foreground=c["primary"], font=theme.FONTS["bold"])
        self.tree.tag_configure("recent", foreground=c["success"])
        self.tree.tag_configure("retained", foreground=c["retain"])

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewClose>>", self._on_close)

        # ---- 右侧详情 ---- #
        detail = ttk.Frame(pane, style="Card.TFrame")
        pane.add(detail, weight=2)

        head = ttk.Frame(detail, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="节点详情", style="H3.TLabel").pack(side="left")
        ttk.Button(head, text="订阅该主题", style="Ghost.TButton",
                   command=self._subscribe_selected).pack(side="right")
        ttk.Button(head, text="载入发布页", style="Ghost.TButton",
                   command=self._load_to_publish).pack(side="right", padx=4)

        self.lbl_path = ttk.Label(detail, text="未选中节点", style="CardMuted.TLabel",
                                  wraplength=280, justify="left")
        self.lbl_path.pack(anchor="w", pady=(4, 6))

        info_wrap = tk.Frame(detail, bg=theme.COLORS["border"], bd=0)
        info_wrap.pack(fill="both", expand=True)
        inner = tk.Frame(info_wrap, bg=theme.COLORS["surface"], bd=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.detail_text = tk.Text(
            inner, width=1, wrap="none", font=theme.FONTS["mono_small"],
            bg=c["surface"], fg=c["text"], relief="flat", bd=0, highlightthickness=0,
            padx=8, pady=6,
        )
        vsb2 = ttk.Scrollbar(inner, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=vsb2.set)
        self.detail_text.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")
        self.detail_text.configure(state="disabled")

    # ================================================================== #
    # 数据接入
    # ================================================================== #
    def on_message(self, record: MessageRecord) -> None:
        """收到消息时把内容并入树结构。"""
        if record.direction == "out":
            return
        parts = [p for p in record.topic.split("/") if p != ""]
        if not parts:
            return
        node = self.root_node
        path = ""
        for part in parts:
            path = f"{path}/{part}" if path else part
            child = node.children.get(part)
            if child is None:
                child = _Node(part, path)
                node.children[part] = child
                self.nodes[path] = child
            node = child
        node.count += 1
        node.last_payload = record.payload
        node.last_time = record.timestamp
        node.last_qos = record.qos
        node.last_retain = record.retain
        self._dirty = True

    def clear_data(self) -> None:
        self.root_node = _Node("", "")
        self.nodes = {"": self.root_node}
        self._dirty = True
        self._refresh_tree()

    # ================================================================== #
    def _schedule_refresh(self) -> None:
        if self._dirty and self.var_autofresh.get():
            self._dirty = False
            self._refresh_tree()
        try:
            self.after(700, self._schedule_refresh)
        except tk.TclError:  # pragma: no cover
            pass

    def _filter_text(self) -> str:
        return self.var_filter.get().strip().lower()

    def _refresh_tree(self) -> None:
        keyword = self._filter_text()
        expanded = {path for path, node in self.nodes.items() if node.expanded}

        # 过滤命中集合
        matched: set = set()
        if keyword:
            for path, node in self.nodes.items():
                if path and node.count and keyword in path.lower():
                    # 命中节点的所有祖先也要显示
                    segs = path.split("/")
                    for i in range(1, len(segs) + 1):
                        matched.add("/".join(segs[:i]))
        else:
            matched = {p for p, n in self.nodes.items() if p}

        self.tree.delete(*self.tree.get_children())
        self._populate("", self.root_node, matched, expanded, level=0)
        self._update_stats()

    def _populate(self, parent_iid: str, parent: _Node, matched: set,
                  expanded: set, level: int) -> None:
        now = time.time()
        visible_children: List[Tuple[str, _Node]] = []
        stack: List[_Node] = list(parent.children.values())
        while stack:
            node = stack.pop()
            if not node.path:
                continue
            if node.path not in matched and not any(
                p.startswith(node.path + "/") for p in matched
            ):
                continue
            visible_children.append((node.path, node))
            stack.extend(node.children.values())

        for path, node in visible_children:
            has_last = node.last_time > 0
            iid = self.tree.insert(
                parent_iid, "end", text=node.name,
                values=(
                    node.path,
                    node.count or "",
                    node.last_qos if has_last else "",
                    _fmt_clock(node.last_time) if has_last else "",
                    _one_line(node.last_payload, 120) if has_last else "",
                ),
                open=(path in expanded) or level < 1,
                tags=("recent",) if has_last and now - node.last_time < 3 else
                     ("retained",) if node.last_retain else ("leaf",),
            )
            node.iid = iid
            self._populate(iid, node, matched, expanded, level + 1)

    def _update_stats(self) -> None:
        topics = sum(1 for p, n in self.nodes.items() if p and n.count)
        total = sum(1 for p, n in self.nodes.items() if p and n.count and not n.children)
        self.lbl_stats.configure(text=f"主题节点 {topics} 个，叶子主题 {total} 个")

    def _expand_all(self, expand: bool) -> None:
        for path, node in self.nodes.items():
            node.expanded = expand
        self._refresh_tree()

    def _clear(self) -> None:
        if not messagebox.askyesno("确认", "确定清空主题树吗？", parent=self):
            return
        self.clear_data()

    # ================================================================== #
    def _node_by_iid(self, iid: str) -> Optional[_Node]:
        for node in self.nodes.values():
            if node.iid == iid and iid:
                return node
        return None

    def _on_open(self, _event=None) -> None:
        node = self._node_by_iid(self.tree.focus())
        if node:
            node.expanded = True

    def _on_close(self, _event=None) -> None:
        node = self._node_by_iid(self.tree.focus())
        if node:
            node.expanded = False

    def _on_select(self, _event=None) -> None:
        node = self._node_by_iid(self.tree.focus())
        if node is None:
            return
        count = self._subtree_count(node)
        self.lbl_path.configure(
            text=f"路径：{node.path}\n"
                 f"该节点及子节点共 {count} 条消息，最后更新 "
                 f"{_fmt_clock(node.last_time) if node.last_time else '—'}"
        )
        pretty = pretty_json(node.last_payload)
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", pretty or node.last_payload or "（无内容）")
        self.detail_text.configure(state="disabled")

    def _subtree_count(self, node: _Node) -> int:
        total = node.count
        stack = list(node.children.values())
        while stack:
            child = stack.pop()
            total += child.count
            stack.extend(child.children.values())
        return total

    def _on_double(self, _event=None) -> None:
        self._load_to_publish()

    def _selected_node(self) -> Optional[_Node]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self._node_by_iid(sel[0])

    def _subscribe_selected(self) -> None:
        node = self._selected_node()
        if node is None:
            return
        pattern = node.path + "/#" if node.children else node.path
        panel = self.app.panels.get("subscribe")
        if panel is not None:
            panel.var_topic.set(pattern)
            panel._add()
            self.app.select_tab("subscribe")

    def _load_to_publish(self) -> None:
        node = self._selected_node()
        if node is None:
            return
        panel = self.app.panels.get("publish")
        if panel is not None:
            panel.on_broker_publish(node.path, node.last_payload, node.last_qos, node.last_retain)
            self.app.select_tab("publish")
            self.app.toast("已载入到发布页", "success")


def _fmt_clock(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"
