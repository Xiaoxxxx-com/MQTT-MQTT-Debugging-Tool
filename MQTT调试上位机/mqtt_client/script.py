"""轻量脚本引擎。

两个能力，都不需要用户学习编程语法：

1. **自动应答规则** —— 收到匹配的消息后自动发布一条回复，
   支持 ``{topic}`` / ``{payload}`` / ``{time}`` / ``{match}`` 等占位符。
2. **定时 / 周期发布** —— 按固定周期向主题发送内容，用于做心跳、
   模拟数据源、压测。

之所以不直接允许用户写 Python 代码，是因为 ``exec`` 用户脚本会带来
任意代码执行风险；这里的规则式设计覆盖了 90% 的调试场景且更安全。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import ScriptRule
from .core import decode_payload, topic_matches


# --------------------------------------------------------------------------- #
# 条件匹配
# --------------------------------------------------------------------------- #

def match_condition(rule: ScriptRule, topic: str, payload: str) -> Tuple[bool, str]:
    """判断消息是否命中规则，返回 (是否命中, 命中片段)。"""
    if not topic_matches(rule.topic_filter, topic):
        return False, ""

    mode = (rule.match_mode or "contains").lower()
    value = rule.match_value or ""

    if mode == "any" or not value:
        return True, ""
    if mode == "contains":
        return (value in payload), value
    if mode == "equals":
        return (value == payload), value
    if mode == "startswith":
        return payload.startswith(value), value
    if mode == "endswith":
        return payload.endswith(value), value
    if mode == "notcontains":
        return (value not in payload), ""
    if mode == "regex":
        try:
            found = re.search(value, payload)
        except re.error:
            return False, ""
        if found:
            return True, found.group(0)
        return False, ""
    return False, ""


def render_template(text: str, topic: str, payload: str, match: str = "",
                    extra: Optional[Dict[str, str]] = None) -> str:
    """把占位符替换成实际内容。

    支持 ``{topic}``、``{payload}``、``{match}``、``{time}``、``{date}``、
    ``{ts}``、``{topic1}``~``{topicN}``（主题按 / 切分）。
    """
    now = datetime.now()
    mapping: Dict[str, str] = {
        "topic": topic,
        "payload": payload,
        "match": match,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "ts": str(int(time.time() * 1000)),
        "ms": str(int(time.time() * 1000) % 1000),
    }
    for idx, part in enumerate(topic.split("/"), start=1):
        mapping[f"topic{idx}"] = part
    if extra:
        mapping.update(extra)

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        return mapping.get(key, m.group(0))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _sub, text)


# --------------------------------------------------------------------------- #
# 规则引擎
# --------------------------------------------------------------------------- #

@dataclass
class RuleHit:
    rule_name: str
    topic: str
    payload: str
    reply_topic: str = ""
    timestamp: float = field(default_factory=time.time)


class ScriptEngine:
    """规则引擎。线程安全，可被 MQTT 回调线程直接调用。"""

    def __init__(self, publish_func: Callable[[str, bytes, int, bool], bool],
                 log_func: Callable[[str, str], None],
                 emit_record: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self._publish = publish_func
        self._log = log_func
        self._emit_record = emit_record
        self.rules: List[ScriptRule] = []
        self.history: List[RuleHit] = []
        self.enabled = True
        self._cooldown: Dict[str, float] = {}   # 规则名 -> 上次触发时间

    def set_rules(self, rules: List[ScriptRule]) -> None:
        self.rules = list(rules)

    def load_history(self, items: List[Dict[str, Any]]) -> None:
        for item in items:
            self.history.append(
                RuleHit(
                    rule_name=item.get("rule_name", ""),
                    topic=item.get("topic", ""),
                    payload=item.get("payload", ""),
                    reply_topic=item.get("reply_topic", ""),
                    timestamp=float(item.get("timestamp", time.time())),
                )
            )

    def export_history(self, limit: int = 500) -> List[Dict[str, Any]]:
        return [
            {
                "rule_name": h.rule_name,
                "topic": h.topic,
                "payload": h.payload,
                "reply_topic": h.reply_topic,
                "timestamp": h.timestamp,
            }
            for h in self.history[-limit:]
        ]

    # ------------------------------------------------------------------ #
    def on_message(self, topic: str, payload: str) -> List[RuleHit]:
        """处理一条收到的消息，返回本次命中的规则。"""
        if not self.enabled:
            return []
        hits: List[RuleHit] = []
        now = time.time()
        for rule in self.rules:
            if not rule.enabled:
                continue
            # 简单的防抖：同一规则 50ms 内不重复触发
            last = self._cooldown.get(rule.name, 0.0)
            if now - last < 0.05:
                continue
            matched, fragment = match_condition(rule, topic, payload)
            if not matched:
                continue
            self._cooldown[rule.name] = now
            hit = self._execute(rule, topic, payload, fragment)
            hits.append(hit)
            self.history.append(hit)
            if len(self.history) > 1000:
                self.history = self.history[-1000:]
        return hits

    def _execute(self, rule: ScriptRule, topic: str, payload: str, fragment: str) -> RuleHit:
        action = (rule.action or "publish").lower()
        hit = RuleHit(rule_name=rule.name, topic=topic, payload=payload)

        if action == "log":
            self._log(f"[规则] {rule.name} 命中主题 {topic}", "info")
            return hit

        # 计算回复主题
        if action == "reply" or not rule.reply_topic:
            reply_topic = topic
        else:
            reply_topic = rule.reply_topic
        reply_topic = render_template(reply_topic, topic, payload, fragment)
        if not reply_topic:
            reply_topic = topic
        hit.reply_topic = reply_topic

        body = render_template(rule.payload or "", topic, payload, fragment)
        data = body.encode("utf-8")

        def _fire() -> None:
            ok = self._publish(reply_topic, data, int(rule.qos), bool(rule.retain))
            level = "success" if ok else "error"
            self._log(f"[规则] {rule.name} → {reply_topic}：{body}", level)
            if ok and self._emit_record:
                self._emit_record(
                    {"topic": reply_topic, "payload": body, "qos": int(rule.qos),
                     "retain": bool(rule.retain), "source": f"脚本规则：{rule.name}"}
                )

        delay = max(0, int(rule.delay_ms)) / 1000.0
        if delay > 0:
            timer = _DelayedCall(delay, _fire)
            timer.start()
        else:
            _fire()
        return hit

    def clear_history(self) -> None:
        self.history.clear()


class _DelayedCall(Thread):
    """延迟执行一次的小工具（守护线程，不阻塞退出）。"""

    def __init__(self, delay: float, func: Callable[[], None]) -> None:
        super().__init__(daemon=True)
        self._delay = delay
        self._func = func

    def run(self) -> None:  # pragma: no cover - 时序相关
        if Event().wait(self._delay):
            return
        try:
            self._func()
        except Exception:  # noqa: BLE001 - 后台线程不能让异常掀翻进程
            pass


# --------------------------------------------------------------------------- #
# 周期发布器
# --------------------------------------------------------------------------- #

@dataclass
class PeriodicTask:
    """一个周期发布任务。"""

    topic: str
    payload: str = ""
    interval: float = 1.0          # 秒
    qos: int = 0
    retain: bool = False
    count: int = 0                 # 0 = 无限次
    enabled: bool = True
    template_mode: str = "文本"    # 文本 / JSON自增 / 随机数 / 时间戳
    step: float = 1.0              # 自增步长

    # 运行状态
    sent: int = 0


class PeriodicPublisher:
    """按周期向主题循环发送消息。"""

    def __init__(self, publish_func: Callable[[str, bytes, int, bool], bool],
                 log_func: Callable[[str, str], None]) -> None:
        self._publish = publish_func
        self._log = log_func
        self.tasks: List[PeriodicTask] = []
        self._threads: Dict[int, Thread] = {}
        self._stops: Dict[int, Event] = {}

    # ------------------------------------------------------------------ #
    def add(self, task: PeriodicTask) -> int:
        self.tasks.append(task)
        return len(self.tasks) - 1

    def remove(self, index: int) -> None:
        self.stop(index)
        if 0 <= index < len(self.tasks):
            self.tasks.pop(index)

    def start(self, index: int) -> bool:
        if not (0 <= index < len(self.tasks)):
            return False
        if index in self._threads and self._threads[index].is_alive():
            return False
        task = self.tasks[index]
        stop_flag = Event()
        self._stops[index] = stop_flag

        def _run() -> None:
            value = 0.0
            self._log(f"[周期] 启动任务 → {task.topic}（间隔 {task.interval}s）", "info")
            while not stop_flag.is_set():
                if not task.enabled:
                    break
                body = self._build_payload(task, value)
                value += task.step
                ok = self._publish(task.topic, body.encode("utf-8"), int(task.qos), bool(task.retain))
                if ok:
                    task.sent += 1
                else:
                    self._log(f"[周期] {task.topic} 发送失败", "error")
                if task.count and task.sent >= task.count:
                    break
                if stop_flag.wait(max(0.01, float(task.interval))):
                    break
            self._log(f"[周期] 任务结束 → {task.topic}，共发送 {task.sent} 次", "warn")
            self._threads.pop(index, None)
            self._stops.pop(index, None)

        thread = Thread(target=_run, daemon=True)
        self._threads[index] = thread
        thread.start()
        return True

    def stop(self, index: int) -> None:
        flag = self._stops.get(index)
        if flag:
            flag.set()

    def stop_all(self) -> None:
        for flag in list(self._stops.values()):
            flag.set()

    def is_running(self, index: int) -> bool:
        t = self._threads.get(index)
        return bool(t and t.is_alive())

    @staticmethod
    def _pick_numeric_key(data: Dict[str, Any]) -> str:
        """挑出用来自增的字段。

        优先用用户显式写的第一个数值字段（排除时间戳），
        这样 ``{"v":0}`` 会自增 ``v`` 而不是被硬塞一个 ``value``。
        """
        for key, val in data.items():
            if key in ("ts", "timestamp", "time"):
                continue
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return key
        return "value"

    @staticmethod
    def _build_payload(task: PeriodicTask, value: float) -> str:
        mode = task.template_mode
        if mode == "时间戳":
            return str(int(time.time() * 1000))
        if mode == "随机数":
            import random

            return str(round(random.uniform(0, 100), 3))
        if mode == "JSON自增":
            try:
                data = json.loads(task.payload) if task.payload.strip() else {}
            except ValueError:
                data = {}
            if isinstance(data, dict):
                # 只在没有任何可用数值字段时才补一个 value，避免污染用户报文
                target = PeriodicPublisher._pick_numeric_key(data)
                data[target] = round(value, 6)
                data["ts"] = int(time.time() * 1000)
                return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            return str(value)
        if mode == "自增":
            return str(value)
        return render_template(task.payload, task.topic, "") if "{" in task.payload else task.payload
