"""容器内冒烟：按宿主的方式导入插件，验证工具注册、唤醒 payload 与面板快照。

用法（在宿主机执行）：
    docker exec -i -e PYTHONPATH=/AstrBot astrbot python /AstrBot/data/plugins/astrbot_plugin_autonomy/tests/smoke_import.py
"""

import asyncio, json, os, sys, types, datetime as dt

sys.path.insert(0, os.environ.get("ASTRBOT_ROOT", "/AstrBot"))

from data.plugins.astrbot_plugin_autonomy import main as m
from data.plugins.astrbot_plugin_autonomy import core as c

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
    class Context:  # 占位避免误用
        pass

cfg = {"enabled": True, "sessions": ["a:b:c"], "mode": "adaptive", "owner_id": "astrbot",
       "interval_minutes": 30, "allow_self_wake": True}
p = m.AutonomyPlugin(Ctx(), cfg)
p._apply_settings()

# 状态文件不落到真实 data 目录
c.save_state = lambda path, state: None

async def run():
    r = await p.wake_now("a:b:c", "手动试一次")
    assert r and r["job_id"] == "job1", r
    payload = Ctx.cron_manager.created[0]["payload"]
    assert payload["sender_id"] == "astrbot" and payload["origin"] == m.PLUGIN_NAME, payload
    assert payload["run_id"] == r["run_id"]
    # 显式打印供报告留证：这三项决定宿主把这一轮判成 admin 还是 member
    print("PAYLOAD", json.dumps({k: payload[k] for k in ("sender_id", "origin", "run_id", "session")}, ensure_ascii=False))
    print("DERIVED_ROLE", (lambda admins: "admin" if payload["sender_id"] in admins else "member")(["astrbot", "1250226509"]))
    snap = p.snapshot("a:b:c")
    assert snap["current"] == "a:b:c" and snap["queued"] == 1, snap["queued"]
    ok, errs = await p.apply_settings({"probability": 0.35, "interval_minutes": 12})
    assert ok and not errs, errs
    assert p._settings["probability"] == 0.35
    ok2, errs2 = await p.apply_settings({"probability": 2})
    assert not ok2 and errs2, errs2
    # 面板接口：五条路由，前缀固定为插件名，方法各自正确
    p._register_web_apis()
    routes = {route: methods for route, _h, methods, _d in Ctx.web_apis}
    expected = {
        f"/{m.PLUGIN_NAME}/state": ("GET",),
        f"/{m.PLUGIN_NAME}/configure": ("POST",),
        f"/{m.PLUGIN_NAME}/wake": ("POST",),
        f"/{m.PLUGIN_NAME}/stop": ("POST",),
        f"/{m.PLUGIN_NAME}/self-cancel": ("POST",),
    }
    assert routes == expected, routes
    print("SMOKE_OK", json.dumps(snap["settings"], ensure_ascii=False)[:120])
    print("SNAP_KEYS", sorted(snap.keys()))
    print("WEB_APIS", len(routes))

asyncio.run(run())
