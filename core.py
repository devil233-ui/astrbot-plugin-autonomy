"""自主性纯逻辑层：配置校验、睡眠时段、概率、状态与运行记录。

这一层不 import astrbot，便于本机 unittest 直接覆盖；
插件主流程只负责把宿主的 cron / 事件 / 配置接进来。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import random
import tempfile
import uuid
from pathlib import Path
from typing import Any

DEFAULT_MOTIVATION = (
    "我现在要不要做点什么？结合当前情况、人格和记忆，自行决定。"
    "已有意图只是可选方向，没有意图也可以行动，也可以选择暂不行动。"
)
DEFAULT_TIMEZONE = "Asia/Shanghai"
DB_PATH_FALLBACK = Path("/AstrBot/data/data_v4.db")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "sessions": [],
    "interval_minutes": 30,
    "probability": 0.2,
    "mode": "adaptive",
    "recent_minutes": 20,
    "recent_multiplier": 0.15,
    "distant_minutes": 360,
    "distant_multiplier": 1.5,
    "sleep_windows": "",
    "timezone": DEFAULT_TIMEZONE,
    "motivation": DEFAULT_MOTIVATION,
    "owner_id": "astrbot",
    "allow_self_wake": False,
    "self_wake_during_sleep": False,
}

# 面板允许改的键：其余键（如 sessions）只由配置文件或插件页维护。
PANEL_KEYS = (
    "enabled",
    "interval_minutes",
    "probability",
    "mode",
    "recent_minutes",
    "recent_multiplier",
    "distant_minutes",
    "distant_multiplier",
    "sleep_windows",
    "timezone",
    "motivation",
    "owner_id",
    "allow_self_wake",
    "self_wake_during_sleep",
)
MODES = ("fixed", "adaptive")
MAX_RUNS = 20

# 排队超过这么久还没开始，就认定这轮没起来（平台不可用/任务被删）
STALE_QUEUE_MINUTES = 10
MAX_INTENTS = 50
STATE_VERSION = 1


# ---------- 时间 ----------
def iso(value: _dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def parse_iso(raw: Any) -> _dt.datetime | None:
    if not raw:
        return None
    text = str(raw).replace("T", " ").rstrip("Z")
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def minutes_since(stamp: _dt.datetime | None, now: _dt.datetime) -> float | None:
    if stamp is None:
        return None
    if stamp.tzinfo is None and now.tzinfo is not None:
        stamp = stamp.replace(tzinfo=now.tzinfo)
    try:
        return max(0.0, (now - stamp).total_seconds() / 60.0)
    except TypeError:
        return None


# ---------- 睡眠时段 ----------
def parse_windows(raw: Any) -> list[tuple[int, int]]:
    """把 "23:30-08:30,12:00-13:00" 解析成分钟区间；非法片段直接忽略。"""
    out: list[tuple[int, int]] = []
    for part in str(raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        left, right = (x.strip() for x in part.split("-", 1))
        try:
            ah, am = (int(x) for x in left.split(":"))
            bh, bm = (int(x) for x in right.split(":"))
        except ValueError:
            continue
        if not (0 <= ah <= 23 and 0 <= bh <= 23 and 0 <= am <= 59 and 0 <= bm <= 59):
            continue
        start, end = ah * 60 + am, bh * 60 + bm
        if start != end:
            out.append((start, end))
    return out


def invalid_windows(raw: Any) -> list[str]:
    """列出解析不了的片段，供面板报错。"""
    bad: list[str] = []
    for part in str(raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            bad.append(part)
            continue
        left, right = (x.strip() for x in part.split("-", 1))
        try:
            ah, am = (int(x) for x in left.split(":"))
            bh, bm = (int(x) for x in right.split(":"))
        except ValueError:
            bad.append(part)
            continue
        if not (0 <= ah <= 23 and 0 <= bh <= 23 and 0 <= am <= 59 and 0 <= bm <= 59):
            bad.append(part)
    return bad


def sleeping(windows: list[tuple[int, int]], now: _dt.datetime) -> bool:
    minute = now.hour * 60 + now.minute
    for start, end in windows:
        if start < end and start <= minute < end:
            return True
        if start > end and (minute >= start or minute < end):
            return True
    return False


# ---------- 概率 ----------
def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def probability(settings: dict, age_minutes: float | None) -> float:
    base = clamp01(settings.get("probability", DEFAULTS["probability"]))
    if str(settings.get("mode", "adaptive")) != "adaptive" or age_minutes is None:
        return base
    recent = float(settings.get("recent_minutes", DEFAULTS["recent_minutes"]))
    distant = float(settings.get("distant_minutes", DEFAULTS["distant_minutes"]))
    if age_minutes < recent:
        factor = float(settings.get("recent_multiplier", DEFAULTS["recent_multiplier"]))
    elif age_minutes >= distant:
        factor = float(settings.get("distant_multiplier", DEFAULTS["distant_multiplier"]))
    else:
        factor = 1.0
    return clamp01(base * factor)


def next_wait_seconds(interval_minutes: int, rng: random.Random | None = None) -> float:
    """保留原有节奏：间隔×±10% 抖动。"""
    rand = rng or random
    return max(60.0, float(interval_minutes) * 60.0 * rand.uniform(0.9, 1.1))


# ---------- 配置 ----------
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_settings(raw: dict | None) -> dict:
    """把宿主配置（可能缺键、类型不对）补齐成完整设置。"""
    src = dict(raw or {})
    out = dict(DEFAULTS)
    out["enabled"] = _as_bool(src.get("enabled"), DEFAULTS["enabled"])
    sessions = src.get("sessions")
    if isinstance(sessions, str):
        sessions = [x.strip() for x in sessions.replace("，", ",").split(",")]
    if isinstance(sessions, (list, tuple)):
        out["sessions"] = [str(x).strip() for x in sessions if str(x).strip()]
    out["interval_minutes"] = max(1, _as_int(src.get("interval_minutes"), 30))
    out["probability"] = clamp01(_as_float(src.get("probability"), 0.2))
    mode = str(src.get("mode") or "adaptive").strip().lower()
    out["mode"] = mode if mode in MODES else "adaptive"
    out["recent_minutes"] = max(0, _as_int(src.get("recent_minutes"), 20))
    out["recent_multiplier"] = max(0.0, _as_float(src.get("recent_multiplier"), 0.15))
    out["distant_minutes"] = max(0, _as_int(src.get("distant_minutes"), 360))
    out["distant_multiplier"] = max(0.0, _as_float(src.get("distant_multiplier"), 1.5))
    out["sleep_windows"] = str(src.get("sleep_windows") or "").strip()
    out["timezone"] = str(src.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    out["motivation"] = str(src.get("motivation") or DEFAULT_MOTIVATION).strip() or DEFAULT_MOTIVATION
    out["owner_id"] = str(src.get("owner_id") or "astrbot").strip() or "astrbot"
    out["allow_self_wake"] = _as_bool(src.get("allow_self_wake"), False)
    out["self_wake_during_sleep"] = _as_bool(src.get("self_wake_during_sleep"), False)
    return out


def validate_patch(patch: dict) -> tuple[dict, list[str]]:
    """校验面板提交的部分字段；返回（清洗后的合法字段，错误列表）。"""
    clean: dict[str, Any] = {}
    errors: list[str] = []
    if not isinstance(patch, dict):
        return clean, ["提交内容不是对象"]
    for key, value in patch.items():
        if key not in PANEL_KEYS:
            errors.append(f"未知配置项：{key}")
            continue
        if key in ("enabled", "allow_self_wake", "self_wake_during_sleep"):
            clean[key] = _as_bool(value, bool(DEFAULTS[key]))
        elif key == "interval_minutes":
            minutes = _as_int(value, -1)
            if not 1 <= minutes <= 1440:
                errors.append("平均检查间隔需在 1–1440 分钟之间")
            else:
                clean[key] = minutes
        elif key == "probability":
            try:
                p = float(value)
            except (TypeError, ValueError):
                errors.append("基础概率需为数字")
                continue
            if not 0.0 <= p <= 1.0:
                errors.append("基础概率需在 0–1 之间")
            else:
                clean[key] = p
        elif key == "mode":
            mode = str(value or "").strip().lower()
            if mode not in MODES:
                errors.append("概率模式只能是 fixed 或 adaptive")
            else:
                clean[key] = mode
        elif key in ("recent_minutes", "distant_minutes"):
            minutes = _as_int(value, -1)
            if minutes < 0:
                errors.append(f"{key} 不能为负")
            else:
                clean[key] = minutes
        elif key in ("recent_multiplier", "distant_multiplier"):
            try:
                factor = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key} 需为数字")
                continue
            if not 0.0 <= factor <= 20.0:
                errors.append(f"{key} 需在 0–20 之间")
            else:
                clean[key] = factor
        elif key == "sleep_windows":
            bad = invalid_windows(value)
            if bad:
                errors.append("睡眠时段格式无效：" + "、".join(bad))
            else:
                clean[key] = str(value or "").strip()
        elif key in ("timezone", "motivation", "owner_id"):
            text = str(value or "").strip()
            if not text:
                errors.append(f"{key} 不能为空")
            elif key == "owner_id" and (" " in text or ":" in text):
                errors.append("主人 ID 不能包含空格或冒号")
            else:
                clean[key] = text
    if (
        "recent_minutes" in clean
        and "distant_minutes" in clean
        and clean["recent_minutes"] > clean["distant_minutes"]
    ):
        errors.append("近期阈值不能大于久未活动阈值")
    return clean, errors


# ---------- 状态文件 ----------
def empty_state() -> dict:
    return {"version": STATE_VERSION, "sessions": {}}


def new_session() -> dict:
    return {
        "last_activity_at": None,
        "next_check_at": None,
        "self_wake": None,
        "intents": [],
        "runs": [],
        "last_run_at": None,
    }


def load_state(path: Path) -> dict:
    """读状态；文件不存在或损坏时返回空状态，不让调度因为状态文件挂掉。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return empty_state()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return empty_state()
    data.setdefault("version", STATE_VERSION)
    return data


def save_state(path: Path, state: dict) -> None:
    """原子写：同目录临时文件 + fsync + replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def bucket(state: dict, umo: str) -> dict:
    sessions = state.setdefault("sessions", {})
    entry = sessions.get(umo)
    if not isinstance(entry, dict):
        entry = new_session()
        sessions[umo] = entry
    for key, value in new_session().items():
        entry.setdefault(key, value)
    return entry


def touch_activity(state: dict, umo: str, now: _dt.datetime) -> None:
    bucket(state, umo)["last_activity_at"] = iso(now)


def set_next_check(state: dict, umo: str, at: _dt.datetime | None) -> None:
    bucket(state, umo)["next_check_at"] = iso(at) if at else None


def prune(items: list, limit: int) -> list:
    return items[-limit:] if limit > 0 else []


def push_run(state: dict, umo: str, run: dict) -> dict:
    entry = bucket(state, umo)
    entry["runs"] = prune(list(entry.get("runs") or []) + [run], MAX_RUNS)
    entry["last_run_at"] = run.get("started_at")
    return run


def find_run(state: dict, umo: str, run_id: str) -> dict | None:
    for run in bucket(state, umo).get("runs") or []:
        if run.get("run_id") == run_id:
            return run
    return None


def finish_run(
    state: dict,
    umo: str,
    run_id: str | None,
    status: str,
    decision: str,
    reason: str,
    now: _dt.datetime,
) -> dict | None:
    """按 run_id 收尾；run_id 缺失时收尾该会话最后一个未结束的运行。"""
    entry = bucket(state, umo)
    target = find_run(state, umo, run_id) if run_id else None
    if target is None:
        for run in reversed(entry.get("runs") or []):
            if run.get("status") in ("queued", "running"):
                target = run
                break
    if target is None:
        return None
    target["status"] = status
    target["decision"] = decision
    target["reason"] = reason
    target["ended_at"] = iso(now)
    return target


def expire_stale_runs(
    state: dict, umo: str, now: _dt.datetime, minutes: int = STALE_QUEUE_MINUTES
) -> list[dict]:
    """排队太久还没开始的运行判失败。

    平台适配器不可用、job 被删、宿主重启都会让排队的唤醒没人来收尾，
    记录如果一直挂着 queued，面板和主人都会以为“她马上要说点什么”。
    """
    expired: list[dict] = []
    for run in bucket(state, umo).get("runs") or []:
        if run.get("status") != "queued":
            continue
        queued_at = parse_iso(run.get("started_at"))
        if queued_at is None:
            continue
        if (now - queued_at).total_seconds() < minutes * 60:
            continue
        run["status"] = "failed"
        run["decision"] = "no_action"
        run["reason"] = "唤醒没有真正开始：任务没起来（平台适配器不可用或任务被删）"
        run["ended_at"] = iso(now)
        expired.append(run)
    return expired


def new_run(umo: str, trigger: str, note: str, now: _dt.datetime) -> dict:
    return {
        "run_id": uuid.uuid4().hex[:12],
        "session": umo,
        "trigger": trigger,
        "note": note,
        "status": "queued",
        "decision": None,
        "reason": None,
        "started_at": iso(now),
        "ended_at": None,
    }


# ---------- 自行安排唤醒 ----------
def set_self_wake(state: dict, umo: str, at: _dt.datetime, note: str, now: _dt.datetime) -> dict:
    entry = bucket(state, umo)
    previous = entry.get("self_wake") or {}
    version = int(previous.get("version") or 0) + 1
    entry["self_wake"] = {
        "at": iso(at),
        "note": note,
        "created_at": iso(now),
        "version": version,
    }
    return entry["self_wake"]


def peek_self_wake(state: dict, umo: str) -> dict | None:
    return bucket(state, umo).get("self_wake")


def consume_self_wake(state: dict, umo: str) -> dict | None:
    """进入判断前原子取走，避免同一安排触发两次。"""
    entry = bucket(state, umo)
    item = entry.get("self_wake")
    if not item:
        return None
    entry["self_wake"] = None
    return item


def cancel_self_wake(state: dict, umo: str) -> bool:
    entry = bucket(state, umo)
    if entry.get("self_wake"):
        entry["self_wake"] = None
        return True
    return False


# ---------- 意图 ----------
def list_intents(state: dict, umo: str) -> list[dict]:
    return list(bucket(state, umo).get("intents") or [])


def add_intent(
    state: dict,
    umo: str,
    text: str,
    now: _dt.datetime,
    kind: str = "thought",
    source: str = "self",
    preferred_at: _dt.datetime | None = None,
    due_at: _dt.datetime | None = None,
) -> dict:
    entry = bucket(state, umo)
    intent = {
        "id": uuid.uuid4().hex[:10],
        "text": text,
        "kind": kind if kind in ("thought", "agreement") else "thought",
        "source": source if source in ("self", "conversation") else "self",
        "state": "open",
        "created_at": iso(now),
        "updated_at": iso(now),
        "preferred_at": iso(preferred_at) if preferred_at else None,
        "due_at": iso(due_at) if due_at else None,
        "version": 1,
        "reason": "",
    }
    entry["intents"] = prune(list(entry.get("intents") or []) + [intent], MAX_INTENTS)
    return intent


def revise_intent(state: dict, umo: str, intent_id: str, now: _dt.datetime, **changes) -> dict | None:
    for intent in list_intents(state, umo):
        if intent.get("id") != intent_id:
            continue
        for key in ("text", "kind", "source", "preferred_at", "due_at", "reason"):
            if key in changes:
                intent[key] = changes[key]
        intent["version"] = int(intent.get("version") or 1) + 1
        intent["updated_at"] = iso(now)
        return intent
    return None


def close_intent(
    state: dict, umo: str, intent_id: str, now: _dt.datetime, state_name: str, reason: str = ""
) -> dict | None:
    for intent in list_intents(state, umo):
        if intent.get("id") != intent_id:
            continue
        intent["state"] = state_name if state_name in ("done", "abandoned") else "done"
        intent["reason"] = reason
        intent["version"] = int(intent.get("version") or 1) + 1
        intent["updated_at"] = iso(now)
        return intent
    return None


def open_intents(state: dict, umo: str) -> list[dict]:
    return [x for x in list_intents(state, umo) if x.get("state") == "open"]
