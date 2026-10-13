"""界面主题：配色、字体、ttk 样式统一入口。

配色走的是浅色主题（与用户当前 IDE 主题一致），以蓝色为主色调，
涨跌/状态色按中国习惯：红=异常/错误，绿=正常/成功。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Dict

# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #

COLORS: Dict[str, str] = {
    "bg": "#F5F6F8",            # 窗口底色
    "surface": "#FFFFFF",       # 卡片/面板
    "surface_alt": "#FAFBFC",   # 次级面板
    "border": "#DDE1E7",        # 边框
    "border_strong": "#C4CAD3",
    "text": "#1F2329",          # 主文字
    "text_muted": "#6B7280",    # 次要文字
    "text_faint": "#9AA1AC",
    "primary": "#2B6BE4",       # 主色
    "primary_hover": "#1F58C7",
    "primary_soft": "#E8F0FE",
    "success": "#12A150",       # 成功（绿）
    "success_soft": "#E7F6EC",
    "danger": "#D93026",        # 失败 / 警告（红）
    "danger_soft": "#FDECEA",
    "warning": "#D98C00",
    "warning_soft": "#FFF6E5",
    "info": "#2B6BE4",
    "rx": "#2B6BE4",            # 收到消息
    "tx": "#12A150",            # 发出消息
    "sys": "#7C4DFF",           # 系统消息
    "retain": "#D98C00",
    # 日志行背景
    "row_rx": "#FFFFFF",
    "row_tx": "#F0F9F3",
    "row_sys": "#F7F5FF",
    "row_err": "#FEF4F3",
    "row_alternate": "#FAFBFC",
    "selection": "#D6E4FF",
}

FONTS: Dict[str, tuple] = {}

_THEME_NAME = "mqttdbg"


def _pick_family(root: tk.Misc) -> str:
    """挑一个系统里存在的中英混排友好的字体。"""
    candidates = [
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Segoe UI",
    ]
    try:
        available = set(tkfont.families(root))
    except tk.TclError:  # pragma: no cover
        available = set()
    for name in candidates:
        if name in available:
            return name
    return "TkDefaultFont"


def _pick_mono(root: tk.Misc) -> str:
    candidates = ["Cascadia Mono", "Consolas", "JetBrains Mono", "Menlo", "DejaVu Sans Mono", "Courier New"]
    try:
        available = set(tkfont.families(root))
    except tk.TclError:  # pragma: no cover
        available = set()
    for name in candidates:
        if name in available:
            return name
    return "TkFixedFont"


def setup(root: tk.Misc) -> ttk.Style:
    """安装主题与全部 ttk 样式，返回 style 对象。"""
    family = _pick_family(root)
    mono = _pick_mono(root)

    FONTS.clear()
    FONTS.update(
        {
            "base": (family, 9),
            "small": (family, 8),
            "tiny": (family, 7),
            "body": (family, 10),
            "label": (family, 9),
            "bold": (family, 9, "bold"),
            "h1": (family, 15, "bold"),
            "h2": (family, 12, "bold"),
            "h3": (family, 10, "bold"),
            "mono": (mono, 9),
            "mono_small": (mono, 8),
            "mono_bold": (mono, 9, "bold"),
            "button": (family, 9),
        }
    )

    style = ttk.Style(root)
    try:
        style.theme_use("clam")   # clam 对自定义颜色支持最好
    except tk.TclError:  # pragma: no cover
        pass

    c = COLORS
    root.configure(bg=c["bg"])

    # ---- 通用 ---- #
    style.configure(".", background=c["bg"], foreground=c["text"], font=FONTS["base"])
    style.configure("TFrame", background=c["bg"])
    style.configure("Card.TFrame", background=c["surface"], relief="flat")
    style.configure("Alt.TFrame", background=c["surface_alt"], relief="flat")
    style.configure("Toolbar.TFrame", background=c["surface"], relief="flat")

    style.configure("TLabel", background=c["bg"], foreground=c["text"], font=FONTS["label"])
    style.configure("Card.TLabel", background=c["surface"], foreground=c["text"])
    style.configure("Muted.TLabel", background=c["bg"], foreground=c["text_muted"], font=FONTS["small"])
    style.configure("CardMuted.TLabel", background=c["surface"], foreground=c["text_muted"], font=FONTS["small"])
    style.configure("H1.TLabel", background=c["surface"], foreground=c["text"], font=FONTS["h1"])
    style.configure("H2.TLabel", background=c["surface"], foreground=c["text"], font=FONTS["h2"])
    style.configure("H3.TLabel", background=c["bg"], foreground=c["text"], font=FONTS["h3"])
    style.configure("Section.TLabel", background=c["surface"], foreground=c["text_muted"],
                    font=(FONTS["small"][0], 8, "bold"))
    style.configure("Value.TLabel", background=c["surface"], foreground=c["text"], font=FONTS["mono_small"])
    style.configure("Link.TLabel", background=c["surface"], foreground=c["primary"], font=FONTS["small"])
    style.configure("Danger.TLabel", background=c["surface"], foreground=c["danger"], font=FONTS["small"])
    style.configure("Success.TLabel", background=c["surface"], foreground=c["success"], font=FONTS["small"])

    # ---- 按钮 ---- #
    style.configure(
        "TButton",
        background=c["surface"],
        foreground=c["text"],
        font=FONTS["button"],
        borderwidth=1,
        focusthickness=0,
        relief="flat",
        padding=(10, 5),
    )
    style.map(
        "TButton",
        background=[("pressed", "#E4E8EF"), ("active", "#EDF0F5"), ("disabled", "#F2F3F5")],
        foreground=[("disabled", c["text_faint"])],
        bordercolor=[("!disabled", c["border_strong"]), ("disabled", c["border"])],
        lightcolor=[("!disabled", c["border_strong"])],
        darkcolor=[("!disabled", c["border_strong"])],
    )
    style.configure(
        "Primary.TButton",
        background=c["primary"],
        foreground="#FFFFFF",
        font=(FONTS["button"][0], 9, "bold"),
        borderwidth=0,
        padding=(14, 6),
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", c["primary_hover"]), ("active", c["primary_hover"]),
                    ("disabled", "#A9BFE8")],
        foreground=[("disabled", "#F0F4FC")],
    )
    style.configure(
        "Danger.TButton",
        background=c["danger"],
        foreground="#FFFFFF",
        font=(FONTS["button"][0], 9, "bold"),
        borderwidth=0,
        padding=(14, 6),
    )
    style.map(
        "Danger.TButton",
        background=[("pressed", "#B3271F"), ("active", "#C02B23"), ("disabled", "#E8A9A4")],
    )
    style.configure(
        "Success.TButton",
        background=c["success"],
        foreground="#FFFFFF",
        font=(FONTS["button"][0], 9, "bold"),
        borderwidth=0,
        padding=(14, 6),
    )
    style.map(
        "Success.TButton",
        background=[("pressed", "#0E8A44"), ("active", "#109850"), ("disabled", "#A5D9BC")],
    )
    style.configure("Ghost.TButton", background=c["surface"], foreground=c["text_muted"],
                    borderwidth=0, padding=(6, 3), font=FONTS["small"])
    style.map("Ghost.TButton",
              background=[("active", c["primary_soft"]), ("pressed", c["primary_soft"])],
              foreground=[("active", c["primary"])])
    style.configure("Icon.TButton", background=c["surface"], foreground=c["text_muted"],
                    borderwidth=0, padding=(4, 2), font=(FONTS["base"][0], 10))
    style.map("Icon.TButton", background=[("active", c["primary_soft"])],
              foreground=[("active", c["primary"])])
    style.configure("Flat.TButton", background=c["surface"], foreground=c["text"],
                    borderwidth=1, padding=(8, 4), font=FONTS["small"])

    # ---- 输入框 ---- #
    for name, bg in (("TEntry", c["surface"]), ("Card.TEntry", c["surface"])):
        style.configure(
            name,
            fieldbackground=bg,
            background=bg,
            foreground=c["text"],
            bordercolor=c["border_strong"],
            lightcolor=c["border_strong"],
            darkcolor=c["border_strong"],
            insertcolor=c["text"],
            padding=4,
            relief="flat",
        )
    style.map(
        "TEntry",
        bordercolor=[("focus", c["primary"]), ("!focus", c["border_strong"])],
        lightcolor=[("focus", c["primary"]), ("!focus", c["border_strong"])],
        darkcolor=[("focus", c["primary"]), ("!focus", c["border_strong"])],
        fieldbackground=[("disabled", "#F2F3F5"), ("readonly", "#F7F8FA")],
        foreground=[("disabled", c["text_faint"])],
    )
    style.map(
        "Card.TEntry",
        bordercolor=[("focus", c["primary"]), ("!focus", c["border_strong"])],
        lightcolor=[("focus", c["primary"]), ("!focus", c["border_strong"])],
        darkcolor=[("focus", c["primary"]), ("!focus", c["border_strong"])],
        fieldbackground=[("disabled", "#F2F3F5"), ("readonly", "#F7F8FA")],
    )

    # ---- 下拉框 ---- #
    style.configure(
        "TCombobox",
        fieldbackground=c["surface"],
        background=c["surface"],
        foreground=c["text"],
        bordercolor=c["border_strong"],
        lightcolor=c["border_strong"],
        darkcolor=c["border_strong"],
        arrowcolor=c["text_muted"],
        padding=3,
        relief="flat",
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", c["surface"]), ("disabled", "#F2F3F5")],
        foreground=[("disabled", c["text_faint"])],
        bordercolor=[("focus", c["primary"])],
        lightcolor=[("focus", c["primary"])],
        darkcolor=[("focus", c["primary"])],
        arrowcolor=[("disabled", c["text_faint"])],
    )
    # 弹出列表样式
    root.option_add("*TCombobox*Listbox.background", c["surface"])
    root.option_add("*TCombobox*Listbox.foreground", c["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["primary"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.font", FONTS["base"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # ---- 复选 / 单选 ---- #
    style.configure(
        "TCheckbutton",
        background=c["surface"],
        foreground=c["text"],
        font=FONTS["label"],
        focuscolor="",
        padding=(2, 2),
    )
    style.map(
        "TCheckbutton",
        background=[("active", c["surface"])],
        foreground=[("disabled", c["text_faint"])],
        indicatorcolor=[("selected", c["primary"]), ("!selected", c["surface"])],
    )
    style.configure("Card.TCheckbutton", background=c["surface"], foreground=c["text"], focuscolor="")
    style.map("Card.TCheckbutton",
              background=[("active", c["surface"])],
              indicatorcolor=[("selected", c["primary"]), ("!selected", c["surface"])])
    style.configure("TRadiobutton", background=c["surface"], foreground=c["text"],
                    font=FONTS["label"], focuscolor="")
    style.map("TRadiobutton",
              background=[("active", c["surface"])],
              indicatorcolor=[("selected", c["primary"]), ("!selected", c["surface"])])

    # ---- 标签页 ---- #
    style.configure(
        "TNotebook",
        background=c["bg"],
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "TNotebook.Tab",
        background=c["bg"],
        foreground=c["text_muted"],
        padding=(16, 8),
        font=FONTS["button"],
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", c["surface"]), ("active", c["primary_soft"])],
        foreground=[("selected", c["primary"]), ("active", c["primary"])],
        expand=[("selected", (0, 0, 0, 0))],
    )

    # ---- 表格 ---- #
    # 行高随字体实际像素走，高分屏缩放后文字不会被行框裁掉
    try:
        base_linespace = int(tkfont.Font(root=root, font=FONTS["base"]).metrics("linespace"))
    except tk.TclError:  # pragma: no cover
        base_linespace = 0
    rowheight = max(22, base_linespace + 6)
    style.configure(
        "Treeview",
        background=c["surface"],
        fieldbackground=c["surface"],
        foreground=c["text"],
        rowheight=rowheight,
        borderwidth=0,
        font=FONTS["base"],
    )
    style.configure(
        "Treeview.Heading",
        background=c["surface_alt"],
        foreground=c["text_muted"],
        font=(FONTS["base"][0], 8, "bold"),
        relief="flat",
        padding=(6, 5),
        borderwidth=0,
    )
    style.map(
        "Treeview.Heading",
        background=[("active", c["primary_soft"])],
        foreground=[("active", c["primary"])],
    )
    style.map(
        "Treeview",
        background=[("selected", c["selection"])],
        foreground=[("selected", c["text"])],
    )
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # ---- 其它 ---- #
    style.configure("TSeparator", background=c["border"])
    style.configure("TPanedwindow", background=c["bg"])
    style.configure(
        "TScrollbar",
        background=c["surface_alt"],
        troughcolor=c["bg"],
        bordercolor=c["bg"],
        arrowcolor=c["text_faint"],
        relief="flat",
        width=11,
    )
    style.map("TScrollbar", background=[("active", c["border_strong"])])
    style.configure(
        "Vertical.TScrollbar",
        background=c["border"],
        troughcolor=c["bg"],
        bordercolor=c["bg"],
        arrowcolor=c["text_faint"],
        relief="flat",
    )
    style.configure("TProgressbar", background=c["primary"], troughcolor=c["surface_alt"],
                    bordercolor=c["surface_alt"], lightcolor=c["primary"], darkcolor=c["primary"])
    style.configure("TLabelframe", background=c["surface"], bordercolor=c["border"],
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=c["surface"], foreground=c["text_muted"],
                    font=FONTS["bold"])
    style.configure("TSpinbox", fieldbackground=c["surface"], background=c["surface"],
                    foreground=c["text"], arrowcolor=c["text_muted"],
                    bordercolor=c["border_strong"], lightcolor=c["border_strong"],
                    darkcolor=c["border_strong"], padding=3)
    style.map("TSpinbox", bordercolor=[("focus", c["primary"])],
              lightcolor=[("focus", c["primary"])], darkcolor=[("focus", c["primary"])])
    style.configure("TScale", background=c["surface"], troughcolor=c["surface_alt"])
    return style


# --------------------------------------------------------------------------- #
# 状态色辅助
# --------------------------------------------------------------------------- #

STATE_COLOR = {
    "connected": COLORS["success"],
    "connecting": COLORS["warning"],
    "reconnecting": COLORS["warning"],
    "disconnected": COLORS["text_faint"],
    "failed": COLORS["danger"],
}

LOG_LEVEL_COLOR = {
    "info": COLORS["text_muted"],
    "success": COLORS["success"],
    "warn": COLORS["warning"],
    "error": COLORS["danger"],
}


def tag_colors() -> Dict[str, str]:
    """给 Text 控件注册用的 tag 配色表。"""
    c = COLORS
    return {
        "rx": c["rx"],
        "tx": c["tx"],
        "sys": c["sys"],
        "info": c["text_muted"],
        "success": c["success"],
        "warn": c["warning"],
        "error": c["danger"],
        "time": c["text_faint"],
        "topic": c["primary"],
        "payload": c["text"],
        "muted": c["text_muted"],
        "retain": c["retain"],
        "highlight": c["danger"],
    }
