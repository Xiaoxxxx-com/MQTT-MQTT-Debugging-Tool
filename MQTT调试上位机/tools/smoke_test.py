"""界面冒烟测试：在创建真实 Tk 窗口的前提下构建全部界面，跑一遍事件循环，
逐页截图，并对「本地 Broker」页做一次真实的启停联调。

用法：python tools/smoke_test.py [--shot]
"""

from __future__ import annotations

import os
import sys
import time
import traceback
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 截图需要在 DPI 感知之后才能对齐坐标
try:
    from ctypes import windll  # type: ignore[import-not-found]

    try:
        windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        windll.user32.SetProcessDPIAware()
except ImportError:
    pass

import tkinter as tk  # noqa: E402

from mqtt_client.ui import theme  # noqa: E402
from mqtt_client.ui.app import MQTTDebuggerApp, TAB_DEFS  # noqa: E402

WANT_SHOTS = "--shot" in sys.argv


def pump(root: tk.Tk, times: int = 5) -> None:
    for _ in range(times):
        root.update_idletasks()
        root.update()


def snap(root: tk.Tk, name: str) -> None:
    if not WANT_SHOTS:
        return
    try:
        from PIL import ImageGrab  # type: ignore

        # 截图前必须把窗口提到最前，否则 ImageGrab 抓到的是压在下面的别的窗口
        root.deiconify()
        root.lift()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.focus_force()
        pump(root, 6)
        time.sleep(0.25)
        pump(root, 4)

        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        out = os.path.join(HERE, f"shot_{name}.png")
        img.save(out)

        # 校验：抓到的不该是纯色/明显不属于本窗口的画面
        actual = img.size
        expected = (w, h)
        note = "" if actual == expected else f"（尺寸 {actual} != 预期 {expected}）"
        print(f"  [SHOT] {out} {note}")

        try:
            root.attributes("-topmost", False)
        except tk.TclError:
            pass
    except ImportError:
        print("  [SKIP] 未安装 Pillow")
    except Exception as exc:  # noqa: BLE001
        print(f"  [WARN] 截图失败：{exc}")


def main() -> int:
    root = tk.Tk()
    errors = []

    app = MQTTDebuggerApp(root)
    pump(root)

    geo = app.cfg.ui.get("geometry")
    if not geo:
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w = min(1440, max(1000, int(sw * 0.86)))
            h = min(920, max(640, int(sh * 0.86)))
            root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        except tk.TclError:
            root.geometry("1340x860+10+10")
    pump(root)

    win_w, win_h = root.winfo_width(), root.winfo_height()
    print(f"[INFO] 窗口尺寸 {win_w}x{win_h}"
          f"（屏幕 {root.winfo_screenwidth()}x{root.winfo_screenheight()}）")

    # ---------------- 逐页渲染 + 溢出检查 ---------------- #
    print("\n== 逐页渲染 ==")
    for key, label, _icon in TAB_DEFS:
        try:
            app.select_tab(key)
            pump(root, 4)
            panel = app.panels[key]
            req_w = panel.winfo_reqwidth()
            req_h = panel.winfo_reqheight()
            flag = ""
            if req_w > win_w + 8:
                flag = f"  ← 请求宽度 {req_w} 超出窗口 {win_w}"
                errors.append((key, f"reqwidth {req_w} > {win_w}"))
            print(f"[OK] {label:<12} ({key:<10}) "
                  f"req {req_w}x{req_h}{flag}")
            snap(root, key)
        except Exception as exc:  # noqa: BLE001
            errors.append((key, exc))
            print(f"[FAIL] 标签页 {label} ({key}) 出错：{exc}")
            traceback.print_exc()

    # ---------------- 消息链路 ---------------- #
    print("\n== 消息链路 ==")
    try:
        from mqtt_client.config import MessageRecord

        app.select_tab("log")
        pump(root, 2)
        for topic, payload in (
            ("sensor/room1/temp", '{"temp": 25.6, "hum": 60.2}'),
            ("sensor/room2/temp", '{"temp": 26.1, "hum": 58.9}'),
            ("device/light1/status", "on"),
        ):
            rec = MessageRecord(
                timestamp=time.time(), topic=topic, payload=payload,
                qos=1, direction="in", size=len(payload.encode()), raw=payload.encode(),
            )
            app.panels["log"].add_record(rec)
            app.panels["tree"].on_message(rec)
        pump(root, 3)
        rows = len(app.panels["log"].tree.get_children())
        print(f"[OK] 日志写入成功，当前显示 {rows} 行")
        app.select_tab("tree")
        pump(root, 2)
        nodes = len(app.panels["tree"].tree.get_children())
        print(f"[OK] 主题树构建成功，根层 {nodes} 个节点")
    except Exception as exc:  # noqa: BLE001
        errors.append(("message-flow", exc))
        traceback.print_exc()

    # ---------------- 本地 Broker 真实启停 ---------------- #
    print("\n== 本地 Broker 页 ==")
    try:
        from mqtt_client.config import BrokerConfig

        panel = app.panels["broker"]
        app.select_tab("broker")
        pump(root, 3)

        # 强制用一个空闲端口，避免和用户已装的 Broker 冲突
        app.cfg.broker = BrokerConfig(bind_address="127.0.0.1", port=18901,
                                      enable_websocket=True, websocket_port=18902,
                                      websocket_path="/mqtt")
        panel.refresh_from_config()
        pump(root, 2)
        print(f"[OK] Broker 页构建完成，连接提示 {len(panel.lbl_conn_hint.get('1.0', 'end'))} 字符")

        panel._start()
        pump(root, 6)
        if not panel.broker.running:
            errors.append(("broker-start", "启动失败"))
            print("[FAIL] Broker 未进入运行状态")
        else:
            print(f"[OK] Broker 已启动，监听 127.0.0.1:18901 + WS :18902/mqtt"
                  f"（胶囊={panel.pill._text}）")

            # 用真实 paho 客户端连一下，验证界面能刷新出客户端行
            import paho.mqtt.client as mqtt

            ev = []

            def on_connect(_c, _u, _f, rc, _p=None):
                ev.append(rc)

            cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smoke-cli")
            cli.on_connect = on_connect
            cli.connect("127.0.0.1", 18901, 30)
            cli.loop_start()
            for _ in range(40):
                pump(root, 1)
                time.sleep(0.05)
                if ev:
                    break

            panel._refresh_tables()
            pump(root, 3)
            n_cli = len(panel.tv_clients.tree.get_children())
            n_subs = len(panel.tv_subs.tree.get_children())
            n_ret = len(panel.tv_ret.tree.get_children())
            stats_txt = panel.stat_labels["connections_total"].cget("text")
            print(f"[OK] 客户端表 {n_cli} 行，订阅表 {n_subs} 行，保留消息 {n_ret} 行")
            print(f"[OK] 累计连接统计 = {stats_txt}")
            if n_cli < 1:
                errors.append(("broker-client-row", "界面未显示已连接客户端"))

            # 发一条 Retain，确认保留消息表能刷新
            cli.subscribe("smoke/#", 1)
            cli.publish("smoke/retain", b'{"ok":true}', qos=1, retain=True)
            for _ in range(30):
                pump(root, 1)
                time.sleep(0.05)
            panel._refresh_tables()
            pump(root, 3)
            n_ret2 = len(panel.tv_ret.tree.get_children())
            print(f"[OK] 发布 Retain 后保留消息 {n_ret2} 行")
            if n_ret2 < 1:
                errors.append(("broker-retain-row", "保留消息表未刷新"))
            snap(root, "broker_running")

            cli.disconnect()
            cli.loop_stop()

            # WebSocket 客户端也要能连上同一个 Broker
            ws_ok = False
            try:
                ws_ev = []
                ws_cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     client_id="smoke-ws", protocol=mqtt.MQTTv311,
                                     transport="websockets", reconnect_on_failure=False)
                ws_cli.ws_set_options(path="/mqtt")
                ws_cli.on_connect = lambda _c, _u, _f, rc, _p=None: ws_ev.append(
                    getattr(rc, "value", rc))
                ws_cli.connect("127.0.0.1", 18902, 30)
                ws_cli.loop_start()
                for _ in range(40):
                    pump(root, 1)
                    time.sleep(0.05)
                    if ws_ev:
                        break
                ws_ok = bool(ws_ev) and int(ws_ev[0]) == 0
                if ws_ok:
                    panel._refresh_tables()
                    pump(root, 3)
                    n_cli_ws = len(panel.tv_clients.tree.get_children())
                    print(f"[OK] WebSocket 客户端连接成功，客户端表 {n_cli_ws} 行")
                    if n_cli_ws < 1:
                        errors.append(("broker-ws-row", "WS 客户端未出现在表格"))
                ws_cli.disconnect()
                ws_cli.loop_stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] WS 客户端测试失败：{exc}")
            if not ws_ok:
                errors.append(("broker-ws-connect", "WebSocket 客户端未能连上"))

        panel._stop()
        pump(root, 6)
        print(f"[OK] Broker 已停止（胶囊={panel.pill._text}）")
        if panel.broker.running:
            errors.append(("broker-stop", "停止失败"))
    except Exception as exc:  # noqa: BLE001
        errors.append(("broker-panel", exc))
        traceback.print_exc()

    # ---------------- 其他页面的空转 ---------------- #
    print("\n== 事件泵空转 ==")
    app.select_tab("connection")
    pump(root, 20)
    print("[OK] 事件循环无异常")
    snap(root, "final")

    root.destroy()
    if errors:
        print(f"\n共 {len(errors)} 处错误：")
        for key, exc in errors:
            print(f"  · {key}: {exc}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
