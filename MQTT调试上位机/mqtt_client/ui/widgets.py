"""可复用的界面组件。"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from . import theme


class Card(ttk.Frame):
    """带标题的白色卡片容器，内部用 ``body`` 放控件。"""

    def __init__(self, master: tk.Misc, title: str = "", subtitle: str = "",
                 padding: int = 12, **kwargs) -> None:
        # 外层只是画边框用的普通 Frame，必须显式指定宽高为 1，
        # 否则它会按内容（尤其是含 Combobox 的子控件）撑开网格列宽，
        # 导致卡片超出容器并被裁切。
        self._outer = tk.Frame(master, bg=theme.COLORS["border"], bd=0,
                               highlightthickness=0, width=1, height=1)
        super().__init__(self._outer, style="Card.TFrame", padding=padding, **kwargs)
        super().pack(fill="both", expand=True, padx=1, pady=1)

        # 标题必须先用 side="top" 占位，正文再用 side="top"+expand 填充剩余空间
        if title:
            head = ttk.Frame(self, style="Card.TFrame")
            head.pack(side="top", fill="x", pady=(0, 8))
            ttk.Label(head, text=title, style="H3.TLabel").pack(side="left")
            if subtitle:
                ttk.Label(head, text=subtitle, style="Muted.TLabel").pack(
                    side="left", padx=(8, 0), pady=(3, 0))

        self.body = ttk.Frame(self, style="Card.TFrame")
        self.body.pack(side="top", fill="both", expand=True)

    def pack(self, **kwargs):  # type: ignore[override]
        self._outer.pack(**kwargs)
        return self

    def grid(self, **kwargs):  # type: ignore[override]
        self._outer.grid(**kwargs)
        return self


class StatusPill(tk.Canvas):
    """圆角状态胶囊：小圆点 + 文字。宽高随文字和字体自动伸缩。"""

    def __init__(self, master: tk.Misc, text: str = "未连接", color: str = "",
                 bg: str = "", width: int = 96, **kwargs) -> None:
        bg = bg or theme.COLORS["surface"]
        super().__init__(master, width=width, height=23, bg=bg,
                         highlightthickness=0, bd=0, **kwargs)
        self._text = text
        self._color = color or theme.COLORS["text_faint"]
        self._bg = bg
        self._min_width = width
        self._draw()

    def set(self, text: str, color: str) -> None:
        self._text = text
        self._color = color
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        c = theme.COLORS
        font = tkfont.Font(font=theme.FONTS["small"])
        text_w = int(font.measure(self._text))
        linespace = int(font.metrics("linespace"))
        # 23px 之前是圆点+文字的固定预留；宽高都不够时随字体/文字撑开
        w = max(self._min_width, 23 + text_w + 14)
        h = max(23, linespace + 10)
        if (int(self.cget("width")), int(self.cget("height"))) != (w, h):
            self.configure(width=w, height=h)
        # 胶囊底
        self._round_rect(1, 3, w - 1, h - 3, 8, fill=self._bg, outline=c["border"])
        # 状态点
        cy = h / 2
        self.create_oval(9, cy - 3.5, 16, cy + 3.5, fill=self._color, outline="")
        self.create_text(23, cy, text=self._text, anchor="w",
                         fill=c["text"], font=theme.FONTS["small"])

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kw)


class LabeledEntry(ttk.Frame):
    """上方标签 + 下方输入框的紧凑组合。"""

    def __init__(self, master: tk.Misc, label: str, textvariable: Optional[tk.Variable] = None,
                 width: int = 20, show: str = "", bg_style: str = "Card", **kwargs) -> None:
        super().__init__(master, style=f"{bg_style}.TFrame")
        self.label_widget = ttk.Label(self, text=label, style=f"{bg_style}Muted.TLabel")
        self.label_widget.pack(anchor="w", pady=(0, 2))
        self.var = textvariable if textvariable is not None else tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width, show=show,
                               style=f"{bg_style}.TEntry", **kwargs)
        self.entry.pack(fill="x")

    def set_label(self, text: str) -> None:
        self.label_widget.configure(text=text)


class ScrollableFrame(ttk.Frame):
    """带垂直滚动条的容器，内容放在 ``inner``。

    内容自然宽度超过可视宽度时自动显示横向滚动条，
    保证窄窗口 / 低分辨率下内容不被裁切。
    """

    def __init__(self, master: tk.Misc, bg_style: str = "Card", **kwargs) -> None:
        super().__init__(master, style=f"{bg_style}.TFrame", **kwargs)
        bg = theme.COLORS["surface"] if bg_style == "Card" else theme.COLORS["bg"]

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)

        # grid 布局 + grid_remove，隐藏横向滚动条后还能在原位置恢复
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        self.hbar.grid_remove()  # 默认隐藏，内容超宽时再显示

        self.inner = ttk.Frame(self.canvas, style=f"{bg_style}.TFrame")
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)
        self.inner.bind("<Configure>", self._bind_wheel, add="+")

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        try:
            req = self.inner.winfo_reqwidth()
            cur = self.canvas.winfo_width()
            if req > cur:
                self.canvas.itemconfigure(self._window, width=req)
        except tk.TclError:
            pass
        self._update_hbar()

    def _on_canvas_configure(self, event) -> None:
        # 让内层容器至少和画布可视宽度一样宽，同时也要能容纳内容自身的自然宽度
        try:
            req = self.inner.winfo_reqwidth()
        except tk.TclError:
            req = event.width
        self.canvas.itemconfigure(self._window, width=max(event.width, req))
        self._update_hbar()

    def _update_hbar(self) -> None:
        """内容自然宽度超出画布可视宽度时显示横向滚动条。"""
        try:
            cw = self.canvas.winfo_width()
            if cw <= 1:
                return  # 尚未完成首次布局，宽度还不可信
            if self.inner.winfo_reqwidth() > cw + 2:
                self.hbar.grid()
            else:
                self.hbar.grid_remove()
        except tk.TclError:
            pass

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event) -> None:
        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif getattr(event, "delta", 0):
            delta = -1 if event.delta > 0 else 1
        if delta:
            self.canvas.yview_scroll(delta, "units")


class SearchableText(tk.Frame):
    """带搜索高亮与行号的只读文本框，用于消息详情展示。"""

    def __init__(self, master: tk.Misc, height: int = 8, mono: bool = True, **kwargs) -> None:
        super().__init__(master, bg=theme.COLORS["border"], bd=0, highlightthickness=0)
        c = theme.COLORS
        inner = tk.Frame(self, bg=c["surface"], bd=0, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.text = tk.Text(
            inner, width=1,
            height=height,
            wrap="none",
            font=theme.FONTS["mono"] if mono else theme.FONTS["base"],
            bg=c["surface"],
            fg=c["text"],
            insertbackground=c["text"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=6,
            selectbackground=c["selection"],
            selectforeground=c["text"],
            **kwargs,
        )
        yscroll = ttk.Scrollbar(inner, orient="vertical", command=self.text.yview)
        xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)

        for tag, color in theme.tag_colors().items():
            kw = {"foreground": color}
            if tag == "highlight":
                kw["background"] = "#FFF3A3"
            self.text.tag_configure(tag, **kw)
        self.text.tag_configure("mono", font=theme.FONTS["mono"])
        self.text.configure(state="disabled")

    # ------------------------------------------------------------------ #
    def set_content(self, content: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.text.configure(state="disabled")

    def get_content(self) -> str:
        return self.text.get("1.0", "end-1c")

    def clear_highlight(self) -> None:
        self.text.tag_remove("highlight", "1.0", "end")


class LogView(tk.Frame):
    """消息日志视图：列 + 高亮行 + 着色文本。"""

    def __init__(self, master: tk.Misc, columns: List[tuple], on_select: Optional[Callable] = None,
                 on_double: Optional[Callable] = None, height: int = 12) -> None:
        super().__init__(master, bg=theme.COLORS["bg"], bd=0, highlightthickness=0)
        self.columns = columns
        keys = [c[0] for c in columns]

        self.tree = ttk.Treeview(self, columns=keys, show="headings", height=height, selectmode="extended")
        self.tree.tag_configure("_dummy", font=theme.FONTS["base"])
        self.tree.tag_configure("_dummy_mono", font=theme.FONTS["mono_small"])
        for key, label, width, anchor in columns:
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("payload", "topic")))

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        c = theme.COLORS
        self.tree.tag_configure("in", foreground=c["text"])
        self.tree.tag_configure("out", foreground=c["tx"], background=c["row_tx"])
        self.tree.tag_configure("sys", foreground=c["sys"], background=c["row_sys"])
        self.tree.tag_configure("error", foreground=c["danger"], background=c["row_err"])
        self.tree.tag_configure("retain", foreground=c["retain"])
        self.tree.tag_configure("even", background=c["row_alternate"])

        if on_select:
            self.tree.bind("<<TreeviewSelect>>", on_select)
        if on_double:
            self.tree.bind("<Double-1>", on_double)

    def add_row(self, values: List[str], tags: tuple = (), item_id: Optional[str] = None,
                at_top: bool = False) -> str:
        if at_top:
            # Treeview 不支持插入到顶部，用 move 把新行挪到第一个
            iid = self.tree.insert("", "end", iid=item_id, values=values, tags=tags)
            self.tree.move(iid, "", 0)
            return iid
        return self.tree.insert("", "end", iid=item_id, values=values, tags=tags)

    def clear(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def selected_values(self) -> Optional[List[str]]:
        sel = self.tree.selection()
        if not sel:
            return None
        return list(self.tree.item(sel[0], "values"))

    def all_items(self) -> List[str]:
        return list(self.tree.get_children())

    def scroll_to_end(self) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.see(children[-1])


class ToolTip:
    """鼠标悬停提示。"""

    def __init__(self, widget: tk.Misc, text: str, delay: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip, text=self.text, justify="left",
            background="#2B3038", foreground="#FFFFFF",
            relief="flat", padx=8, pady=4, font=theme.FONTS["small"],
        ).pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class Toast:
    """轻量浮层提示，自动消失。"""

    _active: Optional["Toast"] = None

    def __init__(self, master: tk.Misc, message: str, kind: str = "info", duration: int = 2600) -> None:
        c = theme.COLORS
        color = {
            "info": c["primary"],
            "success": c["success"],
            "error": c["danger"],
            "warn": c["warning"],
        }.get(kind, c["primary"])

        if Toast._active is not None:
            try:
                Toast._active.close()
            except tk.TclError:
                pass

        self.top = tk.Toplevel(master)
        self.top.wm_overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.97)
        except tk.TclError:  # pragma: no cover
            pass

        frame = tk.Frame(self.top, bg=color, bd=0)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text=message, bg=color, fg="#FFFFFF",
            font=theme.FONTS["body"], padx=18, pady=10, justify="left",
        ).pack()

        self.top.update_idletasks()
        master.update_idletasks()
        try:
            w = self.top.winfo_reqwidth()
            h = self.top.winfo_reqheight()
            px = master.winfo_rootx() + (master.winfo_width() - w) // 2
            py = master.winfo_rooty() + 64
            self.top.wm_geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")
        except tk.TclError:
            pass

        Toast._active = self
        self.top.after(duration, self.close)

    def close(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if Toast._active is self:
            Toast._active = None


def icon_button(master: tk.Misc, text: str, command: Callable[[], None],
                tooltip: str = "", style: str = "Ghost.TButton") -> ttk.Button:
    btn = ttk.Button(master, text=text, command=command, style=style)
    if tooltip:
        ToolTip(btn, tooltip)
    return btn


def make_toolbar(master: tk.Misc) -> ttk.Frame:
    bar = ttk.Frame(master, style="Toolbar.TFrame")
    return bar
