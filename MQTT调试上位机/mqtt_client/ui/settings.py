"""设置 / 工具页：界面偏好、日志持久化、统计信息、依赖自检。"""

from __future__ import annotations

import os
import platform
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List

from .. import __version__
from ..config import app_dir, config_path
from . import theme
from .widgets import Card, ToolTip


class SettingsPanel(ttk.Frame):
    """设置与运行环境信息。"""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, style="TFrame")
        self.app = app
        self._build()

    # ================================================================== #
    def _build(self) -> None:
        wrap = ttk.Frame(self, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=14, pady=12)

        row = ttk.Frame(wrap, style="Card.TFrame")
        row.pack(fill="both", expand=True)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        # ---------------- 界面偏好 ---------------- #
        ui_card = Card(row, title="界面偏好")
        ui_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ui = self.app.cfg.ui
        self.var_max_log = tk.StringVar(value=str(ui.get("max_log_rows", 5000)))
        self.var_autosave = tk.BooleanVar(value=ui.get("autosave_log", True))
        self.var_confirm_exit = tk.BooleanVar(value=ui.get("confirm_exit", False))
        self.var_restore_log = tk.BooleanVar(value=ui.get("restore_log", False))
        self.var_auto_save_cfg = tk.BooleanVar(value=ui.get("autosave_config", True))
        self.var_theme = tk.StringVar(value=ui.get("theme", "浅色"))

        grid = ttk.Frame(ui_card.body, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="日志最大行数", style="CardMuted.TLabel").grid(
            row=0, column=0, sticky="w", pady=4, padx=(0, 8))
        e = ttk.Entry(grid, textvariable=self.var_max_log, style="Card.TEntry", width=10)
        e.grid(row=0, column=1, sticky="w", pady=4)
        ToolTip(e, "超过上限会自动丢弃最旧的消息，避免长时间调试吃满内存")

        ttk.Checkbutton(grid, text="退出时保存消息日志到本地", variable=self.var_autosave,
                        style="Card.TCheckbutton").grid(row=1, column=0, columnspan=2,
                                                        sticky="w", pady=3)
        ttk.Checkbutton(grid, text="启动时恢复上次的日志", variable=self.var_restore_log,
                        style="Card.TCheckbutton").grid(row=2, column=0, columnspan=2,
                                                        sticky="w", pady=3)
        ttk.Checkbutton(grid, text="自动保存配置（修改后即时落盘）", variable=self.var_auto_save_cfg,
                        style="Card.TCheckbutton").grid(row=3, column=0, columnspan=2,
                                                        sticky="w", pady=3)
        ttk.Checkbutton(grid, text="退出前二次确认", variable=self.var_confirm_exit,
                        style="Card.TCheckbutton").grid(row=4, column=0, columnspan=2,
                                                        sticky="w", pady=3)

        btns = ttk.Frame(ui_card.body, style="Card.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="应用设置", style="Primary.TButton",
                   command=self._apply).pack(side="left")
        ttk.Button(btns, text="恢复默认", style="Flat.TButton",
                   command=self._reset).pack(side="left", padx=6)

        # ---------------- 配置与数据 ---------------- #
        data_card = Card(row, title="配置与数据")
        data_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        path_frame = ttk.Frame(data_card.body, style="Card.TFrame")
        path_frame.pack(fill="x")
        ttk.Label(path_frame, text="配置目录", style="Section.TLabel").pack(anchor="w")
        ttk.Label(path_frame, text=app_dir(), style="Value.TLabel",
                  wraplength=340, justify="left").pack(anchor="w", pady=(2, 8))
        ttk.Label(path_frame, text="配置文件", style="Section.TLabel").pack(anchor="w")
        ttk.Label(path_frame, text=config_path(), style="Value.TLabel",
                  wraplength=340, justify="left").pack(anchor="w", pady=(2, 8))

        ops = ttk.Frame(data_card.body, style="Card.TFrame")
        ops.pack(fill="x", pady=(4, 0))
        ttk.Button(ops, text="立即保存配置", style="Flat.TButton",
                   command=lambda: (self.app.save_config(), self.app.toast("配置已保存", "success"))
                   ).pack(side="left")
        ttk.Button(ops, text="导出配置", style="Flat.TButton",
                   command=self._export_config).pack(side="left", padx=6)
        ttk.Button(ops, text="导入配置", style="Flat.TButton",
                   command=self._import_config).pack(side="left")
        ttk.Button(ops, text="打开配置目录", style="Flat.TButton",
                   command=self._open_dir).pack(side="left", padx=6)
        ttk.Button(ops, text="重置全部配置", style="Flat.TButton",
                   command=self._reset_all).pack(side="left")

        # ---------------- 运行环境 ---------------- #
        env_card = Card(row, title="运行环境")
        env_card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))

        self.env_text = tk.Text(
            env_card.body, width=1, height=9, wrap="word", font=theme.FONTS["mono_small"],
            bg=theme.COLORS["surface_alt"], fg=theme.COLORS["text"],
            relief="flat", bd=0, highlightthickness=0, padx=10, pady=8,
        )
        self.env_text.pack(fill="both", expand=True)
        self.env_text.insert("1.0", self._env_report())
        self.env_text.configure(state="disabled")

        # ---------------- 关于 ---------------- #
        about = Card(wrap, title="关于")
        about.pack(fill="x", pady=(10, 0))
        ttk.Label(
            about.body,
            text=(
                f"MQTT 全功能调试上位机  v{__version__}\n"
                "支持 MQTT 3.1 / 3.1.1 / 5.0，TCP 与 WebSocket，TLS 双向认证，\n"
                "遗嘱消息、QoS 0/1/2、Retain、共享订阅、规则自动应答、周期发布、\n"
                "主题层级树、日志导出与过滤。纯 Python + Tkinter，无额外界面框架依赖。"
            ),
            style="CardMuted.TLabel", justify="left",
        ).pack(anchor="w")

    # ================================================================== #
    def _apply(self) -> None:
        ui = self.app.cfg.ui
        try:
            ui["max_log_rows"] = max(200, int(self.var_max_log.get().strip()))
        except ValueError:
            ui["max_log_rows"] = 5000
        ui["autosave_log"] = bool(self.var_autosave.get())
        ui["confirm_exit"] = bool(self.var_confirm_exit.get())
        ui["restore_log"] = bool(self.var_restore_log.get())
        ui["autosave_config"] = bool(self.var_auto_save_cfg.get())
        self.app.apply_ui_settings()
        self.app.save_config()
        self.app.toast("设置已应用", "success")

    def _reset(self) -> None:
        self.var_max_log.set("5000")
        self.var_autosave.set(True)
        self.var_restore_log.set(False)
        self.var_auto_save_cfg.set(True)
        self.var_confirm_exit.set(False)
        self._apply()

    def _export_config(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出配置", defaultextension=".json",
            initialfile="mqtt_debugger_config.json",
            filetypes=[("JSON 文件", "*.json")],
        )
        if not path:
            return
        try:
            self.app.cfg_save_to(path)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        self.app.toast("配置已导出", "success")

    def _import_config(self) -> None:
        path = filedialog.askopenfilename(
            title="导入配置",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno("确认", "导入会覆盖当前全部配置，继续吗？", parent=self):
            return
        ok = self.app.cfg_load_from(path)
        self.app.toast("配置已导入" if ok else "配置文件无法解析，已忽略",
                       "success" if ok else "error")

    def _open_dir(self) -> None:
        path = app_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _reset_all(self) -> None:
        if not messagebox.askyesno("确认", "将清除全部连接档案、订阅、规则与模板，确定吗？", parent=self):
            return
        self.app.reset_config()
        self.app.toast("已恢复出厂配置", "warn")

    # ================================================================== #
    def _env_report(self) -> str:
        import paho.mqtt as paho

        lines: List[str] = []
        lines.append(f"程序版本       : {__version__}")
        lines.append(f"Python 版本    : {sys.version.split()[0]}  ({platform.architecture()[0]})")
        lines.append(f"Python 路径    : {sys.executable}")
        lines.append(f"操作系统       : {platform.system()} {platform.release()} ({platform.version()})")
        lines.append(f"Tk 版本        : {tk.TkVersion}")
        try:
            lines.append(f"paho-mqtt 版本 : {paho.__version__}")
        except AttributeError:
            lines.append("paho-mqtt 版本 : 未知（API v2 支持：" +
                         ("是" if hasattr(paho.mqtt.client, "CallbackAPIVersion") else "否") + "）")
        lines.append(f"配置目录       : {app_dir()}")
        try:
            from paho.mqtt.client import CallbackAPIVersion  # noqa: F401

            lines.append("回调 API       : MQTT v2（推荐）")
        except ImportError:
            lines.append("回调 API       : MQTT v1（旧版兼容模式）")
        return "\n".join(lines)

    def refresh_env(self) -> None:
        self.env_text.configure(state="normal")
        self.env_text.delete("1.0", "end")
        self.env_text.insert("1.0", self._env_report())
        self.env_text.configure(state="disabled")
