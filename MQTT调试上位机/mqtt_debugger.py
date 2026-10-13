#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MQTT 全功能调试上位机 —— 启动入口。

直接运行本文件即可：``python mqtt_debugger.py``
"""

from __future__ import annotations

import os
import sys


def _check_environment() -> None:
    """启动前做一次依赖自检，给出可操作的中文提示。"""
    problems = []

    if sys.version_info < (3, 8):
        problems.append(
            f"Python 版本过低（当前 {sys.version.split()[0]}），需要 3.8 或更高版本。"
        )

    try:
        import tkinter  # noqa: F401
    except ImportError:
        problems.append(
            "当前 Python 缺少 tkinter 图形库。\n"
            "  · Windows：重新安装 Python 时勾选「tcl/tk and IDLE」\n"
            "  · Ubuntu/Debian：sudo apt install python3-tk\n"
            "  · macOS：brew install python-tk"
        )

    try:
        import paho.mqtt.client as _client

        version = getattr(__import__("paho.mqtt", fromlist=["mqtt"]), "__version__", None)
        if version:
            try:
                major = int(str(version).split(".")[0])
                if major < 1:
                    problems.append(f"paho-mqtt 版本过低（{version}），请升级：pip install -U paho-mqtt")
            except ValueError:
                pass
    except ImportError:
        problems.append("缺少 MQTT 客户端库 paho-mqtt，请执行：\n  pip install paho-mqtt")

    if problems:
        message = "启动失败，请先解决以下问题：\n\n" + "\n\n".join(f"• {p}" for p in problems)
        print(message, file=sys.stderr)
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("MQTT 调试上位机 · 环境检查", message)
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


def main() -> None:
    _check_environment()

    # 允许在任意工作目录下运行
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    from mqtt_client.ui.app import run

    run()


if __name__ == "__main__":
    main()
