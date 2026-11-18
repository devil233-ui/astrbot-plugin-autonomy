"""宿主读数：直接调用 CronJobManager._run_active_agent_job，看它算出的 role。

与 tests/smoke_import.py 的区别：那边是插件视角（看 payload 里有什么），
这里让宿主自己的调度代码跑一遍判定式，抓传给 wake_main_agent 的 role。

用法（容器内）：
    docker exec -i -e PYTHONPATH=/AstrBot astrbot python \
        /AstrBot/data/plugins/astrbot_plugin_autonomy/tests/role_probe.py
"""

import asyncio
import datetime as dt
import json
import os
import sys
import types

sys.path.insert(0, os.environ.get("ASTRBOT_ROOT", "/AstrBot"))

import astrbot.core.agent_wakeup as agent_wakeup  # noqa: E402
from astrbot.core.cron.manager import CronJobManager  # noqa: E402

HOST_CFG = os.environ.get("ASTRBOT_CFG", "/AstrBot/data/cmd_config.json")
PLUGIN_CFG = os.environ.get(
    "AUTONOMY_CFG", "/AstrBot/data/config/astrbot_plugin_autonomy_config.json"
)


def load_admins(path: str = HOST_CFG) -> list[str]:
    """真实管理员列表：直接读宿主的 cmd_config.json，不在测试里编。"""
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh).get("admins_id") or []


def load_plugin_cfg(path: str = PLUGIN_CFG) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


ADMINS = load_admins()
_PLUGIN_CFG = load_plugin_cfg()
UMO = (_PLUGIN_CFG.get("sessions") or ["astrbot-mc:GroupMessage:685164226"])[0]
OWNER = _PLUGIN_CFG.get("owner_id") or "astrbot"

captured: list[dict] = []


async def fake_wake(ctx, **kwargs):
    captured.append(kwargs)


class Ctx:
    """只提供 _run_active_agent_job 会碰到的两个入口。"""

    def __init__(self, admins):
        self.admins = admins

    def get_config(self, umo=None):
        return {"admins_id": self.admins}

    def get_platform_inst(self, platform_id):
        return types.SimpleNamespace(meta=lambda: {"id": platform_id})


def make_manager(ctx):
    mgr = object.__new__(CronJobManager)  # 跳过 __init__ 的 scheduler/db 装配
    mgr.ctx = ctx
    return mgr


def make_job(payload):
    return types.SimpleNamespace(
        job_id="probe-job-1",
        name="autonomy probe",
        description="role probe",
        job_type="active_agent",
        run_once=True,
        payload=payload,
    )


async def run_case(label, payload, admins=ADMINS):
    captured.clear()
    mgr = make_manager(Ctx(admins))
    job = make_job(payload)
    err = None
    try:
        await mgr._run_active_agent_job(job, dt.datetime.now())
    except Exception as exc:  # 宿主内部异常要如实记，不吞
        err = f"{type(exc).__name__}: {exc}"
    role = captured[0]["role"] if captured else None
    print(f"{label}: role={role!r} sender_id={payload.get('sender_id')!r} error={err}")
    return role


async def main():
    print(f"ADMINS {ADMINS} UMO {UMO} OWNER {OWNER}")
    orig = agent_wakeup.wake_main_agent
    agent_wakeup.wake_main_agent = fake_wake
    try:
        r1 = await run_case(
            f"插件当前 payload（owner_id={OWNER}）",
            {"session": UMO, "sender_id": OWNER, "origin": "astrbot_plugin_autonomy"},
        )
        r2 = await run_case(
            "主人 QQ 作为 sender_id",
            {"session": UMO, "sender_id": "1250226509", "origin": "astrbot_plugin_autonomy"},
        )
        r3 = await run_case(
            "修复前的 payload（不带 sender_id）",
            {"session": UMO, "origin": "astrbot_plugin_autonomy"},
        )
        r4 = await run_case(
            "非管理员 sender_id",
            {"session": UMO, "sender_id": "999999", "origin": "astrbot_plugin_autonomy"},
        )
    finally:
        agent_wakeup.wake_main_agent = orig

    assert r1 == "admin", r1
    assert r2 == "admin", r2
    assert r3 == "member", r3
    assert r4 == "member", r4
    print("ROLE_PROBE_OK")


if __name__ == "__main__":
    asyncio.run(main())
