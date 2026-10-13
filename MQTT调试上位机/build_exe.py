#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包成独立 exe（Windows）/ 可执行文件（macOS / Linux）。

用法：
    python build_exe.py            # 打包成单文件
    python build_exe.py --onedir   # 打包成文件夹（启动更快）

会先自动安装 PyInstaller，再执行打包，产物在 dist/ 目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

APP_NAME = "MQTT调试上位机"
ENTRY = "mqtt_debugger.py"
HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd: list) -> int:
    print("$", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    onedir = "--onedir" in sys.argv
    os.chdir(HERE)

    # 1. 确保 PyInstaller 可用
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[1/3] 正在安装 PyInstaller …")
        if run([sys.executable, "-m", "pip", "install", "-U", "pyinstaller"]) != 0:
            print("安装 PyInstaller 失败，请手动执行：pip install pyinstaller")
            return 1
    else:
        print("[1/3] PyInstaller 已就绪")

    # 2. 清理旧产物
    print("[2/3] 清理旧产物 …")
    for folder in ("build", "dist"):
        shutil.rmtree(os.path.join(HERE, folder), ignore_errors=True)
    spec = os.path.join(HERE, f"{APP_NAME}.spec")
    if os.path.isfile(spec):
        os.remove(spec)

    # 3. 打包
    print("[3/3] 开始打包 …")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--windowed",                      # 不弹出控制台窗口
        "--onedir" if onedir else "--onefile",
        # paho 有动态导入，显式收集
        "--hidden-import", "paho.mqtt.client",
        "--hidden-import", "paho.mqtt.publish",
        "--collect-submodules", "paho",
        # tkinter 相关
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "tkinter.simpledialog",
        ENTRY,
    ]
    if run(cmd) != 0:
        print("打包失败，请查看上方错误信息。")
        return 1

    print("\n打包完成！")
    out = os.path.join(HERE, "dist", APP_NAME)
    if sys.platform.startswith("win"):
        target = out + ".exe" if not onedir else os.path.join(out, APP_NAME + ".exe")
    else:
        target = out if not onedir else os.path.join(out, APP_NAME)
    if os.path.exists(target):
        print("可执行文件：", target)
        print("双击即可运行，目标机器无需安装 Python 和 paho-mqtt。")
    else:
        print("产物目录：", os.path.join(HERE, "dist"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
