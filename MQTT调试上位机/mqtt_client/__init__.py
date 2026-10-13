"""MQTT 调试上位机 —— 核心包。

分层说明：
    config.py   : 配置数据结构与持久化
    core.py     : 基于 paho-mqtt 的连接/订阅/发布服务，线程安全
    script.py   : 轻量脚本引擎（规则自动应答 / 定时发布）
    ui/         : Tkinter 图形界面
"""

__version__ = "1.0.0"
__all__ = ["config", "core", "script", "ui"]
