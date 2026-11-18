"""自主性面板的后端接口。

前端 views/autonomy/app.js 通过插件视图 bridge 调用，endpoint 名会由宿主拼成
/api/v1/plugins/extensions/astrbot_plugin_autonomy/<endpoint>：

    GET  state         当前快照（?umo=<会话> 可指定查看哪个会话）
    POST configure     保存唤醒方式与她安排的唤醒开关
    POST wake          现在判断一次（跳过概率与睡眠时段）
    POST stop          结束并取消排队（清排队任务 + 叫停正在跑的那轮）
    POST self-cancel   取消她自己安排的唤醒

返回统一为 {"success": true, "data": ...} 或 {"success": false, "error": "..."}，
与 AstrBot 现有插件页面（如 astrbot_plugin_active_reply）保持一致。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

try:  # 与 main.py 一致：包内相对导入，单文件加载时退回顶层
    from . import core as autonomy_core
except ImportError:  # pragma: no cover
    import core as autonomy_core  # type: ignore[no-redef]


def _ok(data: Any = None, **extra: Any) -> dict:
    payload = {"success": True, "data": data}
    payload.update(extra)
    return payload


def _fail(message: str) -> dict:
    return {"success": False, "error": message}


def _umo_from(*sources: dict) -> str:
    for source in sources:
        if not source:
            continue
        value = source.get("umo") or source.get("session") or ""
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value
    return ""


class PanelApi:
    """绑定到插件实例的一组处理器；注册时用 bound method。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    async def state(self) -> dict:
        try:
            umo = (request.query.get("umo") or "").strip() or None
            return _ok(self.plugin.snapshot(umo))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 面板读取状态失败：{exc}")
            return _fail(f"读取状态失败：{exc}")

    async def configure(self) -> dict:
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _fail("请求体必须是 JSON 对象")
        try:
            ok, errors = await self.plugin.apply_settings(body)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 保存设置失败：{exc}")
            return _fail(f"保存设置失败：{exc}")
        if not ok:
            return _fail("；".join(errors) or "设置不合法")
        umo = _umo_from(body)
        return _ok(self.plugin.snapshot(umo or None))

    async def wake(self) -> dict:
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _fail("请求体必须是 JSON 对象")
        umo = _umo_from(body)
        if not umo:
            return _fail("缺少会话标识")
        note = str(body.get("note") or "").strip()
        try:
            job = await self.plugin.wake_now(umo, note)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 手动判断失败：{exc}")
            return _fail(f"手动判断失败：{exc}")
        if not job:
            return _fail("这个会话的自动唤醒已暂停，或排队已经满了")
        return _ok({"job": job})

    async def stop(self) -> dict:
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _fail("请求体必须是 JSON 对象")
        umo = _umo_from(body)
        if not umo:
            return _fail("缺少会话标识")
        try:
            result = await self.plugin.stop_session(umo)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 结束判断失败：{exc}")
            return _fail(f"结束判断失败：{exc}")
        return _ok(result)

    async def self_cancel(self) -> dict:
        body = await request.json(default={}) or {}
        if not isinstance(body, dict):
            return _fail("请求体必须是 JSON 对象")
        umo = _umo_from(body)
        if not umo:
            return _fail("缺少会话标识")
        cancelled = None
        try:
            async with self.plugin._lock:  # noqa: SLF001 - 与主循环共用同一把锁
                cancelled = autonomy_core.cancel_self_wake(self.plugin._state, umo)
                if cancelled:
                    self.plugin._save()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[autonomy] 取消她的安排失败：{exc}")
            return _fail(f"取消她的安排失败：{exc}")
        if not cancelled:
            return _fail("当前没有她安排的唤醒")
        return _ok({"cancelled": cancelled})
