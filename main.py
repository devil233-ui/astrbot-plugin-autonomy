"""自主意识（Autonomy）——把"随时段与概率自行醒来、再由自己决定说不说"搬到 AstrBot。

设计对照 WhaleLife `whalelife-plugin-autonomy`：
- 平均每 interval_minutes 检查一次；
- 每次按 probability 掷骰子决定要不要醒；
- mode=adaptive 时按「距上次活动多久」调概率（刚聊完更安静，久没人说话更想开口）；
- sleep_windows 内不醒；
- 醒来时只把一句 motivation 交给主 agent，由它结合人格/记忆/上下文自行决定
  说话或保持沉默（沉默 = 不调用 send_message_to_user）。

实现上不自己发消息，而是给 cron_manager 建一条 run_once 的 active_agent 任务，
复用 AstrBot 原生的「主动唤醒」链路（含 send_message_to_user 工具与投递）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import random
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger, star
from astrbot.api.event import filter

PLUGIN_NAME = "astrbot_plugin_autonomy"
DEFAULT_MOTIVATION = (
    "我现在要不要做点什么？结合当前情况、人格和记忆，自行决定。"
    "已有意图只是可选方向，没有意图也可以行动，也可以选择暂不行动。"
)
DB_PATH = Path("/AstrBot/data/data_v4.db")


def _parse_windows(raw: str) -> list[tuple[int, int]]:
    """把 "23:30-08:30,12:00-13:00" 解析成分钟区间。"""
    out: list[tuple[int, int]] = []
    for part in str(raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        a, b = (x.strip() for x in part.split("-", 1))
        try:
            ah, am = (int(x) for x in a.split(":"))
            bh, bm = (int(x) for x in b.split(":"))
        except ValueError:
            logger.warning(f"[autonomy] 睡眠时段格式无效，已忽略：{part}")
            continue
        start, end = ah * 60 + am, bh * 60 + bm
        if start != end:
            out.append((start, end))
    return out


class AutonomyPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._task: asyncio.Task | None = None
        self._tz = self._safe_tz(str(self._cfg("timezone", "Asia/Shanghai")))
        self._windows = _parse_windows(self._cfg("sleep_windows", "23:30-08:30"))
        self._sessions = [
            str(s).strip() for s in (self._cfg("sessions", []) or []) if str(s).strip()
        ]

    # ---------- 基础 ----------
    def _cfg(self, key: str, default):
        value = self.config.get(key, default) if isinstance(self.config, dict) else default
        return default if value in (None, "") else value

    @staticmethod
    def _safe_tz(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            logger.warning(f"[autonomy] 时区 {name} 无效，回退 Asia/Shanghai")
            return ZoneInfo("Asia/Shanghai")

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(self._tz)

    def _sleeping(self, now: _dt.datetime) -> bool:
        minute = now.hour * 60 + now.minute
        for start, end in self._windows:
            if start < end and start <= minute < end:
                return True
            if start > end and (minute >= start or minute < end):
                return True
        return False

    def _last_activity_minutes(self, umo: str) -> float | None:
        """距该会话最近一条消息多少分钟；查不到返回 None。"""
        if not DB_PATH.exists():
            return None
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute(
                    "select max(updated_at) from platform_message_history where user_id = ?",
                    (umo,),
                ).fetchone()
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[autonomy] 读取最近活动失败：{exc}")
            return None
        stamp = row[0] if row else None
        if not stamp:
            return None
        if isinstance(stamp, (int, float)):
            ts = _dt.datetime.fromtimestamp(float(stamp), self._tz)
        else:
            text = str(stamp).replace("T", " ").rstrip("Z")
            try:
                ts = _dt.datetime.fromisoformat(text)
            except ValueError:
                return None
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=self._tz)
        delta = (self._now() - ts.astimezone(self._tz)).total_seconds() / 60.0
        return max(0.0, delta)

    def _probability(self, umo: str) -> float:
        base = float(self._cfg("probability", 0.2))
        mode = str(self._cfg("mode", "adaptive"))
        if mode != "adaptive":
            return max(0.0, min(1.0, base))
        age = self._last_activity_minutes(umo)
        if age is None:
            return max(0.0, min(1.0, base))
        recent = float(self._cfg("recent_minutes", 20))
        distant = float(self._cfg("distant_minutes", 360))
        if age < recent:
            factor = float(self._cfg("recent_multiplier", 0.15))
        elif age >= distant:
            factor = float(self._cfg("distant_multiplier", 1.5))
        else:
            factor = 1.0
        return max(0.0, min(1.0, base * factor))

    # ---------- 唤醒 ----------
    async def _wake(self, umo: str) -> None:
        note = str(self._cfg("motivation", DEFAULT_MOTIVATION))
        job = await self.context.cron_manager.add_active_job(
            name=f"自主唤醒 {umo.split(':')[-1]}",
            cron_expression=None,
            payload={"session": umo, "note": note, "origin": PLUGIN_NAME},
            description=note,
            timezone=str(self._tz.key),
            run_once=True,
            run_at=self._now(),
        )
        logger.info(f"[autonomy] 掷中，已唤起 {umo}（job={job.job_id}）")

    async def _tick(self) -> None:
        if not bool(self._cfg("enabled", False)):
            return
        if not self._sessions:
            logger.debug("[autonomy] 未配置会话，跳过")
            return
        now = self._now()
        if self._sleeping(now):
            logger.debug(f"[autonomy] {now:%H:%M} 处于睡眠时段，跳过")
            return
        for umo in self._sessions:
            p = self._probability(umo)
            roll = random.random()
            logger.debug(f"[autonomy] {umo} p={p:.3f} roll={roll:.3f}")
            if roll < p:
                await self._wake(umo)

    async def _loop(self) -> None:
        interval = max(1, int(self._cfg("interval_minutes", 30)))
        logger.info(
            f"[autonomy] 已启动：每 {interval} 分钟检查，会话 {self._sessions or '（未配置）'}"
        )
        while True:
            # 抖动 ±10%，避免与其他定时任务撞在同一秒
            wait = interval * 60 * random.uniform(0.9, 1.1)
            await asyncio.sleep(wait)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[autonomy] 本轮检查失败：{exc}")

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="autonomy-loop")

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # ---------- 观测 ----------
    @filter.command("自主状态")
    async def status(self, event):
        now = self._now()
        lines = [
            f"唤醒开关：{'开' if self._cfg('enabled', False) else '关'}",
            f"间隔：{self._cfg('interval_minutes', 30)} 分钟（抖动 ±10%）",
            f"模式：{self._cfg('mode', 'adaptive')}，基础概率 {self._cfg('probability', 0.2)}",
            f"睡眠时段：{self._cfg('sleep_windows', '23:30-08:30')}（当前{'睡眠中' if self._sleeping(now) else '清醒'}）",
            f"时区：{self._tz.key}，本地时间 {now:%Y-%m-%d %H:%M}",
        ]
        for umo in self._sessions:
            age = self._last_activity_minutes(umo)
            p = self._probability(umo)
            age_text = "无记录" if age is None else f"{age:.0f} 分钟前"
            lines.append(f"· {umo}：距上次活动 {age_text}，本次概率 {p:.3f}")
        yield event.plain_result("\n".join(lines))
