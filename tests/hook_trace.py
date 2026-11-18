"""验证「判断可追踪」：cron 唤醒的开始/结束经宿主 call_event_hook 派发后，能按 job id 落到运行记录。

用法（宿主机执行）：
    docker exec -i -e PYTHONPATH=/AstrBot astrbot python \
        /AstrBot/data/plugins/astrbot_plugin_autonomy/tests/hook_trace.py
"""

import asyncio
import functools
import json
import os
import sys
import types

sys.path.insert(0, os.environ.get("ASTRBOT_ROOT", "/AstrBot"))

from data.plugins.astrbot_plugin_autonomy import core as c  # noqa: E402
from data.plugins.astrbot_plugin_autonomy import main as m  # noqa: E402
from astrbot.core.pipeline.context_utils import call_event_hook  # noqa: E402
from astrbot.core.star.star import StarMetadata, star_map  # noqa: E402
from astrbot.core.star.star_handler import EventType, star_handlers_registry  # noqa: E402

MODULE = "data.plugins.astrbot_plugin_autonomy.main"
UMO = "astrbot-mc:FriendMessage:1250226509"


class Ctx:
    class cron_manager:
        created = []

        @classmethod
        async def add_active_job(cls, **kw):
            cls.created.append(kw)
            return types.SimpleNamespace(job_id="job%d" % len(cls.created))

        @classmethod
        async def delete_job(cls, job_id):
            return None

    web_apis = []

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append((route, handler, tuple(methods), desc))


class Trace:
    def __init__(self):
        self.events = []

    def record(self, name, **kw):
        self.events.append((name, kw.get("handler", ""), kw.get("status")))


def make_event(source, source_id, user_id="astrbot"):
    """尽量贴近宿主事件：call_event_hook 会读 plugins_name / trace / is_stopped。"""
    return types.SimpleNamespace(
        unified_msg_origin=UMO,
        message_obj=types.SimpleNamespace(sender=types.SimpleNamespace(user_id=user_id)),
        source=source,
        source_id=source_id,
        role="admin",
        plugins_name=None,
        trace=Trace(),
        is_stopped=lambda: False,
    )


def run0(plugin):
    return plugin.snapshot(UMO)["runs"][0]


async def main():
    out = {"cases": []}

    registered = [
        h.handler_full_name
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.OnAgentDoneEvent, only_activated=False
        )
        if h.handler_module_path == MODULE
    ]
    out["registered"] = registered

    cfg = {
        "enabled": True,
        "sessions": [UMO],
        "mode": "adaptive",
        "owner_id": "astrbot",
        "interval_minutes": 30,
        "allow_self_wake": True,
    }
    c.save_state = lambda path, state: None
    plugin = m.AutonomyPlugin(Ctx(), cfg)
    plugin._apply_settings()
    # 按宿主 star_manager 的做法把 handler 绑到实例上（partial(raw_handler, plugin)）
    for h in star_handlers_registry.get_handlers_by_module_name(MODULE):
        raw = h.handler.func if isinstance(h.handler, functools.partial) else h.handler
        h.handler = functools.partial(raw, plugin)
    # 让 star_map 认为插件已加载，call_event_hook 才会把 handler 视为激活
    star_map[MODULE] = StarMetadata(
        name="astrbot_plugin_autonomy", activated=True, star_cls=plugin, reserved=False
    )

    # 1) 唤起 → queued；宿主派发 begin → running
    run = await plugin.wake_now(UMO, "手动试一次")
    payload = Ctx.cron_manager.created[-1]["payload"]
    out["wake"] = {"run_id": run["run_id"], "job_id": run["job_id"],
                   "status": run0(plugin)["status"], "sender_id": payload["sender_id"]}
    ev = make_event("cron", run["job_id"])
    await call_event_hook(ev, EventType.OnAgentBeginEvent, types.SimpleNamespace())
    out["after_begin"] = {"status": run0(plugin)["status"], "trace": ev.trace.events}

    # 2) 宿主派发 done(completed)：未调工具 → 兜底原因
    ev = make_event("cron", run["job_id"])
    await call_event_hook(
        ev, EventType.OnAgentDoneEvent, types.SimpleNamespace(run_end_reason="completed"), "ok"
    )
    r = run0(plugin)
    out["after_done"] = {"status": r["status"], "decision": r["decision"], "reason": r["reason"],
                         "trace": ev.trace.events}

    # 3) 未知 job：此时排一条新的 queued 运行，事件不认识它 → 必须仍为 queued（有区分力）
    run_u = await plugin.wake_now(UMO, "留给未知事件")
    await call_event_hook(
        make_event("cron", "job-unknown"),
        EventType.OnAgentDoneEvent,
        types.SimpleNamespace(run_end_reason="completed"),
        "ok",
    )
    out["unknown_job"] = {"queued_run": run_u["run_id"], "status_after": run0(plugin)["status"]}

    # 4) 非 cron 事件：不得收尾
    await call_event_hook(
        make_event("message", run_u["job_id"], user_id="1250226509"),
        EventType.OnAgentDoneEvent,
        types.SimpleNamespace(run_end_reason="completed"),
        "ok",
    )
    out["non_cron"] = run0(plugin)["status"]

    # 5) autonomy_finish 先收尾，再补一个迟到的 done：状态与决定不被覆盖，只补结束时间
    ev_finish = make_event("cron", run_u["job_id"])
    await plugin.autonomy_finish(ev_finish, "no_action", "主人在改代码，这轮不动")
    r = run0(plugin)
    out["finish"] = {"status": r["status"], "decision": r["decision"], "reason": r["reason"]}
    await call_event_hook(
        make_event("cron", run_u["job_id"]),
        EventType.OnAgentDoneEvent,
        types.SimpleNamespace(run_end_reason="completed"),
        "ok",
    )
    r = run0(plugin)
    out["finish_then_done"] = {"status": r["status"], "decision": r["decision"],
                               "reason": r["reason"], "has_ended_at": bool(r.get("ended_at"))}

    # 6) runner 报 error：failed + 出错原因
    run_e = await plugin.wake_now(UMO, "第三次")
    await call_event_hook(
        make_event("cron", run_e["job_id"]),
        EventType.OnAgentDoneEvent,
        types.SimpleNamespace(run_end_reason="error", run_error="boom"),
        "err",
    )
    r = run0(plugin)
    out["error_path"] = {"status": r["status"], "reason": r["reason"]}

    print("HOOK_TRACE " + json.dumps(out, ensure_ascii=False))


asyncio.run(main())
