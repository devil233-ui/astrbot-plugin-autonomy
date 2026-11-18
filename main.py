"""自主意识（Autonomy）——把"随时段与概率自行醒来、再由自己决定说不说"搬到 AstrBot。

设计对照 WhaleLife `whalelife-plugin-autonomy` 0.5.1：

- 平均每 interval_minutes 检查一次，每次按概率掷骰子决定要不要醒；
- mode=adaptive 时按「距上次活动多久」调概率（刚聊完更安静，久没人说话更想开口）；
- 睡眠时段内不醒；她自己安排的唤醒可以突破睡眠时段（开关控制）；
- 醒来时只把一句 motivation 交给主 agent，由它结合人格/记忆/上下文自行决定
  说话或保持沉默。沉默 = 不调用 send_message_to_user；
- 每次判断的运行记录（谁触发、什么时候、给了什么理由）落盘，面板「最近判断」可看。

权限：唤醒借用宿主 cron 的 active_agent 任务，payload 里带 `sender_id=owner_id`；
宿主 `cron/manager.py:_run_active_agent_job` 的 role 判定会把它算成 admin，
因此她在自主轮次里也能使用 shell / python / 文件工具。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import random
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter

try:  # AstrBot 以包方式加载插件，可走相对导入
    from . import core as autonomy_core
    from . import page_api
except ImportError:  # pragma: no cover - 兜底：单文件方式加载时退回顶层导入
    import core as autonomy_core  # type: ignore[no-redef]
    import page_api  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_autonomy"
DB_PATH = Path("/AstrBot/data/data_v4.db")
DEFAULT_MOTIVATION = autonomy_core.DEFAULT_MOTIVATION
TRIGGER_LABELS = {
    "random": "随机唤醒",
    "self": "自己安排的唤醒",
    "manual": "手动判断",
    "agreement": "约定的唤醒",
}
MAX_PANEL_RUNS = 8


def _state_path() -> Path:
    """状态文件：data/plugin_data/<plugin>/state.json。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = Path(get_astrbot_plugin_data_path())
    except Exception:  # noqa: BLE001 - 旧版宿主没有这个 helper
        base = Path("/AstrBot/data/plugin_data")
    return base / PLUGIN_NAME / "state.json"


class AutonomyPlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._state: dict = autonomy_core.empty_state()
        self._settings: dict = autonomy_core.normalize_settings(dict(self.config))
        self._tz = self._safe_tz(self._settings["timezone"])
        self._windows = autonomy_core.parse_windows(self._settings["sleep_windows"])
        self._sessions = list(self._settings["sessions"])

    # ---------- 基础 ----------
    def _apply_settings(self) -> None:
        """配置变更后重算派生值（时区、睡眠时段、会话列表）。"""
        self._settings = autonomy_core.normalize_settings(dict(self.config or {}))
        self._tz = self._safe_tz(self._settings["timezone"])
        self._windows = autonomy_core.parse_windows(self._settings["sleep_windows"])
        self._sessions = list(self._settings["sessions"])

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
        return autonomy_core.sleeping(self._windows, now)

    def _save(self) -> None:
        """调用方需持有 self._lock。写盘失败不打断调度。"""
        try:
            autonomy_core.save_state(_state_path(), self._state)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 状态写盘失败：{exc}")

    # ---------- 活动时间 ----------
    def _db_last_activity(self, umo: str) -> _dt.datetime | None:
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
            return _dt.datetime.fromtimestamp(float(stamp), self._tz)
        return autonomy_core.parse_iso(stamp)

    def _last_activity_at(self, umo: str) -> _dt.datetime | None:
        """取数据库与状态文件里更近的那个活动时间（判断结束也会重置）。"""
        candidates = [
            self._db_last_activity(umo),
            autonomy_core.parse_iso(
                autonomy_core.bucket(self._state, umo).get("last_activity_at")
            ),
        ]
        known = [item for item in candidates if item is not None]
        if not known:
            return None
        return max(known)

    def _age_minutes(self, umo: str, now: _dt.datetime) -> float | None:
        return autonomy_core.minutes_since(self._last_activity_at(umo), now)

    def _probability(self, umo: str, now: _dt.datetime | None = None) -> float:
        now = now or self._now()
        return autonomy_core.probability(self._settings, self._age_minutes(umo, now))

    # ---------- 唤醒 ----------
    async def _wake(self, umo: str, trigger: str, note: str | None = None) -> dict | None:
        """建一条一次性 active_agent 任务，并把运行记录落盘。"""
        text = (note or self._settings["motivation"]).strip() or DEFAULT_MOTIVATION
        now = self._now()
        run = autonomy_core.new_run(umo, trigger, text, now)
        try:
            job = await self.context.cron_manager.add_active_job(
                name=f"自主唤醒 {umo.split(':')[-1]}",
                cron_expression=None,
                payload={
                    "session": umo,
                    "note": text,
                    "origin": PLUGIN_NAME,
                    # 命中宿主 admins_id 才会拿到 admin 权限，见 cron/manager.py
                    "sender_id": self._settings["owner_id"],
                    "run_id": run["run_id"],
                    "trigger": trigger,
                },
                description=text,
                timezone=str(self._tz.key),
                run_once=True,
                run_at=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 唤起 {umo} 失败：{exc}")
            return None
        run["job_id"] = str(getattr(job, "job_id", "") or "")
        async with self._lock:
            autonomy_core.push_run(self._state, umo, run)
            autonomy_core.set_next_check(self._state, umo, None)
            self._save()
        logger.info(
            f"[autonomy] {trigger} 唤起 {umo}（job={run['job_id'] or '?'}，run={run['run_id']}）"
        )
        return run

    async def wake_now(self, umo: str, note: str = "") -> dict | None:
        """面板「现在判断一次」/手动触发。"""
        return await self._wake(umo, "manual", note or None)

    # ---------- 调度 ----------
    def _due_self_wake(self, umo: str, now: _dt.datetime) -> dict | None:
        entry = autonomy_core.peek_self_wake(self._state, umo)
        if not entry:
            return None
        at = autonomy_core.parse_iso(entry.get("at"))
        if at is None:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=self._tz)
        if at > now:
            return None
        if self._sleeping(now) and not self._settings["self_wake_during_sleep"]:
            return None
        return entry

    async def _take_self_wake(self, umo: str) -> dict | None:
        async with self._lock:
            entry = autonomy_core.consume_self_wake(self._state, umo)
            if entry:
                self._save()
            return entry

    async def _tick(self) -> None:
        if not self._settings["enabled"]:
            return
        if not self._sessions:
            logger.debug("[autonomy] 未配置会话，跳过")
            return
        now = self._now()
        self._expire_stale(now)
        sleeping = self._sleeping(now)
        for umo in self._sessions:
            due = self._due_self_wake(umo, now)
            if due:
                taken = await self._take_self_wake(umo)
                if taken:
                    await self._wake(umo, "self", taken.get("note"))
                continue
            if sleeping:
                logger.debug(f"[autonomy] {now:%H:%M} 处于睡眠时段，跳过 {umo}")
                continue
            probability = self._probability(umo, now)
            roll = random.random()
            logger.debug(f"[autonomy] {umo} p={probability:.3f} roll={roll:.3f}")
            if roll < probability:
                await self._wake(umo, "random")

    def _expire_stale(self, now) -> None:
        """排队太久没开始的运行判失败，免得面板一直显示“排队中”。"""
        expired = 0
        for umo in self._sessions:
            expired += len(autonomy_core.expire_stale_runs(self._state, umo, now))
        if expired:
            logger.info(f"[autonomy] {expired} 条排队运行没起来，记为失败")
            self._save()

    def _plan_next_checks(self, seconds: float) -> None:
        target = self._now() + _dt.timedelta(seconds=seconds)
        for umo in self._sessions:
            autonomy_core.set_next_check(self._state, umo, target)

    async def _loop(self) -> None:
        logger.info(
            f"[autonomy] 已启动：每 {self._settings['interval_minutes']} 分钟检查，"
            f"会话 {self._sessions or '（未配置）'}"
        )
        while True:
            wait = autonomy_core.next_wait_seconds(self._settings["interval_minutes"])
            async with self._lock:
                self._plan_next_checks(wait)
                self._save()
            await asyncio.sleep(wait)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[autonomy] 本轮检查失败：{exc}")

    # ---------- 运行记录 ----------
    def _find_run_by_job(self, job_id: str) -> tuple[str, dict] | None:
        for umo, entry in (self._state.get("sessions") or {}).items():
            for run in entry.get("runs") or []:
                if run.get("job_id") and run.get("job_id") == job_id:
                    return umo, run
        return None

    def _find_run(self, umo: str, run_id: str) -> dict | None:
        return autonomy_core.find_run(self._state, umo, run_id)

    @staticmethod
    def _cron_job_id(event) -> str | None:
        if getattr(event, "source", None) != "cron":
            return None
        job_id = getattr(event, "source_id", None)
        return str(job_id) if job_id else None

    @filter.on_agent_begin()
    async def _on_agent_begin(self, event, run_context) -> None:  # noqa: ARG002
        job_id = self._cron_job_id(event)
        if not job_id:
            return
        async with self._lock:
            found = self._find_run_by_job(job_id)
            if not found:
                return
            _, run = found
            if run.get("status") == "queued":
                run["status"] = "running"
                run["started_run_at"] = autonomy_core.iso(self._now())
                self._save()

    @filter.on_agent_done()
    async def _on_agent_done(self, event, run_context, response) -> None:  # noqa: ARG002
        job_id = self._cron_job_id(event)
        if not job_id:
            return
        end_reason = getattr(run_context, "run_end_reason", None) or "completed"
        run_error = getattr(run_context, "run_error", None)
        status = {"completed": "completed", "error": "failed", "cancelled": "interrupted"}.get(
            end_reason, "completed"
        )
        async with self._lock:
            found = self._find_run_by_job(job_id)
            if not found:
                return
            umo, run = found
            if run.get("status") in ("completed", "failed", "interrupted"):
                # autonomy_finish 已经收过尾，只补写结束时间
                run.setdefault("ended_at", autonomy_core.iso(self._now()))
                self._save()
                return
            run["status"] = status
            if end_reason == "max_steps":
                run["status"] = "completed"
            run["decision"] = run.get("decision") or None
            if not run.get("reason"):
                if run_error:
                    run["reason"] = f"本轮出错：{run_error}"
                elif end_reason == "cancelled":
                    run["reason"] = "被叫停"
                elif end_reason == "max_steps":
                    run["reason"] = "步数到上限，自行收尾"
                else:
                    run["reason"] = "未调用 autonomy_finish，按已结束处理"
            run["ended_at"] = autonomy_core.iso(self._now())
            autonomy_core.touch_activity(self._state, umo, self._now())
            self._save()

    # ---------- 停止 ----------
    async def stop_session(self, umo: str, reason: str = "由面板叫停") -> dict:
        """结束并取消排队：删掉排队中的任务，再请求停掉正在跑的那轮。"""
        now = self._now()
        cancelled: list[str] = []
        async with self._lock:
            entry = autonomy_core.bucket(self._state, umo)
            for run in entry.get("runs") or []:
                if run.get("status") in ("queued", "running"):
                    job_id = run.get("job_id")
                    if job_id:
                        try:
                            await self.context.cron_manager.delete_job(job_id)
                            cancelled.append(job_id)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"[autonomy] 删除排队任务 {job_id} 失败：{exc}")
                    run["status"] = "cancelled"
                    run["reason"] = reason
                    run["ended_at"] = autonomy_core.iso(now)
            autonomy_core.cancel_self_wake(self._state, umo)
            self._save()
        stopped = 0
        try:
            from astrbot.core.utils.active_event_registry import active_event_registry

            stopped = active_event_registry.request_agent_stop_all(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[autonomy] 请求停止当前轮失败：{exc}")
        return {"cancelled_jobs": cancelled, "stopped_events": stopped}

    # ---------- 配置 ----------
    async def apply_settings(self, patch: dict) -> tuple[bool, list[str]]:
        clean, errors = autonomy_core.validate_patch(patch)
        if errors:
            return False, errors
        for key, value in clean.items():
            try:
                self.config[key] = value
            except Exception:  # noqa: BLE001 - 兜底：普通 dict
                self.config[key] = value
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[autonomy] 保存配置失败：{exc}")
                return False, [f"保存配置失败：{exc}"]
        self._apply_settings()
        return True, []

    # ---------- 面板数据 ----------
    def snapshot(self, umo: str | None = None) -> dict:
        now = self._now()
        sessions = list(self._sessions)
        current = umo if umo in sessions else (sessions[0] if sessions else (umo or ""))
        entry = autonomy_core.bucket(self._state, current) if current else {}
        if current:
            # 面板读之前先清一次：排太久的运行不该继续显示为“排队中”
            expired = autonomy_core.expire_stale_runs(self._state, current, now)
            if expired:
                self._save()
        last_activity = autonomy_core.parse_iso(entry.get("last_activity_at")) if entry else None
        if current:
            last_activity = self._last_activity_at(current) or last_activity
        probability = self._probability(current, now) if current else None
        runs = list(entry.get("runs") or [])[-MAX_PANEL_RUNS:]
        intents = autonomy_core.list_intents(self._state, current) if current else []
        open_items = [x for x in intents if x.get("state") == "open"]
        others = [x for x in intents if x.get("state") != "open"]
        queued = [x for x in runs if x.get("status") == "queued"]
        running = [x for x in runs if x.get("status") == "running"]
        return {
            "plugin": PLUGIN_NAME,
            "now": autonomy_core.iso(now),
            "timezone": self._tz.key,
            "settings": {key: self._settings[key] for key in autonomy_core.PANEL_KEYS},
            "sessions": sessions,
            "current": current,
            "sleeping": self._sleeping(now),
            "sleep_windows": autonomy_core.parse_windows(self._settings["sleep_windows"]),
            "probability": probability,
            "last_activity_at": autonomy_core.iso(last_activity) if last_activity else None,
            "next_check_at": entry.get("next_check_at") if entry else None,
            "self_wake": entry.get("self_wake") if entry else None,
            "intents": open_items + others,
            "runs": list(reversed(runs)),
            "queued": len(queued),
            "running": bool(running),
        }

    def recent_runs(self, umo: str, limit: int = MAX_PANEL_RUNS) -> list[dict]:
        entry = autonomy_core.bucket(self._state, umo)
        return list(reversed(list(entry.get("runs") or [])[-limit:]))

    # ---------- 生命周期 ----------
    def _register_web_apis(self) -> None:
        """注册面板接口；路由前缀固定为插件名，便于页面内用相对 endpoint 调用。"""
        api = page_api.PanelApi(self)
        routes = [
            ("state", api.state, ["GET"]),
            ("configure", api.configure, ["POST"]),
            ("wake", api.wake, ["POST"]),
            ("stop", api.stop, ["POST"]),
            ("self-cancel", api.self_cancel, ["POST"]),
        ]
        for suffix, handler, methods in routes:
            route = f"/{PLUGIN_NAME}/{suffix}"
            try:
                self.context.register_web_api(route, handler, methods, f"自主性面板：{suffix}")
            except Exception as exc:  # noqa: BLE001 - 旧宿主没有该 API 时不影响调度
                logger.warning(f"[autonomy] 注册面板接口 {route} 失败：{exc}")

    async def initialize(self) -> None:
        self._apply_settings()
        self._state = autonomy_core.load_state(_state_path())
        self._register_web_apis()
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
        async with self._lock:
            self._save()

    # ---------- 工具 ----------
    @filter.llm_tool(name="autonomy_schedule")
    async def autonomy_schedule(
        self,
        event: AstrMessageEvent,
        action: str,
        at: str = "",
        note: str = "",
    ) -> str:
        """查看、设置或取消"下一次自己醒来"的安排（只作用于当前会话）。

        Args:
            action(string): 必填。get 查看当前安排，set 设置下一次，cancel 取消。
            at(string): set 时必填。本地时间，格式 `YYYY-MM-DDTHH:MM` 或 `YYYY-MM-DD HH:MM`，必须在未来。
            note(string): 可选。给这次醒来留一句话，说明为什么想在这个时间醒。
        """
        umo = event.unified_msg_origin
        current = autonomy_core.peek_self_wake(self._state, umo)
        action = (action or "").strip().lower()
        if action in ("get", "查看", ""):
            if not current:
                return "当前没有自己安排的唤醒。"
            return f"下一次安排：{current.get('at')}（第 {current.get('version')} 版）{current.get('note') or ''}"
        if action in ("cancel", "取消"):
            async with self._lock:
                removed = autonomy_core.cancel_self_wake(self._state, umo)
                if removed:
                    self._save()
            if not removed:
                return "本来就没有安排，无需取消。"
            return "已取消自己安排的唤醒。"
        if action not in ("set", "设置"):
            return "action 只能是 get / set / cancel。"
        if not self._settings["allow_self_wake"]:
            return "你没有被允许自己安排唤醒（面板「允许她安排下次唤醒」未开），设置失败。"
        if not self._settings["enabled"]:
            return "自动唤醒总开关是关的，设置下次唤醒没有意义；先让主人打开。"
        target = autonomy_core.parse_iso(at)
        if target is None:
            return "时间格式不对，用 `YYYY-MM-DDTHH:MM` 或 `YYYY-MM-DD HH:MM`。"
        if target.tzinfo is None:
            target = target.replace(tzinfo=self._tz)
        now = self._now()
        if target <= now:
            return "只能是未来的时间。"
        if autonomy_core.sleeping(self._windows, target) and not self._settings[
            "self_wake_during_sleep"
        ]:
            return "那个时间在睡眠时段内，而「允许自行唤醒突破睡眠时段」没开，设置失败。"
        async with self._lock:
            saved = autonomy_core.set_self_wake(
                self._state, umo, target, note.strip(), now
            )
            self._save()
        return f"已安排 {saved['at']} 醒来（第 {saved['version']} 版）。"

    @filter.llm_tool(name="autonomy_intents")
    async def autonomy_intents(
        self,
        event: AstrMessageEvent,
        action: str,
        intent_id: str = "",
        text: str = "",
        kind: str = "thought",
        preferred_at: str = "",
        due_at: str = "",
        reason: str = "",
    ) -> str:
        """维护当前会话的意图：想做的事（thought）和与人的约定（agreement）。

        Args:
            action(string): 必填。list 列出，remember 新增，revise 修改，complete 完成，abandon 放弃。
            intent_id(string): revise / complete / abandon 时必填，取自 list 的返回。
            text(string): remember 时必填；revise 时可改。一句话说明这条意图。
            kind(string): 可选。thought（自己想的）或 agreement（答应过别人）；约定必须有 due_at。
            preferred_at(string): 可选。想做的事的偏好时间，`YYYY-MM-DD HH:MM`。
            due_at(string): 可选。约定时间，必须来自交流里真实说好的时间。
            reason(string): 可选。完成或放弃的理由。
        """
        umo = event.unified_msg_origin
        action = (action or "").strip().lower()
        now = self._now()

        def _parse_time(raw: str) -> _dt.datetime | None:
            parsed = autonomy_core.parse_iso(raw)
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=self._tz)
            return parsed

        if action in ("list", "列表", ""):
            items = autonomy_core.list_intents(self._state, umo)
            if not items:
                return "还没有记录任何意图。"
            lines = []
            for item in items:
                mark = "·" if item.get("state") == "open" else "×"
                when = item.get("due_at") or item.get("preferred_at") or ""
                lines.append(
                    f"{mark} [{item.get('id')}] {item.get('text')}"
                    f"（{'约定' if item.get('kind') == 'agreement' else '念头'}"
                    f"{'，' + str(when) if when else ''}，{item.get('state')}）"
                )
            return "\n".join(lines)
        if action in ("remember", "新增"):
            if not text.strip():
                return "text 不能为空。"
            kind_value = kind if kind in ("thought", "agreement") else "thought"
            due = _parse_time(due_at)
            preferred = _parse_time(preferred_at)
            if kind_value == "agreement" and due is None:
                return "约定类意图必须有 due_at，而且必须是交流里真实说好的时间。"
            async with self._lock:
                intent = autonomy_core.add_intent(
                    self._state,
                    umo,
                    text.strip(),
                    now,
                    kind=kind_value,
                    source="conversation" if kind_value == "agreement" else "self",
                    preferred_at=preferred,
                    due_at=due,
                )
                self._save()
            return f"已记下意图 [{intent['id']}]：{intent['text']}"
        if action not in ("revise", "complete", "abandon"):
            return "action 只能是 list / remember / revise / complete / abandon。"
        if not intent_id.strip():
            return "intent_id 不能为空，先用 list 查。"
        changes: dict = {}
        if text.strip():
            changes["text"] = text.strip()
        parsed_due = _parse_time(due_at) if due_at.strip() else None
        if parsed_due is not None:
            changes["due_at"] = autonomy_core.iso(parsed_due)
        parsed_preferred = _parse_time(preferred_at) if preferred_at.strip() else None
        if parsed_preferred is not None:
            changes["preferred_at"] = autonomy_core.iso(parsed_preferred)
        if reason.strip():
            changes["reason"] = reason.strip()
        async with self._lock:
            if action == "revise":
                updated = autonomy_core.revise_intent(
                    self._state, umo, intent_id.strip(), now, **changes
                )
                self._save()
                return f"已更新意图 [{updated['id']}]。" if updated else "没找到这条意图。"
            state_name = "done" if action == "complete" else "abandoned"
            closed = autonomy_core.close_intent(
                self._state, umo, intent_id.strip(), now, state_name, reason.strip()
            )
            self._save()
            if not closed:
                return "没找到这条意图。"
            return f"已{'完成' if state_name == 'done' else '放弃'}意图 [{closed['id']}]。"

    @filter.llm_tool(name="autonomy_finish")
    async def autonomy_finish(
        self,
        event: AstrMessageEvent,
        decision: str,
        reason: str = "",
        run_id: str = "",
    ) -> str:
        """给本轮自主判断收尾：说明你做了什么决定、为什么。只用于自主唤醒的轮次。

        Args:
            decision(string): 必填。acted 表示说/做了点什么，no_action 表示这轮选择不动。
            reason(string): 可选。一句给主人看的理由，会出现在面板「最近判断」里。
            run_id(string): 可选。本轮运行编号；不填则按当前唤醒任务自动匹配。
        """
        job_id = self._cron_job_id(event)
        if not job_id:
            return "autonomy_finish 只能用于自主唤醒的轮次。"
        umo = event.unified_msg_origin
        decision = (decision or "").strip().lower()
        if decision not in ("acted", "no_action"):
            return "decision 只能是 acted 或 no_action。"
        async with self._lock:
            run = None
            if run_id.strip():
                run = self._find_run(umo, run_id.strip())
            if run is None:
                found = self._find_run_by_job(job_id)
                run = found[1] if found else None
            if run is None:
                return "没找到本轮的运行记录。"
            run["status"] = "completed"
            run["decision"] = decision
            run["reason"] = reason.strip()
            run["ended_at"] = autonomy_core.iso(self._now())
            autonomy_core.touch_activity(self._state, umo, self._now())
            self._save()
        return f"已记录：{'说了/做了点什么' if decision == 'acted' else '这轮不动'}。"

    # ---------- 观测 ----------
    @filter.command("自主状态")
    async def status(self, event):
        now = self._now()
        lines = [
            f"唤醒开关：{'开' if self._settings['enabled'] else '关'}",
            f"间隔：{self._settings['interval_minutes']} 分钟（抖动 ±10%）",
            f"模式：{self._settings['mode']}，基础概率 {self._settings['probability']}",
            f"睡眠时段：{self._settings['sleep_windows'] or '（未设）'}"
            f"（当前{'睡眠中' if self._sleeping(now) else '清醒'}）",
            f"时区：{self._tz.key}，本地时间 {now:%Y-%m-%d %H:%M}",
            f"唤醒身份：{self._settings['owner_id']}",
        ]
        for umo in self._sessions:
            age = self._age_minutes(umo, now)
            pattern = self._probability(umo, now)
            age_text = "无记录" if age is None else f"{age:.0f} 分钟前"
            lines.append(f"· {umo}：距上次活动 {age_text}，本次概率 {pattern:.3f}")
        yield event.plain_result("\n".join(lines))
