"""DTP（ECU 测试平台 Agent）数据包解析。

字段依据《MQTT 数据解析参考（服务端消费手册）》data_payload_reference.md：
Agent 以 JSON / UTF-8 向 Broker 发布 ``dtp.*`` 系列 schema 的业务载荷。

本模块是纯解析逻辑（无界面依赖），供 Broker 页的「数据包解析」视图使用：

* ``parse_packet(topic, payload, ...)`` —— 不是 dtp.* JSON 时返回 ``None``，
  是则归一为带通用列（类型/工位/槽位/用例/判定/数值/规格/msg_id）的字典。
* ``describe(pkt)`` —— 详情面板顶部的人类可读摘要。

实现遵循文档 §8 的约定：

1. 按 ``schema`` 字段路由，不按 topic 判断（同一遥测主题上有两种 schema 共存）；
2. ``null`` 是有意义的值（无界限 / 控制步 / 帧型），不当缺失丢弃；
3. ``ts`` 为 ISO 8601 带东八区偏移，解析为 epoch 便于展示。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, Optional, Union

# schema → 界面上的中文类型标签（顺序即过滤下拉框顺序）
SCHEMA_LABELS: Dict[str, str] = {
    "dtp.test.result.v2": "测试结果",
    "dtp.measure.telemetry.v2": "步骤遥测",
    "dtp.agent.heartbeat.v1": "Agent心跳",
    "dtp.agent.status.v2": "Agent状态",
    "dtp.board.cmd.v1": "板卡指令",
    "dtp.board.status.v1": "板卡回包",
    "dtp.board.telemetry.v1": "板卡遥测",
}

UNKNOWN_LABEL = "未知类型"


def parse_packet(topic: str, payload: Union[str, bytes], qos: int = 0,
                 retain: bool = False) -> Optional[Dict[str, Any]]:
    """把一条 PUBLISH 解析成 DTP 包；不是 ``dtp.*`` JSON 时返回 ``None``。"""
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        text = payload
    if not text or text[0] != "{":
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema.startswith("dtp."):
        return None

    label = SCHEMA_LABELS.get(schema, UNKNOWN_LABEL)
    slot = data.get("slot_id")
    slot_text = str(slot) if isinstance(slot, int) else ""
    msg_id = str(data.get("msg_id") or "")
    station = str(data.get("station_id") or "")
    ts = _parse_ts(data.get("ts"))

    if schema == "dtp.test.result.v2":
        kind = "result"
        subject = str(data.get("case_id") or data.get("name") or "")
        verdict = str(data.get("verdict") or "")
        status = verdict
        measured = data.get("measured")
        unit = _nested(data, "signal", "unit") or ""
        value = _fmt_num(measured) + (f" {unit}" if unit and measured is not None else "")
        spec = _fmt_spec(data.get("min"), data.get("max"))
        if not spec and measured is not None:
            spec = "无界限"
        tag = "ok" if verdict == "PASS" else ("fail" if verdict == "FAIL" else "")
    elif schema == "dtp.measure.telemetry.v2":
        kind = "telemetry"
        subject = str(data.get("step_id") or "")
        verdict = str(data.get("verdict") or "")
        status = verdict or ("成功" if data.get("ok") else "失败")
        unit = str(data.get("unit") or "")
        measured = data.get("measured")
        value = _fmt_num(measured) + (f" {unit}" if unit else "")
        jd = data.get("judge_detail")
        spec = _fmt_spec(jd.get("min"), jd.get("max")) if isinstance(jd, dict) else ""
        # 帧型步的数据在 frame 块里，measured=0.0 只是占位，不适用规格/无界限语义
        if (not spec and measured is not None and data.get("kind") != "frame"
                and data.get("op") == "measure"):
            spec = "无界限"
        if status in ("FAIL", "失败") or data.get("ok") is False:
            tag = "fail"
        elif status:
            tag = "ok"
        else:
            tag = ""
    elif schema == "dtp.board.telemetry.v1":
        kind = "board_tele"
        subject = str(data.get("case_id") or "")
        status = "成功" if data.get("ok") else "失败"
        value = _fmt_num(data.get("measured"))
        spec = ""
        tag = "ok" if data.get("ok") else "fail"
    elif schema == "dtp.board.status.v1":
        kind = "board_status"
        subject = str(data.get("case_id") or "")
        if data.get("ok"):
            status, tag = "成功", "ok"
        else:
            status, tag = "失败", "fail"
        err = data.get("error_message") or data.get("error_code")
        if err:
            status += f" {err}"
        value = _fmt_num(data.get("measured"))
        spec = ""
    elif schema == "dtp.board.cmd.v1":
        kind = "board_cmd"
        subject = str(data.get("case_id") or "")
        status = str(data.get("op") or "")
        retry = data.get("retry_index")
        if isinstance(retry, int) and retry:
            status += f" 重试{retry}"
        value = str(data.get("can_command") or data.get("value") or "")
        spec = _fmt_spec(data.get("min"), data.get("max"))
        tag = ""
    elif schema == "dtp.agent.heartbeat.v1":
        kind = "heartbeat"
        subject = f"{data.get('slot_count', '?')} 槽位"
        status = f"{data.get('io_mode', '?')} · {data.get('engine_state', '?')}"
        rp = data.get("running_project")
        value = str(rp.get("part_number", "")) if isinstance(rp, dict) else ""
        spec = ""
        tag = ""
    elif schema == "dtp.agent.status.v2":
        kind = "agent_status"
        state = str(data.get("engine_state") or "?")
        slots = data.get("slots")
        if isinstance(slots, list):
            running = sum(1 for s in slots
                          if isinstance(s, dict) and s.get("state") == "RUNNING")
            subject = f"{len(slots)} 槽位 / {running} 运行"
        else:
            subject = ""
        status = state
        health = data.get("health")
        if isinstance(health, dict) and health.get("state") not in (None, "", "healthy"):
            status += f" · {health.get('state')}"
        value = str(data.get("io_mode") or "")
        spec = ""
        tag = "fail" if state == "OFFLINE" else ("warn" if state == "STOPPED" else "")
    else:
        kind = "unknown"
        subject = status = value = spec = ""
        tag = ""

    return {
        "time": ts,
        "station": station,
        "kind": label,
        "kind_key": kind,
        "schema": schema,
        "slot": slot_text,
        "subject": subject,
        "status": status,
        "value": value,
        "spec": spec,
        "msg_id": msg_id,
        "tag": tag,
        "retain": bool(retain),
        "qos": int(qos),
        "topic": topic,
        "raw": data,
    }


def describe(pkt: Dict[str, Any]) -> str:
    """生成详情面板顶部的人类可读摘要（下方另附原始 JSON）。"""
    raw = pkt.get("raw") or {}
    lines = [
        f"类型：{pkt.get('kind', '')}（{pkt.get('schema', '')}）　主题：{pkt.get('topic', '')}",
        f"工位：{pkt.get('station') or '—'}　槽位：{pkt.get('slot') or '—'}"
        f"　时间：{datetime.fromtimestamp(pkt.get('time') or time.time()).strftime('%Y-%m-%d %H:%M:%S')}",
        f"msg_id：{pkt.get('msg_id') or '—'}" + ("　[保留消息]" if pkt.get("retain") else ""),
    ]

    if pkt.get("kind_key") == "result":
        verdict = str(raw.get("verdict") or "")
        line = f"用例：{raw.get('case_id', '')} {raw.get('name', '')}".rstrip() + f"　判定：{verdict}"
        if raw.get("kind") == "frame":
            line += "　（帧比对，数值列无意义）"
        lines.append(line)
        if raw.get("measured") is not None or raw.get("min") is not None or raw.get("max") is not None:
            lines.append(
                f"实测：{_fmt_num(raw.get('measured')) or '—'}"
                f"　规格：{_fmt_spec(raw.get('min'), raw.get('max')) or '无界限'}"
                f"　耗时：{raw.get('elapsed_ms', '—')}ms　重试：{raw.get('retry_index', 0)}")
    elif pkt.get("kind_key") in ("telemetry", "board_tele", "board_status"):
        if raw.get("ok") is not None:
            lines.append(f"执行：{'成功' if raw.get('ok') else '失败'}"
                         f"　实测：{_fmt_num(raw.get('measured')) or '—'}{raw.get('unit') or ''}"
                         f"　耗时：{raw.get('elapsed_ms', '—')}ms")
        if raw.get("corr_id"):
            lines.append(f"关联请求 corr_id：{raw.get('corr_id')}")
    elif pkt.get("kind_key") == "heartbeat":
        edge = raw.get("edge")
        if isinstance(edge, dict):
            lines.append(f"主机：{edge.get('hostname', '?')}（{edge.get('ip', '?')}）"
                         f"　版本：{edge.get('app_version', '?')}")
        rp = raw.get("running_project")
        if isinstance(rp, dict) and (rp.get("part_number") or rp.get("project_number")):
            lines.append(f"项目：{rp.get('project_number', '')} {rp.get('part_number', '')}".rstrip())

    tp = raw.get("test_point")
    if isinstance(tp, dict) and (tp.get("code") or tp.get("name")):
        lines.append(f"测试点：{tp.get('code', '')} {tp.get('name', '')}".rstrip())
    sig = raw.get("signal")
    if isinstance(sig, dict) and sig.get("name"):
        unit = sig.get("unit") or ""
        lines.append(f"信号：{sig.get('name', '')}" + (f"（{unit}）" if unit else ""))
    src = raw.get("source")
    if isinstance(src, dict) and src.get("type"):
        lines.append(f"来源：{src.get('type', '?')} / {src.get('device', '?')}"
                     f" / {src.get('channel') or '—'}")
    frame = raw.get("frame")
    if isinstance(frame, dict) and frame:
        lines.append(f"帧数据：{json.dumps(frame, ensure_ascii=False)}")
    err = raw.get("last_error")
    if isinstance(err, dict) and err:
        lines.append(f"最近错误：{err.get('code', '')} {err.get('message', '')}".rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _parse_ts(ts: Any) -> float:
    """ISO 8601（含 +08:00 偏移）→ epoch；解析失败回退当前时间。"""
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts).timestamp()
        except ValueError:
            pass
    return time.time()


def _fmt_num(v: Any) -> str:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _fmt_spec(mn: Any, mx: Any) -> str:
    """规格区间文本；null 语义按文档 §3.3 处理（null = 无界限）。"""
    has_lo, has_hi = mn is not None, mx is not None
    if has_lo and has_hi:
        return f"{_fmt_num(mn)} ~ {_fmt_num(mx)}"
    if has_lo:
        return f"≥{_fmt_num(mn)}"
    if has_hi:
        return f"≤{_fmt_num(mx)}"
    return ""


def _nested(data: Dict[str, Any], *keys: Any) -> Any:
    cur: Any = data
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur
